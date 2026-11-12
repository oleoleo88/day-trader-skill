"""Сборщик: опрашивает ленты, отбирает записи по теме, отсекает очевидные дубли."""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp
import feedparser

from .config import Source
from .db import now
from .extract import first_image
from .textutil import canonical_url, clip, domain_of, esc, similarity, strip_html, title_tokens

if TYPE_CHECKING:
    from .context import Ctx

log = logging.getLogger(__name__)
ALERT_AFTER_FAILS = 6


@dataclass
class Entry:
    guid: str
    url: str
    title: str
    summary: str
    content: str
    image: str | None
    published: int
    publisher: str | None


def keyword_pattern(keywords: list[str]) -> re.Pattern[str]:
    parts = []
    for kw in keywords:
        k = kw.strip().lower().replace("ё", "е")
        if not k:
            continue
        body = r"[\s\-]*".join(re.escape(w) for w in re.split(r"[\s\-]+", k))
        tail = r"(?![0-9a-zа-я])" if k[-1].isdigit() else ""
        parts.append(body + tail)
    if not parts:
        return re.compile(r"(?!x)x")  # ничего не совпадает
    return re.compile(r"(?<![0-9a-zа-я])(?:" + "|".join(parts) + ")", re.I)


def _entry_image(e: dict, summary_html: str, content_html: str, link: str) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key) or []:
            url = (m.get("url") or "").strip()
            kind = (m.get("type") or m.get("medium") or "image").lower()
            if url and ("image" in kind or key == "media_thumbnail"):
                return url
    for enc in e.get("enclosures") or []:
        if (enc.get("type") or "").startswith("image/") and enc.get("href"):
            return enc["href"]
    return first_image(content_html, link) or first_image(summary_html, link)


def parse_feed(raw: bytes) -> tuple[list[Entry], str | None]:
    """Разобрать RSS/Atom. Возвращает записи и текст ошибки, если лента битая и пустая."""
    d = feedparser.parse(raw)
    entries: list[Entry] = []
    now_ts = int(time.time())
    for e in (d.entries or [])[:80]:
        link = (e.get("link") or "").strip()
        title = strip_html(e.get("title") or "").replace("\n", " ").strip()
        if not link or not title:
            continue
        summary_html = e.get("summary") or ""
        content_html = ""
        if e.get("content"):
            content_html = max(((c.get("value") or "") for c in e["content"]), key=len, default="")
        stamp = e.get("published_parsed") or e.get("updated_parsed") or e.get("created_parsed")
        published = min(calendar.timegm(stamp), now_ts) if stamp else now_ts
        publisher = ((e.get("source") or {}).get("title") or "").strip() or None
        summary = strip_html(summary_html)
        if domain_of(link) == "news.google.com":
            # У Google News заголовок вида «Текст - Издание», а анонс — просто ссылка
            if publisher and title.endswith(" - " + publisher):
                title = title[: -len(publisher) - 3].strip()
            summary = ""
        entries.append(Entry(
            guid=str(e.get("id") or link)[:500], url=link, title=title[:400], summary=summary[:3000],
            content=strip_html(content_html)[:30000], image=_entry_image(e, summary_html, content_html, link),
            published=published, publisher=publisher))
    if not entries and getattr(d, "bozo", False):
        return [], f"лента не разбирается: {clip(str(getattr(d, 'bozo_exception', 'неизвестный формат')), 150)}"
    return entries, None


