import { KeyboardEvent, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Message, getMessages, streamChat } from "../api";
import { Activity, emitPulse, emitSend } from "../fx/activity";
import type { Connection } from "../shell";

type Line = Message & { pending?: boolean };
type Notice = { tone: "warn" | "bad" | "dim"; text: string };

function greeting(address: string) {
  const h = new Date().getHours();
  const part = h < 5 ? "Working late" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  return `${part}, ${address}.`;
}

export default function ChatView({
  conn,
  configured,
  address,
  conversationId,
  onConversation,
  onActivity,
  goSettings,
}: {
  conn: Connection | null;
  configured: boolean;
  address: string;
  conversationId: string | null;
  onConversation: (id: string | null, changed: boolean) => void;
  onActivity: (a: Activity) => void;
  goSettings: () => void;
}) {
  const [lines, setLines] = useState<Line[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const abort = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const ownConversation = useRef<string | null>(null);

  // Load a conversation picked from history (but not the one this view just created).
  useEffect(() => {
    if (conversationId === ownConversation.current) return;
    ownConversation.current = conversationId;
    setNotice(null);
    if (!conversationId || !conn) {
      setLines([]);
      return;
    }
    getMessages(conn, conversationId)
      .then(setLines)
      .catch((e) => setNotice({ tone: "bad", text: (e as Error).message }));
  }, [conversationId, conn]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [lines]);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  }, [input]);

  useEffect(() => {
    if (!streaming) onActivity(input.trim() ? "typing" : "idle");
  }, [input, streaming, onActivity]);

  const send = async () => {
    const text = input.trim();
    if (!conn || !text || streaming) return;
    setInput("");
    setNotice(null);
    emitSend();
    onActivity("thinking");
    const tempUser = `u-${Date.now()}`;
    const tempReply = `r-${Date.now()}`;
    setLines((l) => [...l, { id: tempUser, role: "user", content: text }, { id: tempReply, role: "model", content: "", pending: true }]);
    setStreaming(true);
    const ctrl = new AbortController();
    abort.current = ctrl;
    let failed = false;
    try {
      await streamChat(conn, { message: text, conversation_id: conversationId }, ctrl.signal, {
        meta: (d) => {
          if (d.conversation_id !== ownConversation.current) {
            ownConversation.current = d.conversation_id;
            onConversation(d.conversation_id, true);
          }
        },
        notice: (d) => setNotice({ tone: "warn", text: d.message }),
        delta: (d) => {
          onActivity("speaking");
          emitPulse();
          setLines((l) => l.map((x) => (x.id === tempReply ? { ...x, content: x.content + d.text } : x)));
        },
        error: (d) => {
          failed = true;
          setNotice({ tone: "bad", text: d.message });
        },
      });
    } catch (e) {
      if ((e as Error).name === "AbortError") setNotice({ tone: "dim", text: "Stopped." });
      else {
        failed = true;
        setNotice({ tone: "bad", text: (e as Error).message });
      }
    } finally {
      setLines((l) => l.filter((x) => !(x.id === tempReply && !x.content)).map((x) => ({ ...x, pending: false })));
      setStreaming(false);
      abort.current = null;
      onActivity(failed ? "error" : "idle");
      onConversation(ownConversation.current, true);
      box.current?.focus();
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const empty = lines.length === 0;

  return (
    <div className={`stage ${empty ? "is-empty" : ""}`}>
      <div className="transcript" aria-live="polite">
        {empty && (
          <div className="hello">
            <div className="hello-line">{greeting(address)}</div>
            <div className="hello-sub">{configured ? "Systems ready. What do you need?" : "Connect Gemini in Settings to bring me online."}</div>
          </div>
        )}
        {lines.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            {m.role === "model" && <div className="who">JARVIS</div>}
            <div className="text">
              {m.content}
              {m.pending && !m.content && (
                <span className="thinking">
                  <i />
                  <i />
                  <i />
                </span>
              )}
              {m.pending && m.content && <span className="cursor" />}
            </div>
          </div>
        ))}
        <div ref={bottom} />
      </div>

      <div className="dock">
        {notice && <div className={`notice ${notice.tone}`}>{notice.text}</div>}
        {!configured && conn && (
          <button className="connect" onClick={goSettings}>
            Connect Gemini to start →
          </button>
        )}
        <div className={`composer ${streaming ? "busy" : ""}`}>
          <textarea
            ref={box}
            rows={1}
            placeholder={configured ? "Ask JARVIS…" : "Gemini not connected"}
            value={input}
            disabled={!configured || !conn}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKey}
            aria-label="Message JARVIS"
          />
          {streaming ? (
            <button className="send stop" onClick={() => abort.current?.abort()} aria-label="Stop generating" title="Stop">
              <svg viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="2" /></svg>
            </button>
          ) : (
            <button className="send" disabled={!configured || !input.trim()} onClick={send} aria-label="Send" title="Send (Enter)">
              <svg viewBox="0 0 24 24"><path d="M4 12h13M12 5l7 7-7 7" /></svg>
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
