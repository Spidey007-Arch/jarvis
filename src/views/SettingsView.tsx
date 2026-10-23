import { useCallback, useEffect, useState } from "react";
import { ApiError, ModelInfo, ProviderPreset, Settings, SettingsUpdate, getModels, getProviders, getSettings, putSettings } from "../api";
import type { Connection } from "../shell";

type Note = { tone: "ok" | "bad"; text: string } | null;
type Section = "provider" | "model" | "profile";

export default function SettingsView({ conn, onChanged }: { conn: Connection | null; onChanged: () => void }) {
  const [s, setS] = useState<Settings | null>(null);
  const [presets, setPresets] = useState<ProviderPreset[]>([]);
  const [key, setKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [name, setName] = useState("");
  const [address, setAddress] = useState("");
  const [models, setModels] = useState<{ chat: ModelInfo[]; embed: ModelInfo[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notes, setNotes] = useState<Record<Section, Note>>({ provider: null, model: null, profile: null });
  const note = (k: Section, n: Note) => setNotes((prev) => ({ ...prev, [k]: n }));

  const preset = presets.find((p) => p.id === s?.provider);
  const connected = !!s && (s.has_api_key || !preset?.needs_key) && !!s.chat_model;

  const load = useCallback(async () => {
    if (!conn) return;
    try {
      const [fresh, ps] = await Promise.all([getSettings(conn), getProviders(conn)]);
      setS(fresh);
      setPresets(ps);
      setName(fresh.user_name);
      setAddress(fresh.address);
      setBaseUrl(fresh.base_url);
      const p = ps.find((x) => x.id === fresh.provider);
      if ((fresh.has_api_key || !p?.needs_key) && fresh.chat_model) {
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
      note("provider", { tone: "bad", text: (e as Error).message });
    }
  }, [conn]);

  useEffect(() => {
    load();
  }, [load]);

  const save = async (section: Section, body: SettingsUpdate, okText: string, reload = false) => {
    if (!conn) return;
    setBusy(true);
    note(section, null);
    try {
      setS(await putSettings(conn, body));
      note(section, { tone: "ok", text: okText });
      onChanged();
      if (reload) {
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
  if (!s || !preset) return <div className="view-msg">Loading settings…</div>;

  const showBaseUrl = ["ollama", "custom", "omniroute"].includes(preset.id);
  const showKey = preset.needs_key || ["custom", "omniroute"].includes(preset.id);
  const canConnect = !busy && (preset.needs_key ? !!key.trim() || (s.has_api_key && baseUrl !== s.base_url) : !!baseUrl.trim());
  const Note = ({ k }: { k: Section }) => (notes[k] ? <p className={notes[k]!.tone}>{notes[k]!.text}</p> : null);

  return (
    <div className="settings">
      {s.load_error && <div className="banner bad">{s.load_error}</div>}

      <section className="card">
        <h2>AI provider</h2>
        <p className="dim small">Bring your own key. Keys are encrypted with Windows DPAPI and never leave this PC except to call the provider.</p>
        <div className="providers" role="radiogroup" aria-label="AI provider">
          {presets.map((p) => (
            <button
              key={p.id}
              role="radio"
              aria-checked={p.id === s.provider}
              className={`prov ${p.id === s.provider ? "on" : ""} ${p.featured ? "featured" : ""}`}
              disabled={busy}
              onClick={() => p.id !== s.provider && save("provider", { provider: p.id }, `Switched to ${p.name}.`, true)}
            >
              <span>
                {p.featured && <b className="star">★</b>}
                {p.name}
                {p.badge && <em className="badge">{p.badge}</em>}
              </span>
              {s.providers_with_keys.includes(p.id) && <i title="Key saved">●</i>}
            </button>
          ))}
        </div>
        <p className="dim small">{preset.key_hint}</p>

        {showBaseUrl && (
          <label>
            <span>Base URL</span>
            <input value={baseUrl} spellCheck={false} placeholder="https://…/v1" onChange={(e) => setBaseUrl(e.target.value)} />
          </label>
        )}
        <div className="field-row">
          {showKey && (
            <input
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder={
                s.has_api_key
                  ? `Key saved (${s.api_key_hint}). Paste a new one to replace it.`
                  : preset.needs_key
                    ? `Paste your ${preset.name} API key`
                    : "API key (if the server needs one)"
              }
              value={key}
              onChange={(e) => setKey(e.target.value)}
              aria-label={`${preset.name} API key`}
            />
          )}
          <button
            disabled={!canConnect}
            onClick={() => {
              const body: SettingsUpdate = { provider: preset.id };
              if (key.trim()) body.api_key = key.trim();
              if (showBaseUrl) body.base_url = baseUrl.trim();
              save("provider", body, `Connected to ${preset.name}.`, true);
            }}
          >
            {busy ? "CONNECTING…" : "CONNECT"}
          </button>
          {s.has_api_key && (
            <button className="ghost" disabled={busy} onClick={() => save("provider", { api_key: "" }, "Key removed.", true)}>
              REMOVE KEY
            </button>
          )}
        </div>
        <p className={connected ? "ok small" : "dim small"}>{connected ? `● Connected · ${s.chat_model}` : "○ Not connected yet"}</p>
        <Note k="provider" />
      </section>

      <section className="card">
        <h2>Models</h2>
        {!models ? (
          <p className="dim">Connect a provider first. The list comes from what your account can access.</p>
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
                    {m.display_name === m.name ? m.name : `${m.display_name} (${m.name})`}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Memory search</span>
              <select
                value={s.embed_model}
                disabled={busy}
                onChange={(e) =>
                  save("model", { embed_model: e.target.value }, e.target.value ? `Embedding model set to ${e.target.value}.` : "Memory will use keyword search.")
                }
              >
                <option value="">Keyword search (no embeddings)</option>
                {models.embed.map((m) => (
                  <option key={m.name} value={m.name}>
                    Semantic · {m.name}
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
        <label className="switch">
          <input
            type="checkbox"
            checked={s.proactive_alerts}
            disabled={busy}
            onChange={(e) =>
              save("profile", { proactive_alerts: e.target.checked },
                e.target.checked ? "JARVIS will speak up about low battery, a full drive or sustained overload." : "Proactive alerts are off.")
            }
          />
          <span>Speak up proactively (low battery, full drive, sustained CPU or memory overload)</span>
        </label>
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
