"""Рекламные размещения: расписание, маркировка erid, «топ», автоудаление, учёт выручки."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aiogram.types import Message

from . import review
from .db import meta_of, now
from .textutil import clip, esc, visible_len

if TYPE_CHECKING:
    from .context import Ctx

HELP = """<b>Как поставить рекламный пост</b>

1. Пришлите в этот чат готовый пост рекламодателя (текст или фото/видео с подписью).
2. Ответьте на него командой:
<code>/ad 20.11 14:00 top=60 feed=24 erid=2VtzqwXXXXX price=5000 adv=ООО «Ромашка», ИНН 7700000000</code>

<b>Когда:</b> <code>now</code>, <code>+30m</code>, <code>+2h</code>, <code>18:30</code>, <code>20.11 14:00</code> или <code>2026-11-20 14:00</code> (время канала).
<b>top</b> — сколько минут пост остаётся последним в ленте (ничего не выходит следом).
<b>feed</b> — через сколько часов пост удалится сам; 0 — не удалять.
<b>erid</b> — токен от оператора рекламных данных; строка «Реклама. … erid: …» добавится автоматически.
<b>adv</b> — рекламодатель для маркировки. <b>price</b> — цена, для учёта в /stats.
<b>btn</b> — кнопка под постом: <code>btn=Перейти|https://example.com</code>

