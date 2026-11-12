"""Загрузка настроек: секреты из .env / окружения, поведение из config.yaml.

Загрузчик строгий: неизвестный ключ в config.yaml — ошибка, чтобы опечатка
не превратилась в молча проигнорированную настройку.
"""
from __future__ import annotations

import os
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ConfigError(Exception):
    """Ошибка в настройках: текст можно показывать пользователю как есть."""


# ── разделы config.yaml ──────────────────────────────────────────────────────

@dataclass
class ChannelCfg:
    name: str = "GTA 6"
    timezone: str = "Europe/Moscow"
    release_date: str = "2026-11-19"
    footer: bool = True
    source_link: bool = True
    hashtags: bool = True
    category_label: bool = True

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def release(self) -> date:
        return date.fromisoformat(self.release_date)


@dataclass
class PublishingCfg:
    mode: str = "hybrid"
    min_importance: int = 5
    breaking_importance: int = 9
    auto_categories: list[str] = field(default_factory=lambda: ["official", "media", "industry", "guide"])
    auto_min_confidence: float = 0.8
    auto_min_tier: int = 2
    max_posts_per_day: int = 14
    min_gap_minutes: int = 35
    quiet_hours: list[int] = field(default_factory=lambda: [0, 8])
    silent_at_night: bool = True
    stale_after_hours: int = 20
    review_ttl_hours: int = 36


@dataclass
class Source:
    name: str
    url: str
    tier: int = 2
    lang: str = "en"
    focused: bool = False
    enabled: bool = True
    interval: int = 0  # 0 — общий интервал опроса


@dataclass
class CollectorCfg:
    poll_interval_sec: int = 150
    backfill_hours: int = 10
    max_age_hours: int = 48
    min_source_chars: int = 350
    dedupe_window_hours: int = 96
    title_similarity: float = 0.62
    user_agent: str = "Mozilla/5.0 (compatible; NewsReaderBot/1.0)"
    keywords: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)


LLM_ROLES = ("triage", "writer", "moderation", "qa", "poll")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = "claude-opus-5-5"


@dataclass
class LLMCfg:
    models: dict[str, str] = field(default_factory=lambda: {r: DEFAULT_MODEL for r in LLM_ROLES})
    effort: dict[str, str] = field(default_factory=lambda: {
        "triage": "low", "writer": "medium", "moderation": "low", "qa": "low", "poll": "medium"})
    refusal_fallback: bool = True
    timeout_sec: int = 120
    max_concurrency: int = 4


@dataclass
class LinksCfg:
    policy: str = "newcomers"
    newcomer_messages: int = 15
    allow_domains: list[str] = field(default_factory=lambda: ["rockstargames.com", "youtube.com", "youtu.be", "t.me"])
    block_invites: bool = True


@dataclass
class FloodCfg:
    messages: int = 6
    per_seconds: int = 12
    mute_minutes: int = 15
    duplicates: int = 3


@dataclass
class JudgeCfg:
    enabled: bool = True
    scrutiny_messages: int = 2
    min_chars: int = 12
    min_confidence: float = 0.75
    daily_limit: int = 600
    spoiler_guard: bool = True
    politics: str = "delete"


@dataclass
class ModerationCfg:
    enabled: bool = True
    first_comment: bool = True
    first_comment_text: str = "💬 Обсуждаем здесь. Без спама и оскорблений."
    rules_text: str = "<b>Правила</b>: без спама, рекламы и оскорблений."
    delete_service_messages: bool = True
    block_channel_senders: bool = True
    links: LinksCfg = field(default_factory=LinksCfg)
    flood: FloodCfg = field(default_factory=FloodCfg)
    max_custom_emoji: int = 12
    profanity: str = "off"
    hard_phrases: list[str] = field(default_factory=list)
    soft_phrases: list[str] = field(default_factory=list)
    llm_judge: JudgeCfg = field(default_factory=JudgeCfg)
    ladder: list[str] = field(default_factory=lambda: ["warn", "mute:60", "mute:1440", "ban"])
    warn_ttl_days: int = 30
    notice_ttl_sec: int = 30
    log_deletions: bool = True


