"""Scout: polls every source, deduplicates, clusters items into stories. No LLM – fast and free."""

from __future__ import annotations

import asyncio

from .. import sources as S
from ..config import Source
from ..util import canonical_url, detect_lang, hamming, jaccard, now, simhash
from .base import Agent

STORY_WINDOW_H = 36
SIMHASH_MAX_DIST = 10
JACCARD_MIN = 0.5
FAILS_BEFORE_ALERT = 5


class Scout(Agent):
    name = "scout"

    def __init__(self, app):
        super().__init__(app)
        self.sem = asyncio.Semaphore(8)

    def all_sources(self) -> list[Source]:
        return self.app.all_sources()

    def due(self, t: float) -> list[Source]:
        out = []
        for s in self.all_sources():
            st = self.db.source_state(s.id)
            if st["disabled_until"] > t:
                continue
            if t - st["last_poll"] >= s.interval_min * 60:
                out.append(s)
        return out

    async def tick(self) -> int:
        t = now()
        due = self.due(t)
        if not due:
            return 0
        results = await asyncio.gather(*(self.poll_one(s) for s in due), return_exceptions=True)
        return sum(r for r in results if isinstance(r, int))

    async def poll_one(self, src: Source) -> int:
        async with self.sem:
            st = self.db.source_state(src.id)
            self.db.update_source(src.id, last_poll=now())
            try:
                entries, etag, lm = await S.poll(self.app.http, src, st["etag"], st["last_modified"])
            except Exception as e:  # noqa: BLE001 - any source failure is isolated
                await self.source_failed(src, st, str(e))
                return 0
            self.db.update_source(src.id, etag=etag, last_modified=lm, last_ok=now(), failures=0,
                                  last_error=None, disabled_until=0)
            if entries is None:
                return 0
            return self.ingest(src, entries, bootstrap=not st["bootstrapped"])

    async def source_failed(self, src: Source, st, err: str):
        fails = st["failures"] + 1
        disabled_until = 0.0
        if fails >= 3:
            disabled_until = now() + min(6 * 3600, 60 * 2 ** fails)
        self.db.update_source(src.id, failures=fails, last_error=err[:500], disabled_until=disabled_until)
        self.log.warning("source %s failed (%d): %s", src.id, fails, err[:200])
        if fails == FAILS_BEFORE_ALERT:
            await self.app.notify(f"⚠️ Источник <b>{src.name}</b> ({src.id}) не отвечает {fails} раз подряд: "
                                  f"<code>{err[:200]}</code>", key=f"src:{src.id}")

    def ingest(self, src: Source, entries: list[S.Entry], bootstrap: bool = False) -> int:
        """Store new entries. On the first poll of a source everything is marked seen, not news."""
        t = now()
        max_age = 24 * 3600
        new = 0
        for e in entries:
            url = canonical_url(e.url)
            if not url or self.db.one("SELECT 1 FROM items WHERE url=?", (url,)):
                continue
            text = f"{e.title} {e.summary[:300]}"
            lang = detect_lang(text) if src.lang == "auto" else src.lang
            sh = simhash(text)
            fresh = e.published_at is None or t - e.published_at <= max_age
            story = None
            if not bootstrap and fresh and e.title:
                story = self.assign_story(src, e.title, e.summary, lang, sh, t)
            self.db.x(
                "INSERT OR IGNORE INTO items(source_id,pack,url,title,summary,image,lang,published_at,fetched_at,"
                "simhash,story_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (src.id, src.pack, url, e.title, e.summary, e.image, lang, e.published_at or t, t, str(sh), story),
            )
            if story:
                new += 1
        self.db.x("UPDATE sources_state SET bootstrapped=1, items_total=items_total+? WHERE source_id=?",
                  (len(entries), src.id))
        return new

    def assign_story(self, src: Source, title: str, summary: str, lang: str, sh: int, t: float) -> int:
        tier = self.app.effective_tier(src)
        rows = self.db.all("SELECT id,title,simhash,best_tier FROM stories WHERE pack=? AND lang=? AND last_seen>?",
                           (src.pack, lang, t - STORY_WINDOW_H * 3600))
        best, best_score = None, 0.0
        for r in rows:
            d = hamming(sh, int(r["simhash"]))
            j = jaccard(title, r["title"])
            if d <= SIMHASH_MAX_DIST or j >= JACCARD_MIN:
                score = j + (SIMHASH_MAX_DIST - d) / 64
                if score > best_score:
                    best, best_score = r, score
        if best is not None:
            self.db.x("UPDATE stories SET last_seen=?, item_count=item_count+1, best_tier=MIN(best_tier, ?) WHERE id=?",
                      (t, tier, best["id"]))
            return best["id"]
        return self.db.x(
            "INSERT INTO stories(pack,title,lang,simhash,first_seen,last_seen,item_count,best_tier) "
            "VALUES(?,?,?,?,?,?,1,?)", (src.pack, title, lang, str(sh), t, t, tier)).lastrowid
