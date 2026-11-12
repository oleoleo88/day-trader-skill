"""Общий контекст приложения: настройки, соединения и сервисы в одном объекте."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .config import Config
from .db import Database
from .textutil import plural_ru

if TYPE_CHECKING:  # только для подсказок типов, без циклических импортов
    import aiohttp
    from aiogram import Bot

    from .ads import Ads
    from .analytics import Analytics
    from .collector import Collector
    from .editor import Editor
    from .moderation.service import Moderator
    from .notifier import Notifier
    from .publisher import Publisher
    from .rubrics import Rubrics

log = logging.getLogger(__name__)


@dataclass
class Ctx:
    cfg: Config
    db: Database
    bot: "Bot"
    llm: Any
    http: "aiohttp.ClientSession | None" = None

    # заполняется при старте из Telegram
    channel_id: int = 0
    channel_username: str | None = None
    channel_title: str = ""
    discussion_id: int | None = None
    bot_id: int = 0
    bot_username: str = ""

    # сервисы
    notifier: "Notifier" = None  # type: ignore[assignment]
    collector: "Collector" = None  # type: ignore[assignment]
    editor: "Editor" = None  # type: ignore[assignment]
    publisher: "Publisher" = None  # type: ignore[assignment]
    moderator: "Moderator" = None  # type: ignore[assignment]
    rubrics: "Rubrics" = None  # type: ignore[assignment]
    analytics: "Analytics" = None  # type: ignore[assignment]
    ads: "Ads" = None  # type: ignore[assignment]

    _facts: str = field(default="", repr=False)
    _facts_mtime: float = field(default=-1.0, repr=False)

    # ── время ───────────────────────────────────────────────────────────────
    def now_local(self) -> datetime:
        return datetime.now(self.cfg.channel.tz)

    def local(self, ts: int | float) -> datetime:
        return datetime.fromtimestamp(ts, self.cfg.channel.tz)

    def today(self) -> str:
        return self.now_local().date().isoformat()

    def day_start_ts(self) -> int:
        n = self.now_local()
        return int(n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    def days_to_release(self) -> int:
        return (self.cfg.channel.release - self.now_local().date()).days

    def phase_text(self) -> str:
        d = self.days_to_release()
        if d > 0:
            return f"до релиза {d} {plural_ru(d, ('день', 'дня', 'дней'))}"
        if d == 0:
            return "сегодня день релиза"
        return f"игра вышла, прошло {-d} {plural_ru(-d, ('день', 'дня', 'дней'))} после релиза"

    def clock_text(self) -> str:
        n = self.now_local()
        weekdays = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
        return f"{n:%Y-%m-%d} ({weekdays[n.weekday()]}), {n:%H:%M} по московскому времени" \
            if self.cfg.channel.timezone == "Europe/Moscow" else f"{n:%Y-%m-%d} ({weekdays[n.weekday()]}), {n:%H:%M} ({self.cfg.channel.timezone})"

    # ── ссылки ──────────────────────────────────────────────────────────────
    @property
    def channel_link(self) -> str | None:
        return f"https://t.me/{self.channel_username}" if self.channel_username else None

    def post_link(self, msg_id: int) -> str:
        if self.channel_username:
            return f"https://t.me/{self.channel_username}/{msg_id}"
        internal = str(self.channel_id)
        internal = internal[4:] if internal.startswith("-100") else internal.lstrip("-")
        return f"https://t.me/c/{internal}/{msg_id}"

    @property
    def bot_link(self) -> str:
        return f"https://t.me/{self.bot_username}" if self.bot_username else ""

    # ── права ───────────────────────────────────────────────────────────────
    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.cfg.secrets.admin_user_ids

    @property
    def admin_chat(self) -> int:
        return self.cfg.secrets.admin_chat_id

    # ── база фактов ─────────────────────────────────────────────────────────
    def facts(self) -> str:
        """Текст knowledge/facts.md; перечитывается, когда файл изменился."""
        path = self.cfg.facts_path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            if self._facts_mtime != 0:
                log.warning("Не найден файл базы фактов %s — редактор работает без него", path)
                self._facts_mtime = 0
            return "(база фактов пуста)"
        if mtime != self._facts_mtime:
            self._facts = path.read_text(encoding="utf-8").strip() or "(база фактов пуста)"
            self._facts_mtime = mtime
        return self._facts


def ts() -> int:
    return int(time.time())
