"""Работа с текстом: HTML для Telegram, нормализация заголовков и URL, «свёртка» для антиспама."""
from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ── HTML для Telegram ────────────────────────────────────────────────────────

_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")


def esc(text: object) -> str:
    """Экранирование под parse_mode=HTML."""
    return html.escape(str(text), quote=False)


def esc_attr(text: object) -> str:
    return html.escape(str(text), quote=True)


def html_balanced(markup: str) -> bool:
    """True, если теги закрыты в правильном порядке (Telegram отвергает кривую вложенность)."""
    stack: list[str] = []
    for m in _TAG.finditer(markup):
        closing, name = m.group(1), m.group(2).lower()
        if closing:
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


_BOLD = re.compile(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*")
_ITALIC = re.compile(r"(?<![A-Za-z0-9_])__(?=\S)([^\n]+?)(?<=\S)__(?![A-Za-z0-9_])")
_SPOILER = re.compile(r"\|\|(?=\S)([^\n]+?)(?<=\S)\|\|")
_BULLET = re.compile(r"(?m)^[ \t]*(?:[-*–—]|•)[ \t]+")
_HEADING = re.compile(r"(?m)^#{1,6}[ \t]+")


def render_markup(text: str) -> str:
    """Лёгкая разметка редактора → HTML Telegram.

    **жирный**, __курсив__, ||спойлер||, строки с «- » или «• » становятся маркерами.
    Всё остальное экранируется. Если разметка получилась невалидной, она снимается целиком.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = _HEADING.sub("", text)
    text = _BULLET.sub("• ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    safe = esc(text)
    out = _BOLD.sub(r"<b>\1</b>", safe)
    out = _SPOILER.sub(r"<tg-spoiler>\1</tg-spoiler>", out)
    out = _ITALIC.sub(r"<i>\1</i>", out)
    if not html_balanced(out):
        out = safe
    # непарные остатки маркеров читателю не нужны
    return out.replace("**", "").replace("||", "")


def strip_html(markup: str) -> str:
    """HTML ленты/страницы → простой текст."""
    if not markup:
        return ""
    t = re.sub(r"(?is)<(script|style|noscript|iframe)[^>]*>.*?</\1>", " ", markup)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t).replace("\xa0", " ")
    t = re.sub(r"[ \t\f\v]+", " ", t)
    t = re.sub(r" ?\n ?", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def visible_text(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


def visible_len(markup: str) -> int:
    """Длина так, как её считает Telegram: UTF-16 единицы видимого текста."""
    return len(visible_text(markup).encode("utf-16-le")) // 2


def clip(text: str, limit: int) -> str:
    """Обрезать простой текст по границе слова."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: max(1, limit - 1)]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-–—") + "…"


def plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    """plural_ru(3, ('день', 'дня', 'дней')) → 'дня'."""
    n = abs(n) % 100
    if 11 <= n <= 14:
        return forms[2]
    n %= 10
    if n == 1:
        return forms[0]
    if 2 <= n <= 4:
        return forms[1]
    return forms[2]


# ── URL ──────────────────────────────────────────────────────────────────────

_TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|yclid|ysclid|mc_cid|mc_eid|igshid|ref|ref_src|ref_url|from|share|"
                       r"source|cmpid|ocid|ns_.*|at_.*|spm|_openstat|rss|feature|si)$", re.I)


