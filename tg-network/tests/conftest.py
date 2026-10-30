"""Test harness: a full App wired to an in-memory DB, a scripted LLM and a fake Telegram/web."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from email.utils import formatdate
from pathlib import Path

import httpx
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tgnet import config as C  # noqa: E402
from tgnet.app import App  # noqa: E402
from tgnet.db import DB  # noqa: E402
from tgnet.llm import LLMError, LLMResult, Router  # noqa: E402

OWNER = 777
FIX = Path(__file__).resolve().parent / "fixtures"


def run(coro):
    return asyncio.run(coro)


def rss(items: list[dict]) -> str:
    out = ['<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>']
    for it in items:
        ts = it.get("ts", time.time())
        out.append(f"<item><title>{it['title']}</title><link>{it['link']}</link>"
                   f"<description>{it.get('desc', '')}</description><pubDate>{formatdate(ts)}</pubDate></item>")
    out.append("</channel></rss>")
    return "".join(out)


class FakeWeb:
    """httpx transport: Telegram Bot API + arbitrary URLs from a dict."""

    def __init__(self):
        self.pages: dict[str, str] = {}
        self.tg_calls: list[tuple[str, dict]] = []
        self.msg_id = 1000
        self.tg_errors: dict[str, dict] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        m = re.match(r"https://api\.telegram\.org/bot[^/]+/(\w+)", url)
        if m:
            method = m.group(1)
            body = json.loads(request.content or b"{}")
            self.tg_calls.append((method, body))
            if method in self.tg_errors:
                return httpx.Response(200, json=self.tg_errors[method])
            if method in ("sendMessage", "sendPhoto"):
                self.msg_id += 1
                return httpx.Response(200, json={"ok": True, "result": {"message_id": self.msg_id}})
            if method == "getMe":
                return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "testbot"}})
            if method == "getChatAdministrators":
                return httpx.Response(200, json={"ok": True, "result": [{"user": {"id": 5}}]})
            if method == "getUpdates":
                return httpx.Response(200, json={"ok": True, "result": []})
            return httpx.Response(200, json={"ok": True, "result": True})
        for prefix, body in self.pages.items():
            if url.startswith(prefix):
                return httpx.Response(200, text=body)
        return httpx.Response(404, text="not found")

    def calls(self, method: str) -> list[dict]:
        return [b for m, b in self.tg_calls if m == method]


class FakeLLM:
    """Provider stub. `script` maps a marker found in the system prompt to a reply (str/dict/callable)."""

    def __init__(self, script: dict | None = None, fail: int = 0):
        self.script = script or {}
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def complete(self, model, system, user, *, json_mode, temperature, max_tokens, grounding):
        self.calls.append((system, user))
        if self.fail > 0:
            self.fail -= 1
            raise LLMError("boom", retryable=True)
        for marker, reply in self.script.items():
            if marker in system or marker in user:
                if callable(reply):
                    reply = reply(system, user)
                text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
                return LLMResult(text, model, "fake", 100, 50)
        return LLMResult('{"ok": true}', model, "fake", 10, 5)


def write_config(base: Path, channel_overrides: dict | None = None, sources: list[dict] | None = None,
                 network_overrides: dict | None = None) -> C.Network:
    (base / "config" / "channels").mkdir(parents=True, exist_ok=True)
    (base / "config" / "sources").mkdir(parents=True, exist_ok=True)
    net = {
        "owner_chat_id": OWNER, "bot_token": "1:TOKEN", "timezone": "UTC", "db_path": ":memory:",
        "health_port": 0,
        "llm": {"daily_budget_usd": 5, "retries": 1,
                "providers": {"fake": {"kind": "openai", "api_key": "x"}, "fake2": {"kind": "openai", "api_key": "y"}},
                "models": {"m1": {"provider": "fake", "model": "m1", "price_in": 1, "price_out": 1},
                           "m2": {"provider": "fake2", "model": "m2"}},
                "roles": {"default": ["m1", "m2"]}},
    }
    net.update(network_overrides or {})
    ch = {
        "id": "football_ru", "title": "Футбол", "chat_id": "@football", "username": "football",
        "discussion_chat_id": "-100500", "packs": ["football"], "autonomy": "approve",
        "focus": "топ-футбол", "voice": "живой", "format_rules": "коротко",
        "schedule": {"quiet_hours": None, "min_interval_min": 0, "max_posts_per_day": 50},
        "verification": {"use_grounding": False},
    }
    ch.update(channel_overrides or {})
    src = {"pack": "football", "sources": sources or [
        {"id": "bbc", "name": "BBC Sport", "kind": "rss", "url": "https://bbc.test/rss", "tier": 2, "group": "bbc"},
        {"id": "guardian", "name": "The Guardian", "kind": "rss", "url": "https://guardian.test/rss", "tier": 2,
         "group": "guardian"},
        {"id": "insider", "name": "Insider", "kind": "rss", "url": "https://insider.test/rss", "tier": 3,
         "group": "insider"},
    ], "trusted_domains": {"arsenal.com": 1}}
    (base / "config" / "network.yaml").write_text(yaml.safe_dump(net, allow_unicode=True), encoding="utf-8")
    (base / "config" / "channels" / "football_ru.yaml").write_text(yaml.safe_dump(ch, allow_unicode=True), encoding="utf-8")
    (base / "config" / "sources" / "football.yaml").write_text(yaml.safe_dump(src, allow_unicode=True), encoding="utf-8")
    return C.load(base)


class Harness:
    def __init__(self, tmp_path: Path, script: dict | None = None, **cfg_kw):
        self.cfg = write_config(tmp_path, **cfg_kw)
        self.web = FakeWeb()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.web.handler))
        self.db = DB(":memory:")
        self.llm = FakeLLM(script)
        self.router = Router(self.cfg.llm, self.db, self.http, "UTC", providers={"fake": self.llm, "fake2": self.llm})

        async def nosleep(_):
            return None

        self.router.sleep = nosleep
        self.app = App(self.cfg, db=self.db, http=self.http, router=self.router)
        for b in self.app.bots.values():
            b.sleep = nosleep


@pytest.fixture
def harness(tmp_path):
    def make(script=None, **kw):
        return Harness(tmp_path, script, **kw)
    return make
