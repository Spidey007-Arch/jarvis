import { useCallback, useEffect, useState } from "react";
import ApiKeysCard from "./ApiKeysCard";
import ConnectorsCard from "./ConnectorsCard";
import AutomationsCard from "./AutomationsCard";
import {
  LearnedSkill, MySkill, forgetLearnedSkill, getLearnedSkills, getMySkills, getSettings, putSettings, removeMySkill, saveMySkill,
} from "../api";
import type { Connection } from "../shell";

type Note = { tone: "ok" | "bad"; text: string } | null;
type Tab = "instructions" | "skills" | "automations" | "connectors" | "keys";
const TABS: { id: Tab; label: string }[] = [
  { id: "instructions", label: "Instructions" },
  { id: "skills", label: "Skills" },
  { id: "automations", label: "Automations" },
  { id: "connectors", label: "Connectors" },
  { id: "keys", label: "API keys" },
];
const TAB_KEY = "jarvis.customizeTab";
const MAX_INSTRUCTIONS = 4000;
const EMPTY = { name: "", when_to: "", body: "", enabled: true };

/** One place to shape JARVIS: how it behaves, the skills it follows, and what it is connected to. */
export default function CustomizeView({ conn }: { conn: Connection | null }) {
  const [tab, setTab] = useState<Tab>(() => {
    try {
      const t = localStorage.getItem(TAB_KEY) as Tab | null;
      return t && TABS.some((x) => x.id === t) ? t : "instructions";
    } catch {
      return "instructions";
    }
  });
  const pick = (t: Tab) => {
    setTab(t);
    try {
      localStorage.setItem(TAB_KEY, t);
    } catch {
      /* remembered for this visit only */
    }
  };

  return (
    <div className="settings-shell">
      <nav className="settings-nav" aria-label="Customize sections">
        {TABS.map((t) => (
          <button key={t.id} className={tab === t.id ? "on" : ""} aria-current={tab === t.id ? "page" : undefined} onClick={() => pick(t.id)}>
            {t.label}
          </button>
        ))}
      </nav>
      <div className="settings">
        {tab === "instructions" && <Instructions conn={conn} />}
        {tab === "skills" && <Skills conn={conn} />}
        {tab === "automations" && <AutomationsCard conn={conn} />}
        {tab === "connectors" && <ConnectorsCard conn={conn} />}
        {tab === "keys" && <ApiKeysCard conn={conn} />}
      </div>
    </div>
  );
}

