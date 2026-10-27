"""JARVIS's own web search and page reading: no AI service and no key involved.

Search goes to Yahoo, and to DuckDuckGo when Yahoo has nothing (DuckDuckGo often refuses automated
searches). Results that don't match the question (or are adult sites) are dropped, so junk never reaches the
answer. Headlines from Google News and Bing News come alongside, with their dates, for anything recent. The
top pages are fetched (HTML or PDF), their main text extracted, and the passages that best answer the
question are kept, each with its URL, so the answer can quote the exact words it relies on. read_webpage
does the same for one link the user gives. Only the public internet is reachable (no localhost or private
addresses)."""
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
_YAHOO_LINK = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_YAHOO_SNIPPET = re.compile(r'<div class="compText[^"]*"[^>]*>(.*?)</div>', re.S)
_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_ADULT = re.compile(r"porn|xxx|xnxx|xhamster|xvideos|redtube|sex|nsfw|onlyfans|escort", re.I)
_STOP = {"the", "and", "for", "with", "about", "what", "when", "where", "who", "how", "are", "was", "this", "that", "from",
         "give", "details", "detail", "info", "information", "latest", "news", "updates", "update", "tell", "please", "any"}
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
MAX_PAGE_BYTES = 3_000_000
PASSAGE_CHARS = 600


class ResearchError(Exception):
    pass


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment or ""))).strip()


def _tag(xml: str, name: str) -> str:
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", xml, re.S)
    return _clean(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.S)) if m else ""


def _key_words(query: str) -> set[str]:
    return {w for w in _words(query) if w not in _STOP}


def relevant(results: list[dict[str, str]], query: str) -> list[dict[str, str]]:
    """Results that are about the question: enough of its words in the title or snippet, and no adult sites."""
    want = _key_words(query)
    need = 1 if len(want) <= 2 else max(2, (len(want) + 1) // 2)
    keep = []
    for r in results:
        if _ADULT.search(urllib.parse.urlsplit(r["url"]).netloc + " " + r["title"]):
            continue
        if len(want & _words(r["title"] + " " + r.get("snippet", "") + " " + r["url"])) >= need:
            keep.append(r)
    return keep


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


def parse_yahoo(page: str) -> list[dict[str, str]]:
    found = []
    for block in page.split('<div class="dd algo')[1:]:
        m = _YAHOO_LINK.search(block)
        if not m:
            continue
        href, inner = m.groups()
        target = re.search(r"/RU=([^/]+)/", href)
        url = urllib.parse.unquote(target.group(1)) if target else html.unescape(href)
        if not url.startswith("http") or "search.yahoo.com" in url:
            continue
        heading = re.search(r"<h3[^>]*>(.*?)</h3>", inner, re.S)  # the link also holds the site name and address
        title = _clean(heading.group(1) if heading else inner)
        snippet = _YAHOO_SNIPPET.search(block)
        found.append({"title": title, "url": url, "snippet": _clean(snippet.group(1))[:300] if snippet else ""})
    return found


def parse_ddg(page: str) -> list[dict[str, str]]:
    found = []
    for href, title, snippet in _DDG.findall(page):
        url = _real_url(href)
        if "duckduckgo.com/y.js" in url or not url.startswith("http"):
            continue  # ads
        found.append({"title": _clean(title), "url": url, "snippet": _clean(snippet)})
    return found


def parse_news(feed: str, source: str = "") -> list[dict[str, str]]:
    out = []
    for item in _ITEM.findall(feed):
        title = _tag(item, "title")
        if not title:
            continue
        out.append({"title": title, "date": _tag(item, "pubDate")[:16], "source": _tag(item, "source") or source,
                    "summary": _tag(item, "description")[:300] if source else "", "url": _tag(item, "link")})
    return out


async def _search(c: httpx.AsyncClient, query: str, max_results: int) -> tuple[str, list[dict[str, str]]]:
    errors = []
    engines = (("Yahoo", "GET", "https://search.yahoo.com/search", {"params": {"p": query}}, parse_yahoo),
               ("DuckDuckGo", "POST", "https://html.duckduckgo.com/html/", {"data": {"q": query}}, parse_ddg))
    for name, method, url, kw, parse in engines:
        try:
            r = await c.request(method, url, **kw)
            if r.status_code in (429, 500, 502, 503):  # these refuse now and then; once more usually works
                await asyncio.sleep(1.0)
                r = await c.request(method, url, **kw)
        except httpx.HTTPError as e:
            errors.append(f"{name} unreachable ({type(e).__name__})")
            continue
        if r.status_code != 200:
            errors.append(f"{name} HTTP {r.status_code}")
            continue
        found = relevant(parse(r.text), query)
        if found:
            return name, found[:max_results]
        errors.append(f"{name}: nothing relevant")
    raise ResearchError("Web search found nothing relevant: " + "; ".join(errors) + ". Answer from what you know, saying it may "
                        "be out of date, or ask the user to rephrase; don't search again with the same words.")


async def news(c: httpx.AsyncClient, query: str, limit: int = 6) -> list[dict[str, str]]:
    """Recent headlines about the question, newest sources first, from Google News and Bing News (no key needed)."""
    feeds = await asyncio.gather(
        c.get("https://news.google.com/rss/search", params={"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}),
        c.get("https://www.bing.com/news/search", params={"q": query, "format": "rss"}),
        return_exceptions=True)
    items: list[dict[str, str]] = []
    for feed, source in zip(feeds, ("", "Bing News")):
        if isinstance(feed, httpx.Response) and feed.status_code == 200:
            items += parse_news(feed.text, source)
    seen, out = set(), []
    for it in relevant([{**i, "snippet": i["summary"]} for i in items], query):
        key = it["title"].lower()[:60]
        if key not in seen:
            seen.add(key)
            out.append({k: v for k, v in it.items() if k != "snippet" and v})
    return out[:limit]


async def web_research(query: str, max_results: int = 6, read_pages: int = 3) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), headers=HEADERS, follow_redirects=True) as c:
        searched, headlines = await asyncio.gather(_search(c, query, max_results), news(c, query), return_exceptions=True)
        if isinstance(headlines, BaseException):
            headlines = []
        if isinstance(searched, BaseException):
            if not headlines or not isinstance(searched, ResearchError):
                raise searched
            engine, results, texts = "news only", [], []
        else:
            engine, results = searched
            texts = await asyncio.gather(*(_fetch_text(c, x["url"]) for x in results[:read_pages]))
    sources = [{"url": r["url"], "title": r["title"], "passages": passages(t, query)} for r, t in zip(results, texts) if t]
    out: dict[str, Any] = {"query": query, "engine": engine, "results": results, "sources": sources,
                           "note": "This is enough to answer: answer now from these passages and headlines; don't search again "
                                   "unless something essential is missing. Quote the exact words you rely on and name the site."}
    if headlines:
        out["news"] = headlines
    return out


async def read_webpage(url: str, question: str = "") -> dict[str, Any]:
    """One page (HTML or PDF): the passages answering the question, or the start of it."""
    _public(url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), headers=HEADERS, follow_redirects=True) as c:
        text = await _fetch_text(c, url)
    if not text:
        raise ResearchError(f"I couldn't read {url} (not reachable, not a web page or PDF, or it blocks readers).")
    return {"url": url, "chars": len(text),
            "passages": passages(text, question, limit=5) if question else [text[:6000]],
            "note": "Quote the exact words you rely on and name the page."}
