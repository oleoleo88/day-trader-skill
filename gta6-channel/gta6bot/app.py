"""Сборка приложения: соединения, сервисы, проверка прав, фоновые задачи, запуск поллинга."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
                           BotCommandScopeChat, ErrorEvent)

from .ads import Ads
from .analytics import Analytics
from .collector import Collector
from .config import Config
from .context import Ctx
from .db import Database, now
from .editor import Editor
from .handlers import admin, channel, group, private
from .llm import LLM
from .moderation.service import Moderator
from .notifier import Notifier
from .publisher import Publisher
from .rubrics import Rubrics
from .textutil import esc

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "edited_message", "channel_post", "callback_query", "chat_member",
                   "my_chat_member", "message_reaction_count"]
ROUTERS = (admin.router, group.router, channel.router, private.router)


class StartupError(Exception):
    """Запуск невозможен; текст рассчитан на показ пользователю."""


def attach_services(ctx: Ctx) -> None:
    ctx.notifier = Notifier(ctx)
    ctx.publisher = Publisher(ctx)
    ctx.editor = Editor(ctx)
    ctx.collector = Collector(ctx)
    ctx.moderator = Moderator(ctx)
    ctx.rubrics = Rubrics(ctx)
    ctx.analytics = Analytics(ctx)
    ctx.ads = Ads(ctx)


async def create_context(cfg: Config, *, llm: Any = None, with_bot: bool = True, db_path: Any = None) -> Ctx:
    db = await Database.open(db_path or cfg.db_path)
    bot = None
    if with_bot:
        session = None
        if cfg.secrets.telegram_api_base:  # свой сервер Bot API (или тестовый)
            session = AiohttpSession(api=TelegramAPIServer.from_base(cfg.secrets.telegram_api_base))
        bot = Bot(cfg.secrets.bot_token, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    http = aiohttp.ClientSession(
        trust_env=True, timeout=aiohttp.ClientTimeout(total=30, connect=10),
        headers={"User-Agent": cfg.collector.user_agent})
    ctx = Ctx(cfg=cfg, db=db, bot=bot, llm=llm or LLM(cfg.llm, db), http=http)  # type: ignore[arg-type]
    attach_services(ctx)
    return ctx


async def close_context(ctx: Ctx) -> None:
    if ctx.http is not None:
        await ctx.http.close()
    if ctx.bot is not None:
        await ctx.bot.session.close()
    close_llm = getattr(ctx.llm, "close", None)
    if close_llm is not None:
        await close_llm()
    await ctx.db.close()


def build_dispatcher(ctx: Ctx) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["ctx"] = ctx
    for router in ROUTERS:
        # роутеры — объекты уровня модуля; при повторной сборке (тесты) открепляем их от прежнего диспетчера
        parent = router.parent_router
        if parent is not None:
            parent.sub_routers.remove(router)
            router._parent_router = None  # noqa: SLF001
        dp.include_router(router)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        await ctx.notifier.error("обработка события Telegram", event.exception)
        return True

    return dp


async def startup(ctx: Ctx) -> list[str]:
    """Узнать ID канала, чата и бота, проверить права. Возвращает список замечаний (пусто — всё в порядке)."""
    bot, secrets = ctx.bot, ctx.cfg.secrets
    try:
        me = await bot.get_me()
    except TelegramAPIError as e:
        raise StartupError(f"Telegram не принял BOT_TOKEN: {e}") from e
    ctx.bot_id, ctx.bot_username = me.id, me.username or ""
    try:
        chat = await bot.get_chat(secrets.channel_id)
    except TelegramAPIError as e:
        raise StartupError(
            f"Бот не видит канал {secrets.channel_id}: {e}. Добавьте @{ctx.bot_username} в администраторы канала "
            f"и проверьте CHANNEL_ID в .env.") from e
    if chat.type != "channel":
        raise StartupError(f"CHANNEL_ID указывает не на канал, а на чат типа «{chat.type}».")
    ctx.channel_id, ctx.channel_username, ctx.channel_title = chat.id, chat.username, chat.title or ""
    ctx.discussion_id = secrets.discussion_chat_id or chat.linked_chat_id

    problems: list[str] = []
    try:
        member = await bot.get_chat_member(ctx.channel_id, ctx.bot_id)
        if member.status != "administrator":
            problems.append("Бот не администратор канала: публиковать он не сможет.")
        else:
            if not getattr(member, "can_post_messages", False):
                problems.append("В канале у бота нет права «Публикация сообщений».")
            if not getattr(member, "can_delete_messages", False):
                problems.append("В канале у бота нет права «Удаление сообщений» (нужно для автоудаления рекламы и кнопки «Удалить»).")
            if not getattr(member, "can_invite_users", False):
                problems.append("В канале у бота нет права «Пригласительные ссылки» (нужно для /link и учёта прихода).")
    except TelegramAPIError as e:
        problems.append(f"Не удалось проверить права бота в канале: {e}")

    if ctx.discussion_id is None:
        if ctx.cfg.moderation.enabled:
            problems.append("К каналу не привязана группа для комментариев: модерация и первый комментарий не работают. "
                            "Привяжите группу в настройках канала («Обсуждение») и добавьте в неё бота админом.")
    else:
        try:
            member = await bot.get_chat_member(ctx.discussion_id, ctx.bot_id)
            if member.status != "administrator":
                problems.append("В группе комментариев бот не администратор: он не видит сообщения и не может модерировать.")
            else:
                if not getattr(member, "can_delete_messages", False):
                    problems.append("В группе комментариев у бота нет права «Удаление сообщений».")
                if not getattr(member, "can_restrict_members", False):
                    problems.append("В группе комментариев у бота нет права «Блокировка участников».")
        except TelegramAPIError as e:
            problems.append(f"Бот не добавлен в группу комментариев ({ctx.discussion_id}): {e}")

    mode = await ctx.db.kv_get("mode")
    if mode in ("auto", "hybrid", "review"):
        ctx.cfg.publishing.mode = mode  # режим, выбранный командой /mode, переживает перезапуск
    ctx.moderator.refresh_identity()
    return problems


async def set_commands(ctx: Ctx) -> None:
    bot = ctx.bot
    try:
        await bot.set_my_commands(
            [BotCommand(command="start", description="Меню"), BotCommand(command="ask", description="Вопрос об игре")],
            scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(
            [BotCommand(command="ask", description="Вопрос об игре"), BotCommand(command="report", description="Пожаловаться (ответом на сообщение)"),
             BotCommand(command="rules", description="Правила")],
            scope=BotCommandScopeAllGroupChats())
        await bot.set_my_commands(
            [BotCommand(command="status", description="Состояние"), BotCommand(command="queue", description="Очередь"),
             BotCommand(command="post", description="Отдать материал редактору"), BotCommand(command="say", description="Опубликовать свой текст"),
             BotCommand(command="fetch", description="Опросить источники"), BotCommand(command="stats", description="Статистика"),
             BotCommand(command="ads", description="Реклама"), BotCommand(command="pause", description="Пауза"),
             BotCommand(command="resume", description="Продолжить"), BotCommand(command="help", description="Все команды")],
            scope=BotCommandScopeChat(chat_id=ctx.admin_chat))
    except TelegramAPIError as e:
        log.warning("Меню команд не установлено: %s", e)


async def announce(ctx: Ctx, problems: list[str]) -> None:
    pub = ctx.cfg.publishing
    lines = [f"✅ <b>Бот запущен</b> · канал «{esc(ctx.channel_title)}»",
             f"Режим: <b>{pub.mode}</b> · источников: {sum(1 for s in ctx.cfg.collector.sources if s.enabled)} · {ctx.phase_text()}"]
    if problems:
        lines.append("\n⚠️ <b>Что нужно поправить:</b>\n" + "\n".join(f"• {esc(p)}" for p in problems))
    lines.append("\nКоманды: /help")
    sent = await ctx.notifier.send("\n".join(lines))
    if sent is None:
        log.error("Админ-чат %s недоступен. Если это личка — откройте бота и нажмите Start; если группа — добавьте в неё бота.",
                  ctx.admin_chat)


# ── фоновые задачи ───────────────────────────────────────────────────────────

class Jobs:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.tasks: list[asyncio.Task] = []

    def every(self, seconds: float, fn: Callable[[], Awaitable[Any]], name: str, delay: float = 0) -> None:
        self.tasks.append(asyncio.create_task(self._loop(seconds, fn, name, delay), name=name))

    async def _loop(self, seconds: float, fn: Callable[[], Awaitable[Any]], name: str, delay: float) -> None:
        await asyncio.sleep(delay)
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — фоновая задача должна пережить любую ошибку
                await self.ctx.notifier.error(name, e)
            await asyncio.sleep(seconds)

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()


async def cleanup_tick(ctx: Ctx) -> None:
    """Удалить служебные сообщения, чьё время вышло."""
    rows = await ctx.db.all("SELECT chat_id, msg_id FROM cleanup WHERE delete_at<=? LIMIT 50", (now(),))
    for r in rows:
        try:
            await ctx.bot.delete_message(r["chat_id"], r["msg_id"])
        except TelegramAPIError:
            pass
        await ctx.db.execute("DELETE FROM cleanup WHERE chat_id=? AND msg_id=?", (r["chat_id"], r["msg_id"]))


async def analytics_tick(ctx: Ctx) -> None:
    await ctx.analytics.snapshot()
    await ctx.analytics.scrape_views()
    if await ctx.db.kv_get("housekeeping") != ctx.today():
        await ctx.db.kv_set("housekeeping", ctx.today())
        await ctx.analytics.housekeeping()


def start_jobs(ctx: Ctx) -> Jobs:
    jobs = Jobs(ctx)
    jobs.every(30, ctx.collector.run_cycle, "сбор новостей", delay=5)
    jobs.every(20, ctx.publisher.tick, "очередь публикаций", delay=10)
    jobs.every(10, lambda: cleanup_tick(ctx), "уборка служебных сообщений", delay=15)
    jobs.every(20, ctx.moderator.flush_journal, "журнал модерации", delay=20)
    jobs.every(60, ctx.rubrics.tick, "рубрики", delay=25)
    jobs.every(1800, lambda: analytics_tick(ctx), "аналитика", delay=30)
    return jobs


async def run(cfg: Config) -> None:
    ctx = await create_context(cfg)
    jobs: Jobs | None = None
    try:
        problems = await startup(ctx)
        dp = build_dispatcher(ctx)
        await set_commands(ctx)
        await announce(ctx, problems)
        for p in problems:
            log.warning("Замечание при запуске: %s", p)
        jobs = start_jobs(ctx)
        log.info("Бот @%s запущен: канал %s, чат комментариев %s", ctx.bot_username, ctx.channel_id, ctx.discussion_id)
        await dp.start_polling(ctx.bot, allowed_updates=ALLOWED_UPDATES, close_bot_session=False)
    finally:
        if jobs is not None:
            await jobs.stop()
        await close_context(ctx)
