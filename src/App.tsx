import { ReactNode, useCallback, useEffect, useState } from "react";
import Reactor from "./Reactor";
import type { Activity } from "./fx/activity";
import { useTelemetry } from "./telemetry";
import { Connection, Overall, ShellStatus, getConnection, getStatus, insideShell, onStatus } from "./shell";
import { Quota, VoiceEvent, getQuota, getSettings, getVoiceEvents, getVoiceSeq, startBriefing, stopSpeaking } from "./api";
import TaskPanel from "./views/TaskPanel";
import ModelPicker from "./views/ModelPicker";
import Onboarding from "./views/Onboarding";
import ChatView from "./views/ChatView";
import HistoryPanel from "./views/HistoryPanel";
import MemoryView from "./views/MemoryView";
import SettingsView from "./views/SettingsView";
import SystemPanel from "./views/SystemPanel";
import GlobeView from "./views/GlobeView";

type Drawer = "history" | "memory" | "settings" | "system" | null;

const LABEL: Record<Overall, string> = {
  starting: "STARTING",
  online: "ONLINE",
  degraded: "DEGRADED",
  reconnecting: "RECONNECTING",
  failed: "OFFLINE",
};

const COLOR: Record<Overall, string> = {
  online: "#29d3ff",
  starting: "#ffb84d",
  degraded: "#ffb84d",
  reconnecting: "#ffb84d",
  failed: "#ff6b6b",
};

const ICONS: Record<Exclude<Drawer, null>, ReactNode> = {
  history: <path d="M4 6h16M4 12h10M4 18h13" />,
  memory: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2v4M12 18v4M2 12h4M18 12h4M5 5l3 3M16 16l3 3M19 5l-3 3M8 16l-3 3" />
    </>
  ),
  system: <path d="M3 12h4l3-8 4 16 3-8h4" />,
  settings: (
    <>
      <circle cx="12" cy="12" r="3.2" />
      <path d="M12 2.5v3M12 18.5v3M4.2 6.5l2.6 1.5M17.2 16l2.6 1.5M4.2 17.5l2.6-1.5M17.2 8l2.6-1.5" />
    </>
  ),
};

const TITLES: Record<Exclude<Drawer, null>, string> = {
  history: "Conversations",
  memory: "Memory",
  system: "System",
  settings: "Settings",
};

function Gauge({ label, value }: { label: string; value: number | undefined }) {
  const v = Math.max(0, Math.min(100, value ?? 0));
  const c = 2 * Math.PI * 16;
  return (
    <div className={`gauge ${v > 90 ? "hot" : ""}`} title={`${label} ${value === undefined ? "—" : `${v.toFixed(0)}%`}`}>
      <svg viewBox="0 0 40 40">
        <circle cx="20" cy="20" r="16" className="g-track" />
        <circle cx="20" cy="20" r="16" className="g-val" strokeDasharray={`${(v / 100) * c} ${c}`} />
      </svg>
      <span className="g-num">{value === undefined ? "—" : v.toFixed(0)}</span>
      <span className="g-label">{label}</span>
    </div>
  );
}

