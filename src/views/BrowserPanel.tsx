import { KeyboardEvent, MouseEvent, WheelEvent, useCallback, useEffect, useRef, useState } from "react";
import type { Connection } from "../shell";
import { BrowserTab, getBrowserTabs, startBrowser } from "../api";

/**
 * JARVIS's own browser, inside the app. The browser itself runs headless in the background (agent/webdrive.py),
 * so nothing ever opens on the desktop. This panel attaches to one of its tabs over the DevTools socket,
 * streams it as a live picture (Page.startScreencast) and forwards the user's mouse, wheel and keys, so the
 * user can watch JARVIS work, sign in, link WhatsApp, or review a cart and pay — all without a separate window.
 * Closing the panel only stops the picture; JARVIS keeps working in the hidden tab.
 */

type Meta = { deviceWidth: number; deviceHeight: number };

class Cdp {
  private ws: WebSocket;
  private n = 0;
  private waiting = new Map<number, (r: Record<string, unknown>) => void>();
  onEvent: (method: string, params: Record<string, unknown>) => void = () => {};
  onClose: () => void = () => {};
  ready: Promise<void>;

  constructor(url: string) {
    this.ws = new WebSocket(url);
    this.ready = new Promise((ok, bad) => {
      this.ws.onopen = () => ok();
      this.ws.onerror = () => bad(new Error("Could not attach to JARVIS's browser."));
    });
    this.ws.onclose = () => this.onClose();
    this.ws.onmessage = (m) => {
      const msg = JSON.parse(m.data as string);
      if (msg.id && this.waiting.has(msg.id)) {
        this.waiting.get(msg.id)!(msg.result ?? {});
        this.waiting.delete(msg.id);
      } else if (msg.method) this.onEvent(msg.method, msg.params ?? {});
    };
  }

  send(method: string, params: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    if (this.ws.readyState !== WebSocket.OPEN) return Promise.resolve({});
    const id = ++this.n;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((ok) => this.waiting.set(id, ok));
  }

  close() {
    this.onClose = () => {};
    this.ws.close();
  }
}

