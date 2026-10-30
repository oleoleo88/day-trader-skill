"""Fetching and parsing: RSS/Atom, public Telegram channels (t.me/s), Steam official news, articles."""

from __future__ import annotations

import calendar
import json
import logging
import re
from dataclasses import dataclass

import httpx

from .config import Source
from .util import canonical_url, clip, strip_html

log = logging.getLogger("tgnet.sources")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36 tgnet-newsdesk")
MAX_BYTES = 4_000_000


@dataclass
class Entry:
    url: str
    title: str
    summary: str
    published_at: float | None
    image: str | None = None
    views: int | None = None
    message_id: int | None = None


class FetchError(Exception):
    pass


async def fetch(http: httpx.AsyncClient, url: str, etag: str | None = None, last_modified: str | None = None,
                timeout: float = 20) -> tuple[int, str, dict]:
    headers = {"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        r = await http.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        raise FetchError(f"{url}: {e!r}") from e
    if r.status_code == 304:
        return 304, "", dict(r.headers)
    if r.status_code >= 400:
        raise FetchError(f"{url}: HTTP {r.status_code}")
    if len(r.content) > MAX_BYTES:
        raise FetchError(f"{url}: response too large ({len(r.content)} bytes)")
    return r.status_code, r.text, dict(r.headers)


# --------------------------------------------------------------------------- RSS

def parse_rss(text: str) -> list[Entry]:
    import feedparser

    feed = feedparser.parse(text)
    if feed.bozo and not feed.entries:
        raise FetchError(f"feed parse error: {feed.bozo_exception!r}")
    out = []
    for e in feed.entries:
        link = e.get("link") or ""
        title = strip_html(e.get("title"))
        if not link or not title:
            continue
        ts = None
        for k in ("published_parsed", "updated_parsed"):
            if e.get(k):
                ts = float(calendar.timegm(e[k]))
                break
        img = None
        for k in ("media_content", "media_thumbnail"):
            for m in e.get(k) or []:
                if m.get("url"):
                    img = m["url"]
                    break
            if img:
                break
        if not img:
            for enc in e.get("enclosures") or []:
                if str(enc.get("type", "")).startswith("image") and enc.get("href"):
                    img = enc["href"]
                    break
        summary = strip_html(e.get("summary") or e.get("description") or "")
        out.append(Entry(link, title, clip(summary, 1500), ts, img))
    return out


# --------------------------------------------------------------------------- Telegram web preview

_VIEWS = re.compile(r"([\d.,]+)\s*([KMkm]?)")


def parse_views(s: str) -> int | None:
    m = _VIEWS.search(s or "")
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    mult = {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return int(num * mult)


def parse_tme(text: str, handle: str | None = None) -> list[Entry]:
    import lxml.html
    from datetime import datetime

    doc = lxml.html.fromstring(text)
    out = []
    for m in doc.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " tgme_widget_message ")'
                       ' and @data-post]'):
        post = m.get("data-post") or ""
        if "/" not in post:
            continue
        chan, mid = post.rsplit("/", 1)
        if handle and chan.lower() != handle.lower():
            continue  # reposts preview other channels
        body = m.xpath('.//div[contains(@class,"js-message_text")]')
        txt = ""
        if body:
            for br in body[0].xpath(".//br"):
                br.tail = "\n" + (br.tail or "")
            txt = body[0].text_content().strip()
        ts = None
        dt = m.xpath('.//time[@datetime]/@datetime')
        if dt:
            try:
                ts = datetime.fromisoformat(dt[0]).timestamp()
            except ValueError:
                ts = None
        img = None
        for style in m.xpath('.//a[contains(@class,"tgme_widget_message_photo_wrap")]/@style'):
            mm = re.search(r"url\(['\"]?([^'\")]+)", style)
            if mm:
                img = mm.group(1)
                break
        views = None
        v = m.xpath('.//span[contains(@class,"tgme_widget_message_views")]/text()')
        if v:
            views = parse_views(v[0])
        first = txt.split("\n", 1)[0].strip()
        try:
            mid_i = int(mid)
        except ValueError:
            continue
        out.append(Entry(f"https://t.me/{chan}/{mid}", clip(first, 160) if first else "", clip(txt, 3000), ts, img,
                         views, mid_i))
    return out


# --------------------------------------------------------------------------- Steam

def parse_steam(text: str, official_only: bool = True) -> list[Entry]:
    data = json.loads(text)
    out = []
    for n in (data.get("appnews") or {}).get("newsitems") or []:
        if official_only and n.get("feedname") != "steam_community_announcements":
            continue
        body = strip_html(re.sub(r"\[/?[a-z0-9*]+[^\]]*\]", " ", n.get("contents", "")))
        out.append(Entry(n.get("url", ""), strip_html(n.get("title")), clip(body, 2000), float(n.get("date") or 0) or None))
    return out


async def poll(http: httpx.AsyncClient, src: Source, etag: str | None, last_modified: str | None
               ) -> tuple[list[Entry] | None, str | None, str | None]:
    """Fetch one source. Returns (entries or None if unchanged, etag, last_modified)."""
    if src.kind == "telegram":
        handle = src.url.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
        _, text, _ = await fetch(http, f"https://t.me/s/{handle}")
        entries = [e for e in parse_tme(text, handle) if e.summary]
        return entries, None, None
    status, text, headers = await fetch(http, src.url, etag, last_modified)
    if status == 304:
        return None, etag, last_modified
    h = {k.lower(): v for k, v in headers.items()}
    if src.kind == "steam":
        entries = parse_steam(text, src.official_only)
    else:
        entries = parse_rss(text)
    return entries, h.get("etag"), h.get("last-modified")


# --------------------------------------------------------------------------- articles

def extract_article(html_text: str, url: str = "") -> dict:
    """Main text + metadata via trafilatura. Returns {"text","title","date","image"} (may be empty)."""
    try:
        import trafilatura
    except ImportError:  # pragma: no cover
        return {"text": strip_html(html_text)[:8000]}
    raw = trafilatura.extract(html_text, url=url or None, output_format="json", with_metadata=True,
                              include_comments=False, include_tables=False, favor_precision=True)
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except ValueError:
        return {}
    return {"text": d.get("text") or "", "title": d.get("title") or "", "date": d.get("date") or "",
            "image": d.get("image") or ""}


async def fetch_article(http: httpx.AsyncClient, url: str) -> dict:
    if "t.me/" in url:
        return {}
    _, text, _ = await fetch(http, url, timeout=25)
    import asyncio

    return await asyncio.to_thread(extract_article, text, url)


def normalize_entry_url(e: Entry) -> str:
    return canonical_url(e.url)
