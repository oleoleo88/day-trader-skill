"""Админ-чат: команды управления, кнопки согласования, правка текста, ответы пользователям."""
from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import CallbackQuery, ForceReply, Message

from .. import ads as ads_module
from .. import review
from ..context import Ctx
from ..db import meta_of, now
from ..llm import LLMError
from ..prompts import REWRITE_STYLES
from ..textutil import clip, esc, visible_len

log = logging.getLogger(__name__)
router = Router(name="admin")

HELP = """<b>Управление каналом</b>

<b>Публикация</b>
/status — режим, очередь, ближайший выход
/queue — что ждёт публикации и согласования
/mode auto | hybrid | review — режим выпуска
/pause · /resume — остановить и возобновить автопостинг
/pub &lt;№&gt; — выпустить пост сейчас · /drop &lt;№&gt; — снять с очереди

<b>Контент</b>
/post &lt;ссылка или текст&gt; — отдать материал ИИ-редактору, вернётся черновик
/say &lt;текст&gt; — опубликовать ваш текст как есть (или ответьте этой командой на сообщение)
/fetch — опросить источники прямо сейчас
/sources — состояние источников
/digest · /countdown · /poll [poll|quiz] — рубрики вне расписания

<b>Реклама</b>
/ad — как поставить рекламный пост · /ads — размещения · /adcancel &lt;№&gt;

<b>Рост и цифры</b>
/stats [дней] — сводка · /link &lt;название&gt; [цена] — именная ссылка · /links — приход по ссылкам

<b>Комментарии</b>
/unban &lt;id&gt; · /unmute &lt;id&gt; · /trust &lt;id&gt; · /untrust &lt;id&gt;
В самом чате ответом на сообщение: /warn /mute [мин] /ban /del /trust

Черновики приходят сюда карточками с кнопками. Ответ на карточку заявки из бота уходит её автору."""

MODES = {"auto": "всё, что прошло редактора, выходит без согласования",
         "hybrid": "официальное и уверенное выходит само, слухи и сомнительное ждут вас",
         "review": "каждый пост ждёт вашей кнопки"}


class AdminChat(Filter):
    async def __call__(self, message: Message, ctx: Ctx) -> bool:
        return message.chat.id == ctx.admin_chat and ctx.is_admin(message.from_user.id if message.from_user else None)


class AdminCallback(Filter):
    async def __call__(self, callback: CallbackQuery, ctx: Ctx) -> bool:
        return ctx.is_admin(callback.from_user.id)


router.message.filter(AdminChat())
router.callback_query.filter(AdminCallback())


def _int_arg(command: CommandObject) -> int | None:
    raw = (command.args or "").strip().lstrip("#").split(" ")[0]
    return int(raw) if raw.lstrip("-").isdigit() else None


# ── справка и состояние ──────────────────────────────────────────────────────

@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("status"))
async def cmd_status(message: Message, ctx: Ctx) -> None:
    db, pub = ctx.db, ctx.cfg.publishing
    paused = await ctx.publisher.paused()
    queued = await db.scalar("SELECT COUNT(*) FROM posts WHERE status='queued' AND kind!='ad'", default=0)
    in_review = await db.scalar("SELECT COUNT(*) FROM posts WHERE status='review'", default=0)
    ads_wait = await db.scalar("SELECT COUNT(*) FROM posts WHERE status='queued' AND kind='ad'", default=0)
    last = await db.scalar("SELECT MAX(published_at) FROM posts WHERE status='published'", default=0)
    ok, why = await ctx.publisher.can_publish_regular()
    top_until = await ctx.publisher.ad_top_until()
    failing = await db.scalar("SELECT COUNT(*) FROM source_health WHERE fails>=3", default=0)
    total = sum(1 for s in ctx.cfg.collector.sources if s.enabled)
    waiting = await db.scalar("SELECT COUNT(*) FROM items WHERE status='new'", default=0)
    lines = [
        f"📺 <b>{esc(ctx.channel_title or 'Канал')}</b>" + (f" · @{ctx.channel_username}" if ctx.channel_username else ""),
        f"Режим: <b>{pub.mode}</b> — {MODES[pub.mode]}",
        "Автопостинг: " + ("⏸ на паузе (/resume)" if paused else "▶️ работает"),
        f"В очереди: {queued} · на согласовании: {in_review}" + (f" · рекламы в расписании: {ads_wait}" if ads_wait else ""),
        f"Записей ждёт редактора: {waiting}",
        "Последний пост: " + (f"{ctx.local(last):%d.%m %H:%M}" if last else "ещё не было"),
    ]
    if now() < top_until:
        lines.append(f"Рекламный топ до {ctx.local(top_until):%H:%M}: остальные посты ждут")
    else:
        lines.append("Следующий обычный пост: " + ("можно прямо сейчас" if ok else why))
    lines.append(f"Источники: {total - failing} из {total} в порядке" + (" (/sources)" if failing else ""))
    lines.append(f"До релиза: {ctx.phase_text()}")
    await message.answer("\n".join(lines))


