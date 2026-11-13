"""Чат комментариев: первый комментарий, модерация, жалобы, команды модераторов, вопросы к ИИ."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message, ReplyParameters

from ..context import Ctx
from ..moderation.service import display_name, user_link
from ..qa import answer_question
from ..textutil import clip, esc

log = logging.getLogger(__name__)
router = Router(name="group")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
MOD_COMMANDS = ("warn", "mute", "ban", "unban", "unmute", "trust", "untrust", "del")


class InDiscussion(Filter):
    async def __call__(self, message: Message, ctx: Ctx) -> bool:
        return ctx.discussion_id is not None and message.chat.id == ctx.discussion_id


router.message.filter(InDiscussion())
router.edited_message.filter(InDiscussion())


async def _is_moderator(message: Message, ctx: Ctx) -> bool:
    if message.sender_chat is not None:   # анонимный админ группы или сам канал
        return message.sender_chat.id in (message.chat.id, ctx.channel_id)
    user = message.from_user
    return user is not None and (ctx.is_admin(user.id) or await ctx.moderator.is_chat_admin(message.chat.id, user.id))


async def _temp_reply(message: Message, ctx: Ctx, text: str, ttl: int | None = None) -> None:
    """Ответить в ветке и убрать ответ через ttl секунд."""
    thread = message.message_thread_id or message.message_id
    try:
        sent = await ctx.bot.send_message(
            message.chat.id, text, disable_notification=True, link_preview_options=NO_PREVIEW,
            reply_parameters=ReplyParameters(message_id=thread, allow_sending_without_reply=True))
    except TelegramAPIError as e:
        log.debug("Ответ в чате не отправлен: %s", e)
        return
    await ctx.db.schedule_delete(message.chat.id, sent.message_id, ttl or ctx.cfg.moderation.notice_ttl_sec)


async def _drop(message: Message, ctx: Ctx) -> None:
    try:
        await ctx.bot.delete_message(message.chat.id, message.message_id)
    except TelegramAPIError:
        pass


# ── пост канала пришёл в группу ──────────────────────────────────────────────

@router.message(F.is_automatic_forward)
async def on_channel_post(message: Message, ctx: Ctx) -> None:
    """Запоминаем ветку обсуждения поста и оставляем первый комментарий."""
    origin = message.forward_origin
    channel_msg_id = getattr(origin, "message_id", None)
    kind = None
    if channel_msg_id:
        post = await ctx.db.one("SELECT id, kind FROM posts WHERE channel_msg_id=?", (channel_msg_id,))
        if post:
            kind = post["kind"]
            await ctx.db.update_post(post["id"], discussion_msg_id=message.message_id)
    cfg = ctx.cfg.moderation
    if not cfg.first_comment or kind == "ad":
        return
    text = cfg.first_comment_text.strip().replace("{bot_link}", ctx.bot_link).replace("{channel}", ctx.cfg.channel.name)
    if not text:
        return
    try:
        await ctx.bot.send_message(
            message.chat.id, text, disable_notification=True, link_preview_options=NO_PREVIEW,
            reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True))
    except TelegramAPIError as e:
        log.warning("Первый комментарий не отправлен: %s", e)


# ── служебные сообщения ──────────────────────────────────────────────────────

@router.message(F.new_chat_members | F.left_chat_member)
async def on_service_message(message: Message, ctx: Ctx) -> None:
    if ctx.cfg.moderation.delete_service_messages:
        await _drop(message, ctx)


# ── команды модераторов ──────────────────────────────────────────────────────

@router.message(Command(*MOD_COMMANDS))
async def on_mod_command(message: Message, command: CommandObject, ctx: Ctx) -> None:
    if not await _is_moderator(message, ctx):
        # чужая попытка командовать: обычное сообщение, пусть его оценит модерация
        await ctx.moderator.handle(message)
        return
    target = message.reply_to_message
    args = (command.args or "").split()
    user_id, name = None, ""
    if target is not None and not target.is_automatic_forward and target.from_user is not None and target.sender_chat is None:
        user_id, name = target.from_user.id, display_name(target.from_user)
    elif args and args[0].lstrip("-").isdigit():
        user_id, args = int(args[0]), args[1:]
    action = command.command
    await _drop(message, ctx)
    if action == "del":
        if target is not None and not target.is_automatic_forward:
            await _drop(target, ctx)
        return
    if user_id is None:
        await _temp_reply(message, ctx, f"Ответьте командой /{action} на сообщение участника или укажите его ID.")
        return
    if ctx.is_admin(user_id) or await ctx.moderator.is_chat_admin(message.chat.id, user_id):
        await _temp_reply(message, ctx, "Администраторов наказывать нельзя.")
        return
    minutes = 60
    if action == "mute" and args and args[0].isdigit():
        minutes, args = max(1, int(args[0])), args[1:]
    by = display_name(message.from_user) or "админ"
    try:
        result = await ctx.moderator.manual(action, message.chat.id, user_id, name, minutes=minutes, by=by,
                                            reason=" ".join(args))
    except TelegramAPIError as e:
        await _temp_reply(message, ctx, f"Не получилось: {esc(str(e))}")
        return
    if action in ("warn", "mute", "ban") and target is not None:
        await _drop(target, ctx)
    await _temp_reply(message, ctx, result)


# ── жалобы ───────────────────────────────────────────────────────────────────

@router.message(Command("report"))
@router.message(F.reply_to_message, F.text.lower().in_({"@admin", "@админ", "@admins"}))
async def on_report(message: Message, ctx: Ctx) -> None:
    target = message.reply_to_message
    reporter = message.from_user
    await _drop(message, ctx)
    if target is None or target.is_automatic_forward or reporter is None:
        await _temp_reply(message, ctx, "Чтобы пожаловаться, ответьте командой /report на сообщение-нарушение.")
        return
    # не чаще одной жалобы в минуту от человека
    key = f"report:{reporter.id}"
    last = int(await ctx.db.kv_get(key, "0") or 0)
    now_ts = int(message.date.timestamp())
    if now_ts - last < 60:
        return
    await ctx.db.kv_set(key, now_ts)
    author = target.from_user
    author_id = target.sender_chat.id if target.sender_chat else (author.id if author else 0)
    author_name = (target.sender_chat.title or "канал") if target.sender_chat else display_name(author)
    body = target.text or target.caption or "(медиа без текста)"
    internal = str(message.chat.id).removeprefix("-100")
    link = f"https://t.me/c/{internal}/{target.message_id}"
    if target.message_thread_id:
        link += f"?thread={target.message_thread_id}"
    data = f"{message.chat.id}:{target.message_id}:{author_id}"
    await ctx.notifier.send(
        f"🚩 <b>Жалоба</b> от {user_link(reporter.id, display_name(reporter))}\n"
        f"На: {user_link(author_id, author_name) if author_id > 0 else esc(author_name)} (<code>{author_id}</code>)\n"
        f"<a href=\"{link}\">Сообщение</a>: «{esc(clip(body, 500))}»",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"m:del:{data}"),
            InlineKeyboardButton(text="🚫 Удалить и забанить", callback_data=f"m:ban:{data}"),
            InlineKeyboardButton(text="✅ Оставить", callback_data=f"m:ok:{data}")]]))
    await _temp_reply(message, ctx, "Спасибо, жалоба передана модераторам.", ttl=10)


# ── правила и вопросы ────────────────────────────────────────────────────────

@router.message(Command("rules"))
async def on_rules(message: Message, ctx: Ctx) -> None:
    await _drop(message, ctx)
    await _temp_reply(message, ctx, ctx.cfg.moderation.rules_text.strip(), ttl=90)


@router.message(Command("ask"))
async def on_ask(message: Message, command: CommandObject, ctx: Ctx) -> None:
    if await ctx.moderator.handle(message):
        return
    if not ctx.cfg.community.qa or message.from_user is None:
        return
    question = (command.args or "").strip()
    if not question and message.reply_to_message and not message.reply_to_message.is_automatic_forward:
        question = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
    if len(question) < 5:
        await _temp_reply(message, ctx, "Напишите вопрос после команды: /ask когда выйдет версия для ПК?")
        return
    answer = await answer_question(ctx, message.from_user.id, question)
    if answer is None:
        return
    try:
        await message.reply(esc(answer), link_preview_options=NO_PREVIEW, disable_notification=True)
    except TelegramAPIError as e:
        log.debug("Ответ на вопрос не отправлен: %s", e)


# ── всё остальное идёт через модерацию ───────────────────────────────────────

@router.message()
async def on_comment(message: Message, ctx: Ctx) -> None:
    await ctx.moderator.handle(message)


@router.edited_message()
async def on_comment_edit(message: Message, ctx: Ctx) -> None:
    # спамеры часто сначала пишут безобидный текст, а рекламу вставляют правкой
    await ctx.moderator.handle(message, is_edit=True)
