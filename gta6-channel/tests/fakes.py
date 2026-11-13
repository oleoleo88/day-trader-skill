"""Тестовые двойники: сервер Telegram Bot API, сервер Anthropic API и подставной ИИ.

Бот в тестах — настоящий aiogram.Bot, который ходит по HTTP на локальный фейковый сервер.
Так проверяются и наша логика, и то, что запросы к Telegram собираются корректно.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from aiohttp import web

BOT_ID = 4242
BOT_USERNAME = "gta6_test_bot"
CHANNEL_ID = -1001000000001
CHANNEL_USERNAME = "gta6_test"
DISCUSSION_ID = -1002000000002
ADMIN_ID = 111
ADMIN_CHAT_ID = 111          # админ-чат — личка владельца
TOKEN = "4242:TEST-TOKEN"

BOT_USER = {"id": BOT_ID, "is_bot": True, "first_name": "GTA6 Bot", "username": BOT_USERNAME}
GIFTS = {"unlimited_gifts": True, "limited_gifts": True, "unique_gifts": True, "premium_subscription": True,
         "gifts_from_channels": True}
ADMIN_RIGHTS = {
    "status": "administrator", "user": BOT_USER, "can_be_edited": False, "is_anonymous": False,
    "can_manage_chat": True, "can_delete_messages": True, "can_manage_video_chats": False,
    "can_restrict_members": True, "can_promote_members": False, "can_change_info": False,
    "can_invite_users": True, "can_post_stories": False, "can_edit_stories": False, "can_delete_stories": False,
    "can_send_welcome_messages": False, "can_post_messages": True, "can_edit_messages": True,
}


class TelegramError(Exception):
    def __init__(self, code: int, description: str, retry_after: int | None = None):
        self.code, self.description, self.retry_after = code, description, retry_after


class FakeTelegram:
    """Минимальный Bot API: записывает все вызовы и отвечает правдоподобными объектами."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.next_message_id = 1000
        self.fail: dict[str, Callable[[dict], TelegramError | None] | TelegramError] = {}
        self.chats: dict[Any, dict] = {
            CHANNEL_ID: {"id": CHANNEL_ID, "type": "channel", "title": "GTA 6 Тест", "username": CHANNEL_USERNAME,
                         "linked_chat_id": DISCUSSION_ID},
            "@" + CHANNEL_USERNAME: {"id": CHANNEL_ID, "type": "channel", "title": "GTA 6 Тест",
                                     "username": CHANNEL_USERNAME, "linked_chat_id": DISCUSSION_ID},
            DISCUSSION_ID: {"id": DISCUSSION_ID, "type": "supergroup", "title": "GTA 6 Чат"},
            ADMIN_CHAT_ID: {"id": ADMIN_CHAT_ID, "type": "private", "first_name": "Владелец"},
        }
        self.group_admins: set[int] = {BOT_ID, ADMIN_ID}
        self.member_count = 1234
        self.runner: web.AppRunner | None = None
        self.base = ""

    # ── жизненный цикл ──────────────────────────────────────────────────────
    async def start(self) -> str:
        app = web.Application(client_max_size=50 * 1024 * 1024)
        app.router.add_post("/bot{token}/{method}", self._handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        self.base = f"http://127.0.0.1:{port}"
        return self.base

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    # ── удобства для проверок ───────────────────────────────────────────────
    def of(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == method]

    def last(self, method: str) -> dict[str, Any]:
        found = self.of(method)
        assert found, f"вызова {method} не было; были: {[m for m, _ in self.calls]}"
        return found[-1]

    def sent_to(self, chat_id: int) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m in ("sendMessage", "sendPhoto", "sendPoll", "copyMessage")
                and p.get("chat_id") == chat_id]

    def texts_to(self, chat_id: int) -> list[str]:
        return [p.get("text") or p.get("caption") or p.get("question") or "" for p in self.sent_to(chat_id)]

    def clear(self) -> None:
        self.calls.clear()

    # ── обработка запроса ───────────────────────────────────────────────────
    async def _handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        params: dict[str, Any] = {}
        if request.content_type == "application/json":
            params = await request.json()
        else:
            form = await request.post()
            for key, value in form.items():
                if isinstance(value, web.FileField):
                    params[key] = {"__file__": value.filename, "size": len(value.file.read())}
                    continue
                try:
                    params[key] = json.loads(value)
                except (ValueError, TypeError):
                    params[key] = value
        self.calls.append((method, params))
        failure = self.fail.get(method)
        error = failure(params) if callable(failure) else failure
        if error is not None:
            body: dict[str, Any] = {"ok": False, "error_code": error.code, "description": error.description}
            if error.retry_after:
                body["parameters"] = {"retry_after": error.retry_after}
            return web.json_response(body, status=error.code)
        handler = getattr(self, "m_" + method, None)
        result = handler(params) if handler else True
        return web.json_response({"ok": True, "result": result})

    def _message(self, p: dict, **extra: Any) -> dict:
        self.next_message_id += 1
        chat_id = p["chat_id"]
        chat = self.chats.get(chat_id) or {"id": chat_id, "type": "private" if isinstance(chat_id, int) and chat_id > 0 else "supergroup"}
        msg = {"message_id": self.next_message_id, "date": int(time.time()),
               "chat": {k: chat[k] for k in ("id", "type", "title", "username") if k in chat}, "from": BOT_USER}
        msg.update(extra)
        return msg

    # ── методы ──────────────────────────────────────────────────────────────
    def m_getMe(self, p: dict) -> dict:
        return {**BOT_USER, "can_join_groups": True, "can_read_all_group_messages": True, "supports_inline_queries": False}

    def m_getChat(self, p: dict) -> dict:
        chat = self.chats.get(p["chat_id"])
        if chat is None:
            raise_error = TelegramError(400, "Bad Request: chat not found")
            raise web.HTTPBadRequest(text=json.dumps({"ok": False, "error_code": 400, "description": raise_error.description}),
                                     content_type="application/json")
        return {"accent_color_id": 0, "max_reaction_count": 11, "accepted_gift_types": GIFTS, **chat}

    def m_getChatMember(self, p: dict) -> dict:
        user_id = p["user_id"]
        if user_id == BOT_ID:
            return dict(ADMIN_RIGHTS)
        if user_id in self.group_admins:
            return {**ADMIN_RIGHTS, "user": {"id": user_id, "is_bot": False, "first_name": "Админ"}}
        return {"status": "member", "user": {"id": user_id, "is_bot": False, "first_name": "Участник"}}

    def m_getChatAdministrators(self, p: dict) -> list:
        return [{**ADMIN_RIGHTS, "user": {"id": uid, "is_bot": uid == BOT_ID, "first_name": "Админ"}} for uid in sorted(self.group_admins)]

    def m_getChatMemberCount(self, p: dict) -> int:
        return self.member_count

    def m_sendMessage(self, p: dict) -> dict:
        return self._message(p, text=p.get("text", ""))

    def m_sendPhoto(self, p: dict) -> dict:
        return self._message(p, caption=p.get("caption", ""), photo=[
            {"file_id": "photo-file", "file_unique_id": "u1", "width": 1280, "height": 720}])

    def m_sendPoll(self, p: dict) -> dict:
        options = [{"persistent_id": str(i), "text": o["text"], "voter_count": 0} for i, o in enumerate(p["options"])]
        return self._message(p, poll={
            "id": "poll-1", "question": p["question"], "options": options, "total_voter_count": 0, "is_closed": False,
            "is_anonymous": True, "type": p.get("type", "regular"), "allows_multiple_answers": False,
            "allows_revoting": True, "members_only": False})

    def m_copyMessage(self, p: dict) -> dict:
        self.next_message_id += 1
        return {"message_id": self.next_message_id}

    def m_editMessageText(self, p: dict) -> dict:
        return {"message_id": p.get("message_id", 0), "date": int(time.time()),
                "chat": {"id": p.get("chat_id", 0), "type": "private"}, "text": p.get("text", "")}

    def m_editMessageReplyMarkup(self, p: dict) -> dict:
        return {"message_id": p.get("message_id", 0), "date": int(time.time()), "chat": {"id": p.get("chat_id", 0), "type": "private"}}

    def m_createChatInviteLink(self, p: dict) -> dict:
        return {"invite_link": f"https://t.me/+{p.get('name', 'link').replace(' ', '_')}", "creator": BOT_USER,
                "creates_join_request": False, "is_primary": False, "is_revoked": False, "name": p.get("name")}