@dataclass
class RubricCfg:
    enabled: bool = True
    time: str = "10:00"
    weekday: int = 0
    min_posts: int = 0

    @property
    def at(self) -> dtime:
        hh, mm = self.time.split(":")
        return dtime(int(hh), int(mm))


@dataclass
class RubricsCfg:
    countdown: RubricCfg = field(default_factory=lambda: RubricCfg(time="10:00"))
    digest: RubricCfg = field(default_factory=lambda: RubricCfg(time="21:30", min_posts=4))
    poll: RubricCfg = field(default_factory=lambda: RubricCfg(time="18:00", weekday=5))
    weekly_report: RubricCfg = field(default_factory=lambda: RubricCfg(time="10:30", weekday=0))


@dataclass
class CommunityCfg:
    suggestions: bool = True
    ad_inquiries: bool = True
    qa: bool = True
    qa_per_user_per_day: int = 8
    qa_daily_limit: int = 300


@dataclass
class AdsCfg:
    default_top_minutes: int = 60
    default_feed_hours: int = 24
    breaking_overrides_top: bool = False
    require_erid: bool = True


@dataclass
class AnalyticsCfg:
    scrape_views: bool = True
    rkn_reminder: bool = True


@dataclass
class Secrets:
    bot_token: str = ""
    channel_id: int | str = 0
    admin_chat_id: int = 0
    admin_user_ids: frozenset[int] = frozenset()
    discussion_chat_id: int | None = None
    telegram_api_base: str | None = None
    data_dir: Path = Path("data")


@dataclass
class Config:
    channel: ChannelCfg = field(default_factory=ChannelCfg)
    publishing: PublishingCfg = field(default_factory=PublishingCfg)
    collector: CollectorCfg = field(default_factory=CollectorCfg)
    llm: LLMCfg = field(default_factory=LLMCfg)
    moderation: ModerationCfg = field(default_factory=ModerationCfg)
    rubrics: RubricsCfg = field(default_factory=RubricsCfg)
    community: CommunityCfg = field(default_factory=CommunityCfg)
    ads: AdsCfg = field(default_factory=AdsCfg)
    analytics: AnalyticsCfg = field(default_factory=AnalyticsCfg)
    secrets: Secrets = field(default_factory=Secrets)
    base_dir: Path = Path(".")

    @property
    def db_path(self) -> Path:
        return self.secrets.data_dir / "bot.sqlite3"

    @property
    def facts_path(self) -> Path:
        return self.base_dir / "knowledge" / "facts.md"


# ── сборка датаклассов из словаря ────────────────────────────────────────────

def _coerce(tp: Any, value: Any, path: str) -> Any:
    origin = typing.get_origin(tp)
    if is_dataclass(tp):
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: ожидался раздел (словарь), получено {type(value).__name__}")
        return _build(tp, value, path)
    if origin is list:
        if not isinstance(value, list):
            raise ConfigError(f"{path}: ожидался список")
        (inner,) = typing.get_args(tp)
        return [_coerce(inner, v, f"{path}[{i}]") for i, v in enumerate(value)]
    if origin is dict:
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: ожидался словарь")
        return {str(k): str(v) for k, v in value.items()}
    if tp is bool:
        if isinstance(value, bool):
            return value
        raise ConfigError(f"{path}: ожидалось true/false, получено {value!r}")
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{path}: ожидалось целое число, получено {value!r}")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{path}: ожидалось число, получено {value!r}")
        return float(value)
    if tp is str:
        # YAML превращает `off`/`on`/`yes`/`no` в bool — возвращаем человеческий смысл обратно
        if isinstance(value, bool):
            return "on" if value else "off"
        return str(value)
    return value


