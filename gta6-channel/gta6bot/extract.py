"""Достаём из страницы статьи основной текст и главную картинку."""
from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import aiohttp

from .textutil import domain_of, strip_html

log = logging.getLogger(__name__)

_META_IMAGE = (
    re.compile(r"""<meta[^>]+(?:property|name)=["'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["'][^>]*?content=["']([^"']+)["']""", re.I),
    re.compile(r"""<meta[^>]+content=["']([^"']+)["'][^>]*?(?:property|name)=["'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["']""", re.I),
)
_IMG = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)
# Ссылки-переадресации агрегаторов: страницу статьи по ним не получить
_NO_FETCH = ("news.google.com", "youtube.com", "youtu.be", "reddit.com", "x.com", "twitter.com", "t.me")


@dataclass
class Article:
    text: str = ""
    image: str | None = None


def first_image(markup: str, base_url: str = "") -> str | None:
    """Первая осмысленная картинка из HTML (анонс ленты)."""
    for m in _IMG.finditer(markup or ""):
        src = html.unescape(m.group(1)).strip()
        if not src or src.startswith("data:"):
            continue
        low = src.lower()
        if any(x in low for x in ("pixel", "tracker", "feedburner", "/emoji/", "avatar", "gravatar", "1x1", "spacer")):
            continue
        return urljoin(base_url, src) if base_url else src
    return None


def meta_image(page: str, base_url: str) -> str | None:
    for rx in _META_IMAGE:
        m = rx.search(page)
        if m:
            src = html.unescape(m.group(1)).strip()
            if src:
                return urljoin(base_url, src)
    return None


def _extract_text(page: str, url: str) -> str:
    """Основной текст страницы. trafilatura точнее; запасной путь — абзацы <p>."""
    try:
        import trafilatura  # тяжёлый импорт, поэтому ленивый

        text = trafilatura.extract(page, url=url, include_comments=False, include_tables=False, favor_precision=True)
        if text and len(text) > 200:
            return text.strip()
    except Exception as e:  # noqa: BLE001 — сторонний парсер не должен ронять сборщик
        log.debug("trafilatura не справилась с %s: %s", url, e)
    body = re.search(r"(?is)<article[^>]*>(.*?)</article>", page)
    scope = body.group(1) if body else page
    paragraphs = [strip_html(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", scope)]
    return "\n\n".join(p for p in paragraphs if len(p) > 60)


async def fetch_article(http: aiohttp.ClientSession, url: str, *, max_bytes: int = 2_500_000) -> Article:
    """Скачать страницу и вытащить текст с картинкой. При любой проблеме — пустой Article."""
    if any(domain_of(url).endswith(d) for d in _NO_FETCH):
        return Article()
    try:
        async with http.get(url, allow_redirects=True, headers={"Accept": "text/html,application/xhtml+xml"}) as resp:
            if resp.status != 200 or "html" not in resp.headers.get("Content-Type", "html").lower():
                return Article()
            final_url = str(resp.url)
            if any(domain_of(final_url).endswith(d) for d in _NO_FETCH):
                return Article()
            raw = await resp.content.read(max_bytes)
            page = raw.decode(resp.charset or "utf-8", errors="replace")
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError, LookupError) as e:
        log.info("Не удалось скачать статью %s: %s", url, e)
        return Article()
    text = await asyncio.to_thread(_extract_text, page, final_url)
    return Article(text=text, image=meta_image(page, final_url))
