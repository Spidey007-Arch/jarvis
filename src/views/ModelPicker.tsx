import { useEffect, useRef, useState } from "react";
import { ModelStatus, getModelStatus, putSettings, testModel } from "../api";
import type { Connection } from "../shell";

const short = (name: string) => name.replace(/^models\//, "").replace(/^[a-z-]+\//, "");

/** Header button: which model JARVIS thinks with. Open it to switch, see which models are resting after
 *  failures, and test one. */
export default function ModelPicker({ conn, onChanged }: { conn: Connection | null; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState("");
  const [models, setModels] = useState<ModelStatus[] | null>(null);
  const [tests, setTests] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);

  const load = () => {
    if (!conn) return;
    getModelStatus(conn)
      .then((r) => {
        setCurrent(r.current);
        setModels(r.models);
        setError(null);
      })
      .catch((e) => setError((e as Error).message));
  };
  useEffect(load, [conn]);
  useEffect(() => {
    if (!open) return;
    load();
    const close = (e: MouseEvent) => box.current && !box.current.contains(e.target as Node) && setOpen(false);
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);

  const choose = async (name: string) => {
    if (!conn || name === current) return;
    try {
      await putSettings(conn, { chat_model: name });
      setCurrent(name);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const test = async (name: string) => {
    if (!conn) return;
    setTests((t) => ({ ...t, [name]: "testing…" }));
    const r = await testModel(conn, name).catch((e) => ({ ok: false, error: (e as Error).message, ms: undefined }));
    setTests((t) => ({ ...t, [name]: r.ok ? `answered in ${((r.ms ?? 0) / 1000).toFixed(1)} s` : `failed: ${r.error}` }));
    load();
  };

  if (!conn) return null;
  return (
    <div className="model-picker" ref={box}>
      <button className="model-btn" onClick={() => setOpen((o) => !o)} title="The model JARVIS thinks with" aria-expanded={open}>
        <span className="model-glyph" aria-hidden>◈</span>
        {current ? short(current) : "model"}
      </button>
      {open && (
        <div className="model-pop glass" role="dialog" aria-label="Choose a model">
          {error && <p className="bad small">{error}</p>}
          {!models && !error && <p className="dim small">Loading models…</p>}
          <ul>
            {(models ?? []).map((m) => (
              <li key={m.name} className={m.name === current ? "on" : ""}>
                <button className="model-name" onClick={() => choose(m.name)} title={m.name}>
                  <i className={`dot ${m.cooling_s ? "cool" : "ready"}`} />
                  {m.display_name === m.name ? short(m.name) : m.display_name}
                </button>
                <span className="dim small model-meta">
                  {tests[m.name] ?? (m.cooling_s ? `resting ${Math.ceil(m.cooling_s / 60)} min after errors` : m.used_today ? `${m.used_today} today` : "")}
                </span>
                <button className="ghost model-test" onClick={() => test(m.name)}>TEST</button>
              </li>
            ))}
          </ul>
          <p className="dim small">If the chosen model is busy or out of quota, JARVIS uses the next one and says so.</p>
        </div>
      )}
    </div>
  );
}