function Instructions({ conn }: { conn: Connection | null }) {
  const [saved, setSaved] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [note, setNote] = useState<Note>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!conn) return;
    getSettings(conn)
      .then((s) => {
        setSaved(s.instructions ?? "");
        setText(s.instructions ?? "");
      })
      .catch((e) => setNote({ tone: "bad", text: (e as Error).message }));
  }, [conn]);

  const save = async () => {
    if (!conn) return;
    setBusy(true);
    try {
      const s = await putSettings(conn, { instructions: text.trim() });
      setSaved(s.instructions ?? "");
      setText(s.instructions ?? "");
      setNote({ tone: "ok", text: text.trim() ? "Saved. JARVIS follows these from the next message on." : "Cleared." });
    } catch (e) {
      setNote({ tone: "bad", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card">
      <h2>Instructions</h2>
      <p className="dim small">
        How you want JARVIS to behave, in your own words. It reads these before every chat and voice conversation. They can shape its style
        and habits, but never loosen the permission and safety rules.
      </p>
      <textarea
        className="instructions"
        rows={12}
        maxLength={MAX_INSTRUCTIONS}
        disabled={saved === null}
        value={text}
        placeholder={"For example:\n- Keep answers short unless I ask for detail.\n- Reply in Hinglish when I write in Hinglish.\n- My work folder is D:\\Projects; save new files there.\n- Never play music louder than 40%."}
        onChange={(e) => setText(e.target.value)}
      />
      <div className="field-row">
        <button disabled={busy || saved === null || text.trim() === (saved ?? "").trim()} onClick={save}>SAVE</button>
        <span className="dim small">{text.length.toLocaleString()} / {MAX_INSTRUCTIONS.toLocaleString()}</span>
      </div>
      {note && <p className={note.tone}>{note.text}</p>}
    </section>
  );
}

function Skills({ conn }: { conn: Connection | null }) {
  const [mine, setMine] = useState<MySkill[] | null>(null);
  const [learned, setLearned] = useState<LearnedSkill[]>([]);
  const [draft, setDraft] = useState<{ id?: string; name: string; when_to: string; body: string; enabled: boolean } | null>(null);
  const [note, setNote] = useState<Note>(null);
  const [busy, setBusy] = useState(false);
  const fail = (e: unknown) => setNote({ tone: "bad", text: (e as Error).message });

  const load = useCallback(async () => {
    if (!conn) return;
    try {
      const [m, l] = await Promise.all([getMySkills(conn), getLearnedSkills(conn)]);
      setMine(m);
      setLearned(l);
    } catch (e) {
      fail(e);
    }
  }, [conn]);

  useEffect(() => {
    load();
  }, [load]);

  const save = async (s: { id?: string; name: string; when_to: string; body: string; enabled: boolean }, done: string) => {
    if (!conn) return;
    setBusy(true);
    try {
      await saveMySkill(conn, s);
      setDraft(null);
      setNote({ tone: "ok", text: done });
      await load();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (s: MySkill) => {
    if (!conn || !window.confirm(`Delete the skill “${s.name}”?`)) return;
    await removeMySkill(conn, s.id).then(load).catch(fail);
  };

  const forget = async (s: LearnedSkill) => {
    if (!conn) return;
    await forgetLearnedSkill(conn, s.id).then(load).catch(fail);
  };

  return (
    <>
      <section className="card">
        <div className="card-head">
          <h2>Your skills</h2>
          {!draft && <button onClick={() => { setNote(null); setDraft({ ...EMPTY }); }}>+ NEW SKILL</button>}
        </div>
        <p className="dim small">
          Steps you want JARVIS to follow for a kind of request. It uses a skill when your message names it or matches its “use when”.
        </p>

        {draft && (
          <div className="skill-form">
            <label>
              <span>Name</span>
              <input value={draft.name} maxLength={80} placeholder="Morning setup" onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
            </label>
            <label>
              <span>Use when</span>
              <input value={draft.when_to} maxLength={300} placeholder="I say start my work day, or open my work apps"
                onChange={(e) => setDraft({ ...draft, when_to: e.target.value })} />
            </label>
            <label>
              <span>Steps</span>
              <textarea rows={7} maxLength={6000} value={draft.body}
                placeholder={"1. Open Outlook and VS Code\n2. Open D:\\Projects in VS Code\n3. Play my focus playlist on Spotify at 30%\n4. Tell me today's reminders"}
                onChange={(e) => setDraft({ ...draft, body: e.target.value })} />
            </label>
            <div className="field-row">
              <button disabled={busy || !draft.name.trim() || !draft.body.trim()}
                onClick={() => save(draft, draft.id ? "Skill updated." : `Saved. Say “${draft.name.trim()}” to use it.`)}>
                SAVE
              </button>
              <button className="ghost" disabled={busy} onClick={() => setDraft(null)}>CANCEL</button>
            </div>
          </div>
        )}

        {mine && !mine.length && !draft && <p className="dim small">No skills yet.</p>}
        {!!mine?.length && (
          <ul className="skills">
            {mine.map((s) => (
              <li key={s.id} className={s.enabled ? "" : "off"}>
                <div className="skill-main">
                  <strong>{s.name}</strong>
                  {s.when_to && <span className="dim small">Use when: {s.when_to}</span>}
                  <span className="dim small">{s.uses ? `Used ${s.uses} time${s.uses === 1 ? "" : "s"}` : "Not used yet"}</span>
                </div>
                <div className="skill-actions">
                  <label className="switch" title={s.enabled ? "On" : "Off"}>
                    <input type="checkbox" checked={s.enabled} disabled={busy}
                      onChange={(e) => save({ ...s, enabled: e.target.checked }, e.target.checked ? `“${s.name}” is on.` : `“${s.name}” is off.`)} />
                    <span>{s.enabled ? "On" : "Off"}</span>
                  </label>
                  <button className="ghost" disabled={busy} onClick={() => { setNote(null); setDraft({ ...s }); }}>EDIT</button>
                  <button className="ghost" disabled={busy} onClick={() => remove(s)} aria-label={`Delete ${s.name}`}>DELETE</button>
                </div>
              </li>
            ))}
          </ul>
        )}
        {note && <p className={note.tone}>{note.text}</p>}
      </section>

      <section className="card">
        <h2>Learned by JARVIS</h2>
        <p className="dim small">
          Jobs of three or more steps that worked. JARVIS uses one as a guide when a similar request comes in, and stops offering it if it keeps
          failing.
        </p>
        {!learned.length && <p className="dim small">Nothing learned yet.</p>}
        <ul className="skills">
          {learned.map((s) => (
            <li key={s.id}>
              <details className="skill-main">
                <summary>
                  <strong>{s.goal}</strong>
                  <span className="dim small"> · worked {s.successes}, failed {s.failures}</span>
                </summary>
                <ol>
                  {s.steps.map((step, i) => <li key={i}>{step}</li>)}
                </ol>
              </details>
              <div className="skill-actions">
                <button className="ghost" onClick={() => forget(s)} aria-label={`Forget ${s.goal}`}>FORGET</button>
              </div>
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}
