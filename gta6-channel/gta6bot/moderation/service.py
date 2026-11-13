"""Модератор комментариев: правила → ИИ-судья → удаление и наказание → журнал."""
from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import ChatPermissions, Message, ReplyParameters, User

from ..config import parse_ladder_step
from ..db import now
from ..llm import LLMError
from ..prompts import MODERATION_SYSTEM, ModVerdict
from ..textutil import clip, esc, fold, plural_ru
from .rules import ALLOW, MsgInfo, Rules, Verdict

if TYPE_CHECKING:
    from ..context import Ctx

log = logging.getLogger(__name__)

ADMINS_TTL = 600
DUPLICATE_WINDOW = 120
NOTICE_MIN_GAP = 8          # не чаще одной служебной реплики в чате за N секунд
JUDGE_CACHE_SIZE = 3000
MUTED = ChatPermissions(can_send_messages=False)
UNMUTED = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
    can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
    can_send_other_messages=True, can_add_web_page_previews=True)
ACTION_ICONS = {"delete": "🧹 удалено", "warn": "⚠️ предупреждение", "mute": "🔇 мут", "ban": "🚫 бан",
                "unban": "↩️ разбан", "unmute": "↩️ размут"}
JUDGE_REASONS = {
    "spam": "реклама и спам запрещены",
    "scam": "похоже на мошенничество",
    "toxic": "оскорбления и травля запрещены",
    "nsfw": "контент 18+ запрещён",
    "politics": "политика и оффтоп вне игры удаляются",
    "spoiler": "сюжетный спойлер открытым текстом: прячьте его под скрытый текст",
}


def human_minutes(minutes: int) -> str:
    if minutes % 1440 == 0:
        d = minutes // 1440
        return f"{d} {plural_ru(d, ('сутки', 'суток', 'суток'))}"
    if minutes % 60 == 0:
        h = minutes // 60
        return f"{h} {plural_ru(h, ('час', 'часа', 'часов'))}"
    return f"{minutes} {plural_ru(minutes, ('минуту', 'минуты', 'минут'))}"


