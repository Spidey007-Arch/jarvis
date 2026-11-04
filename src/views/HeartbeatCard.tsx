import { useCallback, useEffect, useState } from "react";
import { HeartbeatConfig, HeartbeatView, getHeartbeat, runHeartbeat, saveHeartbeat } from "../api";
import { t } from "../i18n";
import type { Connection } from "../shell";

type Note = { tone: "ok" | "bad"; text: string } | null;
const when = (s: number | null | undefined) => (s ? new Date(s * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "—");
const WAITING: Record<string, string> = {
  off: "Off.",
  "nothing to check": "Add something to check, and JARVIS starts looking.",
  "not yet": "Waiting for the next check.",
  "quiet hours": "Quiet hours: no checks until they end.",
  "you're away": "Paused while you're away from the PC.",
  busy: "Waiting: a task or a conversation is running.",
};

/** A short list JARVIS goes through every so often, speaking up only when something needs the user. */
export default function HeartbeatCard({ conn }: { conn: Connection | null }) {
  const [view, setView] = useState<HeartbeatView | null>(null);
  const [draft, setDraft] = useState<HeartbeatConfig & { text: string } | null>(null);
  const [note, setNote] = useState<Note>(null);
  const [busy, setBusy] = useState(false);
  const fail = (e: unknown) => setNote({ tone: "bad", text: (e as Error).message });

  const load = useCallback(async () => {
    if (!conn) return;
    const v = await getHeartbeat(conn).catch((e) => { fail(e); return null; });
    setView(v);
    if (v) setDraft((d) => d ?? { ...v, text: v.checks.join("\n") });
  }, [conn]);

  useEffect(() => {
    load();
    const id = window.setInterval(load, 15_000); // a check finishes on its own; keep the result current
    return () => window.clearInterval(id);
  }, [load]);

  const save = async (d: HeartbeatConfig & { text: string }, done: string) => {
    if (!conn) return;
    setBusy(true);
    try {
      const checks = d.text.split("\n").map((s) => s.trim()).filter(Boolean);
      const v = await saveHeartbeat(conn, { enabled: d.enabled, every_min: d.every_min, checks, quiet_from: d.quiet_from, quiet_to: d.quiet_to });
      setView(v);
      setDraft({ ...v, text: v.checks.join("\n") });
      setNote({ tone: "ok", text: done });
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  if (!view || !draft) return <section className="card"><h2>{t("Heartbeat")}</h2>{note && <p className={note.tone}>{note.text}</p>}</section>;
  const changed = draft.text.split("\n").map((s) => s.trim()).filter(Boolean).join("\n") !== view.checks.join("\n") ||
    draft.every_min !== view.every_min || draft.quiet_from !== view.quiet_from || draft.quiet_to !== view.quiet_to;

  return (
    <section className="card">
      <div className="card-head">
        <h2>{t("Heartbeat")}</h2>
        <label className="switch">
          <input type="checkbox" checked={draft.enabled} disabled={busy}
            onChange={(e) => save({ ...draft, enabled: e.target.checked }, e.target.checked ? "Heartbeat is on." : "Heartbeat is off.")} />
          <span>{draft.enabled ? t("On") : t("Off")}</span>
        </label>
      </div>
      <p className="dim small">
        Every so often JARVIS goes through this list on its own and says something only when one of them needs you. It only looks:
        it never sends, buys, deletes or changes anything during a check. Quiet checks leave nothing behind; each one uses one AI request.
      </p>

      <label>
        <span>{t("What to check")} (one per line)</span>
        <textarea rows={5} maxLength={2600} value={draft.text} placeholder={view.examples.join("\n")}
          onChange={(e) => setDraft({ ...draft, text: e.target.value })} />
      </label>
      <div className="chips">
        {view.examples.filter((x) => !draft.text.includes(x)).map((x) => (
          <button key={x} className="ghost" onClick={() => setDraft({ ...draft, text: (draft.text.trim() ? draft.text.trim() + "\n" : "") + x })}>+ {x}</button>
        ))}
      </div>
      <div className="grid2">
        <label>
          <span>{t("Every (minutes)")}</span>
          <input type="number" min={15} max={1440} step={5} value={draft.every_min}
            onChange={(e) => setDraft({ ...draft, every_min: Math.max(15, Number(e.target.value) || 30) })} />
        </label>
        <label>
          <span>{t("Quiet from")} … {t("to")}</span>
          <span className="field-row">
            <input type="time" value={draft.quiet_from} onChange={(e) => setDraft({ ...draft, quiet_from: e.target.value })} />
            <input type="time" value={draft.quiet_to} onChange={(e) => setDraft({ ...draft, quiet_to: e.target.value })} />
          </span>
        </label>
      </div>
      <div className="field-row">
        <button disabled={busy || !changed} onClick={() => save(draft, "Saved.")}>{t("SAVE")}</button>
        <button className="ghost" disabled={busy || view.running || !view.checks.length || changed}
          onClick={() => conn && runHeartbeat(conn).then(() => { setNote({ tone: "ok", text: "Checking now…" }); load(); }).catch(fail)}>
          {view.running ? "CHECKING…" : t("CHECK NOW")}
        </button>
      </div>

      <p className="dim small">
        {t("Last check")}: {when(view.last_beat)}
        {view.last.status && ` · ${view.last.status === "clear" ? "all clear" : view.last.status === "alert" ? "spoke up" : "failed: " + (view.last.text ?? "")}`}
        {view.next_beat && view.enabled ? ` · next around ${when(view.next_beat)}` : ""}
        {view.waiting && view.waiting !== "not yet" ? ` · ${WAITING[view.waiting] ?? view.waiting}` : ""}
      </p>
      {!!view.told.length && (
        <>
          <h3>{t("Recently said")}</h3>
          <ul className="skills">
            {view.told.map((x) => (
              <li key={x.at}><div className="skill-main"><span>{x.text}</span><span className="dim small">{when(x.at)}</span></div></li>
            ))}
          </ul>
        </>
      )}
      {note && <p className={note.tone}>{note.text}</p>}
    </section>
  );
}
