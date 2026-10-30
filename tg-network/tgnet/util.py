"""Small shared helpers: time, text normalisation, near-duplicate hashing, JSON extraction."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

log = logging.getLogger("tgnet")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "trafilatura", "htmldate", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def now() -> float:
    return time.time()


def local_dt(ts: float, tz: str) -> datetime:
    return datetime.fromtimestamp(ts, ZoneInfo(tz))


def day_start(ts: float, tz: str) -> float:
    d = local_dt(ts, tz)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.strip().split(":")
    return int(h), int(m)


def in_window(ts: float, window: str | None, tz: str) -> bool:
    """True if ts falls inside a 'HH:MM-HH:MM' local window (may wrap past midnight)."""
    if not window:
        return False
    a, b = window.split("-")
    d = local_dt(ts, tz)
    cur = d.hour * 60 + d.minute
    ah, am = parse_hhmm(a)
    bh, bm = parse_hhmm(b)
    start, end = ah * 60 + am, bh * 60 + bm
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end


def window_end(ts: float, window: str, tz: str) -> float:
    """Next moment at/after ts when the window closes."""
    _, b = window.split("-")
    bh, bm = parse_hhmm(b)
    d = local_dt(ts, tz)
    end = d.replace(hour=bh, minute=bm, second=0, microsecond=0)
    if end.timestamp() <= ts:
        end += timedelta(days=1)
    return end.timestamp()


def iso(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --------------------------------------------------------------------------- urls

_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|yclid$|mc_|ref$|ref_src$|igshid$|at_)", re.I)


def canonical_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)]
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower() or "https", host, path, urlencode(q), ""))


def domain(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def domain_matches(host: str, pattern: str) -> bool:
    host, pattern = host.lower().strip("."), pattern.lower().strip(".")
    return host == pattern or host.endswith("." + pattern)


# --------------------------------------------------------------------------- text

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    import html as _html

    return _WS.sub(" ", _html.unescape(_TAG.sub(" ", s))).strip()


def clip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


_STOP = set(
    """
    the a an of to in on for and or with at by from is are was were be been as it its this that
    after before over into about than new says say said will has have had not but
    и в во на с со к ко по о об от до за из у для что как это не но а же ли бы при над под
    про после перед его ее её их он она они был была были будет который которая также уже
    """.split()
)
_WORD = re.compile(r"[0-9a-zа-я]+", re.I)


def tokens(text: str) -> list[str]:
    t = (text or "").lower().replace("ё", "е")
    return [w for w in _WORD.findall(t) if len(w) > 1 and w not in _STOP]


def simhash(text: str) -> int:
    toks = tokens(text)
    feats = toks + [a + " " + b for a, b in zip(toks, toks[1:])]
    if not feats:
        return 0
    v = [0] * 64
    for f in feats:
        h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
        for i in range(64):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= 1 << i
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def jaccard(a: str, b: str) -> float:
    sa, sb = set(tokens(a)), set(tokens(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def detect_lang(text: str) -> str:
    cyr = sum(1 for c in text if "а" <= c.lower() <= "я" or c in "ёЁ")
    lat = sum(1 for c in text if "a" <= c.lower() <= "z")
    return "ru" if cyr > lat else "en"


_NUM = re.compile(r"(?<![\w])\d+(?:[.,:]\d+)*")


def numbers_in(text: str) -> set[str]:
    """Numeric tokens in text, normalised (1,5 -> 1.5)."""
    out = set()
    for m in _NUM.findall(text or ""):
        out.add(m.replace(",", "."))
    return out


def stable_hash(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- json

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str):
    """Best-effort: parse the first JSON object/array in an LLM reply."""
    if text is None:
        raise ValueError("empty reply")
    t = text.strip()
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    for m in _FENCE.finditer(t):
        try:
            return json.loads(m.group(1).strip())
        except ValueError:
            continue
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(t)):
                c = t[i]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                    continue
                if c == '"':
                    in_str = True
                elif c == opener:
                    depth += 1
                elif c == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(t[start : i + 1])
                        except ValueError:
                            break
            start = t.find(opener, start + 1)
    raise ValueError("no JSON found in reply: " + clip(t, 200))


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
