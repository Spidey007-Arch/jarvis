import { useCallback, useEffect, useState } from "react";
import { Automation, AutomationDraft, deleteAutomation, getAutomations, runAutomation, saveAutomation, setAutomationEnabled } from "../api";
import type { Connection } from "../shell";

type Note = { tone: "ok" | "bad"; text: string } | null;
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const EMPTY: AutomationDraft = { name: "", prompt: "", kind: "daily", at: "09:00", days: [], every_min: 60, deliver: [], enabled: true };
const EXAMPLES: AutomationDraft[] = [
  { ...EMPTY, name: "Morning mail", prompt: "Go through my unread Gmail and tell me what needs a reply today.", days: [0, 1, 2, 3, 4], deliver: ["phone"] },
  { ...EMPTY, name: "Disk watch", prompt: "Check how full each drive is and warn me if any is over 90%.", kind: "interval", every_min: 360 },
  { ...EMPTY, name: "Evening wrap-up", prompt: "Tell me tomorrow's reminders and anything I left unfinished today.", at: "21:00", deliver: ["speak"] },
];

const when = (t: number | null) =>
  t ? new Date(t * 1000).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";

/** Jobs JARVIS does by itself on a schedule. Each run is saved in its own conversation. */
export default function AutomationsCard({ conn, phoneLinked }: { conn: Connection | null; phoneLinked: boolean }) {
  const [items, setItems] = useState<Automation[] | null>(null);
  const [draft, setDraft] = useState<AutomationDraft | null>(null);
  const [note, setNote] = useState<Note>(null);
  const [busy, setBusy] = useState(false);
  const fail = (e: unknown) => setNote({ tone: "bad", text: (e as Error).message });

  const load = useCallback(async () => {
    if (conn) setItems(await getAutomations(conn).catch((e) => { fail(e); return null; }));
  }, [conn]);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 10_000); // runs finish on their own; keep the list current
    return () => window.clearInterval(t);
  }, [load]);

  const act = async (fn: () => Promise<unknown>, done?: string) => {
    setBusy(true);
    try {
      await fn();
      if (done) setNote({ tone: "ok", text: done });
      await load();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const save = (d: AutomationDraft) =>
    conn && act(async () => {
      await saveAutomation(conn, d);
      setDraft(null);
    }, d.id ? "Automation updated." : `Scheduled “${d.name.trim()}”.`);

  const toggle = <T,>(list: T[], v: T) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);

  return (
    <section className="card">
      <div className="card-head">
        <h2>Automations</h2>
        {!draft && <button onClick={() => { setNote(null); setDraft({ ...EMPTY }); }}>+ NEW</button>}
      </div>
      <p className="dim small">
        Jobs JARVIS does by itself on a schedule, while the app is running. Steps that need your OK still ask, in the app or on your phone,
        and are declined after 2 minutes. You can also just ask: “every weekday at 9, check my mail”.
      </p>

      {draft && (
        <div className="skill-form">
          {!draft.id && !draft.name && (
            <div className="chips">
              {EXAMPLES.map((ex) => <button key={ex.name} className="ghost" onClick={() => setDraft({ ...ex })}>{ex.name}</button>)}
            </div>
          )}
          <label>
            <span>Name</span>
            <input value={draft.name} maxLength={80} placeholder="Morning mail" onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
          </label>
          <label>
            <span>What JARVIS should do</span>
            <textarea rows={4} maxLength={4000} value={draft.prompt} placeholder="Go through my unread Gmail and tell me what needs a reply today."
              onChange={(e) => setDraft({ ...draft, prompt: e.target.value })} />
          </label>
          <div className="grid2">
            <label>
              <span>When</span>
              <select value={draft.kind} onChange={(e) => setDraft({ ...draft, kind: e.target.value as AutomationDraft["kind"],
                at: e.target.value === "once" ? new Date(Date.now() + 3600_000).toISOString().slice(0, 16) : e.target.value === "daily" ? "09:00" : "" })}>
                <option value="daily">Every day / some days</option>
                <option value="interval">Every few minutes or hours</option>
                <option value="once">Once</option>
              </select>
            </label>
            {draft.kind === "daily" && (
              <label>
                <span>At</span>
                <input type="time" value={draft.at} onChange={(e) => setDraft({ ...draft, at: e.target.value })} />
              </label>
            )}
            {draft.kind === "once" && (
              <label>
                <span>At</span>
                <input type="datetime-local" value={draft.at} onChange={(e) => setDraft({ ...draft, at: e.target.value })} />
              </label>
            )}
            {draft.kind === "interval" && (
              <label>
                <span>Every (minutes, at least 15)</span>
                <input type="number" min={15} step={15} value={draft.every_min} onChange={(e) => setDraft({ ...draft, every_min: Number(e.target.value) })} />
              </label>
            )}
          </div>
          {draft.kind === "daily" && (
            <div className="chips" role="group" aria-label="Days">
              {DAYS.map((d, i) => (
                <button key={d} className={draft.days.includes(i) ? "" : "ghost"} aria-pressed={draft.days.includes(i)}
                  onClick={() => setDraft({ ...draft, days: toggle(draft.days, i) })}>{d}</button>
              ))}
              <span className="dim small">{draft.days.length ? "" : "none picked = every day"}</span>
            </div>
          )}
          <label className="switch">
            <input type="checkbox" checked={draft.deliver.includes("speak")} onChange={() => setDraft({ ...draft, deliver: toggle(draft.deliver, "speak") })} />
            <span>Say the result out loud</span>
          </label>
          <label className="switch">
            <input type="checkbox" checked={draft.deliver.includes("phone")} onChange={() => setDraft({ ...draft, deliver: toggle(draft.deliver, "phone") })} />
            <span>Send the result to my phone{phoneLinked ? "" : " (link a phone in the Phone tab first)"}</span>
          </label>
          <div className="field-row">
            <button disabled={busy || !draft.name.trim() || !draft.prompt.trim()} onClick={() => save(draft)}>SAVE</button>
            <button className="ghost" disabled={busy} onClick={() => setDraft(null)}>CANCEL</button>
          </div>
        </div>
      )}

      {items && !items.length && !draft && <p className="dim small">No automations yet.</p>}
      {!!items?.length && (
        <ul className="skills">
          {items.map((a) => (
            <li key={a.id} className={a.enabled ? "" : "off"}>
              <div className="skill-main">
                <strong>{a.name}</strong>
                <span className="dim small">{a.schedule}{a.deliver.length ? ` · ${a.deliver.map((d) => (d === "speak" ? "spoken" : "to phone")).join(", ")}` : ""}</span>
                <span className="dim small">
                  {a.running ? "Running now…" : a.enabled ? `Next: ${when(a.next_run)}` : "Off"}
                  {a.last_status && ` · Last: ${a.last_status}${a.last_run ? ` (${when(a.last_run)})` : ""}`}
                </span>
                {a.last_result && a.last_status !== "missed" && (
                  <details>
                    <summary className="dim small">Last result</summary>
                    <p className="small automation-result">{a.last_result}</p>
                  </details>
                )}
              </div>
              <div className="skill-actions">
                <label className="switch">
                  <input type="checkbox" checked={a.enabled} disabled={busy}
                    onChange={(e) => conn && act(() => setAutomationEnabled(conn, a.id, e.target.checked))} />
                  <span>{a.enabled ? "On" : "Off"}</span>
                </label>
                <button className="ghost" disabled={busy || a.running} onClick={() => conn && act(() => runAutomation(conn, a.id), `Running “${a.name}” now.`)}>RUN NOW</button>
                <button className="ghost" disabled={busy} onClick={() => { setNote(null); setDraft({ ...a }); }}>EDIT</button>
                <button className="ghost" disabled={busy}
                  onClick={() => conn && window.confirm(`Delete “${a.name}”?`) && act(() => deleteAutomation(conn, a.id))}>DELETE</button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {note && <p className={note.tone}>{note.text}</p>}
    </section>
  );
}
