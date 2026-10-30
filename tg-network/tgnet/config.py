"""Configuration: YAML files + .env, with ${VAR} / ${VAR:-default} interpolation and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_ENV = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")

AUTONOMY = ("approve", "auto", "manual")


def load_dotenv(path: Path) -> None:
    """Minimal .env loader; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)


def expand_env(value):
    if isinstance(value, str):
        def sub(m):
            v = os.environ.get(m.group(1))
            if v in (None, ""):
                return m.group(2) if m.group(2) is not None else ""
            return v
        return _ENV.sub(sub, value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def _yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return expand_env(yaml.safe_load(fh) or {})


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- dataclasses

@dataclass
class Source:
    id: str
    name: str
    kind: str  # rss | telegram | steam
    url: str
    tier: int  # 1 official, 2 major media, 3 reputable insider/aggregator
    group: str  # independence group: two items from one group are one confirmation
    pack: str
    lang: str = "en"
    interval_min: int = 5
    enabled: bool = True
    official_only: bool = False  # steam: keep announcements only


@dataclass
class Pack:
    name: str
    sources: list[Source]
    trusted_domains: dict[str, int]  # domain -> tier, for grounded web search results


@dataclass
class Schedule:
    quiet_hours: str | None = "01:00-07:30"
    min_interval_min: int = 25
    max_posts_per_day: int = 20
    breaking_score: int = 90  # score at/above which quiet hours and spacing are ignored
    max_age_hours: float = 8.0  # drop posts about stories older than this
    approval_ttl_hours: float = 6.0


@dataclass
class Verification:
    allow_rumors: bool = True
    independent_confirmations: int = 2  # distinct tier<=2 groups needed when no tier-1 source
    max_item_age_hours: float = 24.0
    recheck_minutes: int = 10
    recheck_window_hours: float = 3.0
    use_grounding: bool = True


@dataclass
class Moderation:
    enabled: bool = True
    use_llm: bool = True
    trust_after_messages: int = 5
    trust_after_hours: float = 24.0
    flood_messages: int = 5
    flood_seconds: int = 20
    strikes_mute: int = 2
    strikes_ban: int = 3
    mute_hours: float = 24.0
    stop_words: list[str] = field(default_factory=list)


@dataclass
class AdSlot:
    id: str
    text: str
    advertiser: str
    erid: str
    times: list[str]  # "YYYY-MM-DD HH:MM" (one-off) or "HH:MM" (daily)
    until: str | None = None  # YYYY-MM-DD
    pin: bool = False
    delete_after_hours: float = 0
    quiet_after_minutes: int = 60
    image: str | None = None


@dataclass
class Channel:
    id: str
    title: str
    chat_id: str
    packs: list[str]
    enabled: bool = True
    username: str | None = None  # public @username without @, used for view stats
    discussion_chat_id: str | None = None
    bot_token: str = ""
    language: str = "ru"
    autonomy: str = "approve"
    auto_confidence: float = 0.85
    triage_threshold: int = 60
    max_selected_per_cycle: int = 4
    focus: str = ""
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    voice: str = ""
    format_rules: str = ""
    signature: str = ""
    images: bool = True
    forbidden_phrases: list[str] = field(default_factory=list)
    schedule: Schedule = field(default_factory=Schedule)
    verification: Verification = field(default_factory=Verification)
    moderation: Moderation = field(default_factory=Moderation)
    ads: list[AdSlot] = field(default_factory=list)
    specialists: list[dict] = field(default_factory=list)


@dataclass
class Provider:
    name: str
    kind: str  # gemini | openai
    api_key: str
    base_url: str = ""
    timeout: float = 90.0
    optional: bool = False


@dataclass
class Model:
    alias: str
    provider: str
    model: str
    price_in: float = 0.0  # USD per 1M input tokens
    price_out: float = 0.0
    max_output_tokens: int = 4096


@dataclass
class LLMConfig:
    providers: dict[str, Provider]
    models: dict[str, Model]
    roles: dict[str, list[str]]
    daily_budget_usd: float = 5.0
    essential_roles: list[str] = field(default_factory=lambda: ["moderator"])
    retries: int = 2


@dataclass
class Network:
    base_dir: Path
    owner_chat_id: int
    timezone: str
    db_path: Path
    bot_token: str
    llm: LLMConfig
    channels: dict[str, Channel]
    packs: dict[str, Pack]
    report_time: str = "23:00"
    hr_weekday: int = 6  # 0=Mon
    hr_time: str = "12:00"
    hr_auto_approve: bool = False
    health_port: int = 8787
    learning: dict = field(default_factory=dict)
    log_level: str = "INFO"

    def sources(self) -> list[Source]:
        out = []
        for p in self.packs.values():
            out.extend(s for s in p.sources if s.enabled)
        return out

    def source(self, sid: str) -> Source | None:
        for p in self.packs.values():
            for s in p.sources:
                if s.id == sid:
                    return s
        return None


# --------------------------------------------------------------------------- loading

def _build(cls, data: dict, **extra):
    names = set(cls.__dataclass_fields__)
    kw = {k: v for k, v in (data or {}).items() if k in names}
    kw.update(extra)
    return cls(**kw)


def load_pack(path: Path) -> Pack:
    d = _yaml(path)
    name = d.get("pack") or path.stem
    sources = []
    for s in d.get("sources", []):
        s = dict(s)
        s.setdefault("group", s["id"])
        s.setdefault("name", s["id"])
        s["tier"] = _int(s.get("tier"), 3)
        s["interval_min"] = _int(s.get("interval_min"), 5)
        sources.append(_build(Source, s, pack=name))
    domains = {str(k).lower(): _int(v, 3) for k, v in (d.get("trusted_domains") or {}).items()}
    return Pack(name=name, sources=sources, trusted_domains=domains)


def load_channel(path: Path, default_token: str) -> Channel:
    d = _yaml(path)
    d = dict(d)
    d.setdefault("id", path.stem)
    sched = _build(Schedule, d.pop("schedule", {}) or {})
    ver = _build(Verification, d.pop("verification", {}) or {})
    mod = _build(Moderation, d.pop("moderation", {}) or {})
    ads = [_build(AdSlot, a) for a in (d.pop("ads", []) or [])]
    for k in ("chat_id", "discussion_chat_id"):
        if d.get(k) not in (None, ""):
            d[k] = str(d[k])
        else:
            d[k] = None if k == "discussion_chat_id" else ""
    if d.get("username"):
        d["username"] = str(d["username"]).lstrip("@")
    d["bot_token"] = d.get("bot_token") or default_token
    return _build(Channel, d, schedule=sched, verification=ver, moderation=mod, ads=ads)


def load(base_dir: str | Path = ".") -> Network:
    base = Path(base_dir).resolve()
    load_dotenv(base / ".env")
    cfgdir = base / "config"
    n = _yaml(cfgdir / "network.yaml")
    llm = n.get("llm", {})
    providers = {k: _build(Provider, v, name=k) for k, v in (llm.get("providers") or {}).items()}
    for p in providers.values():
        p.timeout = float(p.timeout or 90)
        p.optional = str(p.optional).lower() in ("1", "true", "yes")
    models = {k: _build(Model, v, alias=k) for k, v in (llm.get("models") or {}).items()}
    for m in models.values():  # values may arrive as strings via ${ENV}
        m.price_in, m.price_out = float(m.price_in or 0), float(m.price_out or 0)
        m.max_output_tokens = _int(m.max_output_tokens, 4096)
    llmcfg = LLMConfig(
        providers=providers,
        models=models,
        roles={k: list(v) for k, v in (llm.get("roles") or {}).items()},
        daily_budget_usd=float(llm.get("daily_budget_usd", 5.0)),
        essential_roles=list(llm.get("essential_roles", ["moderator"])),
        retries=_int(llm.get("retries"), 2),
    )
    token = n.get("bot_token", "")
    packs = {}
    for p in sorted((cfgdir / "sources").glob("*.yaml")):
        pk = load_pack(p)
        packs[pk.name] = pk
    channels = {}
    for p in sorted((cfgdir / "channels").glob("*.yaml")):
        ch = load_channel(p, token)
        channels[ch.id] = ch
    db_path = Path(n.get("db_path", "data/tgnet.sqlite3"))
    if not db_path.is_absolute():
        db_path = base / db_path
    return Network(
        base_dir=base,
        owner_chat_id=_int(n.get("owner_chat_id"), 0),
        timezone=n.get("timezone", "Europe/Moscow"),
        db_path=db_path,
        bot_token=token,
        llm=llmcfg,
        channels=channels,
        packs=packs,
        report_time=str(n.get("report_time", "23:00")),
        hr_weekday=_int(n.get("hr_weekday"), 6),
        hr_time=str(n.get("hr_time", "12:00")),
        hr_auto_approve=bool(n.get("hr_auto_approve", False)),
        health_port=_int(n.get("health_port"), 8787),
        learning=n.get("learning") or {},
        log_level=str(n.get("log_level", "INFO")),
    )


def validate(cfg: Network) -> list[str]:
    """Return a list of human-readable problems (empty = OK)."""
    errs = []
    if not cfg.owner_chat_id:
        errs.append("network.yaml: owner_chat_id не задан (узнать: python -m tgnet whoami)")
    if not cfg.channels:
        errs.append("config/channels: нет ни одного канала")
    for ch in cfg.channels.values():
        if not ch.enabled:
            continue
        if not ch.bot_token:
            errs.append(f"{ch.id}: нет bot_token (BOT_TOKEN в .env)")
        if not ch.chat_id:
            errs.append(f"{ch.id}: chat_id не задан")
        if ch.autonomy not in AUTONOMY:
            errs.append(f"{ch.id}: autonomy должен быть одним из {AUTONOMY}")
        for p in ch.packs:
            if p not in cfg.packs:
                errs.append(f"{ch.id}: неизвестный пакет источников '{p}'")
        for a in ch.ads:
            if not a.erid or not a.advertiser:
                errs.append(f"{ch.id}: реклама '{a.id}' без erid/рекламодателя – публиковаться не будет")
    seen = set()
    for s in (s for p in cfg.packs.values() for s in p.sources):
        if s.id in seen:
            errs.append(f"источник '{s.id}' объявлен дважды")
        seen.add(s.id)
        if s.kind not in ("rss", "telegram", "steam"):
            errs.append(f"источник '{s.id}': неизвестный kind '{s.kind}'")
        if s.tier not in (1, 2, 3):
            errs.append(f"источник '{s.id}': tier должен быть 1, 2 или 3")
    for role, chain in cfg.llm.roles.items():
        for alias in chain:
            if alias not in cfg.llm.models:
                errs.append(f"llm.roles.{role}: неизвестная модель '{alias}'")
    for alias, m in cfg.llm.models.items():
        if m.provider not in cfg.llm.providers:
            errs.append(f"llm.models.{alias}: неизвестный провайдер '{m.provider}'")
        elif not m.model:
            errs.append(f"llm.models.{alias}: пустое имя модели")
    for name, p in cfg.llm.providers.items():
        if not p.optional and not p.api_key:
            errs.append(f"llm.providers.{name}: нет api_key")
        if p.kind not in ("gemini", "openai"):
            errs.append(f"llm.providers.{name}: kind должен быть gemini или openai")
    return errs
