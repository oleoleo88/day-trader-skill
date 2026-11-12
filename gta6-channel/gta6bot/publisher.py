"""Публикация: отправка постов в канал, очередь, интервалы, тихие часы, реклама, автоудаление."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import (BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputPollOption,
                           LinkPreviewOptions, Message)

from . import review
from .db import meta_of, now
from .textutil import clip, domain_of, esc, visible_len, visible_text

if TYPE_CHECKING:
    from .context import Ctx

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
REGULAR_KINDS = ("news", "manual", "suggestion")
SCHEDULED_KINDS = ("digest", "countdown", "poll")
_NO_PREVIEW_DOMAINS = ("news.google.com",)
MAX_SEND_ATTEMPTS = 5


def in_quiet_hours(hour: int, quiet: list[int]) -> bool:
    if len(quiet) != 2 or quiet[0] == quiet[1]:
        return False
    start, end = quiet
    return start <= hour < end if start < end else (hour >= start or hour < end)


class Publisher:
    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx
        self._lock = asyncio.Lock()

    # ── отправка ────────────────────────────────────────────────────────────
    async def send_post(self, chat_id: int, post: Any, *, silent: bool = False,
                        reply_markup: InlineKeyboardMarkup | None = None) -> Message:
        """Отправить пост в чат в том виде, в каком он выйдет в канале (канал и предпросмотр идут одним путём)."""
        meta = meta_of(post)
        bot = self.ctx.bot
        if post["kind"] == "poll" and meta.get("poll"):
            p = meta["poll"]
            quiz = p.get("kind") == "quiz"
            return await bot.send_poll(
                chat_id, question=p["question"], options=[InputPollOption(text=o) for o in p["options"]],
                is_anonymous=True, type="quiz" if quiz else "regular",
                correct_option_ids=[p["correct_index"]] if quiz else None,
                explanation=(p.get("explanation") or None) if quiz else None,
                disable_notification=silent, reply_markup=reply_markup)
        creative = meta.get("creative")
        if creative:
            markup = reply_markup or self._button(meta.get("button"))
            if creative.get("media"):
                sent = await bot.copy_message(
                    chat_id, from_chat_id=creative["chat_id"], message_id=creative["message_id"],
                    caption=post["text"], parse_mode="HTML", disable_notification=silent, reply_markup=markup)
                return Message.model_construct(message_id=sent.message_id)
            return await bot.send_message(chat_id, post["text"], disable_notification=silent, reply_markup=markup)
        return await self._send_content(chat_id, post["text"], post["image_url"], post["source_url"], silent, reply_markup)

    @staticmethod
    def _button(spec: Any) -> InlineKeyboardMarkup | None:
        if isinstance(spec, dict) and spec.get("text") and spec.get("url"):
            return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=spec["text"], url=spec["url"])]])
        return None

    async def _send_content(self, chat_id: int, text: str, image: str | None, source_url: str | None,
                            silent: bool, markup: InlineKeyboardMarkup | None) -> Message:
        bot = self.ctx.bot
        if image and visible_len(text) <= CAPTION_LIMIT:
            try:
                return await bot.send_photo(chat_id, image, caption=text, disable_notification=silent, reply_markup=markup)
            except TelegramBadRequest as e:
                # Telegram не смог сам скачать картинку по ссылке — пробуем загрузить её файлом
                log.info("Фото по ссылке не принято (%s), пробую загрузить файлом", e.message)
                data = await self._download_image(image)
                if data:
                    try:
                        return await bot.send_photo(chat_id, BufferedInputFile(data, "image.jpg"), caption=text,
                                                    disable_notification=silent, reply_markup=markup)
                    except TelegramBadRequest as e2:
                        log.info("Фото файлом тоже не принято (%s), публикую текстом", e2.message)
        preview_url = image
        if not preview_url and source_url and domain_of(source_url) not in _NO_PREVIEW_DOMAINS:
            preview_url = source_url
        preview = (LinkPreviewOptions(url=preview_url, prefer_large_media=True, show_above_text=True)
                   if preview_url else LinkPreviewOptions(is_disabled=True))
        try:
            return await bot.send_message(chat_id, text, link_preview_options=preview,
                                          disable_notification=silent, reply_markup=markup)
        except TelegramBadRequest as e:
            if "parse entities" not in e.message.lower():
                raise
            log.warning("Telegram не разобрал разметку поста, отправляю без неё: %s", e.message)
            return await bot.send_message(chat_id, visible_text(text), parse_mode=None, link_preview_options=preview,
                                          disable_notification=silent, reply_markup=markup)

    async def _download_image(self, url: str) -> bytes | None:
        if self.ctx.http is None or not url.startswith(("http://", "https://")):
            return None
        try:
            async with self.ctx.http.get(url, allow_redirects=True) as resp:
                if resp.status != 200 or not resp.headers.get("Content-Type", "").startswith("image/"):
                    return None
                data = await resp.content.read(10_000_001)
                return data if 0 < len(data) <= 10_000_000 else None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    # ── правила очереди ─────────────────────────────────────────────────────
    async def paused(self) -> bool:
        return await self.ctx.db.kv_get("paused", "0") == "1"

    def quiet_now(self) -> bool:
        return in_quiet_hours(self.ctx.now_local().hour, self.ctx.cfg.publishing.quiet_hours)

    async def ad_top_until(self) -> int:
        """До какого момента последний рекламный пост должен оставаться верхним."""
        row = await self.ctx.db.one(
            "SELECT published_at, meta FROM posts WHERE kind='ad' AND status='published' ORDER BY published_at DESC LIMIT 1")
        if row is None or not row["published_at"]:
            return 0
        return row["published_at"] + int(meta_of(row).get("top_minutes", 0)) * 60

    async def can_publish_regular(self) -> tuple[bool, str]:
        db, pub = self.ctx.db, self.ctx.cfg.publishing
        if self.quiet_now():
            return False, f"тихие часы до {pub.quiet_hours[1]:02d}:00"
        last = await db.scalar("SELECT MAX(published_at) FROM posts WHERE status='published' AND kind!='ad'", default=0)
        wait = pub.min_gap_minutes * 60 - (now() - last)
        if wait > 0:
            return False, f"пауза между постами, ещё {wait // 60 + 1} мин"
        marks = ", ".join("?" for _ in REGULAR_KINDS)
        today = await db.scalar(
            f"SELECT COUNT(*) FROM posts WHERE status='published' AND kind IN ({marks}) AND published_at>=?",
            (*REGULAR_KINDS, self.ctx.day_start_ts()), default=0)
        if today >= pub.max_posts_per_day:
            return False, f"дневной лимит {pub.max_posts_per_day} постов исчерпан"
        return True, ""

    # ── цикл ────────────────────────────────────────────────────────────────
    async def tick(self) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            await self._expire()
            await self._delete_due()
            db = self.ctx.db
            # 1. реклама выходит точно в своё время
            for ad in await db.all(
                    "SELECT id FROM posts WHERE kind='ad' AND status='queued' AND not_before<=? ORDER BY not_before", (now(),)):
                await self.publish(ad["id"])
            if await self.paused():
                return
            in_top = now() < await self.ad_top_until()
            # 2. молнии
            row = await db.one(
                "SELECT id FROM posts WHERE kind='breaking' AND status='queued' AND not_before<=? "
                "ORDER BY importance DESC, id ASC LIMIT 1", (now(),))
            if row and (not in_top or self.ctx.cfg.ads.breaking_overrides_top):
                await self.publish(row["id"])
                return
            if in_top:
                return
            # 3. рубрики по расписанию
            marks = ", ".join("?" for _ in SCHEDULED_KINDS)
            row = await db.one(
                f"SELECT id FROM posts WHERE kind IN ({marks}) AND status='queued' AND not_before<=? ORDER BY not_before LIMIT 1",
                (*SCHEDULED_KINDS, now()))
            if row:
                await self.publish(row["id"])
                return
            # 4. обычные посты: самое важное первым
            ok, _ = await self.can_publish_regular()
            if ok:
                marks = ", ".join("?" for _ in REGULAR_KINDS)
                row = await db.one(
                    f"SELECT id FROM posts WHERE kind IN ({marks}) AND status='queued' AND not_before<=? "
                    f"ORDER BY importance DESC, created_at ASC LIMIT 1", (*REGULAR_KINDS, now()))
                if row:
                    await self.publish(row["id"])

    async def _expire(self) -> None:
        db, pub = self.ctx.db, self.ctx.cfg.publishing
        await db.execute(
            "UPDATE posts SET status='expired' WHERE status='queued' AND kind IN ('news', 'breaking') AND created_at<?",
            (now() - pub.stale_after_hours * 3600,))
        old = await db.all(
            "SELECT * FROM posts WHERE status='review' AND kind!='ad' AND created_at<?", (now() - pub.review_ttl_hours * 3600,))
        for post in old:
            await db.update_post(post["id"], status="expired")
            await review.close_card(self.ctx, post, "⌛ Срок согласования вышел, черновик закрыт")

    async def _delete_due(self) -> None:
        db = self.ctx.db
        due = await db.all(
            "SELECT * FROM posts WHERE status='published' AND delete_at IS NOT NULL AND delete_at<=?", (now(),))
        for post in due:
            ok, error = await self.delete_from_channel(post["id"])
            if not ok:
                await db.update_post(post["id"], delete_at=None)
                await self.ctx.notifier.send(
                    f"⚠️ Не удалось автоматически удалить пост #{post['id']} из канала: {esc(error)}\n"
                    f"Удалите вручную: {self.ctx.post_link(post['channel_msg_id'])}")

    # ── публикация и удаление ───────────────────────────────────────────────
    async def publish(self, post_id: int, *, announce: bool = True) -> tuple[bool, str]:
        """Опубликовать пост в канал. Возвращает (успех, ссылка или текст ошибки)."""
        ctx, db = self.ctx, self.ctx.db
        post = await db.post(post_id)
        if post is None:
            return False, "пост не найден"
        previous = post["status"]
        if not await db.claim_post(post_id, ("queued", "review")):
            return False, "пост уже опубликован или снят"
        silent = ctx.cfg.publishing.silent_at_night and self.quiet_now()
        try:
            msg = await self.send_post(ctx.channel_id, post, silent=silent)
        except TelegramRetryAfter as e:
            await db.update_post(post_id, status=previous, not_before=now() + int(e.retry_after) + 1)
            return False, f"Telegram просит подождать {e.retry_after} с, повторю сам"
        except (TelegramNetworkError, asyncio.TimeoutError) as e:
            attempts = post["attempts"] + 1
            if attempts >= MAX_SEND_ATTEMPTS:
                await db.update_post(post_id, status="failed", attempts=attempts)
                await ctx.notifier.send(f"🛑 Пост #{post_id} не отправлен после {attempts} попыток: нет связи с Telegram.")
            else:
                await db.update_post(post_id, status=previous, attempts=attempts, not_before=now() + 30 * attempts)
            return False, f"нет связи с Telegram ({type(e).__name__})"
        except TelegramAPIError as e:
            await db.update_post(post_id, status="failed")
            await ctx.notifier.send(
                f"🛑 <b>Пост #{post_id} не опубликован</b>: <code>{esc(clip(str(e), 300))}</code>\n"
                f"Частые причины: бот не админ канала или у него нет права публиковать сообщения.")
            return False, str(e)
        meta = meta_of(post)
        delete_at = None
        if post["kind"] == "ad" and int(meta.get("feed_hours", 0)) > 0:
            delete_at = now() + int(meta["feed_hours"]) * 3600
        await db.update_post(post_id, status="published", published_at=now(), channel_msg_id=msg.message_id,
                             delete_at=delete_at)
        link = ctx.post_link(msg.message_id)
        await review.close_card(ctx, post, f"✅ Опубликовано: {link}")
        if announce:
            title = post["headline"] or {"ad": "реклама", "poll": "опрос", "digest": "дайджест",
                                         "countdown": "обратный отсчёт"}.get(post["kind"], "пост")
            await ctx.notifier.send(
                f"📤 Вышел пост #{post_id}: <a href=\"{link}\">{esc(clip(title, 90))}</a>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🗑 Удалить из канала", callback_data=f"p:del:{post_id}")]]))
        return True, link

    async def delete_from_channel(self, post_id: int) -> tuple[bool, str]:
        post = await self.ctx.db.post(post_id)
        if post is None or not post["channel_msg_id"]:
            return False, "пост не найден в канале"
        try:
            await self.ctx.bot.delete_message(self.ctx.channel_id, post["channel_msg_id"])
        except TelegramBadRequest as e:
            if "not found" not in e.message.lower():
                return False, e.message
        except TelegramAPIError as e:
            return False, str(e)
        await self.ctx.db.update_post(post_id, status="deleted", delete_at=None)
        return True, ""
