"""События канала: подписки и отписки, реакции, ручные посты, изменение прав бота."""
from __future__ import annotations

import asyncio
import logging
import re

from aiogram import Router
from aiogram.types import ChatMemberUpdated, Message, MessageReactionCountUpdated

from ..context import Ctx
from ..textutil import clip, esc

log = logging.getLogger(__name__)
router = Router(name="channel")


@router.chat_member()
async def on_member(update: ChatMemberUpdated, ctx: Ctx) -> None:
    if update.chat.id == ctx.channel_id:
        await ctx.analytics.on_chat_member(update)


@router.message_reaction_count()
async def on_reactions(update: MessageReactionCountUpdated, ctx: Ctx) -> None:
    if update.chat.id == ctx.channel_id:
        await ctx.analytics.on_reactions(update)


@router.my_chat_member()
async def on_bot_rights(update: ChatMemberUpdated, ctx: Ctx) -> None:
    """Если бота разжаловали или удалили из канала либо чата — сразу сообщаем владельцу."""
    if update.chat.id not in (ctx.channel_id, ctx.discussion_id):
        return
    where = "канале" if update.chat.id == ctx.channel_id else "чате комментариев"
    status = update.new_chat_member.status
    if status in ("left", "kicked"):
        await ctx.notifier.send(f"🛑 Бота удалили из {where} «{esc(update.chat.title or '')}». Автоматика там остановлена.")
    elif status != "administrator" and update.old_chat_member.status == "administrator":
        await ctx.notifier.send(f"⚠️ У бота забрали права администратора в {where}. Верните их, иначе он не сможет работать.")


@router.channel_post()
async def on_channel_post(message: Message, ctx: Ctx) -> None:
    """Пост, опубликованный в канале вручную, учитываем в интервалах и дайджесте."""
    if message.chat.id != ctx.channel_id:
        return
    await asyncio.sleep(4)  # свои посты бот записывает сам сразу после отправки
    if await ctx.db.one("SELECT 1 FROM posts WHERE channel_msg_id=?", (message.message_id,)):
        return
    text = message.html_text if (message.text or message.caption) else ""
    plain = re.sub(r"<[^>]+>", "", text)
    await ctx.db.create_post(
        kind="manual", status="published", headline=clip(plain.split("\n", 1)[0], 120), text=text,
        category="other", importance=6, confidence=1.0, published_at=int(message.date.timestamp()),
        channel_msg_id=message.message_id, meta={"external": True})
