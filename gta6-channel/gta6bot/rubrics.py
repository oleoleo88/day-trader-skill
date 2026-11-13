"""Рубрики по расписанию: обратный отсчёт, дайджест дня, опрос недели, отчёт админу."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from . import review
from .config import RubricCfg
from .db import now
from .llm import LLMError
from .prompts import CATEGORY_LABELS, POLL_SYSTEM, PollDraft
from .textutil import clip, esc, esc_attr, plural_ru

if TYPE_CHECKING:
    from .context import Ctx

log = logging.getLogger(__name__)

MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря")
ANNOUNCED = date(2023, 12, 5)   # первый трейлер: от него считаем шкалу ожидания
COUNTDOWN_LINES = (
    "Чего ждёте больше всего? Пишите в комментариях 👇",
    "Кто уже освободил место на консоли? 👀",
    "Берёте отгул на день релиза или держитесь до выходных?",
    "С чего начнёте: сюжет или сразу кататься по Вайс-Сити?",
    "Перепроходите старые части или бережёте силы?",
    "Какую машину угоните первой? 🚗",
    "За кого будете играть охотнее: за Джейсона или за Люсию?",
)
WINDOW_HOURS = 3   # рубрика, пропущенная из-за простоя, выходит не позже чем через N часов после своего времени


def progress_bar(done: float, width: int = 12) -> str:
    done = max(0.0, min(1.0, done))
    filled = round(done * width)
    return "▓" * filled + "░" * (width - filled)


class Rubrics:
    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx

    async def _due(self, name: str, cfg: RubricCfg, *, weekly: bool = False) -> bool:
        if not cfg.enabled:
            return False
        n = self.ctx.now_local()
        if weekly and n.weekday() != cfg.weekday:
            return False
        start = datetime.combine(n.date(), cfg.at, tzinfo=n.tzinfo)
        if not (start <= n < start + timedelta(hours=WINDOW_HOURS)):
            return False
        return await self.ctx.db.kv_get(f"rubric:{name}") != n.date().isoformat()

    async def _done(self, name: str) -> None:
        await self.ctx.db.kv_set(f"rubric:{name}", self.ctx.today())

    async def tick(self) -> None:
        r = self.ctx.cfg.rubrics
        if await self._due("countdown", r.countdown):
            await self._done("countdown")
            await self.countdown()
        if await self._due("digest", r.digest):
            await self._done("digest")
            await self.digest()
        if await self._due("poll", r.poll, weekly=True):
            await self._done("poll")
            await self.poll()
        if await self._due("weekly_report", r.weekly_report, weekly=True):
            await self._done("weekly_report")
            await self.ctx.notifier.send(await self.ctx.analytics.stats_text(7, title="Недельный отчёт"))

    def _footer(self) -> str:
        ctx = self.ctx
        if ctx.cfg.channel.footer and ctx.channel_link:
            return f'\n\n<a href="{esc_attr(ctx.channel_link)}">{esc(ctx.cfg.channel.name)}</a>'
        return ""

    # ── обратный отсчёт ─────────────────────────────────────────────────────
    def countdown_text(self) -> str | None:
        ctx = self.ctx
        release = ctx.cfg.channel.release
        days = ctx.days_to_release()
        if days < 0:
            return None
        when = f"{release.day} {MONTHS_GEN[release.month - 1]}"
        if days == 0:
            return (f"🚀 <b>Сегодня релиз GTA VI</b>\n\n{progress_bar(1.0)} 100%\n\n"
                    f"Дождались. Делитесь первыми впечатлениями в комментариях, сюжетные спойлеры "
                    f"прячьте под скрытый текст." + self._footer())
        total = max(1, (release - ANNOUNCED).days)
        done = 1 - days / total
        if days == 1:
            head = "Завтра релиз GTA VI"
        elif days == 7:
            head = "До релиза GTA VI — ровно неделя"
        else:
            head = f"До релиза GTA VI — {days} {plural_ru(days, ('день', 'дня', 'дней'))}"
        line = COUNTDOWN_LINES[days % len(COUNTDOWN_LINES)]
        return (f"⏳ <b>{head}</b>\n\n{progress_bar(done)} {done:.0%}\n\n"
                f"{when} · PlayStation 5 и Xbox Series X|S\n\n{line}" + self._footer())

    async def countdown(self) -> int | None:
        text = self.countdown_text()
        if text is None:
            return None
        days = self.ctx.days_to_release()
        # пост о релизе подтверждает человек: дату могли перенести, а config.yaml не обновить
        needs_card = days == 0 and self.ctx.cfg.publishing.mode != "auto"
        post_id = await self.ctx.db.create_post(
            kind="countdown", status="review" if needs_card else "queued",
            headline=f"Обратный отсчёт: {days} дн.", text=text,
            category="other", importance=5, confidence=1.0, not_before=now())
        if needs_card:
            await review.send_card(self.ctx, post_id)
        return post_id

    # ── дайджест ────────────────────────────────────────────────────────────
    async def digest(self, *, force: bool = False) -> int | None:
        ctx = self.ctx
        rows = await ctx.db.all(
            """SELECT headline, category, channel_msg_id FROM posts
               WHERE status='published' AND kind IN ('news', 'breaking', 'manual', 'suggestion')
                 AND published_at>=? AND channel_msg_id IS NOT NULL AND headline!=''
               ORDER BY importance DESC, published_at ASC LIMIT 8""", (ctx.day_start_ts(),))
        if len(rows) < (1 if force else max(1, ctx.cfg.rubrics.digest.min_posts)):
            return None
        lines = []
        for r in rows:
            emoji = CATEGORY_LABELS.get(r["category"], "📰").split(" ", 1)[0]
            lines.append(f'{emoji} <a href="{esc_attr(ctx.post_link(r["channel_msg_id"]))}">{esc(clip(r["headline"], 110))}</a>')
        n = ctx.now_local()
        text = (f"🗞 <b>Главное за {n.day} {MONTHS_GEN[n.month - 1]}</b>\n\n" + "\n\n".join(lines) + self._footer())
        return await ctx.db.create_post(
            kind="digest", status="queued", headline=f"Дайджест {n:%d.%m}", text=text, category="other",
            importance=5, confidence=1.0, not_before=now())

    # ── опрос / викторина ───────────────────────────────────────────────────
    async def make_poll(self, kind: str | None = None) -> PollDraft:
        ctx = self.ctx
        if kind is None:
            kind = "poll" if ctx.now_local().isocalendar().week % 2 == 0 else "quiz"
        rows = await ctx.db.all(
            """SELECT headline FROM posts WHERE status='published' AND kind IN ('news', 'breaking', 'manual', 'suggestion')
               AND published_at>? AND headline!='' ORDER BY importance DESC, published_at DESC LIMIT 15""",
            (now() - 7 * 86400,))
        recent = "\n".join(f"• {r['headline']}" for r in rows) or "(на этой неделе постов не было)"
        user = (f"Сегодня: {ctx.clock_text()}. Фаза: {ctx.phase_text()}.\n"
                f"Нужен тип: {kind}.\n\nПосты канала за неделю:\n{recent}\n\n"
                f"Подтверждённые факты об игре:\n{ctx.facts()}")
        draft = await ctx.llm.structured("poll", system=POLL_SYSTEM, user=user, schema=PollDraft, max_tokens=4000)
        draft.kind = kind  # тип задаём мы, а не модель
        draft.question = clip(" ".join(draft.question.split()), 290)
        draft.options = [clip(" ".join(o.split()), 95) for o in draft.options if o.strip()][:10]
        draft.explanation = clip(draft.explanation.strip(), 195)
        if len(draft.options) < 2:
            raise LLMError("в опросе меньше двух вариантов")
        if len(set(draft.options)) != len(draft.options):
            raise LLMError("в опросе повторяются варианты")
        if kind == "quiz" and not 0 <= draft.correct_index < len(draft.options):
            raise LLMError("в викторине не указан правильный вариант")
        return draft

    async def poll(self, kind: str | None = None, *, replace_id: int | None = None) -> int | None:
        """Сгенерировать опрос. В режиме auto он сразу встаёт в очередь, иначе идёт на согласование."""
        ctx = self.ctx
        try:
            draft = await self.make_poll(kind)
        except LLMError as e:
            log.warning("Опрос не сгенерирован: %s", e)
            await ctx.notifier.send(f"⚠️ Не удалось придумать опрос: {esc(str(e))}. Попробуйте /poll ещё раз.")
            return None
        meta = {"poll": {"kind": draft.kind, "question": draft.question, "options": draft.options,
                         "correct_index": draft.correct_index if draft.kind == "quiz" else -1,
                         "explanation": draft.explanation if draft.kind == "quiz" else ""}}
        if replace_id:
            old = await ctx.db.post(replace_id)
            if old is not None:
                await review.drop_card(ctx, old)
                await ctx.db.update_post(replace_id, headline=draft.question, meta=meta)
                await review.send_card(ctx, replace_id)
                return replace_id
        needs_card = ctx.cfg.publishing.mode != "auto"
        post_id = await ctx.db.create_post(
            kind="poll", status="review" if needs_card else "queued", headline=draft.question, text="",
            category="other", importance=5, confidence=1.0, not_before=now(), meta=meta)
        if needs_card:
            await review.send_card(ctx, post_id)
        return post_id
