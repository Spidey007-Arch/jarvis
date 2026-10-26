import { useEffect, useState } from "react";
import { getAutostart, setAutostart, stopEverything } from "../api";
import type { Connection } from "../shell";

/** Start with Windows, the emergency stop, and re-running the first-run setup. */
export default function SystemCard({ conn }: { conn: Connection | null }) {
  const [autostart, setAuto] = useState<boolean | null>(null);
  const [note, setNote] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (conn) getAutostart(conn).then((r) => setAuto(r.starts_with_windows)).catch((e) => setNote({ tone: "bad", text: (e as Error).message }));
  }, [conn]);

  const toggle = async (on: boolean) => {
    if (!conn) return;
    setBusy(true);
    try {
      const r = await setAutostart(conn, on);
      setAuto(r.starts_with_windows);
      setNote({ tone: "ok", text: on ? "JARVIS will start when you sign in to Windows." : "JARVIS won't start with Windows." });
    } catch (e) {
      setNote({ tone: "bad", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card">
      <h2>System</h2>
      <label className="switch">
        <input type="checkbox" checked={!!autostart} disabled={busy || autostart === null} onChange={(e) => toggle(e.target.checked)} />
        <span>Start JARVIS when I sign in to Windows</span>
      </label>
      <div className="field-row">
        <button className="deny" onClick={() => conn && stopEverything(conn).then(() => setNote({ tone: "ok", text: "Stopped everything." }))}>
          STOP EVERYTHING
        </button>
        <span className="dim small">Same as Ctrl+Alt+Shift+S from any app: stops speech, voice, the running task and pending approvals.</span>
      </div>
      <div className="field-row">
        <button className="ghost" onClick={() => window.dispatchEvent(new CustomEvent("jarvis:onboarding"))}>RUN FIRST-TIME SETUP AGAIN</button>
      </div>
      {note && <p className={`${note.tone} small`}>{note.text}</p>}
    </section>
  );
}
