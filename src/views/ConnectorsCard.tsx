import { useEffect, useState } from "react";
import { Connector, deleteConnector, getConnectors, retryConnector, saveConnector } from "../api";
import type { Connection } from "../shell";

/** Connectors: MCP servers whose tools JARVIS can use (Slack, Notion, GitHub, a calendar...). */
export default function ConnectorsCard({ conn }: { conn: Connection | null }) {
  const [items, setItems] = useState<Connector[] | null>(null);
  const [kind, setKind] = useState<"local" | "web">("local");
  const [name, setName] = useState("");
  const [command, setCommand] = useState("npx");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [envName, setEnvName] = useState("");
  const [envValue, setEnvValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);

  const load = () => {
    if (conn) getConnectors(conn).then(setItems).catch((e) => setNote({ tone: "bad", text: (e as Error).message }));
  };
  useEffect(load, [conn]);

  const add = async () => {
    if (!conn) return;
    setBusy(true);
    setNote(null);
    try {
      const r = await saveConnector(conn, {
        name, kind, enabled: true,
        ...(kind === "local"
          ? { command, args: args.match(/"[^"]*"|\S+/g)?.map((a) => a.replace(/^"|"$/g, "")) ?? [], ...(envName.trim() ? { env: { [envName.trim()]: envValue } } : {}) }
          : { url, ...(token.trim() ? { token: token.trim() } : {}) }),
      });
      setNote(r.state === "connected" ? { tone: "ok", text: `${r.name} connected: ${r.tools} tools.` } : { tone: "bad", text: `${r.name}: ${r.error ?? r.state}` });
      setName("");
      setArgs("");
      setUrl("");
      setToken("");
      setEnvName("");
      setEnvValue("");
      load();
    } catch (e) {
      setNote({ tone: "bad", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    await fn().catch((e) => setNote({ tone: "bad", text: (e as Error).message }));
    setBusy(false);
    load();
  };

  return (
    <section className="card">
      <h2>Connectors</h2>
      <p className="dim small">
        Connect any MCP server and JARVIS can use its tools: Slack, Notion, GitHub, calendars, databases and more. Reading through a
        connector is always allowed; anything that changes something follows Permissions › connectors. Tokens are encrypted on this PC.
      </p>
      <ul className="connectors">
        {(items ?? []).map((c) => (
          <li key={c.name}>
            <span className={`dot ${c.state === "connected" ? "ready" : c.state === "error" ? "bad" : "cool"}`} />
            <span className="conn-name">{c.name}</span>
            <span className="dim small conn-meta" title={c.error ?? ""}>
              {c.state === "connected" ? `${c.tools} tools${c.server ? ` · ${c.server}` : ""}` : c.state === "error" ? `error: ${c.error}` : c.state}
            </span>
            <button className="ghost" disabled={busy || !conn} onClick={() => conn && act(() => retryConnector(conn, c.name))}>RECONNECT</button>
            <button className="ghost" disabled={busy || !conn} onClick={() => conn && act(() => deleteConnector(conn, c.name))}>REMOVE</button>
          </li>
        ))}
        {items && !items.length && <li className="dim small">No connectors yet.</li>}
      </ul>
      <div className="chips" role="radiogroup" aria-label="Connector kind">
        {(["local", "web"] as const).map((k) => (
          <button key={k} role="radio" aria-checked={kind === k} className={kind === k ? "on" : ""} onClick={() => setKind(k)}>
            {k === "local" ? "Local program" : "Web address"}
          </button>
        ))}
      </div>
      <div className="grid2">
        <label><span>Name</span><input value={name} placeholder="e.g. notion" onChange={(e) => setName(e.target.value)} /></label>
        {kind === "local" ? (
          <>
            <label><span>Command</span><input value={command} spellCheck={false} placeholder="npx" onChange={(e) => setCommand(e.target.value)} /></label>
            <label><span>Arguments</span><input value={args} spellCheck={false} placeholder="-y <server package>" onChange={(e) => setArgs(e.target.value)} /></label>
            <label><span>Secret setting (optional)</span>
              <span className="field-row tight">
                <input value={envName} spellCheck={false} placeholder="NAME, e.g. NOTION_TOKEN" onChange={(e) => setEnvName(e.target.value)} />
                <input type="password" value={envValue} autoComplete="off" placeholder="value" onChange={(e) => setEnvValue(e.target.value)} />
              </span>
            </label>
          </>
        ) : (
          <>
            <label><span>Server address</span><input value={url} spellCheck={false} placeholder="https://…/mcp" onChange={(e) => setUrl(e.target.value)} /></label>
            <label><span>Token (optional)</span><input type="password" value={token} autoComplete="off" placeholder="sent as a Bearer token" onChange={(e) => setToken(e.target.value)} /></label>
          </>
        )}
      </div>
      <div className="field-row">
        <button disabled={busy || !name.trim() || (kind === "local" ? !command.trim() : !url.trim())} onClick={add}>
          {busy ? "CONNECTING…" : "ADD CONNECTOR"}
        </button>
      </div>
      {note && <p className={`${note.tone} small`}>{note.text}</p>}
    </section>
  );
}
