import { useEffect, useState } from "react";
import { TaskOffer, TaskView, controlTask, getTask } from "../api";
import type { Connection } from "../shell";

const STATUS: Record<string, string> = {
  running: "Working",
  paused: "Paused",
  taken_over: "Your turn: JARVIS is waiting",
};

/** The running task: its plan, the step JARVIS is on, the last few actions, and the user's controls.
 *  After a restart, an interrupted task is offered here instead. */
export default function TaskPanel({ conn }: { conn: Connection | null }) {
  const [task, setTask] = useState<TaskView | null>(null);
  const [offer, setOffer] = useState<TaskOffer | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!conn) return;
    let alive = true;
    const poll = async () => {
      try {
        const r = await getTask(conn);
        if (alive) {
          setTask(r.task);
          setOffer(r.offer);
        }
      } catch {
        /* backend restarting */
      }
    };
    poll();
    const id = window.setInterval(poll, 1200);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [conn]);

  const act = async (id: string, what: string) => {
    if (!conn) return;
    setBusy(true);
    try {
      const r = await controlTask(conn, id, what);
      if (what === "continue" && r.prompt) {
        setOffer(null);
        window.dispatchEvent(new CustomEvent("jarvis:voice-text", { detail: r.prompt }));
      } else if (what === "dismiss") setOffer(null);
      else if (r.id) setTask(r as TaskView);
    } catch {
      /* the task ended meanwhile; the next poll shows that */
    } finally {
      setBusy(false);
    }
  };

  if (task) {
    const t = task;
    return (
      <aside className="task-panel glass" aria-label="Task">
        <div className="task-head">
          <span className={`task-dot ${t.status}`} />
          <strong>{STATUS[t.status] ?? t.status}</strong>
          <span className="dim small">{t.steps_taken} steps</span>
        </div>
        <div className="task-goal">{t.goal}</div>
        {t.plan.length > 0 && (
          <ol className="task-plan">
            {t.plan.map((s, i) => (
              <li key={i} className={i < t.current ? "done" : i === t.current ? "now" : ""}>{s}</li>
            ))}
          </ol>
        )}
        {t.thought && <p className="task-thought" title={t.thought}>{t.thought}</p>}
        {t.actions.length > 0 && (
          <ul className="task-acts">
            {t.actions.slice(-3).map((a, i) => (
              <li key={i} className={a.status}>{a.label}</li>
            ))}
          </ul>
        )}
        <div className="task-buttons">
          {t.status === "running" && <button disabled={busy} onClick={() => act(t.id, "pause")}>PAUSE</button>}
          {t.status === "paused" && <button disabled={busy} onClick={() => act(t.id, "resume")}>RESUME</button>}
          {t.status === "taken_over"
            ? <button disabled={busy} onClick={() => act(t.id, "hand_back")}>HAND BACK</button>
            : <button className="ghost" disabled={busy} onClick={() => act(t.id, "take_over")}>TAKE OVER</button>}
          <button className="deny" disabled={busy} onClick={() => act(t.id, "stop")}>STOP</button>
        </div>
        <p className="dim small">Ctrl+Alt+Shift+S stops everything.</p>
      </aside>
    );
  }
  if (offer) {
    return (
      <aside className="task-panel glass" aria-label="Interrupted task">
        <div className="task-head"><strong>Interrupted when JARVIS closed</strong></div>
        <div className="task-goal">{offer.goal}</div>
        {offer.done_steps.length > 0 && <p className="dim small">{offer.done_steps.length} steps were done.</p>}
        <div className="task-buttons">
          <button disabled={busy} onClick={() => act(offer.id, "continue")}>CONTINUE</button>
          <button className="ghost" disabled={busy} onClick={() => act(offer.id, "dismiss")}>DISMISS</button>
        </div>
      </aside>
    );
  }
  return null;
}
