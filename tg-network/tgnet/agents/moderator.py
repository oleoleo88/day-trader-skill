"""Comment moderator for each channel's linked discussion group.

Layer 1: instant rules (links/forwards/mentions from new users, flood, stop-words, bot buttons).
Layer 2: LLM classifier for what slips through (scam, toxicity, hate, ads), only for messages
that look risky, to keep cost near zero. Strikes escalate: delete -> mute -> ban.
"""

from __future__ import annotations

import html
import re
import time
from collections import defaultdict, deque

from ..config import Channel
from ..telegram import Bot, TelegramError
from ..util import clip, now
from .base import Agent

DEFAULT_STOP = (
    "заработ", "доход от", "пассивный доход", "пиши в лс", "пишите в лс", "в личку", "в личные",
    "казино", "casino", "ставки без риска", "договорн", "крипт", "инвест", "арбитраж трафика", "схема",
    "переходи по ссылке", "бесплатные прогнозы", "vip прогноз", "каппер", "onlyfans", "интим",
)
LINK = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/|\b[a-z0-9-]+\.(ru|com|io|me|link|xyz|top|bet)\b)", re.I)
MENTION = re.compile(r"@[a-z0-9_]{5,}", re.I)


class Moderator(Agent):
    name = "moderator"
    role = "moderator"
    prompt = "moderator"
    lesson_role = "moderator"

    def __init__(self, app):
        super().__init__(app)
        self.recent: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=20))
        self.admins: dict[str, tuple[float, set[int]]] = {}

    async def admin_ids(self, bot: Bot, chat_id) -> set[int]:
        key = str(chat_id)
        cached = self.admins.get(key)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        try:
            ids = {a["user"]["id"] for a in await bot.admins(chat_id)}
        except TelegramError:
            ids = cached[1] if cached else set()
        ids.add(self.cfg.owner_chat_id)
        self.admins[key] = (time.monotonic(), ids)
        return ids

    def user_state(self, chat_id: str, user_id: int):
        self.db.x("INSERT OR IGNORE INTO mod_users(chat_id,user_id,first_seen) VALUES(?,?,?)", (chat_id, user_id, now()))
        self.db.x("UPDATE mod_users SET msgs=msgs+1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        return self.db.one("SELECT * FROM mod_users WHERE chat_id=? AND user_id=?", (chat_id, user_id))

    def rules(self, ch: Channel, msg: dict, trusted: bool) -> tuple[str, str] | None:
        """Return (label, reason) for an instant decision, or None."""
        text = (msg.get("text") or msg.get("caption") or "")
        low = text.lower()
        ents = (msg.get("entities") or []) + (msg.get("caption_entities") or [])
        has_link = any(e.get("type") in ("url", "text_link") for e in ents) or bool(LINK.search(text))
        if msg.get("via_bot") or msg.get("reply_markup"):
            return "spam", "сообщение через бота/с кнопками"
        stop = list(DEFAULT_STOP) + [w.lower() for w in ch.moderation.stop_words]
        hit = next((w for w in stop if w in low), None)
        if not trusted:
            if has_link:
                return "spam", "ссылка от нового участника"
            if msg.get("forward_origin") or msg.get("forward_from_chat"):
                return "spam", "пересылка от нового участника"
            if MENTION.search(text):
                return "spam", "упоминание канала/бота от нового участника"
            if hit:
                return "spam", f"стоп-слово «{hit}»"
        elif hit and has_link:
            return "spam", f"стоп-слово «{hit}» + ссылка"
        return None

    def flooding(self, ch: Channel, chat_id: str, user_id: int) -> bool:
        q = self.recent[(chat_id, user_id)]
        t = time.monotonic()
        q.append(t)
        window = [x for x in q if t - x <= ch.moderation.flood_seconds]
        return len(window) > ch.moderation.flood_messages

    def suspicious(self, msg: dict, trusted: bool) -> bool:
        text = msg.get("text") or msg.get("caption") or ""
        if len(text) < 3:
            return False
        if not trusted:
            return True
        letters = [c for c in text if c.isalpha()]
        caps = letters and sum(c.isupper() for c in letters) / len(letters) > 0.6 and len(letters) > 12
        return bool(caps) or len(text) > 600 or bool(LINK.search(text)) or any(
            w in text.lower() for w in ("лох", "дебил", "идиот", "сдохн", "убью"))

    async def classify(self, ch: Channel, text: str) -> tuple[str, int, str]:
        data, _ = await self.ask_json(f"Комментарий:\n«{clip(text, 1500)}»", ch, temperature=0)
        label = str(data.get("label") or "ok")
        try:
            sev = int(data.get("severity") or 0)
        except (TypeError, ValueError):
            sev = 0
        return label, sev, str(data.get("reason") or "")

    async def handle(self, bot: Bot, ch: Channel, msg: dict) -> str:
        if not ch.moderation.enabled:
            return "off"
        chat_id = str(msg["chat"]["id"])
        if msg.get("is_automatic_forward"):
            return "channel_post"
        sender_chat = msg.get("sender_chat")
        user = msg.get("from") or {}
        uid = user.get("id")
        if sender_chat and str(sender_chat.get("id")) in (ch.chat_id, chat_id):
            return "admin"
        if not uid or uid in await self.admin_ids(bot, chat_id):
            return "admin"
        st = self.user_state(chat_id, uid)
        m = ch.moderation
        trusted = st["msgs"] > m.trust_after_messages and now() - st["first_seen"] > m.trust_after_hours * 3600
        if sender_chat:  # writing as another channel = classic promo
            return await self.act(bot, ch, msg, uid, "spam", 3, "комментарий от имени другого канала", trusted)
        if self.flooding(ch, chat_id, uid):
            return await self.act(bot, ch, msg, uid, "flood", 1, "флуд", trusted)
        r = self.rules(ch, msg, trusted)
        if r:
            return await self.act(bot, ch, msg, uid, r[0], 2, r[1], trusted)
        if m.use_llm and self.suspicious(msg, trusted):
            try:
                label, sev, reason = await self.classify(ch, msg.get("text") or msg.get("caption") or "")
            except Exception as e:  # noqa: BLE001 - never block the chat on an LLM outage
                self.log.info("moderation llm unavailable: %s", e)
                return "ok"
            if label in ("scam", "hate", "spam", "ads") or (label != "ok" and sev >= 2):
                return await self.act(bot, ch, msg, uid, label, sev, reason, trusted)
        return "ok"

    async def act(self, bot: Bot, ch: Channel, msg: dict, uid: int, label: str, sev: int, reason: str,
                  trusted: bool) -> str:
        chat_id = str(msg["chat"]["id"])
        m = ch.moderation
        await bot.delete_message(chat_id, msg["message_id"])
        action = "delete"
        if label == "flood":
            try:
                await bot.restrict(chat_id, uid, int(now() + 600))
                action = "mute10m"
            except TelegramError:
                pass
        else:
            self.db.x("UPDATE mod_users SET strikes=strikes+1 WHERE chat_id=? AND user_id=?", (chat_id, uid))
            strikes = self.db.scalar("SELECT strikes FROM mod_users WHERE chat_id=? AND user_id=?", (chat_id, uid), 1)
            try:
                if (label in ("scam", "spam", "ads") and not trusted) or strikes >= m.strikes_ban or sev >= 3:
                    await bot.ban(chat_id, uid)
                    action = "ban"
                elif strikes >= m.strikes_mute:
                    await bot.restrict(chat_id, uid, int(now() + m.mute_hours * 3600))
                    action = f"mute{int(m.mute_hours)}h"
            except TelegramError as e:
                self.log.info("moderation action failed: %s", e)
        text = msg.get("text") or msg.get("caption") or ""
        self.db.x("INSERT INTO mod_actions(ts,chat_id,user_id,message_id,action,reason,text) VALUES(?,?,?,?,?,?,?)",
                  (now(), chat_id, uid, msg["message_id"], action, f"{label}: {reason}"[:300], text[:500]))
        self.log.info("[%s] moderation %s user %s: %s", ch.id, action, uid, reason)
        return action


def mod_summary(db, since: float) -> str:
    rows = db.all("SELECT action, COUNT(*) c FROM mod_actions WHERE ts>=? GROUP BY action", (since,))
    return ", ".join(f"{html.escape(r['action'])}: {r['c']}" for r in rows) or "нарушений нет"