Список размещений: /ads · отмена: /adcancel &lt;номер&gt;"""

_KV = re.compile(r"(\w+)=(.*?)(?=\s+\w+=|$)", re.S)
_REL = re.compile(r"^\+(\d+)\s*([mhмч])$", re.I)
_HM = re.compile(r"^(\d{1,2}):(\d{2})$")
_DMHM = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+(\d{1,2}):(\d{2})$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})$")


@dataclass
class AdSpec:
    when: int = 0
    when_text: str = ""
    top_minutes: int = 60
    feed_hours: int = 24
    erid: str = ""
    price: float = 0.0
    advertiser: str = ""
    button: dict | None = None
    warnings: list[str] = field(default_factory=list)


def parse_when(raw: str, now_local: datetime) -> datetime:
    s = raw.strip().lower()
    if s in ("now", "сейчас", ""):
        return now_local
    m = _REL.match(s)
    if m:
        n = int(m.group(1))
        return now_local + (timedelta(minutes=n) if m.group(2) in "mм" else timedelta(hours=n))
    try:
        m = _HM.match(s)
        if m:
            t = now_local.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            return t if t > now_local else t + timedelta(days=1)
        m = _DMHM.match(s)
        if m:
            year = int(m.group(3)) if m.group(3) else now_local.year
            t = now_local.replace(year=year, month=int(m.group(2)), day=int(m.group(1)), hour=int(m.group(4)),
                                  minute=int(m.group(5)), second=0, microsecond=0)
            if not m.group(3) and t < now_local - timedelta(days=1):
                t = t.replace(year=year + 1)   # «05.01 12:00» в декабре — это уже следующий год
            return t
        m = _ISO.match(s)
        if m:
            return now_local.replace(year=int(m.group(1)), month=int(m.group(2)), day=int(m.group(3)),
                                     hour=int(m.group(4)), minute=int(m.group(5)), second=0, microsecond=0)
    except ValueError:
        raise ValueError(f"такой даты или времени не существует: «{raw.strip()}»") from None
    raise ValueError(f"не понял время «{raw.strip()}». Примеры: now, +30m, 18:30, 20.11 14:00")


def parse_spec(args: str, now_local: datetime, default_top: int, default_feed: int) -> AdSpec:
    """Разобрать аргументы /ad. Ошибки — ValueError с понятным текстом."""
    first_kv = _KV.search(args)
    when_part = args[: first_kv.start()] if first_kv else args
    spec = AdSpec(top_minutes=default_top, feed_hours=default_feed)
    when_dt = parse_when(when_part, now_local)
    if when_dt < now_local - timedelta(minutes=2):
        raise ValueError(f"время {when_dt:%d.%m %H:%M} уже прошло")
    spec.when = int(max(when_dt, now_local).timestamp())
    spec.when_text = "сразу" if when_dt <= now_local else f"{when_dt:%d.%m.%Y %H:%M}"
    for key, value in _KV.findall(args):
        key, value = key.lower(), value.strip()
        try:
            if key == "top":
                spec.top_minutes = int(value)
            elif key == "feed":
                spec.feed_hours = int(value)
            elif key == "price":
                spec.price = float(value.replace(" ", "").replace(",", "."))
            elif key == "erid":
                spec.erid = value
            elif key in ("adv", "advertiser"):
                spec.advertiser = value.strip(" .")
            elif key in ("btn", "button"):
                text, sep, url = value.partition("|")
                if not sep or not url.strip().startswith(("http://", "https://", "tg://")):
                    raise ValueError("кнопка задаётся так: btn=Текст|https://ссылка")
                spec.button = {"text": text.strip()[:60], "url": url.strip()}
            else:
                raise ValueError(f"неизвестный параметр «{key}». Доступны: top, feed, erid, price, adv, btn")
        except ValueError as e:
            if "неизвест" in str(e) or "кнопка" in str(e):
                raise
            raise ValueError(f"параметр {key}: ожидалось число, получено «{value}»") from None
    if spec.top_minutes < 0 or spec.feed_hours < 0 or spec.price < 0:
        raise ValueError("top, feed и price не могут быть отрицательными")
    if spec.feed_hours and spec.feed_hours * 60 < spec.top_minutes:
        raise ValueError("пост удалится раньше, чем закончится «топ»: увеличьте feed или уменьшите top")
    return spec


class Ads:
    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx

    async def create(self, creative: Message, args: str) -> tuple[int, list[str]]:
        """Создать размещение из сообщения-креатива. → (номер поста, предупреждения)."""
        ctx, cfg = self.ctx, self.ctx.cfg.ads
        spec = parse_spec(args, ctx.now_local(), cfg.default_top_minutes, cfg.default_feed_hours)
        has_media = bool(creative.photo or creative.video or creative.animation or creative.document)
        body = (creative.html_text or "").strip() if (creative.text or creative.caption) else ""
        if not body and not has_media:
            raise ValueError("в сообщении-креативе нет ни текста, ни медиа")
        text = body
        if spec.erid:
            if spec.erid not in text:
                parts = ["Реклама"] + ([esc(spec.advertiser)] if spec.advertiser else []) + [f"erid: {esc(spec.erid)}"]
                text = (text + "\n\n" if text else "") + "<i>" + ". ".join(parts) + "</i>"
        elif cfg.require_erid:
            spec.warnings.append(
                "Пост создан без erid. Реклама без маркировки и передачи данных в ЕРИР — нарушение закона о рекламе: "
                "получите токен у оператора рекламных данных и пересоздайте размещение.")
        limit = 1024 if has_media else 4096
        if visible_len(text) > limit:
            raise ValueError(f"текст с маркировкой длиннее {limit} знаков ({visible_len(text)}). "
                             + ("Сократите подпись или пришлите пост без медиа." if has_media else "Сократите текст."))
        if has_media and creative.media_group_id:
            spec.warnings.append("Креатив — часть альбома: опубликуется только то сообщение, на которое вы ответили.")
        post_id = await ctx.db.create_post(
            kind="ad", status="queued", headline=clip(creative.text or creative.caption or "Реклама", 80).replace("\n", " "),
            text=text, category="other", importance=10, confidence=1.0, not_before=spec.when,
            meta={"creative": {"chat_id": creative.chat.id, "message_id": creative.message_id, "media": has_media},
                  "top_minutes": spec.top_minutes, "feed_hours": spec.feed_hours, "erid": spec.erid,
                  "price": spec.price, "advertiser": spec.advertiser, "button": spec.button,
                  "when_text": spec.when_text})
        await review.send_card(ctx, post_id)
        return post_id, spec.warnings

    async def cancel(self, post_id: int) -> str:
        post = await self.ctx.db.post(post_id)
        if post is None or post["kind"] != "ad":
            return f"Рекламного размещения #{post_id} нет."
        if post["status"] == "published":
            ok, error = await self.ctx.publisher.delete_from_channel(post_id)
            return f"Пост #{post_id} удалён из канала." if ok else f"Не удалось удалить пост #{post_id}: {esc(error)}"
        if post["status"] != "queued":
            return f"Размещение #{post_id} уже неактивно."
        await self.ctx.db.update_post(post_id, status="rejected")
        await review.close_card(self.ctx, post, "❌ Размещение отменено")
        return f"Размещение #{post_id} отменено."

    async def listing(self) -> str:
        ctx = self.ctx
        rows = await ctx.db.all(
            """SELECT * FROM posts WHERE kind='ad' AND (status='queued' OR (status='published' AND
               (delete_at IS NOT NULL OR published_at>?))) ORDER BY COALESCE(published_at, not_before)""",
            (now() - 86400,))
        if not rows:
            return "Рекламных размещений нет. Как поставить пост: /ad"
        lines = ["<b>Рекламные размещения</b>"]
        for r in rows:
            m = meta_of(r)
            price = f" · {m['price']:g} ₽" if m.get("price") else ""
            if r["status"] == "queued":
                state = f"🕐 выйдет {ctx.local(r['not_before']):%d.%m %H:%M}"
            else:
                state = f"✅ вышел {ctx.local(r['published_at']):%d.%m %H:%M}"
                if r["delete_at"]:
                    state += f", удалится {ctx.local(r['delete_at']):%d.%m %H:%M}"
            lines.append(f"#{r['id']} · {state} · топ {m.get('top_minutes', 0)} мин{price}\n   {esc(clip(r['headline'], 70))}")
        lines.append("\nОтмена: /adcancel &lt;номер&gt;")
        return "\n".join(lines)

    async def revenue(self, days: int = 30) -> tuple[int, float]:
        rows = await self.ctx.db.all(
            "SELECT meta FROM posts WHERE kind='ad' AND status IN ('published', 'deleted') AND published_at>?",
            (now() - days * 86400,))
        return len(rows), sum(float(meta_of(r).get("price") or 0) for r in rows)

