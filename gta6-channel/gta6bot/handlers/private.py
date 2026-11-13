"""Личка бота для подписчиков: предложить новость, рекламная заявка, вопросы, связь с редакцией."""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message,
                           ReplyParameters)

from ..context import Ctx
from ..db import now
from ..moderation.service import display_name, user_link
from ..qa import answer_question
from ..textutil import esc

log = logging.getLogger(__name__)
router = Router(name="private")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

RATE_LIMIT = 6          # сообщений редакции…
RATE_WINDOW = 600       # …за 10 минут от одного человека
_recent: dict[int, deque[float]] = defaultdict(deque)


class Form(StatesGroup):
    suggest = State()
    ad = State()


def _allowed(user_id: int) -> bool:
    t = time.monotonic()
    q = _recent[user_id]
    while q and t - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return False
    q.append(t)
    if len(_recent) > 50000:
        _recent.clear()
    return True


def _menu(ctx: Ctx) -> InlineKeyboardMarkup:
    c = ctx.cfg.community
    rows = []
    if c.suggestions:
        rows.append([InlineKeyboardButton(text="📰 Предложить новость", callback_data="u:suggest")])
    if c.ad_inquiries:
        rows.append([InlineKeyboardButton(text="📣 Реклама в канале", callback_data="u:ad")])
    rows.append([InlineKeyboardButton(text="⏳ Когда релиз?", callback_data="u:release")])
    if ctx.channel_link:
        rows.append([InlineKeyboardButton(text="➡️ Перейти в канал", url=ctx.channel_link)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _relay(ctx: Ctx, message: Message, title: str, kind: str, ref_id: int = 0,
                 markup: InlineKeyboardMarkup | None = None) -> bool:
    """Переслать сообщение пользователя в админ-чат; ответ админа на него вернётся автору."""
    user = message.from_user
    assert user is not None
    username = f" · @{user.username}" if user.username else ""
    header = await ctx.notifier.send(
        f"{title}\nОт: {user_link(user.id, display_name(user))}{username} · <code>{user.id}</code>", reply_markup=markup)
    if header is None:
        return False
    ids = [header.message_id]
    try:
        copy = await ctx.bot.copy_message(
            ctx.admin_chat, message.chat.id, message.message_id,
            reply_parameters=ReplyParameters(message_id=header.message_id, allow_sending_without_reply=True))
        ids.append(copy.message_id)
    except TelegramAPIError as e:
        log.warning("Сообщение пользователя не скопировано в админ-чат: %s", e)
    for msg_id in ids:
        await ctx.db.execute(
            "INSERT OR REPLACE INTO relay (admin_msg_id, user_id, kind, ref_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (msg_id, user.id, kind, ref_id, now()))
    return True


@router.message(CommandStart())
async def on_start(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    await message.answer(
        f"Привет! Я бот канала <b>{esc(ctx.cfg.channel.name)}</b>.\n\n"
        "Через меня можно прислать новость раньше всех, задать вопрос об игре (/ask) или обсудить рекламу. "
        "Любое сообщение сюда прочитает редакция.", reply_markup=_menu(ctx), link_preview_options=NO_PREVIEW)


@router.message(Command("cancel"))
async def on_cancel(message: Message, state: FSMContext, ctx: Ctx) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=_menu(ctx))


@router.callback_query(F.data == "u:suggest")
async def on_suggest(callback: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await callback.answer()
    if not ctx.cfg.community.suggestions or callback.message is None:
        return
    await state.set_state(Form.suggest)
    await callback.message.answer(
        "Пришлите новость одним сообщением: текст, ссылку, скриншот или видео. Если знаете источник, укажите его.\n\n"
        "Передумали — /cancel")


@router.callback_query(F.data == "u:ad")
async def on_ad(callback: CallbackQuery, state: FSMContext, ctx: Ctx) -> None:
    await callback.answer()
    if not ctx.cfg.community.ad_inquiries or callback.message is None:
        return
    subs = await ctx.db.scalar("SELECT subscribers FROM subs_daily ORDER BY day DESC LIMIT 1")
    audience = f"Сейчас в канале {subs:,} подписчиков. ".replace(",", " ") if subs else ""
    await state.set_state(Form.ad)
    await callback.message.answer(
        f"📣 <b>Реклама в канале</b>\n\n{audience}Напишите одним сообщением: что рекламируете, желаемые даты и формат "
        "(пост, закреп, интеграция), ссылку на продукт. Редакция ответит здесь же.\n\n"
        "Размещаем только промаркированную рекламу. Не берём: казино и ставки, финансовые пирамиды, "
        "VPN-сервисы, читы и взломанные аккаунты.\n\nПередумали — /cancel")


@router.callback_query(F.data == "u:release")
async def on_release(callback: CallbackQuery, ctx: Ctx) -> None:
    await callback.answer()
    if callback.message is None:
        return
    text = ctx.rubrics.countdown_text()
    await callback.message.answer(text or f"GTA VI уже вышла: {ctx.phase_text()}. Все новости — в канале.",
                                  link_preview_options=NO_PREVIEW)


@router.message(Form.suggest)
async def on_suggestion(message: Message, state: FSMContext, ctx: Ctx) -> None:
    user = message.from_user
    if user is None:
        return
    await state.clear()
    if not _allowed(user.id):
        await message.answer("Слишком много сообщений подряд. Попробуйте чуть позже.")
        return
    text = (message.text or message.caption or "").strip()
    sid = await ctx.db.insert(
        "INSERT INTO suggestions (ts, user_id, user_name, text) VALUES (?, ?, ?, ?)",
        (now(), user.id, display_name(user)[:100], text[:6000]))
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ В работу (ИИ-редактор)", callback_data=f"s:take:{sid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"s:no:{sid}")]])
    delivered = await _relay(ctx, message, f"📰 <b>Предложена новость #{sid}</b>", "suggestion", sid, markup)
    await message.answer("Спасибо! Редакция посмотрит и, если новость подойдёт, опубликует её. 🙌" if delivered
                         else "Не получилось передать сообщение, попробуйте позже.", reply_markup=_menu(ctx))


@router.message(Form.ad)
async def on_ad_request(message: Message, state: FSMContext, ctx: Ctx) -> None:
    user = message.from_user
    if user is None:
        return
    await state.clear()
    if not _allowed(user.id):
        await message.answer("Слишком много сообщений подряд. Попробуйте чуть позже.")
        return
    delivered = await _relay(ctx, message, "📣 <b>Рекламная заявка</b>", "ad")
    await message.answer("Заявка у редакции. Ответим здесь, в этом чате." if delivered
                         else "Не получилось передать заявку, попробуйте позже.")


@router.message(Command("ask"))
async def on_ask(message: Message, command: CommandObject, ctx: Ctx) -> None:
    if not ctx.cfg.community.qa or message.from_user is None:
        return
    question = (command.args or "").strip()
    if len(question) < 5:
        await message.answer("Напишите вопрос после команды, например:\n<code>/ask будет ли русский язык?</code>")
        return
    answer = await answer_question(ctx, message.from_user.id, question)
    await message.answer(esc(answer) if answer else "Сейчас ответить не могу, попробуйте позже.",
                         link_preview_options=NO_PREVIEW)


@router.message()
async def on_any(message: Message, ctx: Ctx) -> None:
    """Сообщение без команды — письмо в редакцию."""
    user = message.from_user
    if user is None or user.is_bot:
        return
    if not _allowed(user.id):
        await message.answer("Слишком много сообщений подряд. Попробуйте чуть позже.")
        return
    if await _relay(ctx, message, "✉️ <b>Сообщение в редакцию</b>", "dm"):
        await message.answer("Передал редакции. Если понадобится ответ, он придёт сюда.", reply_markup=_menu(ctx))