@router.message(Command("queue"))
async def cmd_queue(message: Message, ctx: Ctx) -> None:
    rows = await ctx.db.all(
        """SELECT * FROM posts WHERE status IN ('queued', 'review')
           ORDER BY (status='review'), (kind='ad') DESC, (kind='breaking') DESC, importance DESC, created_at LIMIT 30""")
    if not rows:
        await message.answer("Очередь пуста, на согласовании ничего нет.")
        return
    lines = []
    for r in rows:
        mark = "📝 согласование" if r["status"] == "review" else ("📣 реклама" if r["kind"] == "ad" else "🕐 очередь")
        when = f" · {ctx.local(r['not_before']):%d.%m %H:%M}" if r["kind"] == "ad" else f" · важность {r['importance']}"
        lines.append(f"#{r['id']} · {mark}{when}\n   {esc(clip(r['headline'] or review.KIND_NAMES.get(r['kind'], 'пост'), 80))}")
    lines.append("\n/pub &lt;№&gt; — выпустить сейчас · /drop &lt;№&gt; — убрать")
    await message.answer("\n".join(lines))


@router.message(Command("mode"))
async def cmd_mode(message: Message, command: CommandObject, ctx: Ctx) -> None:
    mode = (command.args or "").strip().lower()
    if mode not in MODES:
        current = ctx.cfg.publishing.mode
        await message.answer(f"Сейчас режим <b>{current}</b>.\n" + "\n".join(f"/mode {m} — {d}" for m, d in MODES.items()))
        return
    ctx.cfg.publishing.mode = mode
    await ctx.db.kv_set("mode", mode)
    await message.answer(f"Режим: <b>{mode}</b> — {MODES[mode]}.")


@router.message(Command("pause"))
async def cmd_pause(message: Message, ctx: Ctx) -> None:
    await ctx.db.kv_set("paused", "1")
    await message.answer("⏸ Автопостинг на паузе: новости и рубрики копятся в очереди, реклама по расписанию выходит. "
                         "Кнопка «Опубликовать» и /pub работают. Вернуть: /resume")


@router.message(Command("resume"))
async def cmd_resume(message: Message, ctx: Ctx) -> None:
    await ctx.db.kv_set("paused", "0")
    await message.answer("▶️ Автопостинг возобновлён.")


@router.message(Command("pub"))
async def cmd_pub(message: Message, command: CommandObject, ctx: Ctx) -> None:
    post_id = _int_arg(command)
    if post_id is None:
        await message.answer("Укажите номер поста: /pub 12")
        return
    ok, info = await ctx.publisher.publish(post_id, announce=False)
    await message.answer(f"✅ Опубликовано: {info}" if ok else f"Не получилось: {esc(info)}")


