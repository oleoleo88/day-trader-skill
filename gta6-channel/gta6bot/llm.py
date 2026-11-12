"""Обёртка над Claude API: структурированные ответы, кэш системного промпта, учёт расходов.

Все обращения к модели в проекте идут через LLM.structured(): на вход системный промпт,
сообщение и pydantic-схема, на выход — проверенный объект этой схемы.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from .config import LLMCfg
from .db import Database

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Модели с защитными классификаторами, для которых API умеет сам повторить отклонённый запрос
# на запасной модели. У Haiku 5.5 серверного fallback нет — параметр ему не отправляем.
FALLBACK_MODELS = frozenset({
    "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1"})

# Ориентировочные цены, $ за 1 млн токенов: (вход, выход, чтение кэша, запись кэша на 1 час).
# Нужны только для оценки расходов в /stats; точные цифры — в консоли Anthropic.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 8.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 10.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10, 4.0),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.20),
    "claude-fable-5-2": (8.0, 40.0, 0.20, 16.0),
}


class LLMError(Exception):
    """Сбой обращения к модели. transient=True — имеет смысл повторить позже."""

    def __init__(self, message: str, *, transient: bool = False, fatal: bool = False):
        super().__init__(message)
        self.transient = transient
        self.fatal = fatal  # ошибка настройки (ключ, модель): повторять бессмысленно, нужен человек


class LLMRefusal(LLMError):
    """Модель (или её защитный классификатор) отказалась выполнять запрос."""

    def __init__(self, category: str | None):
        super().__init__(f"модель отклонила запрос (категория: {category or 'не указана'})")
        self.category = category


def estimate_cost(model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    i, o, r, w = price
    return (input_tokens * i + output_tokens * o + cache_read * r + cache_write * w) / 1_000_000


class LLM:
    def __init__(self, cfg: LLMCfg, db: Database | None = None, client: anthropic.AsyncAnthropic | None = None):
        self.cfg = cfg
        self.db = db
        # Ключ берётся из ANTHROPIC_API_KEY (или ANTHROPIC_AUTH_TOKEN) в окружении
        self.client = client or anthropic.AsyncAnthropic(timeout=float(cfg.timeout_sec), max_retries=3)
        self._sem = asyncio.Semaphore(max(1, cfg.max_concurrency))

    async def close(self) -> None:
        await self.client.close()

    async def structured(self, role: str, *, system: str, user: str, schema: type[T], max_tokens: int = 4000) -> T:
        """Один запрос к модели с ответом строго по схеме.

        system — стабильная часть (кэшируется на час), user — всё изменчивое: дата, запись, контекст.
        """
        model = self.cfg.models[role]
        effort = self.cfg.effort[role]
        async with self._sem:
            for attempt in (1, 2):
                resp = await self._call(model, effort, system, user, schema, max_tokens)
                await self._account(role, resp)
                if resp.stop_reason == "refusal":
                    details = getattr(resp, "stop_details", None)
                    raise LLMRefusal(getattr(details, "category", None))
                if resp.stop_reason == "max_tokens":
                    # размышления модели тоже расходуют max_tokens: даём вдвое больше и пробуем ещё раз
                    if attempt == 1:
                        max_tokens *= 2
                        continue
                    raise LLMError("ответ модели не уместился в лимит токенов")
                parsed = resp.parsed_output
                if parsed is None:
                    raise LLMError("модель вернула пустой ответ")
                return parsed
        raise LLMError("не удалось получить ответ модели")  # недостижимо, для статического анализа

    async def _call(self, model: str, effort: str, system: str, user: str, schema: type[BaseModel], max_tokens: int) -> Any:
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": effort},
        )
        try:
            if self.cfg.refusal_fallback and model in FALLBACK_MODELS:
                return await self.client.beta.messages.parse(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            return await self.client.messages.parse(**kwargs)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise LLMError(f"Claude API не принял ключ: {e.message}", fatal=True) from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"модель «{model}» недоступна: {e.message}", fatal=True) from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"Claude API отклонил запрос: {e.message}") from e
        except anthropic.RateLimitError as e:
            raise LLMError("превышен лимит запросов Claude API", transient=True) from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"ошибка Claude API ({e.status_code}): {e.message}", transient=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"нет связи с Claude API: {e}", transient=True) from e

    async def _account(self, role: str, resp: Any) -> None:
        usage = getattr(resp, "usage", None)
        if usage is None or self.db is None:
            return
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await self.db.execute(
            """INSERT INTO llm_usage (day, role, model, calls, input_tokens, output_tokens, cache_read, cache_write)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?)
               ON CONFLICT(day, role, model) DO UPDATE SET
                 calls=calls+1,
                 input_tokens=input_tokens+excluded.input_tokens,
                 output_tokens=output_tokens+excluded.output_tokens,
                 cache_read=cache_read+excluded.cache_read,
                 cache_write=cache_write+excluded.cache_write""",
            (day, role, getattr(resp, "model", "") or self.cfg.models[role],
             usage.input_tokens or 0, usage.output_tokens or 0,
             getattr(usage, "cache_read_input_tokens", 0) or 0,
             getattr(usage, "cache_creation_input_tokens", 0) or 0))

    async def check_models(self) -> list[tuple[str, str, str | None]]:
        """Проверка ключа и доступности моделей без генерации (бесплатно).
        Возвращает (роль, модель, ошибка | None)."""
        results: list[tuple[str, str, str | None]] = []
        seen: dict[str, str | None] = {}
        for role, model in self.cfg.models.items():
            if model not in seen:
                try:
                    await self.client.models.retrieve(model)
                    seen[model] = None
                except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                    seen[model] = f"ключ не принят: {e.message}"
                except anthropic.NotFoundError:
                    seen[model] = "модель не найдена или недоступна вашему аккаунту"
                except anthropic.APIStatusError as e:
                    seen[model] = f"ошибка API {e.status_code}: {e.message}"
                except anthropic.APIConnectionError as e:
                    seen[model] = f"нет связи с api.anthropic.com: {e}"
            results.append((role, model, seen[model]))
        return results
