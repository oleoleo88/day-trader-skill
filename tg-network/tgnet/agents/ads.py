"""Ad slots: publishes paid posts on schedule with mandatory marking (Реклама + рекламодатель + erid),
keeps the "1/N" format (no news for N minutes after an ad, delete after N hours)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import AdSlot, Channel
from ..telegram import TelegramError, sanitize
from ..util import local_dt, now, parse_hhmm
from .base import Agent

GRACE_S = 30 * 60  # publish a slot if we are at most this late (e.g. after a restart)


def marked(slot: AdSlot) -> str:
    return f"{sanitize(slot.text)}\n\n<i>Реклама. {slot.advertiser}. erid: {slot.erid}</i>"


class Ads(Agent):
    name = "ads"

    def occurrences(self, slot: AdSlot, t: float) -> list[float]:
        tz = ZoneInfo(self.cfg.timezone)
        today = local_dt(t, self.cfg.timezone)
        out = []
        for s in slot.times:
            s = s.strip()
            if len(s) > 5:
                out.append(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=tz).timestamp())
            else:
                h, m = parse_hhmm(s)
                out.append(today.replace(hour=h, minute=m, second=0, microsecond=0).timestamp())
        return out

    async def tick(self) -> int:
        t = now()
        n = 0
        for ch in self.app.active_channels():
            if self.app.paused(ch.id):
                continue
            for slot in ch.ads:
                if not slot.erid or not slot.advertiser:
                    continue
                if slot.until and local_dt(t, self.cfg.timezone).strftime("%Y-%m-%d") > slot.until:
                    continue
                for at in self.occurrences(slot, t):
                    key = f"ad:{ch.id}:{slot.id}:{int(at)}"
                    if at <= t < at + GRACE_S and not self.db.get(key):
                        self.db.set(key, t)
                        if await self.publish(ch, slot):
                            n += 1
        await self.cleanup(t)
        return n

    async def publish(self, ch: Channel, slot: AdSlot) -> bool:
        bot = self.app.bot_for(ch)
        text = marked(slot)
        try:
            if slot.image:
                msg = await bot.send_photo(ch.chat_id, slot.image, text)
            else:
                msg = await bot.send_message(ch.chat_id, text, preview=True)
            if slot.pin:
                try:
                    await bot.pin(ch.chat_id, msg["message_id"])
                except TelegramError as e:
                    self.log.info("pin failed: %s", e)
        except TelegramError as e:
            await self.app.notify(f"❌ Реклама {slot.id} в {ch.title} не вышла: <code>{e}</code>", key=f"ad:{slot.id}")
            return False
        t = now()
        self.db.new_post(ch.id, None, "published", kind="ad", agent=self.name, category=slot.id, final=text,
                         published_at=t, message_id=msg["message_id"])
        if slot.quiet_after_minutes:
            self.db.set(f"ad_quiet_until:{ch.id}", t + slot.quiet_after_minutes * 60)
        if slot.delete_after_hours:
            pending = self.db.get("ad_deletes") or []
            pending.append({"chat": ch.chat_id, "msg": msg["message_id"], "bot": ch.id,
                            "at": t + slot.delete_after_hours * 3600})
            self.db.set("ad_deletes", pending)
        await self.app.notify(f"💰 Вышла реклама «{slot.id}» в {ch.title}")
        return True

    async def cleanup(self, t: float) -> None:
        pending = self.db.get("ad_deletes") or []
        keep = []
        for d in pending:
            if d["at"] > t:
                keep.append(d)
                continue
            ch = self.app.channel(d["bot"])
            if ch:
                await self.app.bot_for(ch).delete_message(d["chat"], d["msg"])
        if len(keep) != len(pending):
            self.db.set("ad_deletes", keep)
