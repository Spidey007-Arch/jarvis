import { ReactNode, useCallback, useEffect, useState } from "react";
import Reactor from "./Reactor";
import Particles from "./fx/Particles";
import type { Activity } from "./fx/activity";
import { useTelemetry } from "./telemetry";
import { Connection, Overall, ShellStatus, getConnection, getStatus, insideShell, onStatus } from "./shell";
import { Alert, getAlerts, getSettings } from "./api";
import ChatView from "./views/ChatView";
import HistoryPanel from "./views/HistoryPanel";
import MemoryView from "./views/MemoryView";
import SettingsView from "./views/SettingsView";
import SystemPanel from "./views/SystemPanel";

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

export default function App() {
  const shell = insideShell();
  const [status, setStatus] = useState<ShellStatus | null>(null);
  const [conn, setConn] = useState<Connection | null>(null);
  const [drawer, setDrawer] = useState<Drawer>(null);
  const [configured, setConfigured] = useState(false);
  const [address, setAddress] = useState("sir");
  const [activity, setActivity] = useState<Activity>("idle");
  const [conversation, setConversation] = useState<string | null>(null);
  const [historyVersion, setHistoryVersion] = useState(0);
  const tel = useTelemetry(conn, "backend");
  const [alert, setAlert] = useState<Alert | null>(null);

  // Proactive alerts from the backend monitor: show the newest one for a while.
  useEffect(() => {
    if (!conn) return;
    let since = Date.now() / 1000;
    const id = window.setInterval(async () => {
      try {
        const fresh = await getAlerts(conn, since);
        if (fresh.length) {
          since = fresh[fresh.length - 1].at;
          setAlert(fresh[fresh.length - 1]);
        }
      } catch {
        /* backend restarting; the status chip already shows that */
      }
    }, 10000);
    return () => window.clearInterval(id);
  }, [conn]);

  useEffect(() => {
    if (!alert) return;
    const t = window.setTimeout(() => setAlert(null), 20000);
    return () => window.clearTimeout(t);
  }, [alert]);

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
  const coreActivity: Activity = overall === "failed" ? "offline" : overall !== "online" && activity === "idle" ? "thinking" : activity;
  const s = tel.agent?.stats ?? {};

  return (
    <div className={`app ${overall} act-${coreActivity}`} style={{ ["--core" as string]: COLOR[overall] }}>
      <Particles color={COLOR[overall]} activity={coreActivity} />
      <Reactor activity={coreActivity} label={LABEL[overall]} />

      <header className="topbar">
        <button className="status-chip" onClick={() => setDrawer(drawer === "system" ? null : "system")} title="System details">
          <span className="dot" />
          <span className="brand">J.A.R.V.I.S</span>
          <span className="state">{LABEL[overall]}</span>
        </button>
        <div className="gauges" onClick={() => setDrawer("system")} role="button" tabIndex={0} aria-label="Open system details">
          <Gauge label="CPU" value={s.cpu_percent} />
          <Gauge label="RAM" value={s.ram_percent} />
          <Gauge label="DISK" value={s.disk_percent ?? undefined} />
        </div>
      </header>

      {alert && (
        <div className="alert-toast glass" role="alert" onClick={() => setAlert(null)}>
          <span className="alert-kind">{alert.kind.toUpperCase()}</span>
          {alert.message}
        </div>
      )}

      <nav className="rail" aria-label="Panels">
        {(Object.keys(ICONS) as Exclude<Drawer, null>[]).map((d) => (
          <button key={d} className={drawer === d ? "on" : ""} onClick={() => setDrawer(drawer === d ? null : d)} title={TITLES[d]} aria-label={TITLES[d]}>
            <svg viewBox="0 0 24 24">{ICONS[d]}</svg>
          </button>
        ))}
        <button
          className="rail-new"
          title="New conversation"
          aria-label="New conversation"
          onClick={() => {
            setConversation(null);
            setDrawer(null);
          }}
        >
          <svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>
        </button>
      </nav>

      <main className="front">
        <ChatView
          conn={conn}
          configured={configured}
          address={address}
          conversationId={conversation}
          onConversation={onConversation}
          onActivity={setActivity}
          goSettings={() => setDrawer("settings")}
        />
      </main>

      {drawer && <div className="scrim" onClick={() => setDrawer(null)} />}
      <aside className={`glass drawer ${drawer ? "open" : ""} ${drawer === "history" ? "left" : "right"} ${drawer === "memory" ? "wide" : ""} ${drawer === "settings" ? "xwide" : ""}`} aria-hidden={!drawer}>
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