const MODS = (e: { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }) =>
  (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
const BUTTON = ["left", "middle", "right"] as const;

export default function BrowserPanel({ conn, onClose }: { conn: Connection | null; onClose: () => void }) {
  const [tabs, setTabs] = useState<BrowserTab[]>([]);
  const [tabId, setTabId] = useState<string | null>(null);
  const [frame, setFrame] = useState<string>("");
  const [url, setUrl] = useState("");
  const [editing, setEditing] = useState(false);
  const [note, setNote] = useState<string | null>("Starting JARVIS's browser…");
  const cdp = useRef<Cdp | null>(null);
  const meta = useRef<Meta>({ deviceWidth: 1280, deviceHeight: 800 });
  const img = useRef<HTMLImageElement>(null);
  const lastMove = useRef(0);
  const editingRef = useRef(editing);
  editingRef.current = editing;

  const refresh = useCallback(async () => {
    if (!conn) return;
    try {
      let r = await getBrowserTabs(conn);
      if (!r.running || !r.tabs.length) r = await startBrowser(conn);
      setTabs(r.tabs);
      setTabId((cur) => (cur && r.tabs.some((t) => t.id === cur) ? cur : r.tabs[0]?.id ?? null));
      if (!r.tabs.length) setNote("JARVIS's browser has no tabs open.");
    } catch (e) {
      setNote((e as Error).message);
    }
  }, [conn]);

  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 4000); // JARVIS opens tabs (shop, gmail, whatsapp, maps) as it works
    return () => window.clearInterval(t);
  }, [refresh]);

  // Attach to the chosen tab and stream it.
  useEffect(() => {
    const tab = tabs.find((t) => t.id === tabId);
    if (!tab) return;
    const c = new Cdp(tab.ws);
    cdp.current = c;
    let alive = true;
    c.onEvent = (method, p) => {
      if (method === "Page.screencastFrame") {
        meta.current = p.metadata as Meta;
        setFrame(`data:image/jpeg;base64,${p.data as string}`);
        c.send("Page.screencastFrameAck", { sessionId: p.sessionId });
      } else if (method === "Page.frameNavigated" && !(p.frame as { parentId?: string }).parentId) {
        if (!editingRef.current) setUrl((p.frame as { url: string }).url);
      }
    };
    c.onClose = () => alive && setNote("The connection to JARVIS's browser closed.");
    c.ready
      .then(async () => {
        setNote(null);
        await c.send("Page.enable");
        const r = (await c.send("Runtime.evaluate", { expression: "location.href", returnByValue: true })) as { result?: { value?: string } };
        if (r.result?.value) setUrl(r.result.value);
        await c.send("Page.startScreencast", { format: "jpeg", quality: 75, maxWidth: 1600, maxHeight: 1000, everyNthFrame: 1 });
      })
      .catch((e: Error) => setNote(`${e.message} Restart JARVIS if this keeps happening.`));
    return () => {
      alive = false;
      c.send("Page.stopScreencast").finally(() => c.close());
      if (cdp.current === c) cdp.current = null;
    };
    // the socket follows the tab id, not its changing title
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabId, tabs.find((t) => t.id === tabId)?.ws]);

  const point = (e: { clientX: number; clientY: number }) => {
    const r = img.current!.getBoundingClientRect();
    return {
      x: ((e.clientX - r.left) / r.width) * meta.current.deviceWidth,
      y: ((e.clientY - r.top) / r.height) * meta.current.deviceHeight,
    };
  };

  const mouse = (type: string, e: MouseEvent<HTMLImageElement>) => {
    if (!cdp.current || !img.current) return;
    if (type === "mouseMoved") {
      const now = performance.now();
      if (now - lastMove.current < 40) return;
      lastMove.current = now;
    }
    cdp.current.send("Input.dispatchMouseEvent", {
      type, ...point(e), modifiers: MODS(e),
      button: type === "mouseMoved" ? (e.buttons & 1 ? "left" : "none") : BUTTON[e.button] ?? "left",
      buttons: e.buttons, clickCount: type === "mouseMoved" ? 0 : e.detail || 1,
    });
  };

  const wheel = (e: WheelEvent<HTMLImageElement>) => {
    cdp.current?.send("Input.dispatchMouseEvent", { type: "mouseWheel", ...point(e), deltaX: e.deltaX, deltaY: e.deltaY, modifiers: MODS(e) });
  };

  const key = (type: "keyDown" | "keyUp", e: KeyboardEvent<HTMLDivElement>) => {
    if (!cdp.current) return;
    e.preventDefault();
    e.stopPropagation(); // Escape and shortcuts belong to the page, not the app
    if (type === "keyDown" && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v") {
      navigator.clipboard.readText().then((text) => { if (text) cdp.current?.send("Input.insertText", { text }); }).catch(() => {});
      return;
    }
    const printable = e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
    cdp.current.send("Input.dispatchKeyEvent", {
      type: type === "keyDown" ? (printable ? "keyDown" : "rawKeyDown") : "keyUp",
      key: e.key, code: e.code, windowsVirtualKeyCode: e.keyCode, nativeVirtualKeyCode: e.keyCode,
      modifiers: MODS(e), ...(type === "keyDown" && printable ? { text: e.key } : e.key === "Enter" && type === "keyDown" ? { text: "\r" } : {}),
    });
  };

  const go = (to: string) => {
    let u = to.trim();
    if (!u) return;
    if (!/^[a-z]+:\/\//i.test(u)) u = /\s/.test(u) || !u.includes(".") ? `https://www.google.com/search?q=${encodeURIComponent(u)}` : `https://${u}`;
    cdp.current?.send("Page.navigate", { url: u });
    setEditing(false);
  };

  const history = async (step: number) => {
    const h = (await cdp.current?.send("Page.getNavigationHistory")) as { currentIndex?: number; entries?: { id: number }[] } | undefined;
    const target = h?.entries?.[(h.currentIndex ?? 0) + step];
    if (target) cdp.current?.send("Page.navigateToHistoryEntry", { entryId: target.id });
  };

  return (
    <div className="browser-scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <section className="glass browser-panel" role="dialog" aria-label="JARVIS browser">
        <header className="browser-bar">
          <div className="browser-tabs" role="tablist">
            {tabs.map((t) => (
              <button key={t.id} role="tab" aria-selected={t.id === tabId} className={t.id === tabId ? "on" : ""} title={t.url} onClick={() => setTabId(t.id)}>
                {t.title || "New tab"}
              </button>
            ))}
          </div>
          <button className="close" onClick={onClose} aria-label="Hide browser" title="Hide (JARVIS keeps working in the background)">✕</button>
        </header>
        <div className="browser-nav">
          <button onClick={() => history(-1)} aria-label="Back">‹</button>
          <button onClick={() => history(1)} aria-label="Forward">›</button>
          <button onClick={() => cdp.current?.send("Page.reload")} aria-label="Reload">↻</button>
          <form onSubmit={(e) => { e.preventDefault(); go(url); }}>
            <input value={url} onFocus={() => setEditing(true)} onBlur={() => setEditing(false)} onChange={(e) => setUrl(e.target.value)}
                   onKeyDown={(e) => e.stopPropagation()} spellCheck={false} aria-label="Address" />
          </form>
        </div>
        <div className="browser-view" tabIndex={0} onKeyDown={(e) => key("keyDown", e)} onKeyUp={(e) => key("keyUp", e)}>
          {frame && (
            <img ref={img} src={frame} alt="JARVIS's browser" draggable={false}
                 onMouseDown={(e) => { (e.currentTarget.parentElement as HTMLElement).focus(); mouse("mousePressed", e); }}
                 onMouseUp={(e) => mouse("mouseReleased", e)} onMouseMove={(e) => mouse("mouseMoved", e)}
                 onWheel={wheel} onContextMenu={(e) => e.preventDefault()} />
          )}
          {note && <p className="browser-note">{note}</p>}
        </div>
      </section>
    </div>
  );
}
