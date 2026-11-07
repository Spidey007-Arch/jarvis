import { useCallback, useEffect, useState } from "react";
import { AgentView, deleteAgent, forgetAgent, getAgents, saveAgent, toggleAgent, wakeAgent } from "../api";
import { t } from "../i18n";
import type { Connection } from "../shell";

type Note = { tone: "ok" | "bad"; text: string } | null;
const EMPTY = { name: "", goal: "", every_min: 60 };
const EXAMPLES = [
  { name: "Price watch", goal: "Check the price of the laptop I'm looking at and tell me when it drops by 10% or more.", every_min: 360 },
  { name: "Release watch", goal: "Check my favourite GitHub project for a new release, and tell me what changed when there is one.", every_min: 720 },
];
const when = (s: number | null) => (s ? new Date(s * 1000).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" }) : "never");

/** Goals JARVIS keeps working on over days or weeks, each with a notebook it carries between wakes. */
export default function AgentsCard({ conn }: { conn: Connection | null }) {
  const [agents, setAgents] = useState<AgentView[]>([]);
  const [draft, setDraft] = useState<typeof EMPTY & { id?: string }>(EMPTY);
  const [shown, setShown] = useState("");
  const [note, setNote] = useState<Note>(null);
  const [busy, setBusy] = useState(false);
  const fail = (e: unknown) => setNote({ tone: "bad", text: (e as Error).message });

  const load = useCallback(async () => {
    if (conn) setAgents(await getAgents(conn).catch((e) => { fail(e); return []; }));
  }, [conn]);

  useEffect(() => {
    load();
  }, [load]);

  const act = async (fn: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try {
      await fn();
      setNote({ tone: "ok", text: done });
      await load();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const save = () =>
    conn && act(async () => {
      await saveAgent(conn, draft, draft.id);
      setDraft(EMPTY);
    }, "Saved.");

  return (
    <section className="card">
      <h2>{t("Agents")}</h2>
      <p className="dim small">
        A goal JARVIS keeps working on by itself. It wakes every so often, carries on from its notebook, and speaks only when it has
        something to tell you. Each wake uses one AI request, and a step that needs your yes waits in the app.
      </p>

      <ul className="skills">
        {agents.map((a) => (
          <li key={a.id}>
            <div className="skill-main">
              <span>{a.name}</span>
              <span className="dim small">every {a.every_min} min · {t("last")}: {when(a.last_run)}{a.last_status ? ` · ${a.last_status}` : ""}</span>
              {a.last_text && <span className="dim small">{a.last_text}</span>}
              {shown === a.id && <span className="small notebook">{a.notebook || t("The notebook is empty.")}</span>}
            </div>
            <div className="row-actions">
              <label className="switch">
                <input type="checkbox" checked={a.enabled} disabled={busy} onChange={(e) => conn && act(() => toggleAgent(conn, a.id, e.target.checked), e.target.checked ? "On." : "Paused.")} />
              </label>
              <button className="ghost" disabled={busy || a.running} onClick={() => conn && act(() => wakeAgent(conn, a.id), "Woke it.")}>{a.running ? t("WORKING…") : t("WAKE")}</button>
              <button className="ghost" onClick={() => setShown(shown === a.id ? "" : a.id)}>{t("NOTEBOOK")}</button>
              <button className="ghost" onClick={() => setDraft({ id: a.id, name: a.name, goal: a.goal, every_min: a.every_min })}>{t("EDIT")}</button>
              <button className="ghost" disabled={busy} onClick={() => conn && act(() => forgetAgent(conn, a.id), "It starts fresh next time.")}>{t("FORGET")}</button>
              <button className="ghost" disabled={busy} onClick={() => conn && act(() => deleteAgent(conn, a.id), "Deleted.")}>{t("DELETE")}</button>
            </div>
          </li>
        ))}
      </ul>

      <h3>{draft.id ? t("Edit agent") : t("New agent")}</h3>
      <label>
        <span>{t("Name")}</span>
        <input maxLength={60} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
      </label>
      <label>
        <span>{t("Goal")}</span>
        <textarea rows={3} maxLength={2000} value={draft.goal} onChange={(e) => setDraft({ ...draft, goal: e.target.value })} />
      </label>
      <label>
        <span>{t("Every (minutes)")}</span>
        <input type="number" min={15} max={10080} step={15} value={draft.every_min} onChange={(e) => setDraft({ ...draft, every_min: Math.max(15, Number(e.target.value) || 60) })} />
      </label>
      {!draft.id && !draft.goal && (
        <div className="chips">
          {EXAMPLES.map((x) => <button key={x.name} className="ghost" onClick={() => setDraft(x)}>+ {x.name}</button>)}
        </div>
      )}
      <div className="field-row">
        <button disabled={busy || !draft.name.trim() || !draft.goal.trim()} onClick={save}>{t("SAVE")}</button>
        {draft.id && <button className="ghost" onClick={() => setDraft(EMPTY)}>{t("CANCEL")}</button>}
      </div>
      {note && <p className={note.tone}>{note.text}</p>}
    </section>
  );
}
