"""Telegram Bot API client (raw HTTP, no framework) + Telegram-HTML sanitizer."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from html.parser import HTMLParser

import httpx

log = logging.getLogger("tgnet.tg")

ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre",
           "blockquote", "tg-spoiler", "span"}


class TelegramError(Exception):
    def __init__(self, method: str, code: int, desc: str):
        super().__init__(f"{method}: {code} {desc}")
        self.code, self.desc = code, desc


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "br":
            self.out.append("\n")
            return
        if tag == "p":
            if self.out and not "".join(self.out[-1:]).endswith("\n"):
                self.out.append("\n")
            return
        if tag == "li":
            self.out.append("\n• ")
            return
        if tag not in ALLOWED:
            return
        a = dict(attrs)
        if tag == "a":
            href = (a.get("href") or "").strip()
            if not href.startswith(("http://", "https://", "tg://")):
                return
            self.out.append(f'<a href="{html.escape(href, quote=True)}">')
        elif tag == "span":
            if a.get("class") != "tg-spoiler":
                return
            self.out.append('<span class="tg-spoiler">')
        elif tag == "blockquote" and "expandable" in a:
            self.out.append("<blockquote expandable>")
        else:
            self.out.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "p":
            self.out.append("\n\n")
            return
        if tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        text = "".join(self.out)
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")
        return text.strip()


def sanitize(text: str) -> str:
    """Keep only tags Telegram accepts, close unbalanced ones, escape the rest."""
    p = _Sanitizer()
    p.feed(text or "")
    p.close()
    return p.result()


def plain(text: str) -> str:
    """Telegram HTML -> the plain text a reader sees."""
    p = HTMLParser(convert_charrefs=True)
    buf: list[str] = []
    p.handle_data = buf.append  # type: ignore[assignment]
    p.feed(text or "")
    p.close()
    return "".join(buf)


def visible_len(text: str) -> int:
    """Length Telegram counts against the 4096/1024 limits (entities excluded)."""
    return len(plain(text))


class Bot:
    """Thin async Bot API client with flood-control handling and per-chat pacing."""

    def __init__(self, token: str, http: httpx.AsyncClient, base: str = "https://api.telegram.org"):
        self.token, self.http = token, http
        self.base = f"{base}/bot{token}"
        self._last: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.me: dict | None = None
        self.sleep = asyncio.sleep

    @property
    def bot_id(self) -> str:
        return self.token.split(":")[0]

    async def call(self, method: str, timeout: float = 30, **params):
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(5):
            try:
                r = await self.http.post(f"{self.base}/{method}", json=params, timeout=timeout + 10)
            except httpx.HTTPError as e:
                if attempt == 4:
                    raise TelegramError(method, 0, f"transport: {e!r}") from e
                await self.sleep(min(30, 2 ** attempt))
                continue
            try:
                data = r.json()
            except ValueError:
                data = {"ok": False, "error_code": r.status_code, "description": r.text[:200]}
            if data.get("ok"):
                return data.get("result")
            code = data.get("error_code", r.status_code)
            desc = data.get("description", "")
            if code == 429:
                wait = (data.get("parameters") or {}).get("retry_after", 5)
                log.warning("telegram flood control on %s, sleeping %ss", method, wait)
                await self.sleep(wait + 1)
                continue
            if code >= 500 and attempt < 4:
                await self.sleep(min(30, 2 ** attempt))
                continue
            raise TelegramError(method, code, desc)
        raise TelegramError(method, 429, "flood control retries exhausted")

    async def _pace(self, chat_id, gap: float = 3.0):
        """Telegram allows ~20 msgs/min per group; keep channels and groups comfortably below."""
        key = str(chat_id)
        async with self._lock:
            wait = self._last.get(key, 0) + gap - time.monotonic()
            if wait > 0:
                await self.sleep(wait)
            self._last[key] = time.monotonic()

    async def get_me(self):
        self.me = await self.call("getMe")
        return self.me

    async def send_message(self, chat_id, text: str, reply_markup=None, pace: bool = True, preview: bool = False,
                           reply_to: int | None = None):
        if pace:
            await self._pace(chat_id)
        kw = dict(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=reply_markup,
                  link_preview_options={"is_disabled": not preview})
        if reply_to:
            kw["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        try:
            return await self.call("sendMessage", **kw)
        except TelegramError as e:
            if e.code == 400 and "parse" in e.desc.lower():
                kw.pop("parse_mode")
                kw["text"] = plain(text)
                return await self.call("sendMessage", **kw)
            raise

    async def send_photo(self, chat_id, photo: str, caption: str, reply_markup=None):
        await self._pace(chat_id)
        return await self.call("sendPhoto", chat_id=chat_id, photo=photo, caption=caption, parse_mode="HTML",
                               reply_markup=reply_markup)

    async def edit_text(self, chat_id, message_id: int, text: str, reply_markup=None):
        try:
            return await self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                                   parse_mode="HTML", reply_markup=reply_markup,
                                   link_preview_options={"is_disabled": True})
        except TelegramError as e:
            if "not modified" in e.desc:
                return None
            raise

    async def answer_callback(self, cb_id: str, text: str = ""):
        try:
            return await self.call("answerCallbackQuery", callback_query_id=cb_id, text=text[:190])
        except TelegramError:
            return None

    async def delete_message(self, chat_id, message_id: int) -> bool:
        try:
            await self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
            return True
        except TelegramError as e:
            log.info("delete failed in %s: %s", chat_id, e)
            return False

    async def restrict(self, chat_id, user_id: int, until: int):
        perms = {k: False for k in ("can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
                                    "can_send_videos", "can_send_video_notes", "can_send_voice_notes",
                                    "can_send_polls", "can_send_other_messages", "can_add_web_page_previews")}
        return await self.call("restrictChatMember", chat_id=chat_id, user_id=user_id, permissions=perms,
                               until_date=until, use_independent_chat_permissions=True)

    async def ban(self, chat_id, user_id: int):
        return await self.call("banChatMember", chat_id=chat_id, user_id=user_id, revoke_messages=True)

    async def pin(self, chat_id, message_id: int):
        return await self.call("pinChatMessage", chat_id=chat_id, message_id=message_id, disable_notification=True)

    async def admins(self, chat_id) -> list[dict]:
        return await self.call("getChatAdministrators", chat_id=chat_id)

    async def get_chat(self, chat_id):
        return await self.call("getChat", chat_id=chat_id)

    async def updates(self, offset: int, timeout: int = 50) -> list[dict]:
        return await self.call("getUpdates", timeout=timeout, offset=offset,
                               allowed_updates=["message", "edited_message", "callback_query",
                                                "message_reaction_count"])


def kb(rows: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}
