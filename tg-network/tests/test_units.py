import json
import time

import httpx
import pytest

from conftest import FIX, run
from tgnet import config as C
from tgnet.config import LLMConfig, Model, Provider
from tgnet.control import entities_to_html
from tgnet.db import DB
from tgnet.llm import BudgetExceeded, GeminiProvider, LLMError, OpenAIProvider, Router
from tgnet.sources import parse_rss, parse_steam, parse_tme, parse_views
from tgnet.telegram import sanitize, visible_len
from tgnet.util import (canonical_url, extract_json, hamming, in_window, jaccard, numbers_in, simhash)


# --------------------------------------------------------------------------- util
def test_canonical_url_strips_tracking():
    assert canonical_url("https://www.BBC.co.uk/sport/1/?utm_source=x&id=5#top") == "https://bbc.co.uk/sport/1?id=5"


def test_near_duplicates_are_close():
    a = simhash("Arsenal sign Smith until 2029 in 50m deal")
    b = simhash("Arsenal sign Smith until 2029 in £50m deal")
    c = simhash("Real Madrid lose to Getafe in La Liga")
    assert hamming(a, b) < hamming(a, c)
    assert jaccard("Арсенал подписал Смита", "Смит подписал контракт с Арсеналом") > 0


@pytest.mark.parametrize("text", ['{"a": 1}', '```json\n{"a": 1}\n```', 'Вот ответ: {"a": 1} спасибо',
                                  '<think>hm {"x"</think>{"a": 1}'])
def test_extract_json(text):
    assert extract_json(text) == {"a": 1}


def test_numbers_and_windows():
    assert numbers_in("Счёт 2:1, 50,5 млн, 2029 год") == {"2:1", "50.5", "2029"}
    noon = 1767268800  # 2026-01-01 12:00 UTC
    assert in_window(noon, "11:00-13:00", "UTC")
    assert in_window(noon - 12 * 3600, "23:00-01:00", "UTC")
    assert not in_window(noon, "23:00-01:00", "UTC")


# --------------------------------------------------------------------------- telegram html
def test_sanitize_keeps_telegram_tags_only():
    out = sanitize('<p>Hi <b>bold <script>x</script></b> <a href="javascript:1">bad</a> '
                   '<a href="https://ok.ru">ok</a> 1 < 2</p><div>tail')
    assert "<script>" not in out and "javascript" not in out
    assert '<a href="https://ok.ru">ok</a>' in out
    assert "1 &lt; 2" in out
    assert "<b>bold x</b>" in out


def test_sanitize_closes_unbalanced():
    assert sanitize("<b>open <i>both") == "<b>open <i>both</i></b>"
    assert visible_len("<b>abc</b> &amp;") == 5


def test_entities_to_html_handles_emoji_offsets():
    text = "🔥 Гол! Месси"
    ents = [{"type": "bold", "offset": 3, "length": 4}, {"type": "text_link", "offset": 8, "length": 5,
                                                          "url": "https://x.y"}]
    assert entities_to_html(text, ents) == '🔥 <b>Гол!</b> <a href="https://x.y">Месси</a>'


# --------------------------------------------------------------------------- sources
def test_parse_rss():
    xml = ('<rss><channel><item><title>A &amp; B</title><link>https://x.test/1</link>'
           '<description>&lt;p&gt;text&lt;/p&gt;</description><pubDate>Thu, 29 Oct 2026 10:00:00 GMT</pubDate>'
           '<enclosure url="https://x.test/i.jpg" type="image/jpeg"/></item></channel></rss>')
    [e] = parse_rss(xml)
    assert e.title == "A & B" and e.summary == "text" and e.image == "https://x.test/i.jpg"
    assert e.published_at == 1793268000.0


def test_parse_tme_fixture():
    entries = parse_tme((FIX / "tme.html").read_text(encoding="utf-8"), "testchan")
    assert [e.message_id for e in entries] == [101, 102]
    assert entries[0].views == 12300 and entries[1].views == 987
    assert entries[0].title == "Официально: Смит – игрок «Арсенала»"
    assert "до 2029 года" in entries[0].summary
    assert entries[0].image == "https://cdn.test/p.jpg"


def test_parse_views():
    assert parse_views("1.2M") == 1_200_000 and parse_views("845") == 845


def test_parse_steam_keeps_official_only():
    data = {"appnews": {"newsitems": [
        {"title": "Release Notes", "url": "https://s.test/1", "contents": "[b]Fixed[/b] bugs", "date": 1,
         "feedname": "steam_community_announcements"},
        {"title": "Blog", "url": "https://s.test/2", "contents": "x", "date": 1, "feedname": "pcgamer"}]}}
    [e] = parse_steam(json.dumps(data))
    assert e.title == "Release Notes" and e.summary == "Fixed bugs"


# --------------------------------------------------------------------------- llm
def _cfg(**kw):
    return LLMConfig(providers={"a": Provider("a", "openai", "k"), "b": Provider("b", "openai", "k")},
                     models={"m1": Model("m1", "a", "m1", 1, 1), "m2": Model("m2", "b", "m2")},
                     roles={"default": ["m1", "m2"]}, retries=1, **kw)


class Stub:
    def __init__(self, replies):
        self.replies = list(replies)
        self.n = 0

    async def complete(self, model, *a, **k):
        from tgnet.llm import LLMResult
        self.n += 1
        r = self.replies.pop(0) if self.replies else "{}"
        if isinstance(r, Exception):
            raise r
        return LLMResult(r, model, "x", 1000, 1000)


