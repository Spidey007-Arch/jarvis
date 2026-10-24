"""Drive a dedicated browser window over the Chrome DevTools Protocol, so JARVIS can shop and order food.

The window runs on its own profile in the JARVIS data folder: the user signs in to shops there once.
JARVIS can open pages, read them as text plus numbered controls, click and type. Paying stays with
the user, and that is enforced here, not left to the model: controls that pay or place an order,
and fields for passwords, cards, OTPs or UPI PINs, are refused."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

PORT = 9333
_BROWSERS = [
    r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
]

# What only the user may press: anything that pays, places or confirms an order.
PAYS = re.compile(
    r"\b(place\s+(your\s+)?order|pay(\s+now|\s+securely|\s*(₹|rs\.?|inr)\s*[\d,]+)?|make\s+(a\s+)?payment|proceed\s+to\s+pay(ment)?"
    r"|confirm\s+(and\s+pay|order|payment|purchase)|complete\s+(purchase|order|payment)|buy\s+now|submit\s+order|checkout\s+&?\s*pay)\b",
    re.I,
)
# Fields JARVIS never fills: credentials and payment secrets.
SECRET_FIELD = re.compile(r"pass(word|code)?|card|cvv|cvc|expir|otp|one-time|upi|vpa|\bpin\b|cc-", re.I)


class WebError(Exception):
    """A browser step failed; the message says what to do about it."""


def _profile() -> Path:
    base = Path(os.environ.get("JARVIS_DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "JARVIS")
    p = base / "browser-profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


_TARGET: str | None = None  # the tab JARVIS created and works in


def _get(path: str, timeout: float = 2.0, method: str = "GET") -> Any:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _running() -> bool:
    try:
        _get("/json/version")
        return True
    except OSError:
        return False


def ensure_browser() -> None:
    if _running():
        return
    exe = next((p for p in (os.path.expandvars(b) for b in _BROWSERS) if os.path.isfile(p)), None)
    if not exe:
        raise WebError("No Brave, Chrome or Edge installation was found for the shopping window.")
    subprocess.Popen(
        [exe, f"--remote-debugging-port={PORT}", "--remote-debugging-address=127.0.0.1", f"--user-data-dir={_profile()}",
         "--no-first-run", "--no-default-browser-check", "about:blank"],
        creationflags=0x00000008,  # DETACHED_PROCESS: the window outlives the agent
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        if _running():
            return
        time.sleep(0.4)
    raise WebError("The shopping browser did not start within 20 seconds.")


def _page() -> dict[str, Any]:
    """JARVIS's own tab: created once and then followed by id, so welcome pages or tabs the user opens
    in the shopping window are never driven by mistake."""
    global _TARGET
    pages = [t for t in _get("/json/list") if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    for t in pages:
        if t.get("id") == _TARGET:
            return t
    t = _get("/json/new?about:blank", method="PUT")
    _TARGET = t.get("id")
    if not t.get("webSocketDebuggerUrl"):
        raise WebError("The shopping browser did not open a tab for JARVIS.")
    return t


class _Tab:
    """A short-lived DevTools connection to the current tab."""

    def __init__(self) -> None:
        from websockets.sync.client import connect

        try:
            self.ws = connect(_page()["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024, open_timeout=10)
        except WebError:
            raise
        except Exception as e:  # noqa: BLE001 - handshake/refused while the tab is mid-swap
            raise WebError(f"Could not attach to the shopping tab: {e}") from e
        self.n = 0

    def __enter__(self) -> "_Tab":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.ws.close()

    def send(self, method: str, params: dict[str, Any] | None = None, timeout: float = 20.0) -> dict[str, Any]:
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise WebError(f"The browser did not answer {method} in time.")
            try:
                msg = json.loads(self.ws.recv(timeout=left))
            except TimeoutError as e:
                raise WebError(f"The page did not answer {method} in time (it may still be loading).") from e
            except Exception as e:  # noqa: BLE001 - the tab swapped processes (navigation) or closed
                raise WebError(f"The connection to the tab dropped during {method}.") from e
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise WebError(f"{method} failed: {msg['error'].get('message')}")
                return msg.get("result", {})

    def eval(self, js: str) -> Any:
        r = self.send("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True})
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise WebError(f"The page script failed: {(d.get('exception') or {}).get('description') or d.get('text')}")
        return r.get("result", {}).get("value")

    def wait_loaded(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.eval("document.readyState") in ("interactive", "complete"):
                break  # usable; waiting for every image on a slow line would take minutes
            time.sleep(0.4)
        time.sleep(1.2)  # shops render results with scripts after "complete"


# Numbers every visible control and returns a compact view of the page.
_READ = r"""
(() => {
  const q = %s, offset = %d, limit = %d;
  document.querySelectorAll('[data-jarvis-id]').forEach(e => e.removeAttribute('data-jarvis-id'));
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],[onclick]';
  const seen = new Set(), all = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || s.visibility === 'hidden' || s.display === 'none' || +s.opacity === 0) continue;
    let label = (el.innerText || el.getAttribute('aria-label') || el.value || el.placeholder || el.title || el.alt || '').replace(/\s+/g, ' ').trim();
    if (!label && el.querySelector('img[alt]')) label = el.querySelector('img[alt]').alt.trim();
    const kind = el.tagName === 'A' ? 'link' : el.tagName === 'INPUT' ? 'input:' + (el.type || 'text') : el.tagName.toLowerCase() === 'select' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : 'button';
    if (!label && !kind.startsWith('input') && kind !== 'textarea') continue;
    if (label.length < 24 && kind !== 'link') {
      // "Add to cart" / "ADD" alone does not say which product or dish: name it from the card it sits in.
      let ctx = '';
      const nameIn = (root) => {
        for (const head of root.querySelectorAll('h2,h3,h4,[class*="title" i],[class*="name" i],a[href]')) {
          const t = head.contains(el) ? '' : (head.innerText || '').replace(/\s+/g, ' ').trim();
          if (t.length >= 12) {
            const price = ((root.innerText || '').match(/(₹|Rs\.?)\s?[\d,]+(\.\d+)?/) || [''])[0];
            return t.slice(0, 60) + (price ? ' · ' + price.replace(/\s+/g, '') : '');
          }
        }
        return '';
      };
      // Shops often render the button apart from the product card and join them by an id (Amazon: data-asin).
      for (const attr of ['data-asin', 'data-product-id', 'data-item-id', 'data-pid', 'data-id']) {
        const keyed = el.closest('[' + attr + ']'), val = keyed && keyed.getAttribute(attr);
        if (!val) continue;
        for (const card of document.querySelectorAll('[' + attr + '="' + CSS.escape(val) + '"]')) { ctx = nameIn(card); if (ctx) break; }
        if (ctx) break;
      }
      for (let up = el.parentElement, depth = 0; up && depth < 12 && !ctx; up = up.parentElement, depth++) {
        for (const head of up.querySelectorAll('h2,h3,h4,[class*="title" i],[class*="name" i],a[href]')) {
          const t = head.contains(el) ? '' : (head.innerText || '').replace(/\s+/g, ' ').trim();
          if (t.length >= 12) { ctx = t.slice(0, 60); break; }  // the first real name in the card: product or dish
        }
      }
      if (ctx && !label.includes(ctx)) label = label + ' — ' + ctx;
    }
    const key = kind + '|' + label.slice(0, 60);
    if (kind === 'link' && seen.has(key)) continue;
    seen.add(key);
    all.push({el, kind, label: label.slice(0, 110)});
  }
  const hits = q ? all.filter(c => c.label.toLowerCase().includes(q.toLowerCase())) : all;
  const page = hits.slice(offset, offset + limit);
  page.forEach((c, i) => c.el.setAttribute('data-jarvis-id', String(offset + i + 1)));
  const main = document.querySelector('main') || document.body;
  return {
    title: document.title, url: location.href,
    text: (main.innerText || '').replace(/\n\s*\n+/g, '\n').slice(0, q ? 800 : 2400),
    controls: page.map((c, i) => `${offset + i + 1}: [${c.kind}] ${c.label}`),
    total_controls: hits.length,
  };
})()
"""

_DESCRIBE = r"""
(() => {
  const el = document.querySelector('[data-jarvis-id="%d"]');
  if (!el) return null;
  el.scrollIntoView({block: 'center'});
  const r = el.getBoundingClientRect();
  const attrs = ['type', 'name', 'id', 'autocomplete', 'placeholder', 'aria-label'].map(a => el.getAttribute(a) || '').join(' ');
  return {label: (el.innerText || el.getAttribute('aria-label') || el.value || el.title || '').replace(/\s+/g, ' ').trim().slice(0, 120),
          attrs, x: r.left + r.width / 2, y: r.top + r.height / 2, tag: el.tagName};
})()
"""


def _settled(timeout: float = 25.0) -> dict[str, Any]:
    """Wait for the current tab to finish loading. Navigation can swap the tab's renderer process and
    drop the DevTools connection, so each check uses a fresh connection."""
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with _Tab() as t:
                t.wait_loaded(max(1.0, deadline - time.time()))
                return {"title": t.eval("document.title"), "url": t.eval("location.href")}
        except (WebError, OSError) as e:
            last = e
            time.sleep(0.5)
    raise WebError(f"The page did not finish loading in time ({last}).")


def open_page(url: str) -> dict[str, Any]:
    if not re.match(r"^https?://", url, re.I):
        raise WebError(f"'{url}' is not a web address.")
    ensure_browser()
    with _Tab() as t:
        t.send("Page.bringToFront")
        try:
            t.send("Page.navigate", {"url": url})
        except WebError:
            pass  # the tab swapped processes while navigating; _settled reconnects
    return _settled(60)


def read_page(find: str = "", offset: int = 0) -> dict[str, Any]:
    if not _running():
        raise WebError("The shopping browser is not open; use web_open first.")
    with _Tab() as t:
        return t.eval(_READ % (json.dumps(find or ""), max(0, int(offset)), 80))


def _locate(t: _Tab, n: int) -> dict[str, Any]:
    info = t.eval(_DESCRIBE % int(n))
    if not info:
        raise WebError(f"Control {n} is not on the page any more; call web_read again.")
    return info


def click(n: int) -> dict[str, Any]:
    with _Tab() as t:
        info = _locate(t, n)
        if PAYS.search(info["label"]):
            raise WebError(f"'{info['label']}' pays for or places the order. JARVIS stops here: "
                           "tell the user the cart is ready and that they must review it and press that button themselves.")
        time.sleep(0.25)
        try:
            for kind in ("mousePressed", "mouseReleased"):
                t.send("Input.dispatchMouseEvent", {"type": kind, "x": info["x"], "y": info["y"], "button": "left", "clickCount": 1})
        except WebError:
            pass  # the click navigated and the tab swapped processes
    return {"clicked": info["label"], **_settled(15)}


def type_into(n: int, text: str, submit: bool = False) -> dict[str, Any]:
    with _Tab() as t:
        info = _locate(t, n)
        if SECRET_FIELD.search(info["attrs"]) or SECRET_FIELD.search(info["label"]):
            raise WebError("That field asks for a password, card, OTP or UPI detail. JARVIS never fills those; the user must type it.")
        for kind in ("mousePressed", "mouseReleased"):
            t.send("Input.dispatchMouseEvent", {"type": kind, "x": info["x"], "y": info["y"], "button": "left", "clickCount": 1})
        t.eval(f'(() => {{ const e = document.querySelector(\'[data-jarvis-id="{int(n)}"]\'); if (e && "value" in e) {{ e.select && e.select(); }} }})()')
        t.send("Input.insertText", {"text": text})
        if submit:
            try:
                for kind in ("keyDown", "keyUp"):
                    t.send("Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
            except WebError:
                pass
    return {"typed_into": info["label"] or info["tag"].lower(), "submitted": submit, **(_settled(15) if submit else {})}
