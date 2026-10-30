"""Owner control panel in Telegram + update dispatching (approvals, commands, moderation, reactions)."""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

from .agents.hr import TUNABLE
from .agents.learner import ROLES, lesson_list
from .telegram import Bot, TelegramError, sanitize
from .util import clip, iso, now

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("tgnet.control")

HELP = """<b>Команды</b>
/status – состояние сети и очередей
/report – отчёт за сегодня
/queue – повторить черновики, ждущие одобрения
/pause [канал|all] · /resume [канал|all]
/autopilot &lt;канал&gt; on|off – автопубликация подтверждённых новостей
/teach &lt;канал|all&gt; &lt;роль&gt; &lt;правило&gt; – добавить правило (роли: triage, factcheck, writer, reviewer, moderator)
/lessons [канал] – чему научились агенты · /forget &lt;id&gt; – удалить урок
/error &lt;id поста&gt; &lt;что не так&gt; – сообщить о фактической ошибке (агенты учтут, источник теряет доверие)
/hr – HR-агент сейчас проанализирует сеть и предложит изменения
/agents – агенты-специалисты · /retire &lt;имя&gt;
/set &lt;канал&gt; &lt;параметр&gt; &lt;значение&gt; – настройка (triage_threshold, schedule.min_interval_min, …)
/sources – здоровье источников
/cancel – отменить ожидание правки/причины"""


def entities_to_html(text: str, entities: list[dict] | None) -> str:
    """Telegram message entities (UTF-16 offsets) -> Telegram HTML, so owner formatting survives edits."""
    if not entities:
        return html.escape(text, quote=False)
    u16 = text.encode("utf-16-le")
    n = len(u16) // 2
    tags = {"bold": ("<b>", "</b>"), "italic": ("<i>", "</i>"), "underline": ("<u>", "</u>"),
            "strikethrough": ("<s>", "</s>"), "spoiler": ("<tg-spoiler>", "</tg-spoiler>"),
            "code": ("<code>", "</code>"), "pre": ("<pre>", "</pre>"), "blockquote": ("<blockquote>", "</blockquote>"),
            "expandable_blockquote": ("<blockquote expandable>", "</blockquote>")}
    opens: dict[int, list] = {}
    closes: dict[int, list] = {}
    for idx, e in enumerate(entities):
        t = e.get("type")
        if t == "text_link":
            o, c = f'<a href="{html.escape(e.get("url", ""), quote=True)}">', "</a>"
        elif t in tags:
            o, c = tags[t]
        else:
            continue
        s, ln = int(e["offset"]), int(e["length"])
        if ln <= 0 or s + ln > n:
            continue
        opens.setdefault(s, []).append((-ln, idx, o))  # outer (longer) first
        closes.setdefault(s + ln, []).append((ln, -idx, c))  # inner (shorter) first
    bounds = sorted({0, n, *opens, *closes})
    out = []
    for a, b in zip(bounds, bounds[1:] + [None]):
        out.extend(c for *_, c in sorted(closes.get(a, [])))
        out.extend(o for *_, o in sorted(opens.get(a, [])))
        if b is not None:
            out.append(html.escape(u16[a * 2:b * 2].decode("utf-16-le", errors="ignore"), quote=False))
    return "".join(out)