@router.message(Command("drop"))
async def cmd_drop(message: Message, command: CommandObject, ctx: Ctx) -> None:
    post_id = _int_arg(command)
    post = await ctx.db.post(post_id) if post_id is not None else None
    if post is None:
        await message.answer("Укажите номер поста из /queue: /drop 12")
        return
    await message.answer(await _reject(ctx, post))


async def _reject(ctx: Ctx, post) -> str:
    if post["kind"] == "ad":
        return await ctx.ads.cancel(post["id"])
    if post["status"] not in ("queued", "review"):
        return f"Пост #{post['id']} уже не в очереди."
    await ctx.db.update_post(post["id"], status="rejected")
    await review.close_card(ctx, post, "❌ Отклонён")
    return f"Пост #{post['id']} снят."


# ── контент ──────────────────────────────────────────────────────────────────

_URL_ONLY = re.compile(r"^https?://\S+$")


@router.message(Command("post"))
async def cmd_post(message: Message, command: CommandObject, ctx: Ctx) -> None:
    arg = (command.args or "").strip()
    if not arg and message.reply_to_message:
        arg = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
    if not arg:
        await message.answer("Пришлите ссылку на новость или её текст: <code>/post https://…</code> или <code>/post текст</code>")
        return
    wait = await message.answer("✍️ Готовлю черновик…")
    try:
        if _URL_ONLY.match(arg):
            await ctx.editor.draft_from_url(arg)
        else:
            await ctx.editor.draft_from_text(arg)
    except (LLMError, ValueError) as e:
        await wait.edit_text(f"Не получилось: {esc(str(e))}")
        return
    await wait.delete()


@router.message(Command("say"))
async def cmd_say(message: Message, command: CommandObject, ctx: Ctx) -> None:
    source = message.reply_to_message if (message.reply_to_message and not command.args) else None
    if source is not None:
        text = (source.html_text or "").strip() if (source.text or source.caption) else ""
    else:
        # берём текст с сохранением разметки, без самой команды
        full = message.html_text or ""
        text = re.sub(r"^/say(@\w+)?\s*", "", full, count=1).strip()
    if not text:
        await message.answer("Напишите текст после команды: <code>/say Текст поста</code> — или ответьте командой /say на готовое сообщение.")
        return
    if visible_len(text) > 4096:
        await message.answer("Текст длиннее 4096 знаков, Telegram такой пост не примет.")
        return
    post_id = await ctx.db.create_post(
        kind="manual", status="review", headline=clip(re.sub(r"<[^>]+>", "", text).split("\n", 1)[0], 100), text=text,
        category="other", importance=7, confidence=1.0, meta={"edited": True})
    await review.send_card(ctx, post_id)


@router.message(Command("fetch"))
async def cmd_fetch(message: Message, ctx: Ctx) -> None:
    wait = await message.answer("📡 Опрашиваю источники…")
    stats = await ctx.collector.run_cycle(force=True)
    if not stats:
        await wait.edit_text("Сбор уже идёт, результат придёт карточками.")
        return
    await wait.edit_text(f"Готово. Новых записей по теме: {stats.get('fetched', 0)} · черновиков: {stats.get('drafted', 0)} · "
                         f"отсеяно: {stats.get('skipped', 0)}" + (f" · ошибок: {stats['errors']}" if stats.get("errors") else ""))


@router.message(Command("sources"))
async def cmd_sources(message: Message, ctx: Ctx) -> None:
    await message.answer("<b>Источники</b>\n" + await ctx.collector.health_report())


@router.message(Command("digest"))
async def cmd_digest(message: Message, ctx: Ctx) -> None:
    post_id = await ctx.rubrics.digest(force=True)
    await message.answer(f"Дайджест #{post_id} поставлен в очередь." if post_id else "Сегодня ещё не было постов для дайджеста.")


@router.message(Command("countdown"))
async def cmd_countdown(message: Message, ctx: Ctx) -> None:
    post_id = await ctx.rubrics.countdown()
    await message.answer(f"Отсчёт #{post_id} готов." if post_id else "Дата релиза уже прошла: отсчёт выключен.")


