"""Карточки согласования в админ-чате: предпросмотр поста + служебное сообщение с кнопками."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions, ReplyParameters

from .db import meta_of
from .prompts import CATEGORY_LABELS
from .textutil import clip, esc, esc_attr, visible_len

if TYPE_CHECKING:
    from .context import Ctx

log = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
KIND_NAMES = {
    "news": "Новость", "breaking": "⚡️ Молния", "manual": "Пост редакции", "suggestion": "Из предложки",
    "poll": "Опрос", "ad": "Реклама", "digest": "Дайджест", "countdown": "Обратный отсчёт",
}
EDITABLE_KINDS = ("news", "breaking", "manual", "suggestion")


def keyboard(post: Any) -> InlineKeyboardMarkup:
    pid = post["id"]
    if post["kind"] == "ad":  # реклама выходит по расписанию, из действий только отмена
        return InlineKeyboardMarkup(inline_keyboard=[[Btn(text="❌ Отменить размещение", callback_data=f"p:reject:{pid}")]])
    rows = [[Btn(text="✅ Опубликовать", callback_data=f"p:pub:{pid}"),
             Btn(text="🕐 В очередь", callback_data=f"p:queue:{pid}")]]
    if post["kind"] in EDITABLE_KINDS:
        rows.append([Btn(text="✏️ Править", callback_data=f"p:edit:{pid}"),
                     Btn(text="✂️ Короче", callback_data=f"p:rw-shorter:{pid}"),
                     Btn(text="🔥 Живее", callback_data=f"p:rw-brighter:{pid}"),
                     Btn(text="🧊 Строже", callback_data=f"p:rw-stricter:{pid}")])
    elif post["kind"] == "poll":
        rows.append([Btn(text="♻️ Другой вариант", callback_data=f"p:repoll:{pid}")])
    last = []
    if post["image_url"] and post["kind"] in EDITABLE_KINDS:
        last.append(Btn(text="🖼 Убрать фото", callback_data=f"p:noimg:{pid}"))
    last.append(Btn(text="❌ Отклонить", callback_data=f"p:reject:{pid}"))
    rows.append(last)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_text(post: Any, status: str | None = None) -> str:
    meta = meta_of(post)
    lines = [f"📝 <b>{KIND_NAMES.get(post['kind'], 'Пост')} #{post['id']}</b>"]
    if post["kind"] in EDITABLE_KINDS:
        label = CATEGORY_LABELS.get(post["category"], post["category"])
        lines.append(f"{label} · важность {post['importance']}/10 · уверенность {post['confidence']:.0%}")
        if post["source_name"]:
            name = esc(post["source_name"])
            src = f'<a href="{esc_attr(post["source_url"])}">{name}</a>' if post["source_url"] else name
            tier = meta.get("tier")
            lines.append(f"Источник: {src}" + (f" (уровень {tier})" if tier else ""))
        if meta.get("review_reason"):
            lines.append(f"🚩 <b>Проверить:</b> {esc(clip(meta['review_reason'], 400))}")
        elif meta.get("thin"):
            lines.append("🚩 <b>Проверить:</b> в источнике мало текста, сверьте с первоисточником")
        if meta.get("spoiler"):
            lines.append("🙈 В посте есть скрытый спойлер")
        if meta.get("edited"):
            lines.append("✏️ Текст изменён вручную")
        shape = "фото с подписью" if post["image_url"] and visible_len(post["text"]) <= 1024 else "текст с превью"
        lines.append(f"Длина: {visible_len(post['text'])} зн. · {shape}")
    elif post["kind"] == "ad":
        when = meta.get("when_text") or "по расписанию"
        feed = int(meta.get("feed_hours", 0))
        lines.append(f"Выход: {esc(when)} · топ {meta.get('top_minutes', 0)} мин · "
                     + (f"удаление через {feed} ч" if feed else "без удаления"))
        if meta.get("erid"):
            lines.append(f"erid: <code>{esc(meta['erid'])}</code>")
        if meta.get("price"):
            lines.append(f"Цена: {meta['price']:g} ₽")
    if status:
        lines += ["", status]
    return "\n".join(lines)


def _card_ids(post: Any) -> list[int]:
    try:
        ids = json.loads(post["review_msg_ids"] or "[]")
        return [int(i) for i in ids] if isinstance(ids, list) else []
    except (TypeError, ValueError):
        return []


async def send_card(ctx: "Ctx", post_id: int, *, with_buttons: bool = True) -> None:
    """Показать пост в админ-чате так, как он выйдет в канале, и приложить карточку с кнопками.

    Статус поста карточка не меняет: его выставляет вызывающий код ещё при создании поста,
    иначе публикатор мог бы успеть отправить пост между созданием и показом карточки."""
    post = await ctx.db.post(post_id)
    if post is None:
        return
    preview_id = 0
    note = None
    try:
        preview = await ctx.publisher.send_post(ctx.admin_chat, post, silent=True)
        preview_id = preview.message_id
    except TelegramAPIError as e:
        log.warning("Предпросмотр поста #%s не отправился: %s", post_id, e)
        note = f"⚠️ Предпросмотр не отправился: <code>{esc(clip(str(e), 200))}</code>"
    try:
        card = await ctx.bot.send_message(
            ctx.admin_chat, card_text(post, note),
            reply_markup=keyboard(post) if with_buttons else None, link_preview_options=NO_PREVIEW,
            reply_parameters=ReplyParameters(message_id=preview_id, allow_sending_without_reply=True) if preview_id else None)
    except TelegramAPIError as e:
        log.error("Карточка поста #%s не отправилась в админ-чат: %s", post_id, e)
        await ctx.db.update_post(post_id, review_msg_ids=[preview_id, 0])
        return
    await ctx.db.update_post(post_id, review_msg_ids=[preview_id, card.message_id])


async def close_card(ctx: "Ctx", post: Any, status: str) -> None:
    """Снять кнопки и дописать итог в карточку. Если карточки не было, ничего не делает."""
    ids = _card_ids(post)
    if len(ids) < 2 or not ids[1]:
        return
    try:
        await ctx.bot.edit_message_text(card_text(post, status), chat_id=ctx.admin_chat, message_id=ids[1],
                                        reply_markup=None, link_preview_options=NO_PREVIEW)
    except TelegramAPIError as e:
        log.debug("Карточку поста #%s обновить не удалось: %s", post["id"], e)


async def drop_card(ctx: "Ctx", post: Any) -> None:
    """Удалить карточку и предпросмотр (перед показом новой версии поста)."""
    ids = [i for i in _card_ids(post) if i]
    if not ids:
        return
    try:
        await ctx.bot.delete_messages(ctx.admin_chat, ids)
    except TelegramAPIError as e:
        log.debug("Старую карточку поста #%s удалить не удалось: %s", post["id"], e)
    await ctx.db.update_post(post["id"], review_msg_ids=[])