const clock = (epoch: number) => new Date(epoch * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

/** Today's free-tier usage as JARVIS counted it. Neither Google nor most gateways report remaining
 *  quota, so limits appear once a provider's own quota error has named them. */
const kilo = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`);

function QuotaStrip({ conn, activity }: { conn: Connection | null; activity: Activity }) {
  const [q, setQ] = useState<Quota | null>(null);
  const [open, setOpen] = useState(false);
  const busy = activity === "thinking" || activity === "speaking";

  useEffect(() => {
    if (!conn) return;
    let alive = true;
    const load = () => getQuota(conn).then((v) => alive && setQ(v)).catch(() => {});
    load();
    const id = window.setInterval(load, busy ? 5000 : 30000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [conn, busy]);

  if (!q || !q.models.length) return null;
  const main = q.models[0];
  const out = q.models.filter((m) => m.state === "out").length;
  const pct = main.limit ? Math.min(100, (main.used / main.limit) * 100) : null;
  return (
    <div className={`quota glass ${main.state === "out" ? "is-out" : ""}`}>
      <button className="quota-row" onClick={() => setOpen(!open)} aria-expanded={open} title="Free-tier usage today, counted by JARVIS">
        <span className="quota-label">QUOTA</span>
        <span className="quota-model">{main.model}</span>
        <span className="quota-num">{main.state === "out" ? "OUT" : main.limit ? `${main.used}/${main.limit}` : `${main.used} today`}</span>
        {pct !== null && <span className="quota-bar"><i style={{ width: `${pct}%` }} /></span>}
        {out > 0 && <span className="quota-out">{out} out</span>}
      </button>
      {open && (
        <div className="quota-list">
          <div className="quota-meta">
            via {q.provider_name} · resets {clock(q.resets_at)}
          </div>
          {q.models.map((m) => (
            <div key={m.model} className={`quota-item ${m.state}`}>
              <span>{m.model}</span>
              <span>
                {m.state === "out" ? `${m.reason ?? "out"} · back ${m.until ? clock(m.until) : "later"}` : m.limit ? `${m.used} / ${m.limit}` : `${m.used} today`}
              </span>
            </div>
          ))}
          {q.tokens && q.tokens.input + q.tokens.output > 0 && (
            <div className="quota-meta">
              Today: {kilo(q.tokens.input)} tokens in, {kilo(q.tokens.output)} out
              {q.tokens.input ? ` · ${Math.round((100 * q.tokens.cached) / q.tokens.input)}% from cache` : ""}
            </div>
          )}
          <div className="quota-note">Counted by JARVIS; limits appear once a quota error names them.</div>
        </div>
      )}
    </div>
  );
}

export type VoiceState = "off" | "listening" | "transcribing" | "speaking" | "connecting" | "live";

let briefingAsked = false; // once per window: the backend also refuses a second briefing per launch

/** Follows the agent's voice events: shows when JARVIS is listening, hands what was heard to the
 *  chat, and lets a click stop JARVIS mid-sentence. */
function VoiceLink({ conn, onState, onTurn }: { conn: Connection | null; onState: (s: VoiceState) => void; onTurn: (conversationId: string) => void }) {
  const [state, setState] = useState<VoiceState>("off");
  const [caption, setCaption] = useState<string>("");
  const [heard, setHeard] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    if (!conn) return;
    const ctrl = new AbortController();
    let after = -1;
    const put = (s: VoiceState) => {
      setState(s);
      onState(s);
    };
    (async () => {
      while (!ctrl.signal.aborted) {
        if (after < 0) {
          try {
            after = (await getVoiceSeq(conn)).seq; // start from now: old events are history
            if (!briefingAsked) {
              briefingAsked = true; // JARVIS speaks first: the start-up briefing
              startBriefing(conn).catch(() => {});
            }
          } catch {
            await new Promise((r) => setTimeout(r, 2000));
            continue;
          }
        }
        let events: VoiceEvent[] = [];
        try {
          events = await getVoiceEvents(conn, after, ctrl.signal);
        } catch {
          await new Promise((r) => setTimeout(r, 2000));
          continue;
        }
        for (const e of events) {
          after = Math.max(after, e.seq);
          if (e.event === "levels") {
            // the voice's loudness, 50 ms apart: the arc reactor pulses with it
            window.dispatchEvent(new CustomEvent("jarvis:levels", { detail: { who: e.who, v: e.v } }));
            continue;
          }
          if (e.event === "listening") {
            setHeard(null);
            setNote(null);
            put("listening");
          } else if (e.event === "transcribing") put("transcribing");
          else if (e.event === "speaking") put("speaking");
          else if (e.event === "briefing" && e.text) {
            setCaption(e.text);
            put("speaking");
          }
          else if (e.event === "transcript" && e.text) {
            setHeard(e.text);
            put("off");
            window.dispatchEvent(new CustomEvent("jarvis:voice-text", { detail: e.text }));
          } else if (e.event === "globe") {
            window.dispatchEvent(new CustomEvent("jarvis:globe", { detail: e.text ?? "" }));
          } else if (e.event === "emergency_stop") {
            setCaption("");
            setNote("Stopped everything");
            put("off");
          } else if (e.event === "idle") {
            setCaption("");
            if (e.reason && e.reason !== "spoken" && e.reason !== "interrupted") setNote(e.reason);
            put("off");
          } else if (e.event === "live_connecting") {
            setCaption("");
            setNote(null);
            put("connecting");
          } else if (e.event === "live_start") put("live");
          else if (e.event === "user_text" && e.text) {
            setCaption(`You: ${e.text}`);
            put("live");
          } else if (e.event === "jarvis_text" && e.text) {
            setCaption(e.text);
            put("speaking");
          } else if (e.event === "live_tool") setCaption(`Working on it… (${(e.tool ?? "").replace(/_/g, " ")})`);
          else if (e.event === "live_turn" && e.conversation_id) {
            onTurn(e.conversation_id);
            put("live");
          } else if (e.event === "live_end") {
            setCaption("");
            if (e.conversation_id) onTurn(e.conversation_id);
            put("off");
          } else if (e.event === "error") {
            setNote(e.message ?? "voice error");
            put("off");
          }
        }
      }
    })();
    return () => ctrl.abort();
  }, [conn, onState, onTurn]);

  useEffect(() => {
    if (!heard && !note) return;
    const t = window.setTimeout(() => {
      setHeard(null);
      setNote(null);
    }, 6000);
    return () => window.clearTimeout(t);
  }, [heard, note]);

  if (state === "off" && !heard && !note) return null;
  const live = state === "connecting" || state === "live" || (state === "speaking" && !!caption);
  const label =
    state === "connecting" ? "Connecting"
    : live && caption ? caption
    : state === "live" ? "Live · listening"
    : state === "listening" ? "Listening" : state === "transcribing" ? "Understanding" : state === "speaking" ? "Speaking" : "";
  return (
    <div className={`voice-pill glass ${state} ${live ? "is-live" : ""}`} role="status"
         onClick={() => conn && state !== "off" && stopSpeaking(conn).catch(() => {})}
         title={live ? "Click to end the conversation (or say “that's all”)" : state === "speaking" ? "Click to stop" : undefined}>
      {state !== "off" && (
        <span className="voice-bars" aria-hidden="true"><i /><i /><i /><i /><i /></span>
      )}
      <span>{label || (heard ? `“${heard}”` : note)}</span>
    </div>
  );
}

export default function App() {
  const shell = insideShell();
  const [status, setStatus] = useState<ShellStatus | null>(null);
  const [conn, setConn] = useState<Connection | null>(null);
  const [drawer, setDrawer] = useState<Drawer>(null);
  // The globe is a full screen of its own, not a drawer; once opened it stays loaded while hidden.
  const [globe, setGlobe] = useState<{ open: boolean; started: boolean; target: string | null }>({ open: false, started: false, target: null });
  useEffect(() => {
    const show = (e: Event) => setGlobe({ open: true, started: true, target: (e as CustomEvent<string>).detail || null });
    window.addEventListener("jarvis:globe", show);
    return () => window.removeEventListener("jarvis:globe", show);
  }, []);
  const [configured, setConfigured] = useState(false);
  const [address, setAddress] = useState("sir");
  const [activity, setActivity] = useState<Activity>("idle");
  const [conversation, setConversation] = useState<string | null>(null);
  const [historyVersion, setHistoryVersion] = useState(0);
  const tel = useTelemetry(conn, "backend");
  const [wake, setWake] = useState(1);
  const [voice, setVoiceState] = useState<VoiceState>("off");
  const [reload, setReload] = useState(0);
  const onTurn = useCallback((id: string) => {
    setConversation(id);
    setReload((n) => n + 1);
    setHistoryVersion((v) => v + 1);
  }, []); // bumps replay the aurora sweep: on launch and on every new chat

  useEffect(() => {
    if (!shell) return;
    let unlisten: (() => void) | undefined;
    getStatus().then(setStatus);
    onStatus(setStatus).then((u) => (unlisten = u));
    return () => unlisten?.();
  }, [shell]);

  useEffect(() => {
    if (!shell || status?.backend !== "online") {
      setConn(null);
      return;
    }
    getConnection().then(setConn);
  }, [shell, status?.backend, status?.port]);

  const refreshSettings = useCallback(() => {
    if (!conn) return setConfigured(false);
    getSettings(conn)
      .then((s) => {
        setConfigured(s.ready);
        setAddress(s.address || "sir");
      })
      .catch(() => setConfigured(false));
  }, [conn]);

  useEffect(() => {
    refreshSettings();
  }, [refreshSettings]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDrawer(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const onConversation = useCallback((id: string | null, changed: boolean) => {
    setConversation(id);
    if (changed) setHistoryVersion((v) => v + 1);
  }, []);

  if (!shell) {
    return (
      <main className="not-shell">
        <h1>J.A.R.V.I.S</h1>
        <p>JARVIS is a desktop application. Open it from JARVIS.exe.</p>
      </main>
    );
  }

  const overall: Overall = status?.overall ?? "starting";
  const baseActivity: Activity = overall === "failed" ? "offline" : overall !== "online" && activity === "idle" ? "thinking" : activity;
  const coreActivity: Activity =
    voice === "listening" || voice === "live" ? "listening"
    : voice === "transcribing" || voice === "connecting" ? "thinking"
    : voice === "speaking" ? "speaking" : baseActivity;
  const s = tel.agent?.stats ?? {};

  return (
    <div className={`app ${overall} act-${coreActivity}`} style={{ ["--core" as string]: COLOR[overall] }}>
      <div className="grid" aria-hidden="true" />
      <Reactor activity={coreActivity} label={LABEL[overall]} />
      <div className="aurora" aria-hidden="true"><i /><i /><i /></div>
      <div key={wake} className="aurora wake" aria-hidden="true"><i /><i /><i /></div>

      <header className="topbar">
        <button className="status-chip" onClick={() => setDrawer(drawer === "system" ? null : "system")} title="System details">
          <span className="dot" />
          <span className="brand">J.A.R.V.I.S</span>
          <span className="state">{LABEL[overall]}</span>
        </button>
        <div className="topbar-right">
          <div className="gauges" onClick={() => setDrawer("system")} role="button" tabIndex={0} aria-label="Open system details">
            <Gauge label="CPU" value={s.cpu_percent} />
            <Gauge label="RAM" value={s.ram_percent} />
            <Gauge label="DISK" value={s.disk_percent ?? undefined} />
          </div>
          <ModelPicker conn={conn} onChanged={() => setReload((n) => n + 1)} />
          <QuotaStrip conn={conn} activity={coreActivity} />
        </div>
      </header>

      <nav className="rail" aria-label="Panels">
        {(Object.keys(ICONS) as Exclude<Drawer, null>[]).map((d) => (
          <button key={d} className={drawer === d ? "on" : ""} onClick={() => setDrawer(drawer === d ? null : d)} title={TITLES[d]} aria-label={TITLES[d]}>
            <svg viewBox="0 0 24 24">{ICONS[d]}</svg>
          </button>
        ))}
        <button
          className={globe.open ? "on" : ""}
          onClick={() => setGlobe((g) => ({ ...g, open: !g.open, started: true }))}
          title="Globe (God's Eye View)"
          aria-label="Globe (God's Eye View)"
        >
          <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18" /></svg>
        </button>
        <button
          className="rail-new"
          title="New conversation"
          aria-label="New conversation"
          onClick={() => {
            setConversation(null);
            setDrawer(null);
            setWake((w) => w + 1);
          }}
        >
          <svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>
        </button>
      </nav>

      <VoiceLink conn={conn} onState={setVoiceState} onTurn={onTurn} />

      <main className="front">
        <TaskPanel conn={conn} />
        <Onboarding conn={conn} onChanged={() => setReload((n) => n + 1)} />
        <ChatView
          conn={conn}
          configured={configured}
          address={address}
          conversationId={conversation}
          onConversation={onConversation}
          onActivity={setActivity}
          goSettings={() => setDrawer("settings")}
          reload={reload}
          voice={voice}
        />
      </main>

      {globe.started && <GlobeView conn={conn} open={globe.open} target={globe.target} onClose={() => setGlobe((g) => ({ ...g, open: false }))} />}

      {drawer && <div className="scrim" onClick={() => setDrawer(null)} />}
      <aside className={`glass drawer ${drawer ? "open" : ""} ${drawer === "memory" ? "wide" : ""} ${drawer === "settings" ? "xwide" : ""}`} aria-hidden={!drawer}>
        {drawer && (
          <>
            <header className="drawer-head">
              <h2>{TITLES[drawer]}</h2>
              <button className="close" onClick={() => setDrawer(null)} aria-label="Close panel">✕</button>
            </header>
            <div className="drawer-body">
              {drawer === "history" && (
                <HistoryPanel
                  conn={conn}
                  current={conversation}
                  version={historyVersion}
                  onOpen={(id) => {
                    setConversation(id);
                    setDrawer(null);
                  }}
                />
              )}
              {drawer === "memory" && <MemoryView conn={conn} />}
              {drawer === "settings" && <SettingsView conn={conn} onChanged={refreshSettings} />}
              {drawer === "system" && <SystemPanel status={status} conn={conn} tel={tel} />}
            </div>
          </>
        )}
      </aside>
    </div>
  );
}