@router.message(Command("poll"))
async def cmd_poll(message: Message, command: CommandObject, ctx: Ctx) -> None:
    kind = (command.args or "").strip().lower() or None
    if kind not in (None, "poll", "quiz"):
        await message.answer("/poll — тип по очереди, /poll poll — опрос, /poll quiz — викторина")
        return
    wait = await message.answer("🗳 Придумываю опрос…")
    post_id = await ctx.rubrics.poll(kind)
    if post_id and ctx.cfg.publishing.mode == "auto":
        await wait.edit_text(f"Опрос #{post_id} поставлен в очередь.")
    else:
        await wait.delete()


# ── статистика и ссылки ──────────────────────────────────────────────────────

@router.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject, ctx: Ctx) -> None:
    days = _int_arg(command) or 7
    await ctx.analytics.scrape_views()
    await message.answer(await ctx.analytics.stats_text(max(1, min(days, 90))))


@router.message(Command("link"))
async def cmd_link(message: Message, command: CommandObject, ctx: Ctx) -> None:
    args = (command.args or "").strip()
    if not args:
        await message.answer("Создать именную ссылку: <code>/link название 5000</code> (цена размещения необязательна).")
        return
    name, cost = args, 0.0
    m = re.match(r"^(.*\S)\s+(\d+(?:[.,]\d+)?)$", args)
    if m:
        name, cost = m.group(1), float(m.group(2).replace(",", "."))
    try:
        link = await ctx.analytics.create_link(name, cost)
    except TelegramAPIError as e:
        await message.answer(f"Не получилось создать ссылку: {esc(str(e))}\nБоту в канале нужно право «Приглашение пользователей по ссылке».")
        return
    await message.answer(f"Ссылка «{esc(name[:32])}»:\n<code>{esc(link)}</code>\n\nОтдавайте её только в один источник. Результат — в /links.")


@router.message(Command("links"))
async def cmd_links(message: Message, ctx: Ctx) -> None:
    await message.answer(await ctx.analytics.links_report())


# ── реклама ──────────────────────────────────────────────────────────────────

@router.message(Command("ad"))
async def cmd_ad(message: Message, command: CommandObject, ctx: Ctx) -> None:
    creative = message.reply_to_message
    if creative is None or not (command.args or "").strip():
        await message.answer(ads_module.HELP)
        return
    try:
        post_id, warnings = await ctx.ads.create(creative, command.args or "")
    except ValueError as e:
        await message.answer(f"Не получилось: {esc(str(e))}\n\nСправка: /ad")
        return
    if warnings:
        await message.answer(f"⚠️ Размещение #{post_id}:\n" + "\n".join(f"• {esc(w)}" for w in warnings))


@router.message(Command("ads"))
async def cmd_ads(message: Message, ctx: Ctx) -> None:
    await message.answer(await ctx.ads.listing())


@router.message(Command("adcancel"))
async def cmd_adcancel(message: Message, command: CommandObject, ctx: Ctx) -> None:
    post_id = _int_arg(command)
    await message.answer(await ctx.ads.cancel(post_id) if post_id is not None else "Укажите номер: /adcancel 12")


# ── модерация из админ-чата ──────────────────────────────────────────────────

@router.message(Command("unban", "unmute", "trust", "untrust"))
async def cmd_mod(message: Message, command: CommandObject, ctx: Ctx) -> None:
    user_id = _int_arg(command)
    if user_id is None or ctx.discussion_id is None:
        await message.answer(f"Укажите числовой ID пользователя: /{command.command} 123456789" if ctx.discussion_id
                             else "К каналу не привязана группа комментариев.")
        return
    try:
        by = message.from_user.full_name if message.from_user else "админ"
        await message.answer(await ctx.moderator.manual(command.command, ctx.discussion_id, user_id, by=by))
    except TelegramAPIError as e:
        await message.answer(f"Не получилось: {esc(str(e))}")


