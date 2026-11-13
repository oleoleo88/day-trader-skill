"""ИИ-редактор: отбор записей, написание постов, переписывание, сборка итогового HTML."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from . import review
from .db import meta_of, now
from .extract import fetch_article
from .llm import LLMError, LLMRefusal
from .prompts import (CATEGORY_HASHTAG, CATEGORY_LABELS, REWRITE_STYLES, TOPIC_HASHTAGS, Draft, Triage,
                      triage_system, writer_system)
from .textutil import canonical_url, clip, esc, esc_attr, render_markup, title_tokens, visible_len, youtube_id

if TYPE_CHECKING:
    from .context import Ctx

log = logging.getLogger(__name__)

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
MAX_ATTEMPTS = 4
_TIER_NAMES = {1: "официальный первоисточник", 2: "крупное издание", 3: "фан-сайт, инсайдер или сообщество"}
_TOPIC_BY_LOWER = {t.lower(): t for t in TOPIC_HASHTAGS}
_NEWS_KINDS = ("news", "breaking", "manual", "suggestion")


def item_note(item: Any) -> dict:
    try:
        data = json.loads(item["note"] or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def compose_post(ctx: "Ctx", *, headline: str, body: str, category: str, source_name: str | None,
                 source_url: str | None, tags: list[str], breaking: bool = False) -> str:
    """Собрать итоговый HTML поста из частей."""
    ch = ctx.cfg.channel

    def build(body_text: str) -> str:
        head = f"<b>{esc(headline)}</b>"
        if breaking:
            head = "⚡️ " + head
        credit = []
        if ch.category_label and category in CATEGORY_LABELS:
            credit.append(CATEGORY_LABELS[category])
        if ch.source_link and source_name:
            credit.append(f'<a href="{esc_attr(source_url)}">{esc(source_name)}</a>' if source_url else esc(source_name))
        blocks = [head + (f"\n<i>{' · '.join(credit)}</i>" if credit else "")]
        if body_text:
            blocks.append(render_markup(body_text))
        tail = []
        if ch.hashtags and tags:
            tail.append(" ".join(tags))
        if ch.footer and ctx.channel_link:
            tail.append(f'<a href="{esc_attr(ctx.channel_link)}">{esc(ch.name)}</a>')
        if tail:
            blocks.append("\n".join(tail))
        return "\n\n".join(blocks)

    text = build(body)
    if visible_len(text) > TEXT_LIMIT:
        overhead = visible_len(build(""))
        text = build(clip(body, max(200, TEXT_LIMIT - overhead - 20)))
    return text


def pick_tags(category: str, proposed: list[str]) -> list[str]:
    tags: list[str] = []
    if category in CATEGORY_HASHTAG:
        tags.append(CATEGORY_HASHTAG[category])
    for raw in proposed or []:
        tag = _TOPIC_BY_LOWER.get(("#" + raw.strip().lstrip("#")).lower())
        if tag and tag not in tags:
            tags.append(tag)
        if len(tags) >= 3:
            break
    return tags


class Editor:
    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx

    # ── поток новых записей ─────────────────────────────────────────────────
    async def process_new(self, limit: int = 25) -> dict[str, int]:
        """Обработать записи со статусом new: официальные источники первыми, дальше по времени."""
        stats = {"drafted": 0, "skipped": 0, "errors": 0}
        rows = await self.ctx.db.all(
            "SELECT * FROM items WHERE status='new' ORDER BY tier ASC, published_at ASC LIMIT ?", (limit,))
        for item in rows:
            try:
                status = await self.process_item(item)
            except LLMRefusal as e:
                await self._set_item(item["id"], "error", reason=str(e))
                stats["errors"] += 1
                continue
            except LLMError as e:
                stats["errors"] += 1
                if e.fatal:
                    await self.ctx.notifier.once(
                        "llm-fatal", f"🛑 <b>ИИ-редактор остановлен</b>: {esc(str(e))}\n"
                                     f"Проверьте ANTHROPIC_API_KEY и названия моделей в config.yaml.", every=3600)
                    break
                if e.transient:
                    log.warning("Claude API временно недоступен, запись #%s подождёт: %s", item["id"], e)
                    break  # не долбим API, продолжим в следующем цикле
                attempts = item["attempts"] + 1
                if attempts >= MAX_ATTEMPTS:
                    await self._set_item(item["id"], "error", reason=str(e))
                else:
                    await self.ctx.db.execute("UPDATE items SET attempts=? WHERE id=?", (attempts, item["id"]))
                continue
            except Exception as e:  # noqa: BLE001 — одна запись не должна останавливать поток
                stats["errors"] += 1
                await self.ctx.notifier.error("обработка записи редактором", e)
                await self._set_item(item["id"], "error", reason=f"{type(e).__name__}: {e}")
                continue
            stats["drafted" if status == "drafted" else "skipped"] += 1
        return stats

    async def _set_item(self, item_id: int, status: str, *, reason: str = "", dup_of: int | None = None, **extra: Any) -> None:
        row = await self.ctx.db.one("SELECT note FROM items WHERE id=?", (item_id,))
        note = item_note(row) if row else {}
        if reason:
            note["reason"] = clip(reason, 300)
        note.update(extra)
        await self.ctx.db.execute(
            "UPDATE items SET status=?, dup_of=COALESCE(?, dup_of), note=? WHERE id=?",
            (status, dup_of, json.dumps(note, ensure_ascii=False), item_id))

    async def _recent_posts(self) -> list[Any]:
        since = now() - self.ctx.cfg.collector.dedupe_window_hours * 3600
        marks = ", ".join("?" for _ in _NEWS_KINDS)
        return await self.ctx.db.all(
            f"""SELECT id, headline, created_at FROM posts
                WHERE kind IN ({marks}) AND status IN ('review', 'queued', 'publishing', 'published')
                  AND created_at>? AND headline!='' ORDER BY id DESC LIMIT 60""", (*_NEWS_KINDS, since))

    async def triage(self, item: Any, recent: list[Any]) -> Triage:
        ctx = self.ctx
        if recent:
            listing = "\n".join(f"[#{r['id']}] {ctx.local(r['created_at']):%d.%m %H:%M} — «{r['headline']}»" for r in recent)
        else:
            listing = "(пока ничего не публиковалось)"
        excerpt = (item["content"] or item["summary"] or "").strip()
        user = (
            f"Сегодня: {ctx.clock_text()}. Фаза: {ctx.phase_text()}.\n\n"
            f"Недавние материалы канала (для поиска дублей):\n{listing}\n\n"
            f"Новая запись\n"
            f"Источник: {item_note(item).get('publisher') or item['source']} "
            f"(уровень {item['tier']}: {_TIER_NAMES.get(item['tier'], '')}), язык: {item['lang']}\n"
            f"Опубликовано: {ctx.local(item['published_at']):%Y-%m-%d %H:%M}\n"
            f"Заголовок: {item['title']}\n"
            f"Текст: {clip(excerpt, 2500) if excerpt else '(в ленте только заголовок)'}"
        )
        t = await ctx.llm.structured("triage", system=triage_system(ctx.facts()), user=user, schema=Triage, max_tokens=3000)
        t.importance = max(1, min(10, int(t.importance)))
        if t.duplicate_of and t.duplicate_of not in {r["id"] for r in recent}:
            t.duplicate_of = 0  # модель сослалась на номер, которого нет в списке
        return t

    async def process_item(self, item: Any) -> str:
        """Провести одну запись через отбор и, если прошла, написать пост. Возвращает новый статус записи."""
        ctx, cfg = self.ctx, self.ctx.cfg
        if (now() - item["published_at"]) / 3600 > cfg.collector.max_age_hours:
            await self._set_item(item["id"], "rejected", reason="устарела, пока ждала обработки")
            return "rejected"
        recent = await self._recent_posts()
        t = await self.triage(item, recent)
        if not t.relevant:
            await self._set_item(item["id"], "filtered", reason=t.reason)
            return "filtered"
        if t.duplicate_of and not t.adds_new_info:
            await self._set_item(item["id"], "duplicate", reason=t.reason, of_post=t.duplicate_of)
            return "duplicate"
        if t.importance < cfg.publishing.min_importance:
            await self._set_item(item["id"], "rejected", reason=f"важность {t.importance}/10: {t.reason}")
            return "rejected"

        source_text, image = await self._source_text(item)
        thin = len(source_text) < cfg.collector.min_source_chars
        if thin and t.importance < 8:
            await self._set_item(item["id"], "thin", reason="в источнике слишком мало текста для поста")
            return "thin"

        follow_up = None
        if t.duplicate_of:
            follow_up = next((r for r in recent if r["id"] == t.duplicate_of), None)
        draft = await self.write(item, t, source_text, thin=thin, follow_up=follow_up)
        post_id = await self._save_draft(item, t, draft, image=image, thin=thin)
        await self._set_item(item["id"], "drafted", reason=t.reason, post=post_id)
        await self.route(post_id)
        return "drafted"

    async def _source_text(self, item: Any) -> tuple[str, str | None]:
        """Лучший доступный текст источника и картинка. При нехватке текста докачиваем страницу."""
        text = (item["content"] or "").strip()
        image = item["image_url"]
        if len(text) < self.ctx.cfg.collector.min_source_chars and self.ctx.http is not None:
            art = await fetch_article(self.ctx.http, item["url"])
            if len(art.text) > len(text):
                text = art.text
            image = image or art.image
            if art.text or art.image:
                await self.ctx.db.execute(
                    "UPDATE items SET content=?, image_url=? WHERE id=?", (text[:30000], image, item["id"]))
        if len(text) < len(item["summary"] or ""):
            text = item["summary"]
        return text.strip(), image

    async def write(self, item: Any, t: Triage, source_text: str, *, thin: bool = False,
                    follow_up: Any = None, instruction: str = "", current: str = "") -> Draft:
        ctx = self.ctx
        parts = [
            f"Сегодня: {ctx.clock_text()}. Фаза: {ctx.phase_text()}.",
            f"Оценка выпускающего редактора: рубрика {t.category}, важность {t.importance}/10."
            + (" В материале есть сюжетные спойлеры." if t.spoiler else ""),
        ]
        if follow_up is not None:
            parts.append(f"Это продолжение темы, по которой у нас уже есть материал: «{follow_up['headline']}». "
                         f"Не пересказывай его, дай новое и одной фразой напомни контекст.")
        parts.append(
            f"Источник: {item_note(item).get('publisher') or item['source']} "
            f"(уровень {item['tier']}: {_TIER_NAMES.get(item['tier'], '')}), язык: {item['lang']}\n"
            f"Заголовок источника: {item['title']}\n"
            f"Текст источника:\n\"\"\"\n{source_text or '(текста нет, только заголовок)'}\n\"\"\"")
        if len(source_text) >= 30000:
            parts.append("Текст источника обрезан по длине: опирайся только на то, что видишь.")
        if thin:
            parts.append("Текста в источнике мало. Напиши короткую заметку строго по тому, что есть, "
                         "и поставь needs_review=true: редактор сверит её с первоисточником.")
        if current:
            parts.append(f"Текущая версия поста, которую нужно переработать:\n\"\"\"\n{current}\n\"\"\"")
        if instruction:
            parts.append(f"Задача редактора: {instruction}")
        draft = await ctx.llm.structured("writer", system=writer_system(ctx.facts()), user="\n\n".join(parts),
                                         schema=Draft, max_tokens=8000)
        draft.headline = clip(" ".join(draft.headline.split()).lstrip("⚡️ ").strip(), 140)
        draft.body = draft.body.strip()
        draft.confidence = max(0.0, min(1.0, float(draft.confidence)))
        if not draft.headline or not draft.body:
            raise LLMError("редактор вернул пустой заголовок или текст")
        return draft

    async def _save_draft(self, item: Any, t: Triage, draft: Draft, *, image: str | None, thin: bool,
                          kind: str | None = None) -> int:
        ctx, pub = self.ctx, self.ctx.cfg.publishing
        needs_review = draft.needs_review or thin
        breaking = (t.importance >= pub.breaking_importance and not needs_review
                    and (item["tier"] == 1 or t.category in ("official", "media", "industry")))
        tags = pick_tags(t.category, draft.hashtags)
        source_name = item_note(item).get("publisher") or item["source"]
        source_url = item["url"] or None
        text = compose_post(ctx, headline=draft.headline, body=draft.body, category=t.category,
                            source_name=source_name, source_url=source_url, tags=tags, breaking=breaking)
        if youtube_id(item["url"] or ""):
            image = None  # ролик лучше показать встроенным плеером через превью ссылки
        return await ctx.db.create_post(
            kind=kind or ("breaking" if breaking else "news"), item_id=item["id"], status="review",
            headline=draft.headline, text=text, image_url=image, source_url=source_url, source_name=source_name,
            category=t.category, importance=t.importance, confidence=draft.confidence,
            meta={"body": draft.body, "tags": tags, "tier": item["tier"], "thin": thin,
                  "needs_review": needs_review, "review_reason": draft.review_reason.strip(),
                  "spoiler": bool(draft.spoiler or t.spoiler), "triage_reason": t.reason})

    # ── маршрутизация ───────────────────────────────────────────────────────
    def wants_review(self, post: Any) -> bool:
        pub = self.ctx.cfg.publishing
        meta = meta_of(post)
        if pub.mode == "review":
            return True
        # Сомнения самого редактора, скудный источник и спойлеры всегда смотрит человек
        if meta.get("needs_review") or meta.get("thin") or meta.get("spoiler"):
            return True
        if pub.mode == "auto":
            return False
        return not (post["category"] in pub.auto_categories
                    and post["confidence"] >= pub.auto_min_confidence
                    and int(meta.get("tier", 3)) <= pub.auto_min_tier)

    async def route(self, post_id: int, *, force_review: bool = False) -> str:
        """Отправить черновик на согласование или сразу в очередь."""
        post = await self.ctx.db.post(post_id)
        if post is None:
            return "missing"
        if force_review or self.wants_review(post):
            if not self.ctx.dry_run:
                await review.send_card(self.ctx, post_id)
            return "review"
        await self.ctx.db.update_post(post_id, status="queued")
        return "queued"

    # ── ручные сценарии ─────────────────────────────────────────────────────
    async def draft_from_url(self, url: str) -> int:
        """/post <ссылка>: скачать статью и подготовить черновик (всегда на согласование)."""
        ctx = self.ctx
        if ctx.http is None:
            raise LLMError("нет HTTP-клиента")
        art = await fetch_article(ctx.http, url)
        if len(art.text) < 150:
            raise ValueError("не удалось достать текст по ссылке. Пришлите текст новости командой /post <текст>")
        title = clip(art.text.split("\n", 1)[0], 200)
        item_id = await self._manual_item(url=url, title=title, content=art.text, image=art.image)
        return await self._draft_manual(item_id, kind="manual")

    async def draft_from_text(self, text: str, *, kind: str = "manual", credit: str | None = None) -> int:
        """/post <текст> и предложка: оформить заметку (всегда на согласование)."""
        title = clip(text.strip().split("\n", 1)[0], 200)
        item_id = await self._manual_item(url="", title=title, content=text.strip(), image=None, publisher=credit)
        return await self._draft_manual(item_id, kind=kind)

    async def _manual_item(self, *, url: str, title: str, content: str, image: str | None, publisher: str | None = None) -> int:
        from .textutil import domain_of
        name = publisher or (domain_of(url) if url else "")
        guid = canonical_url(url) if url else f"manual-{now()}-{abs(hash(content)) % 10**8}"
        await self.ctx.db.execute("DELETE FROM items WHERE source='Вручную' AND guid=?", (guid,))
        return await self.ctx.db.insert(
            """INSERT INTO items (source, tier, lang, guid, url, url_canon, title, summary, content, image_url,
                                  published_at, fetched_at, title_tokens, status, note)
               VALUES ('Вручную', 2, 'ru', ?, ?, ?, ?, '', ?, ?, ?, ?, ?, 'manual', ?)""",
            (guid, url, canonical_url(url) if url else guid, title, content[:30000], image, now(), now(),
             json.dumps(title_tokens(title), ensure_ascii=False),
             json.dumps({"publisher": name}, ensure_ascii=False)))

    async def _draft_manual(self, item_id: int, *, kind: str) -> int:
        item = await self.ctx.db.one("SELECT * FROM items WHERE id=?", (item_id,))
        try:
            t = await self.triage(item, await self._recent_posts())
        except LLMRefusal:
            raise
        except LLMError as e:
            if e.fatal or e.transient:
                raise
            t = Triage(relevant=True, duplicate_of=0, adds_new_info=False, category="other", importance=6,
                       spoiler=False, reason="оценка недоступна")
        draft = await self.write(item, t, item["content"])
        post_id = await self._save_draft(item, t, draft, image=item["image_url"], thin=False, kind=kind)
        post = await self.ctx.db.post(post_id)
        if not item["url"] and not item_note(item).get("publisher"):
            # заметка редакции: строки с источником нет
            await self._recompose(post, source_name=None, source_url=None)
        await self._set_item(item_id, "drafted", post=post_id)
        await self.route(post_id, force_review=True)
        return post_id

    async def _recompose(self, post: Any, **override: Any) -> None:
        meta = meta_of(post)
        text = compose_post(
            self.ctx, headline=override.get("headline", post["headline"]), body=override.get("body", meta.get("body", "")),
            category=post["category"], source_name=override.get("source_name", post["source_name"]),
            source_url=override.get("source_url", post["source_url"]), tags=meta.get("tags", []),
            breaking=post["kind"] == "breaking")
        fields: dict[str, Any] = {"text": text}
        for key in ("headline", "source_name", "source_url"):
            if key in override:
                fields[key] = override[key]
        if "body" in override:
            meta["body"] = override["body"]
            meta.pop("edited", None)
            fields["meta"] = meta
        await self.ctx.db.update_post(post["id"], **fields)

    async def rewrite(self, post_id: int, style: str) -> None:
        """Переписать черновик в другом ключе, сохранив факты источника."""
        post = await self.ctx.db.post(post_id)
        if post is None:
            raise ValueError("пост не найден")
        item = await self.ctx.db.one("SELECT * FROM items WHERE id=?", (post["item_id"],)) if post["item_id"] else None
        if item is None:
            raise ValueError("у поста нет исходного материала, переписать его нельзя — поправьте текст вручную")
        meta = meta_of(post)
        t = Triage(relevant=True, duplicate_of=0, adds_new_info=False, category=post["category"],
                   importance=post["importance"], spoiler=bool(meta.get("spoiler")), reason="")
        current = f"{post['headline']}\n\n{meta.get('body', '')}"
        source_text = (item["content"] or item["summary"] or "").strip()
        draft = await self.write(item, t, source_text, thin=bool(meta.get("thin")),
                                 instruction=REWRITE_STYLES[style], current=current)
        await self.ctx.db.update_post(post_id, confidence=draft.confidence)
        post = await self.ctx.db.post(post_id)
        await self._recompose(post, headline=draft.headline, body=draft.body)
