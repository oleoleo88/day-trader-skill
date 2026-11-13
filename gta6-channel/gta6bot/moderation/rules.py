"""Правила модерации без обращений к сети: по описанию сообщения возвращают вердикт.

Модуль чистый (никакого Telegram и базы), поэтому легко проверяется тестами.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import ModerationCfg
from ..textutil import domain_of, fold, has_profanity, phrase_pattern

_INVITE = re.compile(r"(?:t\.me|telegram\.me|telegram\.dog)/(?:\+|joinchat/)[\w-]+", re.I)
_TME = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/([A-Za-z][\w]{3,31})(?:[/?#]\S*)?", re.I)
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.I)
# домен без схемы: «заходи на site.com»; список зон намеренно узкий, чтобы не ловить «и т.д.»
_BARE_DOMAIN = re.compile(
    r"(?<![\w@.])((?:[a-z0-9-]+\.)+(?:com|net|org|ru|рф|io|me|gg|xyz|top|site|online|shop|store|club|pro|app|link|cc|su|ly|to|biz|info|live|fun|vip|bet|win))(?![\w.])(?:/\S*)?",
    re.I)
_TME_SERVICE = {"joinchat", "addstickers", "addemoji", "share", "proxy", "socks", "iv", "s", "c", "boost", "addlist", "addtheme", "setlanguage", "login", "confirmphone", "bg", "invoice", "giftcode", "m", "k", "a"}


@dataclass
class MsgInfo:
    text: str = ""
    urls: list[str] = field(default_factory=list)       # всё, что Telegram разметил как ссылку
    mentions: list[str] = field(default_factory=list)   # @имена без собаки
    mention_kinds: dict[str, str] = field(default_factory=dict)  # имя → channel | group | user | unknown
    custom_emoji: int = 0
    has_buttons: bool = False
    is_story: bool = False
    forwarded_from_channel: bool = False
    sender_is_channel: bool = False
    user_name: str = ""
    prior_messages: int = 0     # сколько сообщений автор уже написал в этом чате
    recent_count: int = 0       # сообщений за окно флуда, включая это
    duplicate_count: int = 0    # одинаковых сообщений за 2 минуты, включая это


@dataclass
class Verdict:
    action: str = "allow"       # allow | delete | suspect
    rule: str = ""
    reason: str = ""            # короткое объяснение для пользователя и журнала
    penalty: str = "none"       # none | warn | mute | ban
    mute_minutes: int = 0
    # Спросить ИИ-судью, если он доступен: "verdict" — судья может отменить удаление целиком,
    # "penalty" — сообщение удаляется в любом случае, судья решает только, банить ли автора
    confirm: str = ""
    notice: bool = True         # показывать ли автору пояснение в чате


ALLOW = Verdict()


class Rules:
    def __init__(self, cfg: ModerationCfg, own_usernames: set[str] | None = None):
        self.cfg = cfg
        self.hard = phrase_pattern(cfg.hard_phrases)
        self.soft = phrase_pattern(cfg.soft_phrases)
        self.allow_domains = {d.lower().removeprefix("www.") for d in cfg.links.allow_domains}
        self.own = {u.lower().lstrip("@") for u in (own_usernames or set()) if u}

    # ── вспомогательное ─────────────────────────────────────────────────────
    def is_newcomer(self, m: MsgInfo) -> bool:
        return m.prior_messages < self.cfg.links.newcomer_messages

    def _domain_allowed(self, domain: str) -> bool:
        return any(domain == d or domain.endswith("." + d) for d in self.allow_domains)

    def collect_links(self, m: MsgInfo) -> tuple[list[str], list[str], bool]:
        """→ (внешние домены, чужие t.me-имена, есть ли ссылка-приглашение)."""
        haystack = " ".join([m.text, *m.urls])
        invite = bool(_INVITE.search(haystack))
        tme_names: list[str] = []
        for match in _TME.finditer(haystack):
            name = match.group(1).lower()
            if name not in _TME_SERVICE and name not in self.own and name not in tme_names:
                tme_names.append(name)
        domains: list[str] = []
        candidates = list(m.urls) + _URL.findall(m.text) + [x.group(0) for x in _BARE_DOMAIN.finditer(m.text)]
        for raw in candidates:
            d = domain_of(raw.strip(".,;:!?)»\"'"))
            if d and "." in d and d not in ("t.me", "telegram.me", "telegram.dog") and d not in domains:
                domains.append(d)
        return domains, tme_names, invite

    # ── вердикт ─────────────────────────────────────────────────────────────
    def evaluate(self, m: MsgInfo) -> Verdict:
        cfg = self.cfg
        newcomer = self.is_newcomer(m)

        if m.sender_is_channel and cfg.block_channel_senders:
            return Verdict("delete", "channel_sender", "комментарии от имени каналов отключены", "ban", notice=False)

        if cfg.flood.messages > 0 and m.recent_count > cfg.flood.messages:
            return Verdict("delete", "flood", "слишком много сообщений подряд", "mute", cfg.flood.mute_minutes)

        if cfg.flood.duplicates > 0 and m.duplicate_count >= cfg.flood.duplicates:
            return Verdict("delete", "duplicate", "повтор одного и того же сообщения", "warn")

        if m.has_buttons or (m.is_story and newcomer):
            return Verdict("delete", "promo_format", "рекламный формат сообщения", "ban" if newcomer else "warn",
                           notice=not newcomer)

        domains, tme_names, invite = self.collect_links(m)
        foreign_chats = [n for n in m.mentions if m.mention_kinds.get(n.lower()) in ("channel", "group")
                         and n.lower() not in self.own]
        if cfg.links.block_invites and (invite or tme_names or foreign_chats):
            return Verdict("delete", "invite", "ссылки и приглашения в другие каналы запрещены",
                           "ban" if newcomer else "warn", confirm="penalty" if newcomer else "", notice=not newcomer)

        folded = fold(m.text)
        if self.hard and folded:
            hit = self.hard.search(folded)
            if hit:
                return Verdict("delete", "hard_phrase", "похоже на спам или мошенничество",
                               "ban" if newcomer else "warn", confirm="verdict", notice=not newcomer)

        blocked = [d for d in domains if not self._domain_allowed(d)]
        if blocked and (cfg.links.policy == "all" or (cfg.links.policy == "newcomers" and newcomer)):
            who = "новым участникам " if cfg.links.policy == "newcomers" else ""
            return Verdict("delete", "link", f"ссылки на сторонние сайты {who}запрещены", "warn")

        if cfg.profanity != "off" and has_profanity(m.text):
            return Verdict("delete", "profanity", "в комментариях без мата", "warn" if cfg.profanity == "warn" else "none")

        # дальше — не нарушения, а поводы показать сообщение ИИ-судье
        if cfg.max_custom_emoji > 0 and m.custom_emoji > cfg.max_custom_emoji:
            return Verdict("suspect", "emoji_wall", "стена эмодзи")
        if self.soft and folded and self.soft.search(folded):
            return Verdict("suspect", "soft_phrase", "подозрительная формулировка")
        if m.forwarded_from_channel and newcomer:
            return Verdict("suspect", "forward", "пересылка из канала от нового участника")
        if newcomer and m.user_name:
            name = fold(m.user_name)
            if (self.hard and self.hard.search(name)) or _TME.search(m.user_name) or _BARE_DOMAIN.search(m.user_name):
                return Verdict("suspect", "name", "реклама в имени профиля")
        return ALLOW
