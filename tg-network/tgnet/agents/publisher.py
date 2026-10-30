"""Publisher: owner approval queue, posting schedule, publishing, freshness control."""

from __future__ import annotations

import html

from ..config import Channel
from ..telegram import TelegramError, kb, visible_len
from ..util import clip, day_start, in_window, now
from .base import Agent
from .verifier import load_sheet

STATUS_LABEL = {"confirmed": "✅ подтверждено", "reported": "🟡 одно СМИ (с атрибуцией)", "rumor": "🟠 слух/инсайд"}


def compose(ch: Channel, text: str) -> str:
    return f"{text}\n\n{ch.signature}" if ch.signature else text


class Publisher(Agent):
    name = "publisher"

    async def tick(self) -> int:
        await self.route_ready()
        await self.expire_awaiting()
        n = 0
        for ch in self.app.active_channels():
            if not self.app.paused(ch.id):
                n += await self.publish_due(ch)
        return n

    # ------------------------------------------------------------------ approval routing
    def needs_approval(self, ch: Channel, p) -> bool:
        mode = self.app.autonomy(ch)
        if mode in ("approve", "manual"):
            return True
        sheet = load_sheet(p["facts"])
        if sheet.get("status") == "rumor":
            return True
        return (p["confidence"] or 0) < ch.auto_confidence

    async def route_ready(self) -> None:
        while True:
            p = self.db.claim(("ready",), lease_s=300)
            if p is None:
                return
            ch = self.app.channel(p["channel_id"])
            if ch is None:
                self.db.advance(p["id"], "rejected", owner_note="канал отключён")
                continue
            if self.needs_approval(ch, p):
                try:
                    await self.request_approval(ch, p)
                except TelegramError as e:
                    self.log.warning("approval request failed: %s", e)
                    self.db.fail(p["id"], f"approval: {e}")
            else:
                self.db.advance(p["id"], "scheduled", scheduled_at=now())

    def approval_text(self, ch: Channel, p, note: str = "") -> str:
        sheet = load_sheet(p["facts"])
        status = STATUS_LABEL.get(sheet.get("status"), sheet.get("status") or "–")
        srcs = ", ".join(sheet.get("attribution") or []) or "–"
        links = " ".join(f'<a href="{html.escape(u)}">[{i + 1}]</a>' for i, u in enumerate(sheet.get("links") or []))
        conf = p["confidence"] or 0
        head = (f"📝 <b>{html.escape(ch.title)}</b> · #{p['id']} · {p['category'] or p['kind']} · "
                f"важность {p['score'] or 0:.0f}\nПроверка: {status} · уверенность {conf:.0%}\n"
                f"Источники: {html.escape(srcs)} {links}")
        if p["image"]:
            head += f'\n🖼 <a href="{html.escape(p["image"])}">картинка</a>'
        if note:
            head += f"\n{note}"
        body = compose(ch, p["final"] or p["draft"] or "")
        if visible_len(head) + visible_len(body) > 3900:
            body = html.escape(clip(body, 3900 - visible_len(head)))
        return f"{head}\n━━━━━━━━━━━━\n{body}"

    @staticmethod
    def approval_kb(post_id: int) -> dict:
        return kb([[("✅ В очередь", f"ap:{post_id}"), ("⚡ Сейчас", f"now:{post_id}")],
                   [("✏️ Править", f"ed:{post_id}"), ("🗑 Отклонить", f"rj:{post_id}")]])

    async def request_approval(self, ch: Channel, p) -> None:
        msg = await self.app.control_bot.send_message(self.cfg.owner_chat_id, self.approval_text(ch, p),
                                                      reply_markup=self.approval_kb(p["id"]), pace=False)
        self.db.advance(p["id"], "awaiting", approval_msg_id=msg["message_id"],
                        expires_at=now() + ch.schedule.approval_ttl_hours * 3600)

    async def expire_awaiting(self) -> None:
        for p in self.db.all("SELECT * FROM posts WHERE status='awaiting' AND expires_at<?", (now(),)):
            self.db.advance(p["id"], "expired", owner_note="не одобрен вовремя")
            await self.app.mark_owner_msg(p["approval_msg_id"], f"⌛ #{p['id']} истёк – новость устарела")

    # ------------------------------------------------------------------ schedule
    def last_published(self, ch: Channel) -> float:
        return float(self.db.scalar("SELECT MAX(published_at) FROM posts WHERE channel_id=?", (ch.id,), 0.0))

    def published_today(self, ch: Channel, t: float) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM posts WHERE channel_id=? AND published_at>=? AND kind!='ad'",
                                  (ch.id, day_start(t, self.cfg.timezone)), 0))

    def is_stale(self, ch: Channel, p, t: float) -> bool:
        if p["owner_action"] in ("approved_now",):
            return False
        if p["story_id"]:
            first = self.db.scalar("SELECT first_seen FROM stories WHERE id=?", (p["story_id"],), t)
        else:
            first = p["created_at"]
        return t - first > ch.schedule.max_age_hours * 3600

    async def publish_due(self, ch: Channel) -> int:
        t = now()
        if (self.db.get(f"ad_quiet_until:{ch.id}") or 0) > t:
            return 0
        rows = self.db.all(
            "SELECT * FROM posts WHERE channel_id=? AND status='scheduled' AND scheduled_at<=? AND lease_until<? "
            "ORDER BY (owner_action='approved_now') DESC, score DESC, id", (ch.id, t, t))
        sc = ch.schedule
        for p in rows:
            if self.is_stale(ch, p, t):
                self.db.advance(p["id"], "expired", owner_note="устарел в очереди")
                continue
            urgent = (p["score"] or 0) >= sc.breaking_score or p["owner_action"] == "approved_now"
            if not urgent:
                if in_window(t, sc.quiet_hours, self.cfg.timezone):
                    return 0
                if t - self.last_published(ch) < sc.min_interval_min * 60:
                    return 0
                if self.published_today(ch, t) >= sc.max_posts_per_day:
                    return 0
            if self.db.x("UPDATE posts SET lease_until=? WHERE id=? AND lease_until<?", (t + 120, p["id"], t)).rowcount != 1:
                continue
            await self.publish(ch, p)
            return 1
        return 0

    async def publish(self, ch: Channel, p) -> None:
        bot = self.app.bot_for(ch)
        text = compose(ch, p["final"] or p["draft"] or "")
        try:
            msg = None
            if p["image"] and ch.images and visible_len(text) <= 1024:
                try:
                    msg = await bot.send_photo(ch.chat_id, p["image"], text)
                except TelegramError as e:
                    if e.code not in (400,):
                        raise
                    self.log.info("photo rejected (%s), sending text only", e.desc)
            if msg is None:
                msg = await bot.send_message(ch.chat_id, text)
        except TelegramError as e:
            dead = self.db.fail(p["id"], f"publish: {e}")
            if e.code in (400, 403) or dead:
                await self.app.notify(f"❌ Не могу опубликовать в <b>{html.escape(ch.title)}</b>: "
                                      f"<code>{html.escape(str(e))}</code>\nПроверь, что бот – админ канала.",
                                      key=f"pubfail:{ch.id}")
            return
        self.db.advance(p["id"], "published", published_at=now(), message_id=msg["message_id"])
        self.app.learner.on_published(p)
        self.log.info("[%s] published post %s as message %s", ch.id, p["id"], msg["message_id"])