def user_link(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{esc(clip(name or str(user_id), 40))}</a>'


def display_name(user: User | None) -> str:
    if user is None:
        return ""
    return " ".join(x for x in (user.first_name, user.last_name) if x).strip() or (user.username or str(user.id))


def mask_spoilers(text: str, entities: list[Any]) -> str:
    """Заменить фрагменты под скрытым текстом на [скрыто]: правильно оформленный спойлер нарушением не является."""
    spans = sorted(((e.offset, e.length) for e in entities if e.type == "spoiler"), reverse=True)
    if not spans:
        return text
    raw = text.encode("utf-16-le")
    for offset, length in spans:
        raw = raw[: offset * 2] + "[скрыто]".encode("utf-16-le") + raw[(offset + length) * 2:]
    return raw.decode("utf-16-le", errors="replace")


class Moderator:
    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx
        self.cfg = ctx.cfg.moderation
        self.rules = Rules(self.cfg)
        self.ladder = [parse_ladder_step(s) for s in self.cfg.ladder]
        self._flood: dict[int, deque[float]] = defaultdict(deque)
        self._dups: dict[int, deque[tuple[float, str]]] = defaultdict(deque)
        self._admins: dict[int, tuple[float, set[int]]] = {}
        self._mentions: dict[str, str] = {}
        self._judge_cache: OrderedDict[str, ModVerdict] = OrderedDict()
        self._journal: list[str] = []
        self._last_notice: dict[int, float] = {}

    def refresh_identity(self) -> None:
        """Вызывается после старта, когда известны имена канала и бота."""
        own = {u for u in (self.ctx.channel_username, self.ctx.bot_username) if u}
        self.rules = Rules(self.cfg, own_usernames=own)

    # ── кто освобождён от модерации ─────────────────────────────────────────
    async def is_chat_admin(self, chat_id: int, user_id: int) -> bool:
        cached = self._admins.get(chat_id)
        if cached is None or time.monotonic() - cached[0] > ADMINS_TTL:
            try:
                admins = await self.ctx.bot.get_chat_administrators(chat_id)
                ids = {a.user.id for a in admins}
            except TelegramAPIError as e:
                log.warning("Не удалось получить список админов чата %s: %s", chat_id, e)
                ids = cached[1] if cached else set()
            cached = (time.monotonic(), ids)
            self._admins[chat_id] = cached
        return user_id in cached[1]

    async def is_exempt(self, message: Message) -> bool:
        if message.is_automatic_forward:
            return True
        chat_id = message.chat.id
        if message.sender_chat is not None:
            # сам канал и анонимные админы группы
            return message.sender_chat.id in (self.ctx.channel_id, chat_id)
        user = message.from_user
        if user is None or user.is_bot:
            return True
        if self.ctx.is_admin(user.id) or await self.is_chat_admin(chat_id, user.id):
            return True
        row = await self.ctx.db.one("SELECT trusted FROM mod_users WHERE chat_id=? AND user_id=?", (chat_id, user.id))
        return bool(row and row["trusted"])

    # ── описание сообщения ──────────────────────────────────────────────────
    async def _mention_kind(self, username: str) -> str:
        key = username.lower()
        if key in self._mentions:
            return self._mentions[key]
        kind = "unknown"
        try:
            chat = await self.ctx.bot.get_chat("@" + username)
            kind = {"channel": "channel", "supergroup": "group", "group": "group", "private": "user"}.get(chat.type, "unknown")
        except TelegramBadRequest:
            kind = "user"       # для обычных пользователей Telegram отвечает «chat not found»
        except TelegramAPIError:
            return "unknown"    # сеть моргнула: не запоминаем
        if len(self._mentions) > 5000:
            self._mentions.clear()
        self._mentions[key] = kind
        return kind

    async def describe(self, message: Message, *, count: bool) -> tuple[MsgInfo, str]:
        """Собрать MsgInfo и текст для судьи. count=False — правка: счётчики не трогаем."""
        text = message.text or message.caption or ""
        entities = list(message.entities or []) + list(message.caption_entities or [])
        urls, mentions, custom = [], [], 0
        for e in entities:
            if e.type == "text_link" and e.url:
                urls.append(e.url)
            elif e.type == "url":
                urls.append(e.extract_from(text))
            elif e.type == "mention":
                mentions.append(e.extract_from(text).lstrip("@"))
            elif e.type == "custom_emoji":
                custom += 1
        user = message.from_user
        sender_is_channel = message.sender_chat is not None
        author_id = message.sender_chat.id if sender_is_channel else (user.id if user else 0)
        name = (message.sender_chat.title or "") if sender_is_channel else display_name(user)
        chat_id = message.chat.id
        db = self.ctx.db

        prior = 0
        if not sender_is_channel and user is not None:
            row = await db.one("SELECT msg_count FROM mod_users WHERE chat_id=? AND user_id=?", (chat_id, user.id))
            prior = row["msg_count"] if row else 0
            if count:
                await db.execute(
                    """INSERT INTO mod_users (chat_id, user_id, name, first_seen, msg_count) VALUES (?, ?, ?, ?, 1)
                       ON CONFLICT(chat_id, user_id) DO UPDATE SET msg_count=msg_count+1, name=excluded.name""",
                    (chat_id, user.id, name[:100], now()))

        recent = duplicates = 0
        if count:
            t = time.monotonic()
            window = self._flood[author_id]
            window.append(t)
            while window and t - window[0] > self.cfg.flood.per_seconds:
                window.popleft()
            recent = len(window)
            digest = fold(text)
            if len(digest) >= 6:
                seen = self._dups[author_id]
                seen.append((t, digest))
                while seen and t - seen[0][0] > DUPLICATE_WINDOW:
                    seen.popleft()
                duplicates = sum(1 for _, d in seen if d == digest)
            if len(self._flood) > 20000:   # память не должна расти бесконечно
                self._flood.clear()
                self._dups.clear()

        kinds = {}
        for m in mentions[:3]:
            if m.lower() not in self.rules.own:
                kinds[m.lower()] = await self._mention_kind(m)

        origin = message.forward_origin
        info = MsgInfo(
            text=text, urls=urls, mentions=mentions, mention_kinds=kinds, custom_emoji=custom,
            has_buttons=message.reply_markup is not None, is_story=message.story is not None,
            forwarded_from_channel=bool(origin and origin.type == "channel"),
            sender_is_channel=sender_is_channel, user_name=name, prior_messages=prior,
            recent_count=recent, duplicate_count=duplicates)
        return info, mask_spoilers(text, entities)

    # ── ИИ-судья ────────────────────────────────────────────────────────────
    async def judge(self, message: Message, info: MsgInfo, text: str, rule: Verdict) -> ModVerdict | None:
        """Вердикт ИИ или None, если судья выключен, исчерпал суточный лимит или недоступен."""
        j = self.cfg.llm_judge
        if not j.enabled or not text.strip():
            return None
        key_src = fold(text) + ("|" + fold(info.user_name) if rule.rule == "name" else "")
        key = hashlib.sha1(key_src.encode()).hexdigest()
        cached = self._judge_cache.get(key)
        if cached is not None:
            self._judge_cache.move_to_end(key)
            return cached
        used = await self.ctx.db.kv_incr(f"judge:{self.ctx.today()}")
        if used > j.daily_limit:
            if used == j.daily_limit + 1:
                await self.ctx.notifier.send(
                    f"ℹ️ ИИ-судья комментариев исчерпал суточный лимит ({j.daily_limit}). До полуночи работают только правила. "
                    f"Лимит задаётся в config.yaml: moderation.llm_judge.daily_limit.")
            return None

        lines = []
        if message.message_thread_id:
            post = await self.ctx.db.one(
                "SELECT headline FROM posts WHERE discussion_msg_id=? AND headline!=''", (message.message_thread_id,))
            if post:
                lines.append(f"Пост канала, под которым идёт обсуждение: «{post['headline']}»")
        status = "новый участник" if self.rules.is_newcomer(info) else "давний участник"
        lines.append(f"Автор: «{clip(info.user_name, 80)}» · {status}, сообщений в чате до этого: {info.prior_messages}")
        if rule.rule:
            lines.append(f"Что насторожило автоматику: {rule.reason}")
        parent = message.reply_to_message
        if parent is not None and not parent.is_automatic_forward and (parent.text or parent.caption):
            lines.append(f"Это ответ на реплику: «{clip(parent.text or parent.caption or '', 300)}»")
        if info.urls:
            lines.append("Ссылки в сообщении: " + ", ".join(clip(u, 100) for u in info.urls[:5]))
        lines.append(f"Комментарий:\n\"\"\"\n{clip(text, 3000)}\n\"\"\"")
        try:
            verdict = await self.ctx.llm.structured(
                "moderation", system=MODERATION_SYSTEM, user="\n".join(lines), schema=ModVerdict, max_tokens=2000)
        except LLMError as e:
            log.warning("ИИ-судья недоступен: %s", e)
            if e.fatal:
                await self.ctx.notifier.once("judge-fatal", f"⚠️ ИИ-судья комментариев не работает: {esc(str(e))}", every=3600)
            return None
        verdict.confidence = max(0.0, min(1.0, float(verdict.confidence)))
        self._judge_cache[key] = verdict
        if len(self._judge_cache) > JUDGE_CACHE_SIZE:
            self._judge_cache.popitem(last=False)
        return verdict

    def _from_judge(self, jv: ModVerdict, newcomer: bool) -> Verdict:
        j = self.cfg.llm_judge
        if jv.verdict in ("spam", "scam"):
            return Verdict("delete", f"ai_{jv.verdict}", JUDGE_REASONS[jv.verdict], "ban" if newcomer else "warn",
                           notice=not newcomer)
        if jv.verdict in ("toxic", "nsfw"):
            return Verdict("delete", f"ai_{jv.verdict}", JUDGE_REASONS[jv.verdict], "warn")
        if jv.verdict == "politics" and j.politics == "delete":
            return Verdict("delete", "ai_politics", JUDGE_REASONS["politics"], "none")
        if jv.verdict == "spoiler" and j.spoiler_guard:
            return Verdict("delete", "ai_spoiler", JUDGE_REASONS["spoiler"], "none")
        return ALLOW

    def merge(self, rule: Verdict, jv: ModVerdict | None, info: MsgInfo) -> Verdict:
        """Свести вердикт правил и мнение судьи в итоговое решение."""
        newcomer = self.rules.is_newcomer(info)
        confident = jv is not None and jv.confidence >= self.cfg.llm_judge.min_confidence
        if rule.confirm == "verdict":
            if jv is None:
                # судьи нет: удаляем по правилу, но необратимый бан заменяем суточным мутом
                return replace(rule, penalty="mute", mute_minutes=1440) if rule.penalty == "ban" else rule
            if jv.verdict == "ok":
                return ALLOW if confident else replace(rule, penalty="warn", notice=True)
            judged = self._from_judge(jv, newcomer) if confident else ALLOW
            return judged if judged.action == "delete" else replace(rule, penalty="warn", notice=True)
        if rule.confirm == "penalty":
            if confident and jv is not None and jv.verdict in ("spam", "scam"):
                return rule
            return replace(rule, penalty="warn", notice=True)
        if jv is None or not confident:
            return ALLOW
        return self._from_judge(jv, newcomer)

    # ── основной вход ───────────────────────────────────────────────────────
    async def handle(self, message: Message, *, is_edit: bool = False) -> bool:
        """Проверить сообщение. True — сообщение удалено."""
        if not self.cfg.enabled or await self.is_exempt(message):
            return False
        info, judge_text = await self.describe(message, count=not is_edit)
        rule = self.rules.evaluate(info)
        j = self.cfg.llm_judge
        final, jv = rule, None
        if rule.action != "delete" or rule.confirm:
            scrutiny = (rule.action == "allow" and info.prior_messages < j.scrutiny_messages
                        and len(judge_text.strip()) >= j.min_chars)
            if rule.action == "suspect" or rule.confirm or scrutiny:
                jv = await self.judge(message, info, judge_text, rule)
                final = self.merge(rule, jv, info)
            else:
                final = ALLOW
        if final.action != "delete":
            return False
        await self.punish(message, info, final, judge_note=jv.reason if jv else "")
        return True

    # ── наказания ───────────────────────────────────────────────────────────
    async def punish(self, message: Message, info: MsgInfo, v: Verdict, *, judge_note: str = "") -> None:
        ctx, chat_id = self.ctx, message.chat.id
        try:
            await ctx.bot.delete_message(chat_id, message.message_id)
        except TelegramBadRequest as e:
            if "not found" not in e.message.lower():
                await ctx.notifier.once(
                    "mod-rights", "⚠️ Не могу удалять сообщения в чате комментариев. Дайте боту в группе права "
                                  f"администратора: «Удаление сообщений» и «Блокировка участников». ({esc(e.message)})", every=3600)
                return
        except TelegramAPIError as e:
            log.warning("Сообщение %s не удалено: %s", message.message_id, e)
            return

        action, detail = "delete", ""
        if info.sender_is_channel and message.sender_chat is not None:
            author_id, author = message.sender_chat.id, f"канал «{esc(clip(info.user_name, 40))}»"
            if v.penalty == "ban":
                try:
                    await ctx.bot.ban_chat_sender_chat(chat_id, author_id)
                    action = "ban"
                except TelegramAPIError as e:
                    log.warning("Канал-отправитель %s не заблокирован: %s", author_id, e)
        else:
            user = message.from_user
            assert user is not None
            author_id, author = user.id, user_link(user.id, info.user_name)
            try:
                if v.penalty == "warn":
                    action, detail = await self._warn(chat_id, user.id)
                elif v.penalty == "mute":
                    await self._mute(chat_id, user.id, v.mute_minutes)
                    action, detail = "mute", f"режим «только чтение» на {human_minutes(v.mute_minutes)}"
                elif v.penalty == "ban":
                    await self._ban(chat_id, user.id)
                    action = "ban"
            except TelegramAPIError as e:
                log.warning("Наказание %s для %s не применено: %s", v.penalty, user.id, e)
                await ctx.notifier.once("mod-ban-rights", f"⚠️ Не удалось применить наказание в чате комментариев: {esc(str(e))}. "
                                                         "Проверьте право бота «Блокировка участников».", every=3600)

        await self.record(chat_id, author_id, info.user_name, action, v.rule, v.reason + (f" · ИИ: {judge_note}" if judge_note else ""), info.text)
        if v.notice and action != "ban":
            tail = f" {detail[0].upper() + detail[1:]}." if detail else ""
            await self.notice(message, f"{'🔇' if action == 'mute' else '⚠️'} {author}, сообщение удалено: {esc(v.reason)}.{tail}")
        elif v.notice and action == "ban" and not self.rules.is_newcomer(info):
            await self.notice(message, f"🚫 {author} заблокирован: {esc(v.reason)}.")

    async def _warn(self, chat_id: int, user_id: int) -> tuple[str, str]:
        """Очередной шаг лестницы наказаний. → (действие, пояснение для реплики)."""
        db = self.ctx.db
        row = await db.one("SELECT warns, last_warn_at FROM mod_users WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        warns = row["warns"] if row else 0
        if row and row["last_warn_at"] and now() - row["last_warn_at"] > self.cfg.warn_ttl_days * 86400:
            warns = 0
        warns += 1
        await db.execute(
            """INSERT INTO mod_users (chat_id, user_id, first_seen, warns, last_warn_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(chat_id, user_id) DO UPDATE SET warns=excluded.warns, last_warn_at=excluded.last_warn_at""",
            (chat_id, user_id, now(), warns, now()))
        kind, minutes = self.ladder[min(warns, len(self.ladder)) - 1]
        if kind == "mute":
            await self._mute(chat_id, user_id, minutes)
            return "mute", f"режим «только чтение» на {human_minutes(minutes)}"
        if kind == "ban":
            await self._ban(chat_id, user_id)
            return "ban", ""
        left = next((i for i, (k, _) in enumerate(self.ladder) if k != "warn"), len(self.ladder)) - warns
        nxt = "следующее нарушение — ограничение" if left <= 0 else f"предупреждение {warns}"
        return "warn", nxt

    async def _mute(self, chat_id: int, user_id: int, minutes: int) -> None:
        await self.ctx.bot.restrict_chat_member(chat_id, user_id, permissions=MUTED, until_date=now() + max(1, minutes) * 60)

    async def _ban(self, chat_id: int, user_id: int) -> None:
        await self.ctx.bot.ban_chat_member(chat_id, user_id, revoke_messages=True)
        await self.ctx.db.execute(
            """INSERT INTO mod_users (chat_id, user_id, first_seen, banned) VALUES (?, ?, ?, 1)
               ON CONFLICT(chat_id, user_id) DO UPDATE SET banned=1""", (chat_id, user_id, now()))

    async def record(self, chat_id: int, user_id: int, name: str, action: str, rule: str, reason: str, text: str) -> None:
        await self.ctx.db.execute(
            "INSERT INTO mod_log (ts, chat_id, user_id, user_name, action, rule, reason, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now(), chat_id, user_id, name[:100], action, rule, reason[:300], text[:1000]))
        if action == "delete" and not self.cfg.log_deletions:
            return
        who = user_link(user_id, name) if user_id > 0 else f"канал «{esc(clip(name, 40))}»"
        undo = {"ban": f" · /unban {user_id}", "mute": f" · /unmute {user_id}"}.get(action, "") if user_id > 0 else ""
        self._journal.append(
            f"• {ACTION_ICONS.get(action, action)} · {who} · {esc(reason)}"
            + (f"\n   «{esc(clip(text, 160))}»" if text.strip() else "") + undo)

    async def flush_journal(self) -> None:
        """Раз в несколько секунд отправлять накопленный журнал одним сообщением в админ-чат."""
        if not self._journal:
            return
        batch, self._journal = self._journal[:12], self._journal[12:]
        extra = f"\n… и ещё {len(self._journal)}" if self._journal else ""
        if extra:
            self._journal.clear()
        await self.ctx.notifier.send("🛡 <b>Модерация</b>\n" + "\n".join(batch) + extra)

    async def notice(self, message: Message, text: str) -> None:
        """Короткая реплика в ветке комментариев, которая сама исчезнет."""
        chat_id = message.chat.id
        t = time.monotonic()
        if t - self._last_notice.get(chat_id, -1e9) < NOTICE_MIN_GAP:
            return
        self._last_notice[chat_id] = t
        thread = message.message_thread_id
        try:
            sent = await self.ctx.bot.send_message(
                chat_id, text, disable_notification=True,
                reply_parameters=ReplyParameters(message_id=thread, allow_sending_without_reply=True) if thread else None)
        except TelegramAPIError as e:
            log.debug("Реплика модератора не отправлена: %s", e)
            return
        await self.ctx.db.schedule_delete(chat_id, sent.message_id, self.cfg.notice_ttl_sec)

    # ── ручные действия админов ─────────────────────────────────────────────
    async def manual(self, action: str, chat_id: int, user_id: int, name: str = "", *, minutes: int = 60,
                     by: str = "", reason: str = "") -> str:
        """warn | mute | ban | unban | unmute | trust | untrust. Возвращает текст результата."""
        bot, db = self.ctx.bot, self.ctx.db
        who = user_link(user_id, name)
        note = f"вручную ({by})" + (f": {reason}" if reason else "")
        if action == "warn":
            done, detail = await self._warn(chat_id, user_id)
            await self.record(chat_id, user_id, name, done, "manual", note, "")
            return f"⚠️ {who}: предупреждение." + (f" {detail[0].upper() + detail[1:]}." if detail else "")
        if action == "mute":
            await self._mute(chat_id, user_id, minutes)
            await self.record(chat_id, user_id, name, "mute", "manual", note, "")
            return f"🔇 {who}: режим «только чтение» на {human_minutes(minutes)}."
        if action == "ban":
            await self._ban(chat_id, user_id)
            await self.record(chat_id, user_id, name, "ban", "manual", note, "")
            return f"🚫 {who} заблокирован."
        if action == "unban":
            await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
            await db.execute("UPDATE mod_users SET banned=0, warns=0 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
            await self.record(chat_id, user_id, name, "unban", "manual", note, "")
            return f"↩️ {who} разблокирован, предупреждения сняты."
        if action == "unmute":
            await bot.restrict_chat_member(chat_id, user_id, permissions=UNMUTED)
            await db.execute("UPDATE mod_users SET warns=0 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
            await self.record(chat_id, user_id, name, "unmute", "manual", note, "")
            return f"↩️ {who}: ограничения сняты."
        if action in ("trust", "untrust"):
            flag = 1 if action == "trust" else 0
            await db.execute(
                """INSERT INTO mod_users (chat_id, user_id, name, first_seen, trusted) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id, user_id) DO UPDATE SET trusted=excluded.trusted""",
                (chat_id, user_id, name[:100], now(), flag))
            return f"🤝 {who}: модерация {'отключена (доверенный участник)' if flag else 'снова включена'}."
        raise ValueError(f"неизвестное действие {action}")