def canonical_url(url: str) -> str:
    """Убрать трекинг и косметику, чтобы одна статья давала один ключ."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (p.hostname or "").lower()
    for prefix in ("www.", "m.", "amp."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    if host in ("youtu.be",):
        vid = p.path.strip("/")
        return f"https://youtube.com/watch?v={vid}" if vid else url
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not _TRACKING.match(k)]
    query.sort()
    path = re.sub(r"/+$", "", p.path) or "/"
    path = re.sub(r"/amp$", "", path) or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def domain_of(url: str) -> str:
    try:
        host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


_YT = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def youtube_id(url: str) -> str | None:
    m = _YT.search(url or "")
    return m.group(1) if m else None


# ── схожесть заголовков ──────────────────────────────────────────────────────

_STOP = frozenset("""
a an the of to in on at for and or but with from by is are was were be been it its this that as has have had
will would can could not no new says said after before over about into more than up out how why what when
и в во на с со к по за из у о об от до для не но а же ли бы что это как так уже еще ещё или при над под
про без все всё его ее её их был была были будет стало стал
""".split())
# В нише эти слова есть почти в каждом заголовке и ничего не различают
_UBIQUITOUS = frozenset({"gta", "6", "rockstar", "games", "game", "игра", "игры"})
_ALIASES = {
    "vi": "6", "six": "6", "шесть": "6", "гта": "gta", "gtavi": "gta", "gta6": "gta", "гта6": "gta",
    "рокстар": "rockstar", "рокстара": "rockstar", "theft": "gta", "auto": "gta", "grand": "gta",
    "трейлер": "trailer", "трейлера": "trailer", "трейлере": "trailer", "трейлеру": "trailer",
    "утечка": "leak", "утечки": "leak", "утечку": "leak", "leaked": "leak", "leaks": "leak",
    "релиз": "release", "релиза": "release", "релизу": "release", "выход": "release", "выхода": "release",
    "дата": "date", "даты": "date", "дату": "date", "перенос": "delay", "перенесли": "delay",
    "delayed": "delay", "delays": "delay", "цена": "price", "цены": "price", "цену": "price",
    "предзаказ": "preorder", "предзаказы": "preorder", "предзаказа": "preorder", "preorders": "preorder",
    "pre-order": "preorder", "пк": "pc",
}


def title_tokens(title: str) -> list[str]:
    """Заголовок → отсортированный список смысловых токенов (для сравнения на дубли)."""
    t = unicodedata.normalize("NFKC", title).lower().replace("ё", "е")
    t = re.sub(r"[^0-9a-zа-я]+", " ", t)
    out: set[str] = set()
    for tok in t.split():
        tok = _ALIASES.get(tok, tok)
        if tok in _STOP or tok in _UBIQUITOUS or len(tok) < 2 and not tok.isdigit():
            continue
        if re.fullmatch(r"[а-я]+", tok) and len(tok) > 5:
            tok = tok[:5]                      # грубое отсечение русских окончаний
        elif re.fullmatch(r"[a-z]+", tok) and len(tok) > 4 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        out.add(tok)
    return sorted(out)


def similarity(a: list[str] | set[str], b: list[str] | set[str]) -> float:
    """Схожесть двух наборов токенов: Жаккар, усиленный вложенностью для коротких заголовков."""
    sa, sb = set(a), set(b)
    if len(sa) < 3 or len(sb) < 3:
        return 0.0
    inter = len(sa & sb)
    if not inter:
        return 0.0
    jaccard = inter / len(sa | sb)
    smaller = min(len(sa), len(sb))
    containment = inter / smaller if smaller >= 4 else 0.0
    return max(jaccard, containment * 0.8)


# ── «свёртка» текста для антиспама ───────────────────────────────────────────

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­‎‏‪‫‬‭‮⁣"), None)
# Латинские и цифровые двойники кириллицы: «зapaбoтoк», «д0х0д» читаются глазом, но не строковым поиском
_LOOKALIKE = str.maketrans({
    "a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "y": "у", "k": "к", "m": "м", "t": "т",
    "h": "н", "b": "в", "0": "о", "3": "з", "@": "а", "$": "с",
})


def fold(text: str) -> str:
    """Привести текст к виду, устойчивому к обфускации спамеров.

    Фразы из стоп-листа сворачиваются этой же функцией, поэтому сравнение симметрично.
    """
    t = unicodedata.normalize("NFKC", text or "").lower().translate(_ZERO_WIDTH).replace("ё", "е")
    t = t.translate(_LOOKALIKE)
    t = re.sub(r"[^0-9a-zа-я]+", " ", t)
    t = re.sub(r"([a-zа-я])\1+", r"\1", t)                       # «зааараб» → «зараб»
    t = re.sub(r"(?<![0-9a-zа-я])(?:[0-9a-zа-я] ){3,}[0-9a-zа-я](?![0-9a-zа-я])",
               lambda m: m.group(0).replace(" ", ""), t)         # «з а р а б о т о к»
    return re.sub(r"\s+", " ", t).strip()


def phrase_pattern(phrases: list[str]) -> re.Pattern[str] | None:
    """Один регэксп на весь список: фраза должна начинаться с начала слова (это основа, окончание свободное)."""
    folded = sorted({fold(p) for p in phrases if fold(p)}, key=len, reverse=True)
    if not folded:
        return None
    return re.compile(r"(?<![0-9a-zа-я])(?:" + "|".join(re.escape(p) for p in folded) + ")")


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]")


def emoji_count(text: str) -> int:
    return len(_EMOJI.findall(text or ""))


# Обсценная лексика: применяется только если moderation.profanity != off
PROFANITY = re.compile(
    r"(?<![а-я])(?:на|по|за|при|вы|от|у|до|не|ни|о|об|с|рас|раз|пере|про|под)?"
    r"(?:ху[йеяюи]|пизд|[её]б(?:а[тлн]|у[тч]|ну|л[оа]|ит|ыр)|бля(?:д|т|$|\b)|муд[аои][кл]|пид[оа]р|гандон|долбо[её]б|залуп)")


def has_profanity(text: str) -> bool:
    t = unicodedata.normalize("NFKC", text or "").lower().translate(_ZERO_WIDTH).replace("ё", "е")
    return bool(PROFANITY.search(t))