class Collector:
    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx
        self._kw = keyword_pattern(ctx.cfg.collector.keywords)
        self._lock = asyncio.Lock()

    # ── цикл ────────────────────────────────────────────────────────────────
    async def run_cycle(self, force: bool = False) -> dict[str, int]:
        """Опросить источники, у которых подошло время, и передать новое редактору."""
        if self._lock.locked():
            return {}
        async with self._lock:
            sources = [s for s in self.ctx.cfg.collector.sources if s.enabled]
            sem = asyncio.Semaphore(6)

            async def guarded(src: Source) -> int:
                async with sem:
                    try:
                        return await self.poll_source(src, force=force)
                    except Exception as e:  # noqa: BLE001 — один источник не должен ломать цикл
                        log.exception("Сбой опроса источника %s", src.name)
                        await self._fail(src, src.interval or self.ctx.cfg.collector.poll_interval_sec, f"{type(e).__name__}: {e}")
                        return 0

            fresh = sum(await asyncio.gather(*(guarded(s) for s in sources)))
            stats = await self.ctx.editor.process_new()
            stats["fetched"] = fresh
            return stats

    async def _health(self, src: Source):
        db = self.ctx.db
        row = await db.one("SELECT * FROM source_health WHERE source=?", (src.name,))
        if row is None:
            await db.execute("INSERT OR IGNORE INTO source_health (source) VALUES (?)", (src.name,))
            row = await db.one("SELECT * FROM source_health WHERE source=?", (src.name,))
        return row

    async def _ok(self, src: Source, interval: int, entries: int | None, etag: str = "", last_modified: str = "") -> None:
        sets = "ok_at=?, fails=0, last_error='', seeded=1, next_poll_at=?"
        params: list = [now(), now() + max(20, interval) - 5]
        if entries is not None:
            sets += ", entries=?, etag=?, last_modified=?"
            params += [entries, etag, last_modified]
        await self.ctx.db.execute(f"UPDATE source_health SET {sets} WHERE source=?", (*params, src.name))

    async def _fail(self, src: Source, interval: int, error: str) -> None:
        db = self.ctx.db
        await self._health(src)
        row = await db.one("SELECT fails FROM source_health WHERE source=?", (src.name,))
        fails = (row["fails"] if row else 0) + 1
        backoff = min(max(20, interval) * 2 ** min(fails, 5), 3600)
        await db.execute(
            "UPDATE source_health SET fail_at=?, fails=?, last_error=?, next_poll_at=? WHERE source=?",
            (now(), fails, error[:300], now() + backoff, src.name))
        log.warning("Источник «%s» не ответил (%s подряд): %s", src.name, fails, error)
        if fails == ALERT_AFTER_FAILS:
            await self.ctx.notifier.send(
                f"📡 Источник <b>{esc(src.name)}</b> не отвечает {fails} раз подряд.\n"
                f"Последняя ошибка: <code>{esc(clip(error, 200))}</code>\n"
                f"Бот продолжит попытки реже. Если лента переехала, поправьте url в config.yaml.")

    async def _download(self, src: Source, etag: str = "", last_modified: str = "") -> tuple[int, bytes, str, str]:
        headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        assert self.ctx.http is not None
        async with self.ctx.http.get(src.url, headers=headers, allow_redirects=True) as resp:
            if resp.status != 200:
                return resp.status, b"", "", ""
            raw = await resp.content.read(6_000_000)
            return 200, raw, resp.headers.get("ETag", ""), resp.headers.get("Last-Modified", "")

    async def poll_source(self, src: Source, force: bool = False) -> int:
        """Опросить один источник. Возвращает число новых записей, взятых в работу."""
        health = await self._health(src)
        if not force and health["next_poll_at"] > now():
            return 0
        interval = src.interval or self.ctx.cfg.collector.poll_interval_sec
        try:
            status, raw, etag, last_modified = await self._download(src, health["etag"], health["last_modified"])
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            await self._fail(src, interval, f"{type(e).__name__}: {e}".rstrip(": "))
            return 0
        if status == 304:
            await self._ok(src, interval, None)
            return 0
        if status != 200:
            await self._fail(src, interval, f"HTTP {status}")
            return 0
        entries, error = await asyncio.to_thread(parse_feed, raw)
        if error:
            await self._fail(src, interval, error)
            return 0
        first_run = not health["seeded"]
        fresh = 0
        for entry in entries:
            if await self._ingest(src, entry, first_run):
                fresh += 1
        await self._ok(src, interval, len(entries), etag, last_modified)
        return fresh

    # ── приём записи ────────────────────────────────────────────────────────
    def matches_topic(self, text: str) -> bool:
        return bool(self._kw.search(text.lower().replace("ё", "е")))

    async def _ingest(self, src: Source, e: Entry, first_run: bool) -> bool:
        cfg = self.ctx.cfg.collector
        if not src.focused and not self.matches_topic(f"{e.title} {e.summary}"):
            return False
        age_hours = (now() - e.published) / 3600
        # Старое храним только как память для поиска дублей, в работу не берём
        status = "seeded" if age_hours > cfg.max_age_hours or (first_run and age_hours > cfg.backfill_hours) else "new"
        tokens = title_tokens(e.title)
        canon = canonical_url(e.url)
        conn = self.ctx.db.conn
        cur = await conn.execute(
            """INSERT OR IGNORE INTO items
               (source, tier, lang, guid, url, url_canon, title, summary, content, image_url,
                published_at, fetched_at, title_tokens, status, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (src.name, src.tier, src.lang, e.guid, e.url, canon, e.title, e.summary, e.content, e.image,
             e.published, now(), json.dumps(tokens, ensure_ascii=False), status,
             json.dumps({"publisher": e.publisher or src.name}, ensure_ascii=False)))
        await conn.commit()
        if cur.rowcount != 1:
            return False  # запись уже была
        item_id = cur.lastrowid
        if status != "new":
            return False
        # Официальные источники не отсекаем по словам: пусть решает редактор
        if src.tier > 1:
            dup = await self.find_duplicate(item_id, canon, tokens)
            if dup:
                await self.ctx.db.execute("UPDATE items SET status='duplicate', dup_of=? WHERE id=?", (dup, item_id))
                return False
        return True

    async def find_duplicate(self, item_id: int, canon: str, tokens: list[str]) -> int | None:
        """Тот же URL или почти тот же заголовок среди недавних записей."""
        db = self.ctx.db
        cfg = self.ctx.cfg.collector
        since = now() - cfg.dedupe_window_hours * 3600
        same = await db.one("SELECT id FROM items WHERE url_canon=? AND id!=? ORDER BY id LIMIT 1", (canon, item_id))
        if same:
            return same["id"]
        rows = await db.all(
            "SELECT id, title_tokens FROM items WHERE id!=? AND fetched_at>? AND status!='duplicate'", (item_id, since))
        best_id, best = None, 0.0
        for r in rows:
            try:
                other = json.loads(r["title_tokens"])
            except ValueError:
                continue
            score = similarity(tokens, other)
            if score > best:
                best_id, best = r["id"], score
        return best_id if best >= cfg.title_similarity else None

    # ── диагностика ─────────────────────────────────────────────────────────
    async def check_sources(self) -> list[tuple[Source, bool, str]]:
        """Живая проверка всех включённых лент (без записи в базу)."""
        sem = asyncio.Semaphore(6)

        async def one(src: Source) -> tuple[Source, bool, str]:
            async with sem:
                try:
                    status, raw, _, _ = await self._download(src)
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    return src, False, f"{type(e).__name__}: {e}".rstrip(": ")
                if status != 200:
                    return src, False, f"HTTP {status}"
                entries, error = await asyncio.to_thread(parse_feed, raw)
                if error:
                    return src, False, error
                if not entries:
                    return src, True, "лента открылась, но записей сейчас нет"
                on_topic = sum(1 for e in entries if src.focused or self.matches_topic(f"{e.title} {e.summary}"))
                newest_h = (now() - max(e.published for e in entries)) / 3600
                return src, True, f"записей: {len(entries)}, по теме: {on_topic}, свежайшая: {newest_h:.0f} ч назад"

        return list(await asyncio.gather(*(one(s) for s in self.ctx.cfg.collector.sources if s.enabled)))

    async def health_report(self) -> str:
        rows = {r["source"]: r for r in await self.ctx.db.all("SELECT * FROM source_health")}
        lines = []
        for s in self.ctx.cfg.collector.sources:
            if not s.enabled:
                lines.append(f"⏸ {esc(s.name)} — выключен")
                continue
            h = rows.get(s.name)
            if h is None or (not h["ok_at"] and not h["fail_at"]):
                lines.append(f"⏳ {esc(s.name)} — ещё не опрашивался")
            elif h["fails"]:
                lines.append(f"❌ {esc(s.name)} — сбоев подряд: {h['fails']}; {esc(clip(h['last_error'], 80))}")
            else:
                ago = max(0, (now() - h["ok_at"]) // 60)
                lines.append(f"✅ {esc(s.name)} — записей в ленте: {h['entries']}, опрошен {ago} мин назад")
        return "\n".join(lines)
