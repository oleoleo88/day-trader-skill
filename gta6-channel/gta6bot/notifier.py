"""Сообщения в админ-чат: уведомления, предупреждения с защитой от повторов."""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions, Message

from .textutil import clip, esc

if TYPE_CHECKING:
    from .context import Ctx

log = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class Notifier:
    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx
        self._last: dict[str, float] = {}

    async def send(self, text: str, **kwargs: Any) -> Message | None:
        """Отправить сообщение в админ-чат. Сбой отправки не должен ронять вызывающий код."""
        kwargs.setdefault("link_preview_options", NO_PREVIEW)
        if self.ctx.dry_run or self.ctx.bot is None:
            log.info("[админ-чат] %s", text)
            return None
        try:
            return await self.ctx.bot.send_message(self.ctx.admin_chat, clip_html(text), **kwargs)
        except TelegramAPIError as e:
            log.error("Не удалось написать в админ-чат: %s | текст: %s", e, text[:300])
            return None

    async def once(self, key: str, text: str, every: int = 1800) -> None:
        """Не чаще раза в `every` секунд на один ключ — чтобы авария не превращалась во флуд."""
        t = time.monotonic()
        if t - self._last.get(key, -1e9) < every:
            return
        self._last[key] = t
        await self.send(text)

    async def error(self, where: str, exc: BaseException) -> None:
        log.exception("Ошибка в %s", where, exc_info=exc)
        await self.once(f"err:{where}:{type(exc).__name__}",
                        f"⚠️ <b>Сбой: {esc(where)}</b>\n<code>{esc(clip(f'{type(exc).__name__}: {exc}', 700))}</code>")


def clip_html(text: str, limit: int = 4000) -> str:
    """Грубая страховка от слишком длинных служебных сообщений (разметку не режем посередине тега)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    lt, gt = cut.rfind("<"), cut.rfind(">")
    if lt > gt:
        cut = cut[:lt]
    # закрываем то, что могло остаться открытым
    for tag in ("code", "pre", "b", "i", "a", "blockquote"):
        if cut.count(f"<{tag}") > cut.count(f"</{tag}>"):
            cut += f"</{tag}>"
    return cut + "…"
