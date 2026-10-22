"""Key-free web research: DuckDuckGo results plus the text of the top pages."""
from __future__ import annotations

import asyncio
import html
import re
import urllib.parse
from typing import Any

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_RESULT = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?(?:class="result__snippet"[^>]*>(.*?)</a>)?', re.S)


class ResearchError(Exception):
    pass


def _clean(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment or "")).strip()


def _real_url(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    q = urllib.parse.urlparse(href)
    if "duckduckgo.com" in q.netloc and q.path.startswith("/l/"):
        target = urllib.parse.parse_qs(q.query).get("uddg", [""])[0]
        return urllib.parse.unquote(target)
    return href


def page_text(raw: str, limit: int = 3500) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form)[^>]*>.*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


async def web_research(query: str, max_results: int = 6, read_pages: int = 3) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), headers={"User-Agent": UA}, follow_redirects=True) as c:
        try:
            r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
        except httpx.HTTPError as e:
            raise ResearchError(f"Web search failed: could not reach DuckDuckGo ({e}).") from e
        if r.status_code != 200:
            raise ResearchError(f"Web search failed: DuckDuckGo answered HTTP {r.status_code}.")
        results = []
        for href, title, snippet in _RESULT.findall(r.text):
            url = _real_url(href)
            if "duckduckgo.com/y.js" in url or not url.startswith("http"):
                continue  # ads
            results.append({"title": _clean(title), "url": url, "snippet": _clean(snippet)})
            if len(results) >= max_results:
                break
        if not results:
            raise ResearchError("Web search returned no results (DuckDuckGo may be rate-limiting; try again shortly).")

        async def fetch(url: str) -> dict[str, str] | None:
            try:
                resp = await c.get(url, timeout=12.0)
                if resp.status_code == 200 and "html" in resp.headers.get("content-type", ""):
                    return {"url": url, "text": page_text(resp.text)}
            except httpx.HTTPError:
                return None
            return None

        pages = [p for p in await asyncio.gather(*(fetch(x["url"]) for x in results[:read_pages])) if p]
    return {"query": query, "results": results, "pages": pages,
            "note": "Answer from these sources and cite the URLs you used."}
