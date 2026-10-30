"""LLM layer: Gemini + any OpenAI-compatible API (incl. a local Ollama on the laptop).

Each agent role has an ordered fallback chain of models. The router retries transient
errors, trips a circuit breaker on a model that keeps failing, logs token spend and
enforces a daily budget, so one dead provider never stops the network.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

import httpx

from .config import LLMConfig, Model, Provider
from .db import DB
from .util import day_start, extract_json, now

log = logging.getLogger("tgnet.llm")


class LLMError(Exception):
    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable


class BudgetExceeded(LLMError):
    def __init__(self, msg: str):
        super().__init__(msg, retryable=False)


@dataclass
class LLMResult:
    text: str
    model: str
    provider: str
    tin: int = 0
    tout: int = 0
    citations: list[dict] = field(default_factory=list)  # [{"url":..., "title":...}] from web grounding


def _status_error(r: httpx.Response) -> LLMError:
    body = r.text[:400]
    retry = r.status_code in (408, 409, 429) or r.status_code >= 500
    return LLMError(f"HTTP {r.status_code}: {body}", retryable=retry)


class GeminiProvider:
    def __init__(self, cfg: Provider, http: httpx.AsyncClient):
        self.cfg, self.http = cfg, http
        self.base = (cfg.base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")

    async def complete(self, model: str, system: str, user: str, *, json_mode: bool, temperature: float,
                       max_tokens: int, grounding: bool) -> LLMResult:
        gen = {"temperature": temperature, "maxOutputTokens": max_tokens}
        body: dict = {"contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": gen}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if grounding:
            body["tools"] = [{"google_search": {}}]
        elif json_mode:
            gen["responseMimeType"] = "application/json"
        try:
            r = await self.http.post(f"{self.base}/models/{model}:generateContent", json=body,
                                     headers={"x-goog-api-key": self.cfg.api_key}, timeout=self.cfg.timeout)
        except httpx.HTTPError as e:
            raise LLMError(f"gemini transport: {e!r}") from e
        if r.status_code != 200:
            raise _status_error(r)
        data = r.json()
        fb = data.get("promptFeedback") or {}
        if fb.get("blockReason"):
            raise LLMError(f"gemini blocked prompt: {fb['blockReason']}", retryable=False)
        cands = data.get("candidates") or []
        if not cands:
            raise LLMError("gemini: no candidates")
        c = cands[0]
        parts = (c.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text.strip():
            raise LLMError(f"gemini empty reply (finishReason={c.get('finishReason')})",
                           retryable=c.get("finishReason") not in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"))
        cites = []
        for ch in (c.get("groundingMetadata") or {}).get("groundingChunks") or []:
            web = ch.get("web") or {}
            if web.get("uri") or web.get("title"):
                cites.append({"url": web.get("uri", ""), "title": web.get("title", "")})
        u = data.get("usageMetadata") or {}
        return LLMResult(text, model, self.cfg.name, int(u.get("promptTokenCount", 0)),
                         int(u.get("candidatesTokenCount", 0)) + int(u.get("thoughtsTokenCount", 0)), cites)


class OpenAIProvider:
    def __init__(self, cfg: Provider, http: httpx.AsyncClient):
        self.cfg, self.http = cfg, http
        self.base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        self.json_unsupported: set[str] = set()

    async def complete(self, model: str, system: str, user: str, *, json_mode: bool, temperature: float,
                       max_tokens: int, grounding: bool) -> LLMResult:
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
        body: dict = {"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens}
        if json_mode and model not in self.json_unsupported:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}
        try:
            r = await self.http.post(f"{self.base}/chat/completions", json=body, headers=headers,
                                     timeout=self.cfg.timeout)
            if r.status_code == 400 and "response_format" in body:
                # some gateways reject json mode: remember and retry without it
                self.json_unsupported.add(model)
                body.pop("response_format")
                r = await self.http.post(f"{self.base}/chat/completions", json=body, headers=headers,
                                         timeout=self.cfg.timeout)
        except httpx.HTTPError as e:
            raise LLMError(f"{self.cfg.name} transport: {e!r}") from e
        if r.status_code != 200:
            raise _status_error(r)
        data = r.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"{self.cfg.name}: malformed reply {str(data)[:200]}") from e
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        if not text:
            raise LLMError(f"{self.cfg.name}: empty reply")
        u = data.get("usage") or {}
        return LLMResult(text, model, self.cfg.name, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)))


class Breaker:
    """Open after `threshold` consecutive failures; half-open again after `cooldown` seconds."""

    def __init__(self, threshold: int = 3, cooldown: float = 300):
        self.threshold, self.cooldown = threshold, cooldown
        self.fails, self.opened_at = 0, 0.0

    def available(self) -> bool:
        return self.fails < self.threshold or time.monotonic() - self.opened_at >= self.cooldown

    def ok(self):
        self.fails = 0

    def fail(self):
        self.fails += 1
        if self.fails >= self.threshold:
            self.opened_at = time.monotonic()


class Router:
    def __init__(self, cfg: LLMConfig, db: DB, http: httpx.AsyncClient, tz: str = "UTC", providers=None):
        self.cfg, self.db, self.tz = cfg, db, tz
        self.providers = providers or {}
        for name, p in cfg.providers.items():
            if name not in self.providers:
                self.providers[name] = GeminiProvider(p, http) if p.kind == "gemini" else OpenAIProvider(p, http)
        self.breakers: dict[str, Breaker] = {}
        self.sleep = asyncio.sleep

    def chain(self, role: str) -> list[Model]:
        aliases = self.cfg.roles.get(role) or self.cfg.roles.get("default") or list(self.cfg.models)
        return [self.cfg.models[a] for a in aliases if a in self.cfg.models]

    def spent_today(self) -> float:
        return self.db.spend_since(day_start(now(), self.tz))

    def _record(self, role: str, m: Model, res: LLMResult | None, ms: int, err: str | None):
        tin = res.tin if res else 0
        tout = res.tout if res else 0
        cost = (tin * m.price_in + tout * m.price_out) / 1e6
        self.db.x("INSERT INTO llm_usage(ts,role,provider,model,tin,tout,cost,ok,ms,error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (now(), role, m.provider, m.model, tin, tout, cost, 1 if res else 0, ms, (err or "")[:300]))

    async def complete(self, role: str, system: str, user: str, *, json: bool = False, temperature: float = 0.4,
                       max_tokens: int | None = None, grounding: bool = False) -> LLMResult:
        over_budget = self.spent_today() >= self.cfg.daily_budget_usd
        errors = []
        for m in self.chain(role):
            if over_budget and (m.price_in or m.price_out) and role not in self.cfg.essential_roles:
                errors.append(f"{m.alias}: budget")
                continue
            br = self.breakers.setdefault(m.alias, Breaker())
            if not br.available():
                errors.append(f"{m.alias}: circuit open")
                continue
            prov = self.providers[m.provider]
            g = grounding and isinstance(prov, GeminiProvider)
            for attempt in range(self.cfg.retries + 1):
                t0 = time.monotonic()
                try:
                    res = await prov.complete(m.model, system, user, json_mode=json, temperature=temperature,
                                              max_tokens=max_tokens or m.max_output_tokens, grounding=g)
                except LLMError as e:
                    self._record(role, m, None, int((time.monotonic() - t0) * 1000), str(e))
                    errors.append(f"{m.alias}: {e}")
                    log.warning("llm %s/%s failed (attempt %d): %s", role, m.alias, attempt + 1, e)
                    if not e.retryable:
                        break
                    await self.sleep(min(20, 2 * 2 ** attempt))
                    continue
                self._record(role, m, res, int((time.monotonic() - t0) * 1000), None)
                br.ok()
                return res
            br.fail()
        if over_budget and all(e.endswith(": budget") for e in errors):
            raise BudgetExceeded(f"дневной бюджет LLM ${self.cfg.daily_budget_usd} исчерпан (роль {role})")
        raise LLMError(f"все модели для роли '{role}' недоступны: " + " | ".join(errors[-4:]))

    async def json(self, role: str, system: str, user: str, *, temperature: float = 0.2,
                   max_tokens: int | None = None, grounding: bool = False) -> tuple[dict, LLMResult]:
        """Complete and parse a JSON object; one repair round if the model returns junk."""
        res = await self.complete(role, system, user, json=True, temperature=temperature,
                                  max_tokens=max_tokens, grounding=grounding)
        try:
            data = extract_json(res.text)
        except ValueError:
            fix = await self.complete(role, "Ты возвращаешь только валидный JSON без пояснений.",
                                      "Исправь, чтобы это был один валидный JSON-объект, ничего не добавляя:\n\n"
                                      + res.text[:6000], json=True, temperature=0)
            data = extract_json(fix.text)
            fix.citations = res.citations
            res = fix
        if isinstance(data, list):
            data = {"items": data}
        if not isinstance(data, dict):
            raise LLMError("модель вернула не объект JSON", retryable=True)
        return data, res
