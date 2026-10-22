import { useCallback, useEffect, useState } from "react";
import { ApiError, ModelInfo, Settings, getModels, getSettings, putSettings } from "../api";
import type { Connection } from "../shell";

type Note = { tone: "ok" | "bad"; text: string } | null;
type Section = "key" | "model" | "profile";

export default function SettingsView({ conn, onChanged }: { conn: Connection | null; onChanged: () => void }) {
  const [s, setS] = useState<Settings | null>(null);
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [address, setAddress] = useState("");
  const [models, setModels] = useState<{ chat: ModelInfo[]; embed: ModelInfo[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notes, setNotes] = useState<Record<Section, Note>>({ key: null, model: null, profile: null });
  const note = (k: Section, n: Note) => setNotes((prev) => ({ ...prev, [k]: n }));

  const load = useCallback(async () => {
    if (!conn) return;
    try {
      const fresh = await getSettings(conn);
      setS(fresh);
      setName(fresh.user_name);
      setAddress(fresh.address);
      if (fresh.has_gemini_key) {
        try {
          setModels(await getModels(conn));
        } catch (e) {
          setModels(null);
          note("model", { tone: "bad", text: (e as Error).message });
        }
      } else {
        setModels(null);
      }
    } catch (e) {
      note("key", { tone: "bad", text: (e as Error).message });
    }
  }, [conn]);

  useEffect(() => {
    load();
  }, [load]);

  const save = async (section: Section, body: Parameters<typeof putSettings>[1], okText: string) => {
    if (!conn) return;
    setBusy(true);
    try {
      const fresh = await putSettings(conn, body);
      setS(fresh);
      note(section, { tone: "ok", text: okText });
      onChanged();
      if (body.gemini_api_key !== undefined) {
        setKey("");
        await load();
      }
    } catch (e) {
      note(section, { tone: "bad", text: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  };

  if (!conn) return <div className="view-msg">Waiting for the backend to come online…</div>;
  if (!s) return <div className="view-msg">Loading settings…</div>;

  const Note = ({ k }: { k: Section }) => (notes[k] ? <p className={notes[k]!.tone}>{notes[k]!.text}</p> : null);

  return (
    <div className="settings">
      {s.load_error && <div className="banner bad">{s.load_error}</div>}

      <section className="card">
        <h2>Gemini API key</h2>
        <p className="dim small">
          Stored encrypted with Windows DPAPI, so only your Windows account on this PC can read it. Get a key at
          aistudio.google.com.
        </p>
        <div className="field-row">
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder={s.has_gemini_key ? `Key saved (${s.gemini_key_hint}). Paste a new one to replace it.` : "Paste your API key"}
            value={key}
            onChange={(e) => setKey(e.target.value)}
            aria-label="Gemini API key"
          />
          <button disabled={busy || !key.trim()} onClick={() => save("key", { gemini_api_key: key.trim() }, "Key verified with Google and saved.")}>
            {busy ? "CHECKING…" : "SAVE KEY"}
          </button>
          {s.has_gemini_key && (
            <button className="ghost" disabled={busy} onClick={() => save("key", { gemini_api_key: "" }, "Key removed.")}>
              REMOVE
            </button>
          )}
        </div>
        <Note k="key" />
      </section>

      <section className="card">
        <h2>Models</h2>
        {!s.has_gemini_key ? (
          <p className="dim">Save an API key first. The list comes from what your key can access.</p>
        ) : !models ? (
          <p className="dim">Loading models…</p>
        ) : (
          <div className="grid2">
            <label>
              <span>Chat model</span>
              <select
                value={s.chat_model}
                disabled={busy}
                onChange={(e) => save("model", { chat_model: e.target.value }, `Chat model set to ${e.target.value}.`)}
              >
                {!s.chat_model && <option value="">Choose…</option>}
                {models.chat.map((m) => (
                  <option key={m.name} value={m.name}>
                    {m.display_name} ({m.name})
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Memory embedding model</span>
              <select
                value={s.embed_model}
                disabled={busy}
                onChange={(e) => save("model", { embed_model: e.target.value }, `Embedding model set to ${e.target.value}.`)}
              >
                {!s.embed_model && <option value="">Choose…</option>}
                {models.embed.map((m) => (
                  <option key={m.name} value={m.name}>
                    {m.display_name} ({m.name})
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}
        <Note k="model" />
      </section>

      <section className="card">
        <h2>How JARVIS addresses you</h2>
        <div className="grid2">
          <label>
            <span>Your name</span>
            <input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
          </label>
          <label>
            <span>Form of address</span>
            <input value={address} maxLength={40} placeholder="sir" onChange={(e) => setAddress(e.target.value)} />
          </label>
        </div>
        <div className="field-row">
          <button
            disabled={busy || (name === s.user_name && address === s.address)}
            onClick={() => save("profile", { user_name: name.trim(), address: address.trim() || "sir" }, "Saved.")}
          >
            SAVE
          </button>
        </div>
        <Note k="profile" />
      </section>
    </div>
  );
}