# ── кнопки карточек ──────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("p:"))
async def on_post_button(callback: CallbackQuery, ctx: Ctx) -> None:
    _, action, raw_id = (callback.data or "").split(":", 2)
    post = await ctx.db.post(int(raw_id))
    if post is None:
        await callback.answer("Пост не найден", show_alert=True)
        return
    pid = post["id"]

    if action == "pub":
        await callback.answer("Публикую…")
        ok, info = await ctx.publisher.publish(pid, announce=False)
        if not ok and callback.message:
            await callback.message.answer(f"Пост #{pid} не опубликован: {esc(info)}")
        return

    if action == "del":
        ok, error = await ctx.publisher.delete_from_channel(pid)
        await callback.answer("Удалено из канала" if ok else f"Не удалось: {error}"[:190], show_alert=not ok)
        if ok and callback.message:
            try:
                await callback.message.edit_text(f"🗑 Пост #{pid} удалён из канала.", reply_markup=None)
            except TelegramAPIError:
                pass
        return

    if action == "reject":
        await callback.answer(re.sub(r"<[^>]+>", "", await _reject(ctx, post))[:190])
        return

    if post["status"] != "review":
        await callback.answer("Этот черновик уже обработан", show_alert=True)
        return

    if action == "queue":
        await ctx.db.update_post(pid, status="queued", not_before=now())
        ok, why = await ctx.publisher.can_publish_regular()
        when = "выйдет в ближайшую минуту" if ok or post["kind"] == "breaking" else f"ждёт: {why}"
        await review.close_card(ctx, post, f"🕐 В очереди, {when}")
        await callback.answer("Поставлен в очередь")
    elif action == "edit":
        prompt = await ctx.bot.send_message(
            ctx.admin_chat,
            f"✏️ Пришлите новый текст поста #{pid} <b>ответом на это сообщение</b>. Жирный, курсив, ссылки и скрытый "
            f"текст сохранятся. Текст заменит пост целиком, вместе с подписью.",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Новый текст поста"))
        await ctx.db.kv_set(f"editprompt:{prompt.message_id}", pid)
        await callback.answer()
    elif action.startswith("rw-"):
        style = action[3:]
        if style not in REWRITE_STYLES:
            await callback.answer("Неизвестный стиль", show_alert=True)
            return
        await callback.answer("Переписываю, это займёт несколько секунд…")
        try:
            await ctx.editor.rewrite(pid, style)
        except (LLMError, ValueError) as e:
            if callback.message:
                await callback.message.answer(f"Не получилось переписать пост #{pid}: {esc(str(e))}")
            return
        await review.drop_card(ctx, post)
        await review.send_card(ctx, pid)
    elif action == "noimg":
        await ctx.db.update_post(pid, image_url=None)
        await callback.answer("Фото убрано")
        await review.drop_card(ctx, post)
        await review.send_card(ctx, pid)
    elif action == "repoll":
        await callback.answer("Придумываю другой вариант…")
        await ctx.rubrics.poll(meta_of(post).get("poll", {}).get("kind"), replace_id=pid)
    else:
        await callback.answer("Неизвестное действие", show_alert=True)


@router.callback_query(F.data.startswith("m:"))
async def on_report_button(callback: CallbackQuery, ctx: Ctx) -> None:
    """Кнопки под жалобой из чата: m:<действие>:<чат>:<сообщение>:<автор>."""
    try:
        _, action, chat_id, msg_id, user_id = (callback.data or "").split(":")
        chat, msg, user = int(chat_id), int(msg_id), int(user_id)
    except ValueError:
        await callback.answer("Некорректные данные кнопки", show_alert=True)
        return
    result = "Оставлено как есть"
    try:
        if action in ("del", "ban"):
            try:
                await ctx.bot.delete_message(chat, msg)
            except TelegramAPIError:
                pass
            result = "Сообщение удалено"
        if action == "ban" and user > 0:
            await ctx.moderator.manual("ban", chat, user, by=callback.from_user.full_name, reason="по жалобе")
            result = "Сообщение удалено, автор заблокирован"
    except TelegramAPIError as e:
        await callback.answer(f"Не получилось: {e}"[:190], show_alert=True)
        return
    await callback.answer(result)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.reply(f"✔️ {result} ({esc(callback.from_user.full_name)})")
        except TelegramAPIError:
            pass


@router.callback_query(F.data.startswith("s:"))
async def on_suggestion_button(callback: CallbackQuery, ctx: Ctx) -> None:
    """Кнопки под предложенной новостью: s:<take|no>:<номер>."""
    _, action, raw_id = (callback.data or "").split(":", 2)
    row = await ctx.db.one("SELECT * FROM suggestions WHERE id=?", (int(raw_id),))
    if row is None or row["status"] != "new":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return
    if action == "take":
        if len(row["text"].strip()) < 30:
            await callback.answer("В заявке нет текста: оформите пост вручную через /post или /say", show_alert=True)
            return
        await callback.answer("Отдаю редактору…")
        await ctx.db.execute("UPDATE suggestions SET status='accepted' WHERE id=?", (row["id"],))
        try:
            await ctx.editor.draft_from_text(row["text"], kind="suggestion", credit="прислал подписчик")
        except (LLMError, ValueError) as e:
            await ctx.db.execute("UPDATE suggestions SET status='new' WHERE id=?", (row["id"],))
            if callback.message:
                await callback.message.answer(f"Не получилось подготовить черновик: {esc(str(e))}")
            return
        note = "✅ Взято в работу"
        try:
            await ctx.bot.send_message(row["user_id"], "Спасибо! Редакция взяла вашу новость в работу 🙌")
        except TelegramAPIError:
            pass
    else:
        await ctx.db.execute("UPDATE suggestions SET status='rejected' WHERE id=?", (row["id"],))
        await callback.answer("Отклонено")
        note = "❌ Отклонено"
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.reply(note)
        except TelegramAPIError:
            pass


# ── ответы на служебные сообщения ────────────────────────────────────────────

@router.message(F.reply_to_message, ~F.text.startswith("/"))
async def on_admin_reply(message: Message, ctx: Ctx) -> None:
    """Ответ на запрос правки меняет текст поста; ответ на заявку пользователя уходит её автору."""
    target = message.reply_to_message
    assert target is not None
    raw_post = await ctx.db.kv_get(f"editprompt:{target.message_id}")
    if raw_post is not None:
        post = await ctx.db.post(int(raw_post))
        if post is None or post["status"] not in ("review", "queued"):
            await message.reply("Этот пост уже обработан, правка не применена.")
            return
        if not (message.text or message.caption):
            await message.reply("Нужен текст. Пришлите новый текст поста ответом на запрос.")
            return
        text = (message.html_text or "").strip()
        if visible_len(text) > 4096:
            await message.reply(f"Слишком длинно: {visible_len(text)} знаков при лимите 4096.")
            return
        meta = meta_of(post)
        meta["edited"] = True
        meta.pop("review_reason", None)
        await ctx.db.update_post(post["id"], text=text, meta=meta,
                                 headline=clip(re.sub(r"<[^>]+>", "", text).split("\n", 1)[0], 140))
        await ctx.db.kv_del(f"editprompt:{target.message_id}")
        await review.drop_card(ctx, post)
        if post["status"] == "queued":
            await ctx.db.update_post(post["id"], status="review")
        await review.send_card(ctx, post["id"])
        return

    relay = await ctx.db.one("SELECT user_id FROM relay WHERE admin_msg_id=?", (target.message_id,))
    if relay is not None:
        try:
            if message.text:
                await ctx.bot.send_message(relay["user_id"], f"💬 <b>Ответ редакции</b>\n\n{message.html_text}")
            else:
                await ctx.bot.copy_message(relay["user_id"], message.chat.id, message.message_id)
            await message.reply("Отправлено ✓")
        except TelegramAPIError as e:
            await message.reply(f"Не доставлено: {esc(str(e))}. Возможно, пользователь заблокировал бота.")
