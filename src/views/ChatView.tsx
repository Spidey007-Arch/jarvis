import { KeyboardEvent, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ActionEvent, Message, answerConfirmation, forSpeech, getMessages, listenNow, speak, streamChat } from "../api";
import { Activity, emitPulse, emitSend } from "../fx/activity";
import type { Connection } from "../shell";

type Line = Message & { pending?: boolean };

const ICON: Record<ActionEvent["status"], string> = {
  running: "",
  awaiting_confirmation: "?",
  done: "✓",
  failed: "✕",
  declined: "⊘",
};

/** One action JARVIS took. Risky ones become an Allow/Deny card until answered. */
function ActionRow({ a, live, onAnswer }: { a: ActionEvent; live: boolean; onAnswer: (ok: boolean) => void }) {
  const [answered, setAnswered] = useState(false);
  if (a.status === "awaiting_confirmation" && live) {
    return (
      <li className="act confirm-card">
        <div className="confirm-title">
          <span className="confirm-badge">PERMISSION</span> {a.label}
        </div>
        {a.detail && a.detail !== "{}" && <code className="confirm-detail">{a.detail}</code>}
        <div className="confirm-buttons">
          <button className="allow" disabled={answered} onClick={() => { setAnswered(true); onAnswer(true); }}>Allow</button>
          <button className="deny" disabled={answered} onClick={() => { setAnswered(true); onAnswer(false); }}>Deny</button>
        </div>
      </li>
    );
  }
  const status = a.status === "awaiting_confirmation" ? "declined" : a.status;
  return (
    <li className={`act ${status}`} title={a.error || undefined}>
      <span className="act-icon">{status === "running" ? <i className="spin-dot" /> : ICON[status]}</span>
      <span className="act-label">{a.label}</span>
      {status === "failed" && a.error && <span className="act-err">{a.error}</span>}
      {status === "declined" && <span className="act-err">not done</span>}
    </li>
  );
}
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
  reload = 0,
}: {
  conn: Connection | null;
  configured: boolean;
  address: string;
  conversationId: string | null;
  onConversation: (id: string | null, changed: boolean) => void;
  onActivity: (a: Activity) => void;
  goSettings: () => void;
  reload?: number; // bumped when a live voice turn was saved to this conversation
}) {
  const [lines, setLines] = useState<Line[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const abort = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const ownConversation = useRef<string | null>(null);

  // Load a conversation picked from history (but not the one this view just created), or refresh it
  // when a live voice session added to it.
  const lastReload = useRef(reload);
  useEffect(() => {
    if (conversationId === ownConversation.current && reload === lastReload.current) return;
    lastReload.current = reload;
    ownConversation.current = conversationId;
    setNotice(null);
    if (!conversationId || !conn) {
      setLines([]);
      return;
    }
    getMessages(conn, conversationId)
      .then(setLines)
      .catch((e) => setNotice({ tone: "bad", text: (e as Error).message }));
  }, [conversationId, conn, reload]);

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

  const send = async (spoken?: string) => {
    const text = (spoken ?? input).trim();
    if (!conn || !text || streaming) return;
    if (spoken === undefined) setInput("");
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
    let full = "";
    // Replies are revealed word by word at a natural pace; the orb pulses with each word.
    let buffer = "";
    let timer: number | undefined;
    let drained: (() => void) | null = null;
    const pump = () => {
      if (!buffer) {
        window.clearInterval(timer);
        timer = undefined;
        drained?.();
        drained = null;
        return;
      }
      let n = Math.max(4, Math.ceil(buffer.length / 45));
      const space = buffer.indexOf(" ", n);
      if (space !== -1 && space - n < 12) n = space + 1;
      const piece = buffer.slice(0, n);
      buffer = buffer.slice(n);
      onActivity("speaking");
      emitPulse();
      setLines((l) => l.map((x) => (x.id === tempReply ? { ...x, content: x.content + piece } : x)));
    };
    try {
      await streamChat(conn, { message: text, conversation_id: conversationId, voice: spoken !== undefined }, ctrl.signal, {
        meta: (d) => {
          if (d.conversation_id !== ownConversation.current) {
            ownConversation.current = d.conversation_id;
            onConversation(d.conversation_id, true);
          }
        },
        notice: (d) => setNotice({ tone: "warn", text: d.message }),
        delta: (d) => {
          full += d.text;
          buffer += d.text;
          if (timer === undefined) timer = window.setInterval(pump, 35);
        },
        error: (d) => {
          failed = true;
          setNotice({ tone: "bad", text: d.message });
        },
        tool: (d) => {
          onActivity(d.status === "awaiting_confirmation" ? "idle" : "thinking");
          setLines((l) =>
            l.map((x) => {
              if (x.id !== tempReply) return x;
              const acts = x.actions ?? [];
              const i = acts.findIndex((a) => a.id === d.id);
              return { ...x, actions: i < 0 ? [...acts, d] : acts.map((a, j) => (j === i ? d : a)) };
            }),
          );
        },
      });
    } catch (e) {
      if ((e as Error).name === "AbortError") setNotice({ tone: "dim", text: "Stopped." });
      else {
        failed = true;
        setNotice({ tone: "bad", text: (e as Error).message });
      }
    } finally {
      if (ctrl.signal.aborted && buffer) {
        const rest = buffer;
        buffer = "";
        setLines((l) => l.map((x) => (x.id === tempReply ? { ...x, content: x.content + rest } : x)));
      } else if (timer !== undefined) {
        await new Promise<void>((resolve) => (drained = resolve));
      }
      window.clearInterval(timer);
      setLines((l) => l.filter((x) => !(x.id === tempReply && !x.content && !x.actions?.length)).map((x) => ({ ...x, pending: false })));
      setStreaming(false);
      abort.current = null;
      onActivity(failed ? "error" : "idle");
      onConversation(ownConversation.current, true);
      // Spoken to, so answer out loud.
      if (spoken !== undefined && !failed && !ctrl.signal.aborted && full.trim()) {
        speak(conn, forSpeech(full)).catch((e) => setNotice({ tone: "warn", text: `Could not speak the reply: ${(e as Error).message}` }));
      }
      box.current?.focus();
    }
  };

  // Push-to-talk: the mic button or Ctrl+Space anywhere in the window starts listening at once.
  const talk = () => {
    if (!conn || streaming) return;
    listenNow(conn).catch((e) => setNotice({ tone: "bad", text: (e as Error).message }));
  };
  const talkRef = useRef(talk);
  talkRef.current = talk;
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.ctrlKey && e.code === "Space") {
        e.preventDefault();
        talkRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Words heard after the wake word arrive as a window event (see VoiceLink in App).
  const sendRef = useRef(send);
  sendRef.current = send;
  useEffect(() => {
    const onVoice = (e: Event) => sendRef.current((e as CustomEvent<string>).detail);
    window.addEventListener("jarvis:voice-text", onVoice);
    return () => window.removeEventListener("jarvis:voice-text", onVoice);
  }, []);

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
            <div className="hello-sub">{configured ? "Systems ready. What do you need?" : "Connect an AI provider in Settings to bring me online."}</div>
          </div>
        )}
        {lines.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            {m.role === "model" && <div className="who">JARVIS</div>}
            {!!m.actions?.length && (
              <ul className="acts">
                {m.actions.map((a) => (
                  <ActionRow key={a.id} a={a} live={!!m.pending} onAnswer={(ok) => conn && answerConfirmation(conn, a.id, ok).catch((e) => setNotice({ tone: "bad", text: (e as Error).message }))} />
                ))}
              </ul>
            )}
            <div className="text">
              {m.content}
              {m.pending && !m.content && (
                <span className="thinking" role="status" aria-label="JARVIS is thinking">
                  <span className="thinking-text">Thinking</span>
                  <span className="thinking-bar" />
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
            Connect an AI provider to start →
          </button>
        )}
        <div className={`composer ${streaming ? "busy" : ""}`}>
          <textarea
            ref={box}
            rows={1}
            placeholder={configured ? "Ask JARVIS…" : "No AI provider connected"}
            value={input}
            disabled={!configured || !conn}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKey}
            aria-label="Message JARVIS"
          />
          <button className="mic" disabled={!configured || !conn || streaming} onClick={talk} aria-label="Talk to JARVIS" title="Talk (Ctrl+Space)">
            <svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" /></svg>
          </button>
          {streaming ? (
            <button className="send stop" onClick={() => abort.current?.abort()} aria-label="Stop generating" title="Stop">
              <svg viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="2" /></svg>
            </button>
          ) : (
            <button className="send" disabled={!configured || !input.trim()} onClick={() => send()} aria-label="Send" title="Send (Enter)">
              <svg viewBox="0 0 24 24"><path d="M4 12h13M12 5l7 7-7 7" /></svg>
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
