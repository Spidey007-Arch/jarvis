"""JARVIS's own web search and page reading: no AI service and no key involved.

Search goes to DuckDuckGo, and to Bing when DuckDuckGo refuses (it rate-limits). The top pages are fetched
(HTML or PDF), their main text extracted, and the passages that best answer the question are kept, each
with its URL, so the answer can quote the exact words it relies on. read_webpage does the same for one
link the user gives. Only the public internet is reachable (no localhost or private addresses)."""
from __future__ import annotations

import asyncio
import html
import io
import re
import urllib.parse
from typing import Any

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_DDG = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?(?:class="result__snippet"[^>]*>(.*?)</a>)?', re.S)
_BING = re.compile(r'<li class="b_algo".*?<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a></h2>(.*?)</li>', re.S)
MAX_PAGE_BYTES = 3_000_000
PASSAGE_CHARS = 600


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


def page_text(raw: str, limit: int = 20000) -> str:
    """The readable main text of an HTML page: the <article> or <main> if there is one, without menus and scripts."""
    body = raw
    for tag in ("article", "main"):
        m = re.search(rf"(?is)<{tag}[^>]*>(.*?)</{tag}>", raw)
        if m and len(m.group(1)) > 1500:
            body = m.group(1)
            break
    body = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form|aside|button)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", body)
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:limit]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def passages(text: str, question: str, limit: int = 3) -> list[str]:
    """The parts of a page that best answer a question, in page order."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if len(p.strip()) > 40]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) > PASSAGE_CHARS and buf:
            chunks.append(buf)
            buf = ""
        buf = (buf + " " + p).strip()
    if buf:
        chunks.append(buf)
    want = _words(question)
    scored = sorted(((len(want & _words(c)), i, c) for i, c in enumerate(chunks)), key=lambda x: (-x[0], x[1]))
    best = [x for x in scored if x[0] > 0][:limit] or scored[:1]
    return [c for _s, _i, c in sorted(best, key=lambda x: x[1])]


def _public(url: str) -> None:
    from .public_apis import _public_host

    host = urllib.parse.urlsplit(url).hostname or ""
    if not url.startswith(("http://", "https://")) or not host:
        raise ResearchError("Only http(s) links can be read.")
    try:
        _public_host(host)
    except Exception as e:  # noqa: BLE001 - its message says what is wrong
        raise ResearchError(str(e)) from e


async def _fetch_text(c: httpx.AsyncClient, url: str) -> str | None:
    """A page's text: HTML pages and PDFs (read locally), within a size limit; every redirect stays public."""
    try:
        _public(url)
        async with c.stream("GET", url, timeout=15.0) as resp:
            _public(str(resp.url))
            if resp.status_code != 200:
                return None
            kind = resp.headers.get("content-type", "")
            data = b""
            async for chunk in resp.aiter_bytes():
                data += chunk
                if len(data) > MAX_PAGE_BYTES:
                    break
    except (httpx.HTTPError, ResearchError):
        return None
    if "pdf" in kind or url.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            return "\n\n".join(f"[p. {i}] {(pg.extract_text() or '').strip()}" for i, pg in enumerate(reader.pages[:60], 1))
        except Exception:  # noqa: BLE001 - not every "PDF" is readable
            return None
    if "html" in kind or "text" in kind:
        return page_text(data.decode(resp.encoding or "utf-8", errors="replace"))
    return None


async def _search(c: httpx.AsyncClient, query: str, max_results: int) -> tuple[str, list[dict[str, str]]]:
    errors = []
    try:
        r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
        if r.status_code == 200:
            found = []
            for href, title, snippet in _DDG.findall(r.text):
                url = _real_url(href)
                if "duckduckgo.com/y.js" in url or not url.startswith("http"):
                    continue  # ads
                found.append({"title": _clean(title), "url": url, "snippet": _clean(snippet)})
            if found:
                return "DuckDuckGo", found[:max_results]
        errors.append(f"DuckDuckGo HTTP {r.status_code}")
    except httpx.HTTPError as e:
        errors.append(f"DuckDuckGo unreachable ({type(e).__name__})")
    try:
        r = await c.get("https://www.bing.com/search", params={"q": query, "setlang": "en"})
        if r.status_code == 200:
            found = [{"title": _clean(t), "url": html.unescape(u), "snippet": _clean(s)[:300]} for u, t, s in _BING.findall(r.text)
                     if u.startswith("http")]
            if found:
                return "Bing", found[:max_results]
        errors.append(f"Bing HTTP {r.status_code}")
    except httpx.HTTPError as e:
        errors.append(f"Bing unreachable ({type(e).__name__})")
    raise ResearchError("Web search found nothing: " + "; ".join(errors) + ". Try again shortly or rephrase.")


async def web_research(query: str, max_results: int = 6, read_pages: int = 3) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), headers={"User-Agent": UA}, follow_redirects=True) as c:
        engine, results = await _search(c, query, max_results)
        texts = await asyncio.gather(*(_fetch_text(c, x["url"]) for x in results[:read_pages]))
    sources = [{"url": r["url"], "title": r["title"], "passages": passages(t, query)} for r, t in zip(results, texts) if t]
    return {"query": query, "engine": engine, "results": results, "sources": sources,
            "note": "Answer from these passages. Quote the exact words you rely on and name the site they come from."}


async def read_webpage(url: str, question: str = "") -> dict[str, Any]:
    """One page (HTML or PDF): the passages answering the question, or the start of it."""
    _public(url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), headers={"User-Agent": UA}, follow_redirects=True) as c:
        text = await _fetch_text(c, url)
    if not text:
        raise ResearchError(f"I couldn't read {url} (not reachable, not a web page or PDF, or it blocks readers).")
    return {"url": url, "chars": len(text),
            "passages": passages(text, question, limit=5) if question else [text[:6000]],
            "note": "Quote the exact words you rely on and name the page."}