def _build(cls: type, data: dict, path: str) -> Any:
    hints = typing.get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        where = path or "config.yaml"
        raise ConfigError(f"{where}: неизвестные ключи {sorted(unknown)}. Допустимые: {sorted(known)}")
    kwargs = {}
    for f in fields(cls):
        if f.name in data and data[f.name] is not None:
            kwargs[f.name] = _coerce(hints[f.name], data[f.name], f"{path}.{f.name}" if path else f.name)
    try:
        return cls(**kwargs)
    except TypeError as e:  # не хватает обязательного поля (например, url у источника)
        raise ConfigError(f"{path}: {e}") from None


# ── .env ─────────────────────────────────────────────────────────────────────

def load_dotenv(path: Path) -> None:
    """Минимальный разбор .env: KEY=VALUE, комментарии с #, кавычки снимаются.
    Уже заданные переменные окружения не перезаписываются."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        if key and key not in os.environ:
            os.environ[key] = value


def _int_env(name: str, required: bool = True) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        if required:
            raise ConfigError(f"В .env не задано {name}")
        return None
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} должно быть числом (ID чата), сейчас: {raw!r}") from None


def _load_secrets(base_dir: Path) -> Secrets:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("В .env не задан BOT_TOKEN (получите его у @BotFather)")
    raw_channel = os.environ.get("CHANNEL_ID", "").strip()
    if not raw_channel:
        raise ConfigError("В .env не задан CHANNEL_ID (@username канала или числовой ID вида -100…)")
    channel: int | str
    if raw_channel.lstrip("-").isdigit():
        channel = int(raw_channel)
    else:
        channel = raw_channel if raw_channel.startswith("@") else "@" + raw_channel
    admin_chat = _int_env("ADMIN_CHAT_ID")
    raw_admins = os.environ.get("ADMIN_USER_IDS", "").replace(";", ",")
    try:
        admins = frozenset(int(x) for x in raw_admins.split(",") if x.strip())
    except ValueError:
        raise ConfigError("ADMIN_USER_IDS — числовые ID через запятую, например 123456,789012") from None
    if not admins:
        if admin_chat and admin_chat > 0:  # админ-чат — личка с владельцем
            admins = frozenset({admin_chat})
        else:
            raise ConfigError("В .env не задан ADMIN_USER_IDS (ваш числовой Telegram ID)")
    data_dir = Path(os.environ.get("DATA_DIR", "").strip() or base_dir / "data")
    return Secrets(
        bot_token=token,
        channel_id=channel,
        admin_chat_id=admin_chat or 0,
        admin_user_ids=admins,
        discussion_chat_id=_int_env("DISCUSSION_CHAT_ID", required=False),
        telegram_api_base=os.environ.get("TELEGRAM_API_BASE", "").strip() or None,
        data_dir=data_dir,
    )


# ── проверка значений ────────────────────────────────────────────────────────

def parse_ladder_step(step: str) -> tuple[str, int]:
    """'warn' → ('warn', 0); 'mute:60' → ('mute', 60); 'ban' → ('ban', 0)."""
    kind, _, arg = step.partition(":")
    kind = kind.strip().lower()
    if kind not in ("warn", "mute", "ban"):
        raise ConfigError(f"moderation.ladder: неизвестный шаг {step!r} (допустимо warn, mute:<минуты>, ban)")
    minutes = 0
    if kind == "mute":
        if not arg.strip().isdigit() or int(arg) <= 0:
            raise ConfigError(f"moderation.ladder: у шага mute нужно число минут, например mute:60 (сейчас {step!r})")
        minutes = int(arg)
    return kind, minutes


def _validate(cfg: Config) -> None:
    p = cfg.publishing
    if p.mode not in ("auto", "hybrid", "review"):
        raise ConfigError(f"publishing.mode: допустимо auto | hybrid | review, сейчас {p.mode!r}")
    if not (1 <= p.min_importance <= 10 and 1 <= p.breaking_importance <= 10):
        raise ConfigError("publishing: min_importance и breaking_importance должны быть в диапазоне 1..10")
    if p.quiet_hours and (len(p.quiet_hours) != 2 or not all(0 <= h <= 24 for h in p.quiet_hours)):
        raise ConfigError("publishing.quiet_hours: два часа [с, до] в диапазоне 0..24 или пустой список")
    try:
        cfg.channel.tz
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"channel.timezone: неизвестный часовой пояс {cfg.channel.timezone!r}") from None
    try:
        cfg.channel.release
    except ValueError:
        raise ConfigError(f"channel.release_date: нужна дата ГГГГ-ММ-ДД, сейчас {cfg.channel.release_date!r}") from None
    for role in LLM_ROLES:
        cfg.llm.models.setdefault(role, DEFAULT_MODEL)
        cfg.llm.effort.setdefault(role, "medium")
        if cfg.llm.effort[role] not in EFFORTS:
            raise ConfigError(f"llm.effort.{role}: допустимо {' | '.join(EFFORTS)}, сейчас {cfg.llm.effort[role]!r}")
    extra_roles = (set(cfg.llm.models) | set(cfg.llm.effort)) - set(LLM_ROLES)
    if extra_roles:
        raise ConfigError(f"llm: неизвестные роли {sorted(extra_roles)}. Допустимые: {list(LLM_ROLES)}")
    m = cfg.moderation
    if m.links.policy not in ("off", "newcomers", "all"):
        raise ConfigError("moderation.links.policy: допустимо off | newcomers | all")
    if m.profanity not in ("off", "delete", "warn"):
        raise ConfigError("moderation.profanity: допустимо off | delete | warn")
    if m.llm_judge.politics not in ("delete", "ignore"):
        raise ConfigError("moderation.llm_judge.politics: допустимо delete | ignore")
    if not m.ladder:
        raise ConfigError("moderation.ladder: нужен хотя бы один шаг")
    for step in m.ladder:
        parse_ladder_step(step)
    for name in ("countdown", "digest", "poll", "weekly_report"):
        r: RubricCfg = getattr(cfg.rubrics, name)
        try:
            r.at
        except (ValueError, TypeError):
            raise ConfigError(f"rubrics.{name}.time: нужно время ЧЧ:ММ, сейчас {r.time!r}") from None
        if not 0 <= r.weekday <= 6:
            raise ConfigError(f"rubrics.{name}.weekday: 0 (пн) … 6 (вс)")
    names = [s.name for s in cfg.collector.sources]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise ConfigError(f"collector.sources: повторяются названия {sorted(dup)}")
    for s in cfg.collector.sources:
        if not s.url.startswith(("http://", "https://")):
            raise ConfigError(f"collector.sources: у «{s.name}» некорректный url {s.url!r}")
        if s.tier not in (1, 2, 3):
            raise ConfigError(f"collector.sources: у «{s.name}» tier должен быть 1, 2 или 3")


def load_config(base_dir: str | Path | None = None, *, require_secrets: bool = True) -> Config:
    base = Path(base_dir or os.environ.get("GTA6BOT_HOME") or Path.cwd()).resolve()
    load_dotenv(base / ".env")
    yaml_path = base / "config.yaml"
    if not yaml_path.is_file():
        raise ConfigError(f"Не найден {yaml_path}. Запускайте бота из папки проекта.")
    try:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"config.yaml: ошибка синтаксиса YAML: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml: на верхнем уровне должны быть разделы (channel, publishing, …)")
    if "secrets" in raw or "base_dir" in raw:
        raise ConfigError("config.yaml: секреты задаются только в .env")
    cfg: Config = _build(Config, raw, "")
    cfg.base_dir = base
    if require_secrets:
        cfg.secrets = _load_secrets(base)
    else:
        cfg.secrets = Secrets(data_dir=Path(os.environ.get("DATA_DIR", "").strip() or base / "data"))
    _validate(cfg)
    return cfg
