"""Аналитика: подписчики, приход по инвайт-ссылкам, просмотры и реакции, сводные отчёты."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import TYPE_CHECKING

import aiohttp
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberUpdated, MessageReactionCountUpdated

from .db import now
from .llm import estimate_cost
from .textutil import clip, esc, esc_attr

if TYPE_CHECKING:
    from .context import Ctx

log = logging.getLogger(__name__)

_POST_BLOCK = re.compile(r'data-post="[^"/]+/(\d+)"(.*?)(?=data-post="|\Z)', re.S)
_VIEWS = re.compile(r'tgme_widget_message_views">([^<]+)<')
_MEMBER = ("member", "administrator", "creator", "restricted")


def parse_count(raw: str) -> int | None:
    """'1.2K' → 1200, '3.4M' → 3400000, '987' → 987."""
    s = raw.strip().replace(",", ".").replace("\xa0", "").replace(" ", "").upper()
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


def parse_views(page: str) -> dict[int, int]:
    """HTML публичной страницы t.me/s/<канал> → {номер поста: просмотры}."""
    out: dict[int, int] = {}
    for m in _POST_BLOCK.finditer(page):
        views = _VIEWS.search(m.group(2))
        if views:
            n = parse_count(views.group(1))
            if n is not None:
                out[int(m.group(1))] = n
    return out


def fmt(n: int | float) -> str:
    return f"{int(n):,}".replace(",", " ")


def signed(n: int) -> str:
    return f"+{fmt(n)}" if n > 0 else (f"−{fmt(-n)}" if n < 0 else "0")


class Analytics:
    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx

    # ── подписчики ──────────────────────────────────────────────────────────
    async def snapshot(self) -> int | None:
        """Записать число подписчиков на сегодня (перезаписывается в течение дня)."""
        ctx = self.ctx
        try:
            count = await ctx.bot.get_chat_member_count(ctx.channel_id)
        except TelegramAPIError as e:
            log.warning("Не удалось получить число подписчиков: %s", e)
            return None
        await ctx.db.execute(
            "INSERT INTO subs_daily (day, subscribers) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET subscribers=excluded.subscribers",
            (ctx.today(), count))
        if ctx.cfg.analytics.rkn_reminder:
            await self._rkn_reminder(count)
        return count

    async def _rkn_reminder(self, count: int) -> None:
        db = self.ctx.db
        if count >= 10_000 and await db.kv_get("rkn:10k") is None:
            await db.kv_set("rkn:10k", now())
            await self.ctx.notifier.send(
                "🏛 <b>В канале больше 10 000 подписчиков.</b>\n"
                "По закону владелец такого канала обязан подать сведения в Роскомнадзор (через Госуслуги) — "
                "на это отводится 10 дней. Без регистрации российским рекламодателям запрещено у вас размещаться, "
                "а другим каналам — делать репосты. Порядок действий — в docs/STRATEGY.md, раздел «Юридический минимум».")
        elif 9_000 <= count < 10_000 and await db.kv_get("rkn:9k") is None:
            await db.kv_set("rkn:9k", now())
            await self.ctx.notifier.send(
                "🏛 До 10 000 подписчиков осталось меньше тысячи. После этой отметки канал нужно зарегистрировать "
                "в Роскомнадзоре в течение 10 дней — подготовьтесь заранее (docs/STRATEGY.md, «Юридический минимум»).")

    async def on_chat_member(self, upd: ChatMemberUpdated) -> None:
        """Событие вступления или выхода из канала; запоминаем ссылку, по которой человек пришёл."""
        was = upd.old_chat_member.status in _MEMBER and getattr(upd.old_chat_member, "is_member", True) is not False
        is_now = upd.new_chat_member.status in _MEMBER and getattr(upd.new_chat_member, "is_member", True) is not False
        if was == is_now:
            return
        link = upd.invite_link.invite_link if upd.invite_link else ""
        await self.ctx.db.execute(
            "INSERT INTO joins (ts, user_id, event, link) VALUES (?, ?, ?, ?)",
            (now(), upd.new_chat_member.user.id, "join" if is_now else "leave", link))

    async def on_reactions(self, upd: MessageReactionCountUpdated) -> None:
        total = sum(r.total_count for r in upd.reactions)
        await self.ctx.db.execute("UPDATE posts SET reactions=? WHERE channel_msg_id=?", (total, upd.message_id))

    # ── просмотры ───────────────────────────────────────────────────────────
    async def scrape_views(self) -> int:
        """Прочитать просмотры последних постов с публичной страницы канала. Возвращает число обновлённых постов."""
        ctx = self.ctx
        if not ctx.cfg.analytics.scrape_views or not ctx.channel_username or ctx.http is None:
            return 0
        try:
            async with ctx.http.get(f"https://t.me/s/{ctx.channel_username}") as resp:
                if resp.status != 200:
                    return 0
                page = await resp.text(errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.info("Страница канала для подсчёта просмотров недоступна: %s", e)
            return 0
        updated = 0
        for msg_id, views in parse_views(page).items():
            updated += await ctx.db.execute(
                "UPDATE posts SET views=? WHERE channel_msg_id=? AND status='published'", (views, msg_id))
        return updated

    # ── инвайт-ссылки ───────────────────────────────────────────────────────
    async def create_link(self, name: str, cost: float = 0.0) -> str:
        """Именная пригласительная ссылка: по ней видно, сколько людей привёл конкретный источник."""
        ctx = self.ctx
        link = await ctx.bot.create_chat_invite_link(ctx.channel_id, name=name[:32])
        await ctx.db.execute(
            "INSERT OR REPLACE INTO invite_links (link, name, cost, created_at) VALUES (?, ?, ?, ?)",
            (link.invite_link, name[:32], cost, now()))
        return link.invite_link

    async def links_report(self) -> str:
        db = self.ctx.db
        links = await db.all("SELECT * FROM invite_links ORDER BY created_at DESC LIMIT 30")
        if not links:
            return ("Именных ссылок пока нет. Создайте ссылку под каждую закупку рекламы:\n"
                    "<code>/link название 5000</code> — где 5000 это цена размещения в рублях (необязательно).")
        lines = ["<b>Приход по ссылкам</b>"]
        for l in links:
            joined = await db.scalar("SELECT COUNT(DISTINCT user_id) FROM joins WHERE link=? AND event='join'", (l["link"],), default=0)
            left = await db.scalar(
                """SELECT COUNT(DISTINCT user_id) FROM joins WHERE event='leave' AND user_id IN
                   (SELECT user_id FROM joins WHERE link=? AND event='join')""", (l["link"],), default=0)
            stay = joined - left
            row = f"• <b>{esc(l['name'])}</b>: пришло {fmt(joined)}, ушло {fmt(left)}, осталось {fmt(stay)}"
            if l["cost"]:
                row += f" · {fmt(l['cost'])} ₽"
                if stay > 0:
                    row += f" → {l['cost'] / stay:.1f} ₽ за подписчика"
            lines.append(row + f"\n   <code>{esc(l['link'])}</code>")
        return "\n".join(lines)

    # ── отчёт ───────────────────────────────────────────────────────────────
    async def _subs(self, days_ago: int) -> int | None:
        return await self.ctx.db.scalar(
            "SELECT subscribers FROM subs_daily WHERE day<=date(?, ?) ORDER BY day DESC LIMIT 1",
            (self.ctx.today(), f"-{days_ago} day"))

    async def stats_text(self, days: int = 7, *, title: str | None = None) -> str:
        ctx, db = self.ctx, self.ctx.db
        since = now() - days * 86400
        current = await self.snapshot()
        lines = [f"📊 <b>{title or 'Статистика'} · {days} дн.</b>", ""]

        if current is not None:
            parts = []
            for d, label in ((1, "за сутки"), (7, "за 7 дней"), (30, "за 30 дней")):
                prev = await self._subs(d)
                if prev is not None:
                    parts.append(f"{label} {signed(current - prev)}")
            lines.append(f"👥 Подписчики: <b>{fmt(current)}</b>" + (f" ({', '.join(parts)})" if parts else ""))
        joined = await db.scalar("SELECT COUNT(*) FROM joins WHERE event='join' AND ts>?", (since,), default=0)
        left = await db.scalar("SELECT COUNT(*) FROM joins WHERE event='leave' AND ts>?", (since,), default=0)
        if joined or left:
            lines.append(f"➕ Пришло {fmt(joined)} · ➖ ушло {fmt(left)}")

        published = await db.scalar(
            "SELECT COUNT(*) FROM posts WHERE status='published' AND kind!='ad' AND published_at>?", (since,), default=0)
        breaking = await db.scalar(
            "SELECT COUNT(*) FROM posts WHERE status='published' AND kind='breaking' AND published_at>?", (since,), default=0)
        queued = await db.scalar("SELECT COUNT(*) FROM posts WHERE status='queued' AND kind!='ad'", default=0)
        in_review = await db.scalar("SELECT COUNT(*) FROM posts WHERE status='review'", default=0)
        lines.append(f"📰 Постов вышло: {published}" + (f" (молний: {breaking})" if breaking else "")
                     + f" · в очереди {queued} · на согласовании {in_review}")
        seen = await db.scalar("SELECT COUNT(*) FROM items WHERE fetched_at>? AND status!='seeded'", (since,), default=0)
        if seen:
            lines.append(f"📡 Записей из лент по теме: {fmt(seen)}")

        avg = await db.one(
            "SELECT AVG(views) a, COUNT(*) n FROM posts WHERE status='published' AND kind!='ad' AND views IS NOT NULL AND published_at>?",
            (since,))
        if avg and avg["n"]:
            line = f"👁 Средние просмотры: <b>{fmt(avg['a'])}</b> (по {avg['n']} постам)"
            if current:
                line += f" · охват {avg['a'] / current:.0%} от базы"
            lines.append(line)
        top = await db.all(
            """SELECT headline, channel_msg_id, views, reactions FROM posts
               WHERE status='published' AND kind!='ad' AND published_at>? AND channel_msg_id IS NOT NULL
                 AND (views IS NOT NULL OR reactions IS NOT NULL)
               ORDER BY COALESCE(views, 0) DESC, COALESCE(reactions, 0) DESC LIMIT 3""", (since,))
        if top:
            lines.append("🏆 Лучшие посты:")
            for t in top:
                stat = " · ".join(x for x in (f"👁 {fmt(t['views'])}" if t["views"] is not None else "",
                                              f"❤️ {fmt(t['reactions'])}" if t["reactions"] is not None else "") if x)
                lines.append(f'   <a href="{esc_attr(ctx.post_link(t["channel_msg_id"]))}">{esc(clip(t["headline"] or "пост", 60))}</a> — {stat}')

        mod = {r["action"]: r["n"] for r in await db.all(
            "SELECT action, COUNT(*) n FROM mod_log WHERE ts>? GROUP BY action", (since,))}
        if mod:
            lines.append(f"🛡 Модерация: удалено {mod.get('delete', 0)}, предупреждений {mod.get('warn', 0)}, "
                         f"мутов {mod.get('mute', 0)}, банов {mod.get('ban', 0)}")

        usage = await db.all(
            """SELECT model, SUM(calls) calls, SUM(input_tokens) i, SUM(output_tokens) o, SUM(cache_read) r, SUM(cache_write) w
               FROM llm_usage WHERE day>=date(?, ?) GROUP BY model""", (ctx.today(), f"-{days} day"))
        if usage:
            calls = sum(u["calls"] for u in usage)
            costs = [estimate_cost(u["model"], u["i"], u["o"], u["r"], u["w"]) for u in usage]
            known = [c for c in costs if c is not None]
            cost = f" · примерно ${sum(known):.2f}" if known else ""
            lines.append(f"🤖 Запросов к ИИ: {fmt(calls)}{cost}")

        count, revenue = await ctx.ads.revenue(30)
        if count:
            lines.append(f"💰 Реклама за 30 дней: размещений {count}" + (f", выручка {fmt(revenue)} ₽" if revenue else ""))
        return "\n".join(lines)

    # ── уборка ──────────────────────────────────────────────────────────────
    async def housekeeping(self) -> None:
        """Раз в сутки чистим то, что больше не понадобится."""
        db = self.ctx.db
        month = now() - 30 * 86400
        await db.execute("DELETE FROM items WHERE fetched_at<? AND status!='drafted'", (month,))
        await db.execute("DELETE FROM mod_log WHERE ts<?", (now() - 90 * 86400,))
        await db.execute("DELETE FROM relay WHERE created_at<?", (month,))
        # суточные счётчики вида judge:ГГГГ-ММ-ДД и qa:ГГГГ-ММ-ДД[:пользователь]
        cutoff = (self.ctx.now_local().date() - timedelta(days=3)).isoformat()
        await db.execute("DELETE FROM kv WHERE k LIKE 'judge:%' AND substr(k, 7, 10)<?", (cutoff,))
        await db.execute("DELETE FROM kv WHERE k LIKE 'qa:%' AND substr(k, 4, 10)<?", (cutoff,))