# ── подставной ИИ ────────────────────────────────────────────────────────────

class FakeLLM:
    """Вместо Claude: на каждую схему ответа — заранее заданный объект или функция от текста запроса."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responders: dict[str, Any] = {}

    def on(self, schema: type, response: Any) -> None:
        self.responders[schema.__name__] = response

    def count(self, role: str) -> int:
        return sum(1 for c in self.calls if c["role"] == role)

    def last(self, role: str) -> dict[str, Any]:
        found = [c for c in self.calls if c["role"] == role]
        assert found, f"обращений к роли {role} не было"
        return found[-1]

    async def structured(self, role: str, *, system: str, user: str, schema: type, max_tokens: int = 4000) -> Any:
        self.calls.append({"role": role, "system": system, "user": user, "schema": schema.__name__})
        responder = self.responders.get(schema.__name__)
        if responder is None:
            raise AssertionError(f"FakeLLM: нет ответа для схемы {schema.__name__}")
        result = responder(user) if callable(responder) else responder
        if isinstance(result, Exception):
            raise result
        return result.model_copy(deep=True)

    async def check_models(self) -> list:
        return []


# ── фейковый Anthropic API ───────────────────────────────────────────────────

class FakeAnthropic:
    """Сервер, принимающий POST /v1/messages и отдающий заданные ответы; запоминает тела и заголовки запросов."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.queue: list[Any] = []       # очередь ответов: dict (тело 200) или (status, dict)
        self.runner: web.AppRunner | None = None
        self.base = ""

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/v1/messages", self._messages)
        app.router.add_get("/v1/models/{model}", self._model)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        self.base = f"http://127.0.0.1:{port}"
        return self.base

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    @staticmethod
    def message(payload: dict | None, *, model: str = "claude-opus-5-5", stop_reason: str = "end_turn",
                usage: dict | None = None, stop_details: dict | None = None) -> dict:
        content: list[dict] = [{"type": "thinking", "thinking": "", "signature": "sig"}]
        if payload is not None:
            content.append({"type": "text", "text": json.dumps(payload, ensure_ascii=False)})
        body = {"id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": content,
                "stop_reason": stop_reason, "stop_sequence": None,
                "usage": usage or {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 900}}
        if stop_details is not None:
            body["stop_details"] = stop_details
        return body

    async def _messages(self, request: web.Request) -> web.Response:
        self.requests.append(await request.json())
        self.headers.append({k.lower(): v for k, v in request.headers.items()})
        assert self.queue, "FakeAnthropic: очередь ответов пуста"
        item = self.queue.pop(0)
        if isinstance(item, tuple):
            status, body = item
            return web.json_response(body, status=status)
        return web.json_response(item)

    async def _model(self, request: web.Request) -> web.Response:
        model = request.match_info["model"]
        if model.startswith("claude-"):
            return web.json_response({"id": model, "type": "model", "display_name": model, "created_at": "2026-01-01T00:00:00Z"})
        return web.json_response({"type": "error", "error": {"type": "not_found_error", "message": "model not found"}}, status=404)
