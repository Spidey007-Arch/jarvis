import { useEffect, useState } from "react";
import { ApiKeys, deleteApiKey, getApiKeys, saveApiKey } from "../api";
import type { Connection } from "../shell";

/** Keys for public APIs JARVIS can call (weather, news, ...). Stored encrypted; JARVIS uses them but never sees them. */
export default function ApiKeysCard({ conn }: { conn: Connection | null }) {
  const [info, setInfo] = useState<ApiKeys | null>(null);
  const [service, setService] = useState("");
  const [key, setKey] = useState("");
  const [note, setNote] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (conn) getApiKeys(conn).then(setInfo).catch((e) => setNote({ tone: "bad", text: (e as Error).message }));
  }, [conn]);

  const save = async () => {
    if (!conn || !service.trim() || !key.trim()) return;
    setBusy(true);
    try {
      const r = await saveApiKey(conn, service.trim(), key.trim());
      setInfo(info && { ...info, saved: r.saved });
      setNote({ tone: "ok", text: `Saved. JARVIS can now use ${service.trim()}.` });
      setService("");
      setKey("");
    } catch (e) {
      setNote({ tone: "bad", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const remove = async (name: string) => {
    if (!conn) return;
    const r = await deleteApiKey(conn, name).catch((e) => {
      setNote({ tone: "bad", text: (e as Error).message });
      return null;
    });
    if (r) setInfo(info && { ...info, saved: r.saved });
  };

  return (
    <section className="card">
      <h2>API keys</h2>
      <p className="dim small">
        JARVIS can use all {info ? info.catalog.toLocaleString() : "~2,200"} APIs from the public-apis list. About half need no key; for the
        others, get a free key from the API's site and save it here. Keys are encrypted on this PC and never shown to the AI.
      </p>
      {!!info?.saved.length && (
        <ul className="keys">
          {info.saved.map((name) => (
            <li key={name}>
              <span>{name}</span>
              <button className="ghost" onClick={() => remove(name)} aria-label={`Remove the ${name} key`}>REMOVE</button>
            </li>
          ))}
        </ul>
      )}
      <div className="field-row keys-add">
        <input list="need-key-apis" placeholder="API name, e.g. OpenWeatherMap" value={service} onChange={(e) => setService(e.target.value)} />
        <input type="password" placeholder="Key" value={key} autoComplete="off" onChange={(e) => setKey(e.target.value)} />
        <button disabled={busy || !service.trim() || !key.trim()} onClick={save}>SAVE</button>
      </div>
      <datalist id="need-key-apis">{info?.need_key.map((n) => <option key={n} value={n} />)}</datalist>
      {note && <p className={note.tone}>{note.text}</p>}
    </section>
  );
}