class Control:
    def __init__(self, app: "App"):
        self.app, self.db, self.cfg = app, app.db, app.cfg

    @property
    def owner(self) -> int:
        return self.cfg.owner_chat_id

    async def poll(self, bot: Bot) -> None:
        key = f"tg_offset:{bot.bot_id}"
        offset = int(self.db.get(key, 0) or 0)
        ups = await bot.updates(offset, timeout=50)
        for u in ups:
            try:
                await self.dispatch(bot, u)
            except Exception:  # noqa: BLE001 - one bad update must not stall the queue
                log.exception("update %s failed", u.get("update_id"))
            self.db.set(key, u["update_id"] + 1)

    async def dispatch(self, bot: Bot, u: dict) -> None:
        if "callback_query" in u:
            await self.on_callback(bot, u["callback_query"])
            return
        if "message_reaction_count" in u:
            r = u["message_reaction_count"]
            total = sum(x.get("total_count", 0) for x in r.get("reactions", []))
            self.app.analyst.record_reactions(r["chat"]["id"], r["message_id"], total)
            for ch in self.app.active_channels():
                if str(r["chat"]["id"]) == ch.chat_id or (r["chat"].get("username") or "").lower() == (ch.username or "").lower():
                    self.app.analyst.record_reactions(ch.chat_id, r["message_id"], total)
            return
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            return
        chat = msg.get("chat") or {}
        if chat.get("type") == "private":
            if (msg.get("from") or {}).get("id") == self.owner and bot is self.app.control_bot and "message" in u:
                await self.on_owner(bot, msg)
            return
        ch = self.app.channel_by_discussion(chat)
        if ch is not None and "message" in u:
            await self.app.moderator.handle(bot, ch, msg)

    # ------------------------------------------------------------------ callbacks
    async def on_callback(self, bot: Bot, cb: dict) -> None:
        if (cb.get("from") or {}).get("id") != self.owner:
            await bot.answer_callback(cb["id"], "Нет доступа")
            return
        action, _, arg = (cb.get("data") or "").partition(":")
        msg_id = (cb.get("message") or {}).get("message_id")
        if action in ("ap", "now", "ed", "rj"):
            await self.on_post_action(bot, cb, action, int(arg), msg_id)
        elif action == "auto":
            self.db.set(f"autonomy:{arg}", "auto")
            await bot.answer_callback(cb["id"], "Автопилот включён")
            await self.app.mark_owner_msg(msg_id, f"🚀 Автопилот для {arg} включён. Выключить: /autopilot {arg} off")
        elif action == "hr_ok":
            result = await self.app.hr.apply(int(arg))
            await bot.answer_callback(cb["id"], result)
            await self.app.mark_owner_msg(msg_id, f"✅ {html.escape(result)}")
        elif action == "hr_no":
            self.app.hr.reject(int(arg))
            await bot.answer_callback(cb["id"], "Отклонено")
            await self.app.mark_owner_msg(msg_id, "❌ Предложение отклонено")
        else:
            await bot.answer_callback(cb["id"])

    async def on_post_action(self, bot: Bot, cb: dict, action: str, pid: int, msg_id: int | None) -> None:
        p = self.db.post(pid)
        if p is None or p["status"] != "awaiting":
            await bot.answer_callback(cb["id"], f"Пост уже обработан ({p['status'] if p else 'нет'})")
            return
        if action == "ap":
            self.db.advance(pid, "scheduled", scheduled_at=now(), owner_action="approved")
            await bot.answer_callback(cb["id"], "В очереди на публикацию")
            await self.app.mark_owner_msg(msg_id, f"✅ #{pid} в очереди")
            await self.app.learner.maybe_suggest_autopilot(p["channel_id"])
        elif action == "now":
            self.db.advance(pid, "scheduled", scheduled_at=now(), owner_action="approved_now")
            await bot.answer_callback(cb["id"], "Публикую")
            await self.app.mark_owner_msg(msg_id, f"⚡ #{pid} публикуется вне очереди")
        elif action == "ed":
            self.db.set("pending:edit", pid)
            self.db.update_post(pid, expires_at=now() + 3600 * 3)
            await bot.answer_callback(cb["id"], "Жду исправленный текст")
            await bot.send_message(self.owner, f"✏️ Пришли исправленный текст поста #{pid} целиком одним сообщением "
                                               f"(форматирование сохранится). /cancel – отмена.", pace=False,
                                   reply_to=msg_id)
        elif action == "rj":
            self.db.advance(pid, "rejected", owner_action="rejected", owner_note="отклонён владельцем")
            self.db.set("pending:reason", pid)
            await bot.answer_callback(cb["id"], "Отклонено")
            await self.app.mark_owner_msg(msg_id, f"🗑 #{pid} отклонён. Почему? Ответь одним сообщением – агенты "
                                                  f"учтут. /skip – без причины.")

    # ------------------------------------------------------------------ owner messages
    async def on_owner(self, bot: Bot, msg: dict) -> None:
        text = msg.get("text") or msg.get("caption") or ""
        if text.startswith("/"):
            await self.command(bot, text)
            return
        pe = self.db.get("pending:edit")
        if pe:
            await self.apply_edit(bot, int(pe), msg)
            return
        pr = self.db.get("pending:reason")
        if pr:
            self.db.delete("pending:reason")
            self.db.update_post(int(pr), owner_note=f"отклонён: {text}"[:500])
            self.app.spawn(self.learn_reject(bot, int(pr), text))
            await bot.send_message(self.owner, "Понял, разбираю, что учесть 👍", pace=False)
            return
        await bot.send_message(self.owner, HELP, pace=False)

    async def learn_reject(self, bot: Bot, pid: int, reason: str) -> None:
        ids = await self.app.learner.learn_from_reject(pid, reason)
        if ids:
            await bot.send_message(self.owner, f"🎓 Новых уроков: {len(ids)} (/lessons)", pace=False)

    async def apply_edit(self, bot: Bot, pid: int, msg: dict) -> None:
        self.db.delete("pending:edit")
        p = self.db.post(pid)
        if p is None or p["status"] != "awaiting":
            await bot.send_message(self.owner, f"Пост #{pid} уже не ждёт правки.", pace=False)
            return
        raw = msg.get("text") or msg.get("caption") or ""
        ents = msg.get("entities") or msg.get("caption_entities")
        new = entities_to_html(raw, ents) if ents else sanitize(raw)
        ch = self.app.channel(p["channel_id"])
        if ch and ch.signature and new.rstrip().endswith(sanitize(ch.signature).strip()):
            new = new.rstrip()[: -len(sanitize(ch.signature).strip())].rstrip()
        draft = p["final"] or p["draft"] or ""
        self.db.advance(pid, "scheduled", final=new, scheduled_at=now(), owner_action="edited")
        await bot.send_message(self.owner, f"✅ #{pid} с твоей правкой поставлен в очередь. Учусь на разнице…", pace=False)
        self.app.spawn(self.learn_edit(bot, pid, draft, new))

    async def learn_edit(self, bot: Bot, pid: int, draft: str, final: str) -> None:
        ids = await self.app.learner.learn_from_edit(pid, draft, final)
        if ids:
            rows = self.db.all(f"SELECT role, text FROM lessons WHERE id IN ({','.join('?' * len(ids))})", ids)
            await bot.send_message(self.owner, "🎓 Запомнил:\n" + "\n".join(
                f"• [{r['role']}] {html.escape(r['text'])}" for r in rows), pace=False)

    async def command(self, bot: Bot, text: str) -> None:
        parts = text.strip().split(maxsplit=3)
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        reply = await self.run_command(cmd, args, text)
        if reply:
            for chunk in _chunks(reply, 3900):
                await bot.send_message(self.owner, chunk, pace=False)

    def _channel_arg(self, args: list[str]) -> list[str] | None:
        if not args or args[0] == "all":
            return list(self.cfg.channels)
        return [args[0]] if args[0] in self.cfg.channels else None

    async def run_command(self, cmd: str, args: list[str], raw: str) -> str:
        app = self.app
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/status":
            return self.status()
        if cmd == "/report":
            return app.analyst.report()
        if cmd in ("/pause", "/resume"):
            val = cmd == "/pause"
            if not args or args[0] == "all":
                self.db.set("paused:all", val)
                if not val:
                    for c in self.cfg.channels:
                        self.db.set(f"paused:{c}", False)
                return "⏸ Вся сеть на паузе" if val else "▶️ Сеть работает"
            if args[0] not in self.cfg.channels:
                return "Нет такого канала: " + ", ".join(self.cfg.channels)
            self.db.set(f"paused:{args[0]}", val)
            return f"{'⏸' if val else '▶️'} {args[0]}"
        if cmd == "/autopilot":
            if len(args) < 2 or args[0] not in self.cfg.channels or args[1] not in ("on", "off"):
                return "Формат: /autopilot <канал> on|off"
            self.db.set(f"autonomy:{args[0]}", "auto" if args[1] == "on" else "approve")
            return f"Режим {args[0]}: {'автопилот' if args[1] == 'on' else 'одобрение владельцем'}"
        if cmd == "/teach":
            if len(args) < 3 or args[1] not in ROLES:
                return "Формат: /teach <канал|all> <роль> <правило>\nРоли: " + ", ".join(ROLES)
            chs = self._channel_arg(args[:1])
            if chs is None:
                return "Нет такого канала"
            rule = args[2]
            if args[0] == "all":
                lid = self.db.add_lesson(None, args[1], rule, "owner_rule", weight=5)
            else:
                lid = self.db.add_lesson(args[0], args[1], rule, "owner_rule", weight=5)
            return f"📌 Правило #{lid} добавлено для роли {args[1]}"
        if cmd == "/lessons":
            rows = lesson_list(self.db, args[0] if args else None)
            if not rows:
                return "Уроков пока нет"
            return "\n".join(f"#{r['id']} [{r['channel_id'] or 'все'}/{r['role']}] ({r['origin']}, вес {r['weight']:.1f}) "
                             f"{html.escape(r['text'])}" for r in rows[:60])
        if cmd == "/forget":
            if not args or not args[0].isdigit():
                return "Формат: /forget <id>"
            self.db.x("UPDATE lessons SET active=0 WHERE id=?", (int(args[0]),))
            return f"Урок #{args[0]} удалён"
        if cmd == "/error":
            if len(args) < 2 or not args[0].lstrip("#").isdigit():
                return "Формат: /error <id поста> <что не так>"
            pid = int(args[0].lstrip("#"))
            desc = raw.split(maxsplit=2)[2]
            self.app.spawn(self._learn_error(pid, desc))
            return f"Принято. Источники поста #{pid} теряют доверие, агенты разбирают ошибку."
        if cmd == "/hr":
            self.app.spawn(self._hr())
            return "🧑‍💼 HR-агент анализирует сеть, предложения придут сюда."
        if cmd == "/agents":
            rows = self.db.all("SELECT * FROM agents ORDER BY status, name")
            if not rows:
                return "Специалистов пока нет"
            return "\n".join(f"{'🟢' if r['status'] == 'active' else '⚪'} <b>{html.escape(r['name'])}</b> → {r['channel_id']} "
                             f"({r['origin']}, запусков {r['runs']}){' – ' + html.escape(r['notes']) if r['notes'] else ''}"
                             for r in rows)
        if cmd == "/retire":
            if not args:
                return "Формат: /retire <имя агента>"
            n = self.db.x("UPDATE agents SET status='retired' WHERE name=?", (args[0],)).rowcount
            return "Готово" if n else "Нет такого агента"
        if cmd == "/set":
            if len(args) < 3 or args[0] not in self.cfg.channels or args[1] not in TUNABLE:
                return "Формат: /set <канал> <параметр> <значение>\nПараметры: " + ", ".join(TUNABLE)
            lo, hi, typ = TUNABLE[args[1]]
            try:
                v = typ(args[2])
            except ValueError:
                return "Значение должно быть числом"
            if not lo <= v <= hi:
                return f"Допустимо {lo}..{hi}"
            app.set_setting(self.cfg.channels[args[0]], args[1], v)
            return f"{args[0]}.{args[1]} = {v}"
        if cmd == "/sources":
            return self.sources()
        if cmd == "/queue":
            rows = self.db.all("SELECT * FROM posts WHERE status='awaiting' ORDER BY id")
            for p in rows:
                ch = app.channel(p["channel_id"])
                if ch:
                    try:
                        await app.publisher.request_approval(ch, p)
                    except TelegramError as e:
                        log.warning("re-send approval failed: %s", e)
            return f"Повторил {len(rows)} черновиков" if rows else "Очередь одобрения пуста"
        if cmd in ("/cancel", "/skip"):
            self.db.delete("pending:edit")
            self.db.delete("pending:reason")
            return "Ок"
        return "Не знаю такой команды. /help"

    async def _learn_error(self, pid: int, desc: str) -> None:
        ids = await self.app.learner.learn_from_error(pid, desc)
        await self.app.notify(f"🎓 Разбор ошибки #{pid}: новых уроков {len(ids)} (/lessons)")

    async def _hr(self) -> None:
        n = await self.app.hr.run()
        if not n:
            await self.app.notify("🧑‍💼 HR: сейчас менять ничего не нужно.")

    def status(self) -> str:
        app, t = self.app, now()
        lines = ["🟢 <b>Сеть работает</b>" if not self.db.get("paused:all") else "⏸ <b>Сеть на паузе</b>"]
        for ch in app.active_channels():
            counts = {r["status"]: r["c"] for r in self.db.all(
                "SELECT status, COUNT(*) c FROM posts WHERE channel_id=? AND status NOT IN "
                "('published','rejected','expired') GROUP BY status", (ch.id,))}
            last = self.db.scalar("SELECT MAX(published_at) FROM posts WHERE channel_id=?", (ch.id,))
            pipe = " · ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "пусто"
            lines.append(f"\n<b>{html.escape(ch.title)}</b> [{ch.id}] {app.autonomy(ch)}{' ⏸' if app.paused(ch.id) else ''}\n"
                         f"Конвейер: {pipe}\nПоследний пост: {iso(last)}")
        loops = app.loop_ages()
        stale = [k for k, age in loops.items() if age > 900]
        lines.append(f"\n🤖 LLM сегодня: ${app.router.spent_today():.2f} / ${self.cfg.llm.daily_budget_usd:.2f}")
        lines.append("⚙️ Циклы: " + ("все живы" if not stale else "зависли: " + ", ".join(stale)))
        errs = self.db.all("SELECT ts, component, msg FROM events WHERE level='ERROR' AND ts>? ORDER BY ts DESC LIMIT 5",
                           (t - 86400,))
        if errs:
            lines.append("❗ Последние ошибки:\n" + "\n".join(
                f"• {iso(e['ts'])} {e['component']}: {html.escape(clip(e['msg'], 150))}" for e in errs))
        return "\n".join(lines)

    def sources(self) -> str:
        lines = []
        for s in self.app.all_sources():
            st = self.db.source_state(s.id)
            rel = st["alpha"] / (st["alpha"] + st["beta"])
            mark = "🟢" if st["failures"] == 0 and st["last_ok"] else ("🔴" if st["failures"] >= 3 else "🟡")
            lines.append(f"{mark} {s.id} T{self.app.effective_tier(s)} · надёжность {rel:.0%} · материалов {st['items_total']}"
                         + (f" · {html.escape(clip(st['last_error'] or '', 60))}" if st["failures"] else ""))
        return "\n".join(lines) or "Источников нет"


def _chunks(text: str, n: int) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > n and cur:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out