async def _nosleep(_):
    return None


def test_router_falls_back_to_next_model():
    db = DB(":memory:")
    a = Stub([LLMError("down"), LLMError("down")])
    b = Stub(['{"ok": 1}'])
    r = Router(_cfg(), db, None, providers={"a": a, "b": b})
    r.sleep = _nosleep
    data, res = run(r.json("writer", "s", "u"))
    assert data == {"ok": 1} and res.model == "m2" and a.n == 2
    assert db.scalar("SELECT COUNT(*) FROM llm_usage WHERE ok=0") == 2


def test_router_circuit_breaker_skips_dead_model():
    db = DB(":memory:")
    a = Stub([LLMError("x")] * 20)
    b = Stub(['{"v": 1}'] * 10)
    r = Router(_cfg(), db, None, providers={"a": a, "b": b})
    r.sleep = _nosleep
    for _ in range(4):
        run(r.complete("x", "s", "u"))
    assert a.n == 6  # 3 failing rounds x 2 attempts, then the breaker opens


def test_router_budget_blocks_paid_models():
    db = DB(":memory:")
    db.x("INSERT INTO llm_usage(ts,role,provider,model,tin,tout,cost,ok,ms) VALUES(?,?,?,?,?,?,?,?,?)",
         (time.time(), "x", "a", "m1", 0, 0, 99.0, 1, 1))
    a, b = Stub(["{}"]), Stub(["{}"])
    r = Router(_cfg(daily_budget_usd=1), db, None, providers={"a": a, "b": b})
    res = run(r.complete("writer", "s", "u"))
    assert res.model == "m2" and a.n == 0  # free model still allowed
    r2 = Router(LLMConfig(providers={"a": Provider("a", "openai", "k")}, models={"m1": Model("m1", "a", "m1", 1, 1)},
                          roles={"default": ["m1"]}, daily_budget_usd=1), db, None, providers={"a": a})
    with pytest.raises(BudgetExceeded):
        run(r2.complete("writer", "s", "u"))


def test_router_repairs_bad_json():
    db = DB(":memory:")
    a = Stub(["not json at all", '{"fixed": true}'])
    r = Router(_cfg(), db, None, providers={"a": a, "b": Stub([])})
    data, _ = run(r.json("x", "s", "u"))
    assert data == {"fixed": True}


def test_gemini_provider_parses_grounding():
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        seen["key"] = req.headers.get("x-goog-api-key")
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "thinking", "thought": True}, {"text": '{"verdict":"supported"}'}]},
                            "groundingMetadata": {"groundingChunks": [{"web": {"uri": "https://vertex/r1", "title": "arsenal.com"}}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            p = GeminiProvider(Provider("g", "gemini", "KEY"), http)
            return await p.complete("gm", "sys", "u", json_mode=True, temperature=0, max_tokens=10, grounding=True)

    res = run(go())
    assert res.text == '{"verdict":"supported"}'
    assert res.citations == [{"url": "https://vertex/r1", "title": "arsenal.com"}]
    assert seen["key"] == "KEY" and seen["body"]["tools"] == [{"google_search": {}}]
    assert "responseMimeType" not in seen["body"]["generationConfig"]


def test_openai_provider_retries_without_json_mode():
    bodies = []

    def handler(req: httpx.Request):
        b = json.loads(req.content)
        bodies.append(b)
        if "response_format" in b:
            return httpx.Response(400, json={"error": "response_format unsupported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>x</think>{\"a\":1}"}}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 4}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            p = OpenAIProvider(Provider("o", "openai", "sk", "https://api.test/v1"), http)
            return await p.complete("m", "s", "u", json_mode=True, temperature=0, max_tokens=5, grounding=False)

    res = run(go())
    assert res.text == '{"a":1}' and len(bodies) == 2 and res.tout == 4


# --------------------------------------------------------------------------- config
def test_shipped_config_is_valid(monkeypatch):
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent
    for k, v in {"OWNER_CHAT_ID": "1", "BOT_TOKEN": "1:x", "GEMINI_API_KEY": "g", "GEMINI_FAST_MODEL": "f",
                 "GEMINI_SMART_MODEL": "s", "FOOTBALL_CHAT_ID": "@f", "ESPORTS_CHAT_ID": "@e"}.items():
        monkeypatch.setenv(k, v)
    cfg = C.load(base)
    assert C.validate(cfg) == []
    assert set(cfg.channels) == {"football_ru", "esports_ru"}
    assert cfg.llm.models["fast"].price_in == 0.3 and cfg.owner_chat_id == 1
    assert cfg.channels["football_ru"].discussion_chat_id is None


def test_validate_reports_problems(harness):
    h = harness()
    h.cfg.channels["football_ru"].autonomy = "yolo"
    h.cfg.llm.roles["x"] = ["nope"]
    errs = C.validate(h.cfg)
    assert any("autonomy" in e for e in errs) and any("nope" in e for e in errs)


def test_env_interpolation(monkeypatch):
    monkeypatch.setenv("A_SET", "1")
    monkeypatch.delenv("A_UNSET", raising=False)
    assert C.expand_env({"x": ["${A_SET}", "${A_UNSET:-dflt}", "${A_UNSET}"]}) == {"x": ["1", "dflt", ""]}
