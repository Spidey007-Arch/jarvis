import { useCallback, useEffect, useState } from "react";
import { WorkflowStep, WorkflowView, deleteWorkflow, getWorkflows, runWorkflow, saveWorkflow } from "../api";
import { t } from "../i18n";
import type { Connection } from "../shell";

type Note = { tone: "ok" | "bad"; text: string } | null;
type Draft = { id?: string; name: string; steps: WorkflowStep[] };

const KINDS: { id: WorkflowStep["kind"]; label: string; hint: string }[] = [
  { id: "ask", label: "Ask JARVIS", hint: "What should it do? {last} is the last result." },
  { id: "check", label: "Yes or no?", hint: "A question about the last result." },
  { id: "say", label: "Say aloud", hint: "What should it say?" },
  { id: "wait", label: "Wait", hint: "" },
  { id: "stop", label: "Stop", hint: "" },
];
const newStep = (n: number, kind: WorkflowStep["kind"] = "ask"): WorkflowStep => ({ id: `s${n}`, kind, text: "", ...(kind === "wait" ? { seconds: 60 } : {}) });
const EMPTY = (): Draft => ({ name: "", steps: [newStep(1)] });
const freeId = (steps: WorkflowStep[]) => {
  let n = steps.length + 1;
  while (steps.some((s) => s.id === `s${n}`)) n += 1;
  return n;
};

/** Where a step goes next: on to the following step, to the end, or to any other step. */
function Target({ value, steps, self, label, onChange }: { value?: string; steps: WorkflowStep[]; self: string; label: string; onChange: (v: string) => void }) {
  return (
    <label className="step-target">
      <span>{label}</span>
      <select value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
        <option value="">{t("next step")}</option>
        <option value="end">{t("finish")}</option>
        {steps.map((s, i) => <option key={s.id} value={s.id}>{s.id === self ? `${i + 1} (${t("this one again")})` : `${t("step")} ${i + 1}`}</option>)}
      </select>
    </label>
  );
}

/** Fixed steps and branches, for jobs where the order matters. */
export default function WorkflowsCard({ conn }: { conn: Connection | null }) {
  const [flows, setFlows] = useState<WorkflowView[]>([]);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [note, setNote] = useState<Note>(null);
  const [busy, setBusy] = useState(false);
  const fail = (e: unknown) => setNote({ tone: "bad", text: (e as Error).message });

  const load = useCallback(async () => {
    if (conn) setFlows(await getWorkflows(conn).catch((e) => { fail(e); return []; }));
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

  const change = (i: number, patch: Partial<WorkflowStep>) => setDraft({ ...draft, steps: draft.steps.map((s, n) => (n === i ? { ...s, ...patch } : s)) });
  const move = (i: number, by: number) => {
    const steps = [...draft.steps];
    const [s] = steps.splice(i, 1);
    steps.splice(i + by, 0, s);
    setDraft({ ...draft, steps });
  };
  const remove = (id: string) => {
    const gone = (v?: string) => (v === id ? undefined : v);
    setDraft({ ...draft, steps: draft.steps.filter((s) => s.id !== id).map((s) => ({ ...s, next: gone(s.next), yes: gone(s.yes), no: gone(s.no) })) });
  };

  return (
    <section className="card">
      <h2>{t("Workflows")}</h2>
      <p className="dim small">
        A job laid out as steps, for when the order matters. Permissions work as in chat. Say “run the morning report workflow”, or start
        one from an automation.
      </p>

      <ul className="skills">
        {flows.map((w) => (
          <li key={w.id}>
            <div className="skill-main">
              <span>{w.name}</span>
              <span className="dim small">{w.steps.length} {t("steps")}{w.last_status ? ` · ${w.last_status}` : ""}</span>
              {!!w.last_trail.length && <span className="dim small">{w.last_trail.map((x) => `${x.id}: ${x.result.slice(0, 60)}`).join(" → ")}</span>}
            </div>
            <div className="row-actions">
              <button className="ghost" disabled={busy || w.running} onClick={() => conn && act(() => runWorkflow(conn, w.id), `${w.name} finished.`)}>{w.running ? t("WORKING…") : t("RUN")}</button>
              <button className="ghost" onClick={() => setDraft({ id: w.id, name: w.name, steps: w.steps })}>{t("EDIT")}</button>
              <button className="ghost" disabled={busy} onClick={() => conn && act(() => deleteWorkflow(conn, w.id), "Deleted.")}>{t("DELETE")}</button>
            </div>
          </li>
        ))}
      </ul>

      <h3>{draft.id ? t("Edit workflow") : t("New workflow")}</h3>
      <label>
        <span>{t("Name")}</span>
        <input maxLength={60} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
      </label>

      <ol className="flow">
        {draft.steps.map((s, i) => {
          const kind = KINDS.find((k) => k.id === s.kind) ?? KINDS[0];
          return (
            <li key={s.id} className={`flow-step ${s.kind}`}>
              <div className="flow-head">
                <span className="flow-no">{i + 1}</span>
                <div className="chips">
                  {KINDS.map((k) => (
                    <button key={k.id} className={s.kind === k.id ? "on" : ""} onClick={() => change(i, { kind: k.id, ...(k.id === "wait" && !s.seconds ? { seconds: 60 } : {}) })}>{t(k.label)}</button>
                  ))}
                </div>
                <div className="row-actions">
                  <button className="ghost" aria-label={t("Move up")} disabled={i === 0} onClick={() => move(i, -1)}>↑</button>
                  <button className="ghost" aria-label={t("Move down")} disabled={i === draft.steps.length - 1} onClick={() => move(i, 1)}>↓</button>
                  <button className="ghost" aria-label={t("Remove step")} disabled={draft.steps.length === 1} onClick={() => remove(s.id)}>×</button>
                </div>
              </div>
              {kind.hint && <textarea rows={2} maxLength={4000} value={s.text} placeholder={t(kind.hint)} onChange={(e) => change(i, { text: e.target.value })} />}
              {s.kind === "wait" && (
                <label>
                  <span>{t("Seconds")}</span>
                  <input type="number" min={1} max={3600} value={s.seconds ?? 60} onChange={(e) => change(i, { seconds: Number(e.target.value) || 1 })} />
                </label>
              )}
              {s.kind === "check" ? (
                <div className="flow-branches">
                  <Target label={t("If yes, go to")} value={s.yes} steps={draft.steps} self={s.id} onChange={(v) => change(i, { yes: v || undefined })} />
                  <Target label={t("If no, go to")} value={s.no} steps={draft.steps} self={s.id} onChange={(v) => change(i, { no: v || undefined })} />
                </div>
              ) : s.kind !== "stop" && (
                <div className="flow-branches">
                  <Target label={t("Then go to")} value={s.next} steps={draft.steps} self={s.id} onChange={(v) => change(i, { next: v || undefined })} />
                </div>
              )}
            </li>
          );
        })}
      </ol>
      <div className="field-row">
        <button className="ghost" onClick={() => setDraft({ ...draft, steps: [...draft.steps, newStep(freeId(draft.steps))] })}>+ {t("ADD STEP")}</button>
        <button disabled={busy || !draft.name.trim()} onClick={() => conn && act(async () => { await saveWorkflow(conn, draft, draft.id); setDraft(EMPTY()); }, "Saved.")}>{t("SAVE")}</button>
        {draft.id && <button className="ghost" onClick={() => setDraft(EMPTY())}>{t("CANCEL")}</button>}
      </div>
      {note && <p className={note.tone}>{note.text}</p>}
    </section>
  );
}
