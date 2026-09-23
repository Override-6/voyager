"""Free web search without an API key: DuckDuckGo first, then Brave, then Bing (HTML scraping).

Each engine is an async function returning hits; parsers are pure functions of the HTML so they can be tested
offline. An engine that blocks us (bot check, 4xx/5xx, no results) is skipped for COOLDOWN seconds so a blocked
engine costs one wasted request per five minutes, not one per search.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import httpx2
from lxml import html as lhtml

COOLDOWN = 300.0
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
RECENCY = {"day": ("d", "pd"), "week": ("w", "pw"), "month": ("m", "pm"), "year": ("y", "py")}  # (duckduckgo, brave)


class SearchError(Exception):
    pass


@dataclass
class Hit:
    title: str
    url: str
    snippet: str = ""


def _text(node) -> str:
    return " ".join(node.text_content().split()) if node is not None else ""


def _first(node, xpath: str):
    found = node.xpath(xpath)
    return found[0] if found else None


# ------------------------------------------------------------------ parsers
def parse_ddg(page: str) -> list[Hit]:
    doc, hits = lhtml.fromstring(page), []
    for a in doc.xpath('//a[contains(@class,"result__a")]'):
        if a.xpath('ancestor::div[contains(@class,"result--ad")]'):
            continue
        href = a.get("href", "")
        real = parse_qs(urlparse(href).query).get("uddg", [href])[0]  # DDG wraps links in a redirect
        box = _first(a, 'ancestor::div[contains(concat(" ", normalize-space(@class), " "), " result ")][1]')
        snip = _first(box, './/*[contains(@class,"result__snippet")]') if box is not None else None
        if real.startswith("http"):
            hits.append(Hit(_text(a), real, _text(snip)))
    return hits


def parse_brave(page: str) -> list[Hit]:
    doc, hits = lhtml.fromstring(page), []
    for n in doc.xpath('//div[contains(@class,"snippet") and @data-type="web"]'):
        a = _first(n, './/a[starts-with(@href,"http")]')
        title = _first(n, './/*[contains(@class,"search-snippet-title") or contains(concat(" ", @class, " "), " title ")]')
        snip = _first(n, './/*[contains(@class,"generic-snippet")]')
        if a is not None and title is not None:
            hits.append(Hit(_text(title), a.get("href"), _text(snip)))
    return hits


def _unwrap_bing(href: str) -> str:
    u = parse_qs(urlparse(href).query).get("u", [""])[0]
    if u.startswith("a1"):  # /ck/a?...&u=a1<base64 of the real url>
        try:
            return base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode()
        except ValueError:
            pass
    return href


def parse_bing(page: str) -> list[Hit]:
    doc, hits = lhtml.fromstring(page), []
    for n in doc.xpath('//li[contains(@class,"b_algo")]'):
        a = _first(n, ".//h2/a")
        snip = _first(n, './/div[contains(@class,"b_caption")]//p | .//*[contains(@class,"b_lineclamp")]')
        if a is not None:
            hits.append(Hit(_text(a), _unwrap_bing(a.get("href", "")), _text(snip)))
    return hits


# ------------------------------------------------------------------ engines
def _check(r: httpx2.Response, engine: str) -> None:
    body = r.text[:4000].lower()
    if r.status_code != 200 or "anomaly" in body or "captcha" in body:
        raise SearchError(f"{engine} blocked the request (HTTP {r.status_code})")


async def ddg(c: httpx2.AsyncClient, q: str, recency: str | None) -> list[Hit]:
    data = {"q": q, "b": ""} | ({"df": RECENCY[recency][0]} if recency else {})
    r = await c.post("https://html.duckduckgo.com/html/", data=data, headers={"Referer": "https://html.duckduckgo.com/"})
    _check(r, "DuckDuckGo")
    return parse_ddg(r.text)


async def brave(c: httpx2.AsyncClient, q: str, recency: str | None) -> list[Hit]:
    params = {"q": q, "source": "web"} | ({"tf": RECENCY[recency][1]} if recency else {})
    r = await c.get("https://search.brave.com/search", params=params)
    _check(r, "Brave")
    return parse_brave(r.text)


async def bing(c: httpx2.AsyncClient, q: str, recency: str | None) -> list[Hit]:  # no recency filter (needs opaque params)
    r = await c.get("https://www.bing.com/search", params={"q": q, "setlang": "en"})
    _check(r, "Bing")
    return parse_bing(r.text)


Engine = Callable[[httpx2.AsyncClient, str, "str | None"], Awaitable[list[Hit]]]
ENGINES: list[tuple[str, Engine]] = [("DuckDuckGo", ddg), ("Brave", brave), ("Bing", bing)]
_blocked_until: dict[str, float] = {}


def reset_cooldowns() -> None:
    _blocked_until.clear()


async def search(c: httpx2.AsyncClient, query: str, limit: int, recency: str | None = None) -> tuple[str, list[Hit]]:
    """Returns (engine name, hits) from the first engine that answers; raises SearchError listing every failure."""
    problems: list[str] = []
    for name, fn in ENGINES:
        if _blocked_until.get(name, 0) > time.monotonic():
            problems.append(f"{name}: skipped (recently blocked)")
            continue
        try:
            hits = await fn(c, query, recency)
        except (SearchError, httpx2.HTTPError, ValueError) as e:  # ValueError: unparseable HTML
            _blocked_until[name] = time.monotonic() + COOLDOWN
            problems.append(f"{name}: {e or type(e).__name__}")
            continue
        if hits:
            return name, hits[:limit]
        problems.append(f"{name}: no results")
    raise SearchError("; ".join(problems))
