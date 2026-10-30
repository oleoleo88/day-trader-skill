"""Analyst: collects post views/reactions, sends the daily report, and rewrites the
channel playbook (data-driven lessons for the editor and the writer) from real performance."""

from __future__ import annotations

import html
import statistics

from ..config import Channel
from ..sources import fetch, parse_tme
from ..telegram import plain
from ..util import clip, day_start, dumps, local_dt, now
from .base import Agent
from .moderator import mod_summary


class Analyst(Agent):
    name = "analyst"
    role = "analyst"
    prompt = "analyst"

    # ------------------------------------------------------------------ metrics
    async def collect(self) -> int:
        n = 0
        for ch in self.app.active_channels():
            if ch.username:
                try:
                    n += await self.collect_channel(ch)
                except Exception as e:  # noqa: BLE001
                    self.log.info("views fetch failed for %s: %s", ch.id, e)
        return n

    async def collect_channel(self, ch: Channel) -> int:
        t = now()
        want = {r["message_id"]: r["id"] for r in self.db.all(
            "SELECT id, message_id FROM posts WHERE channel_id=? AND message_id IS NOT NULL AND published_at>?",
            (ch.id, t - 3 * 86400))}
        if not want:
            return 0
        got, before, pages = 0, None, 0
        while want and pages < 4:
            url = f"https://t.me/s/{ch.username}" + (f"?before={before}" if before else "")
            _, text, _ = await fetch(self.app.http, url)
            entries = parse_tme(text, ch.username)
            if not entries:
                break
            for e in entries:
                pid = want.pop(e.message_id, None)
                if pid and e.views is not None:
                    react = self.db.get(f"react:{ch.chat_id}:{e.message_id}") or 0
                    self.db.x("INSERT OR REPLACE INTO metrics(post_id,ts,views,reactions) VALUES(?,?,?,?)",
                              (pid, t, e.views, react))
                    got += 1
            oldest = min(e.message_id for e in entries)
            if not want or oldest <= min(want):
                break
            before, pages = oldest, pages + 1
        return got

    def record_reactions(self, chat_id, message_id: int, total: int) -> None:
        self.db.set(f"react:{chat_id}:{message_id}", total)

    # ------------------------------------------------------------------ performance table
    def performance(self, ch: Channel, days: int = 7) -> list[dict]:
        rows = self.db.all(
            "SELECT p.id, p.category, p.kind, p.published_at, p.final, p.image, p.facts, MAX(m.views) v, "
            "MAX(m.reactions) r FROM posts p LEFT JOIN metrics m ON m.post_id=p.id "
            "WHERE p.channel_id=? AND p.published_at>? GROUP BY p.id ORDER BY p.published_at",
            (ch.id, now() - days * 86400))
        views = [r["v"] for r in rows if r["v"]]
        med = statistics.median(views) if views else 0
        out = []
        for r in rows:
            out.append({"id": r["id"], "category": r["category"] or r["kind"], "hour": local_dt(r["published_at"], self.cfg.timezone).hour,
                        "len": len(plain(r["final"] or "")), "image": bool(r["image"]), "views": r["v"] or 0,
                        "reactions": r["r"] or 0, "rel": round((r["v"] or 0) / med, 2) if med else None,
                        "text": clip(plain(r["final"] or ""), 160)})
        return out

    # ------------------------------------------------------------------ daily report
    def report(self) -> str:
        t = now()
        since = day_start(t, self.cfg.timezone)
        lines = [f"📊 <b>Отчёт за {local_dt(t, self.cfg.timezone):%d.%m}</b>"]
        for ch in self.app.active_channels():
            pub = self.db.all("SELECT p.id, p.final, MAX(m.views) v FROM posts p LEFT JOIN metrics m ON m.post_id=p.id "
                              "WHERE p.channel_id=? AND p.published_at>=? GROUP BY p.id ORDER BY v DESC", (ch.id, since))
            counts = {r["status"]: r["c"] for r in self.db.all(
                "SELECT status, COUNT(*) c FROM posts WHERE channel_id=? AND updated_at>=? GROUP BY status", (ch.id, since))}
            mode = self.app.autonomy(ch) + (" ⏸" if self.app.paused(ch.id) else "")
            lines.append(f"\n<b>{html.escape(ch.title)}</b> · режим {mode}")
            lines.append(f"Опубликовано: {len(pub)} · отклонено: {counts.get('rejected', 0)} · "
                         f"истекло: {counts.get('expired', 0)} · ждут тебя: {counts.get('awaiting', 0)} · "
                         f"сбоев: {counts.get('failed', 0)}")
            views = [r["v"] for r in pub if r["v"]]
            if views:
                lines.append(f"Просмотры: всего {sum(views)}, медиана {int(statistics.median(views))}")
                for r in pub[:3]:
                    if r["v"]:
                        lines.append(f"  🔥 {r['v']} – {html.escape(clip(plain(r['final'] or ''), 70))}")
        spend = self.app.router.spent_today()
        lines.append(f"\n🤖 LLM сегодня: ${spend:.2f} из ${self.cfg.llm.daily_budget_usd:.2f}")
        lines.append(f"🛡 Модерация: {mod_summary(self.db, since)}")
        bad = self.db.all("SELECT source_id, last_error FROM sources_state WHERE failures>=3")
        if bad:
            lines.append("⚠️ Источники с ошибками: " + ", ".join(html.escape(b["source_id"]) for b in bad[:10]))
        errs = self.db.scalar("SELECT COUNT(*) FROM events WHERE level='ERROR' AND ts>=?", (since,), 0)
        if errs:
            lines.append(f"❗ Ошибок в журнале: {errs} (см. /status)")
        lessons = self.db.scalar("SELECT COUNT(*) FROM lessons WHERE active=1 AND created_at>=?", (since,), 0)
        if lessons:
            lines.append(f"🎓 Новых уроков сегодня: {lessons}")
        return "\n".join(lines)

    async def daily(self) -> None:
        await self.app.control_bot.send_message(self.cfg.owner_chat_id, self.report(), pace=False)
        for ch in self.app.active_channels():
            try:
                await self.update_playbook(ch)
            except Exception as e:  # noqa: BLE001
                self.log.info("playbook update failed for %s: %s", ch.id, e)

    async def update_playbook(self, ch: Channel) -> int:
        perf = [p for p in self.performance(ch, 7) if p["views"]]
        if len(perf) < 15:
            return 0
        data, _ = await self.ask_json("Статистика постов за 7 дней (rel = просмотры / медиана канала):\n"
                                      + "\n".join(dumps(p) for p in perf), ch, temperature=0.2)
        new = []
        for les in (data.get("lessons") or [])[:6]:
            if isinstance(les, dict) and les.get("role") in ("triage", "writer") and les.get("text"):
                new.append((les["role"], str(les["text"])))
        if not new:
            return 0
        self.db.x("UPDATE lessons SET active=0 WHERE channel_id=? AND origin='analyst'", (ch.id,))
        for role, text in new:
            self.db.add_lesson(ch.id, role, text, "analyst", weight=1.5)
        return len(new)
