import { useCallback, useEffect, useState } from "react";
import { DeveloperView, getDeveloper, setA2A } from "../api";
import { t } from "../i18n";
import type { Connection } from "../shell";

/** Using JARVIS from outside its window: the command line, Python, other apps and other agents. */
export default function DeveloperCard({ conn }: { conn: Connection | null }) {
  const [view, setView] = useState<DeveloperView | null>(null);
  const [key, setKey] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (conn) setView(await getDeveloper(conn).catch((e) => { setError((e as Error).message); return null; }));
  }, [conn]);

  useEffect(() => {
    load();
  }, [load]);

  const toggle = async (on: boolean) => {
    if (!conn) return;
    try {
      const res = await setA2A(conn, on);
      setKey(res.key ?? "");
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!view) return <section className="card"><h2>{t("Developers")}</h2>{error && <p className="bad">{error}</p>}</section>;
  const mcp = JSON.stringify({ mcpServers: { jarvis: view.mcp } }, null, 2);
  return (
    <section className="card">
      <h2>{t("Developers")}</h2>
      <p className="dim small">
        These all talk to JARVIS while it's running and go through the same permissions as chat. Nothing here opens JARVIS to the network.
      </p>

      <h3>{t("Command line")}</h3>
      <p className="dim small">Run <code>setup\jarvis.cmd</code>, or add the setup folder to your PATH and type <code>jarvis</code>.</p>
      <pre className="code-block">{`jarvis ask "what's on tomorrow?"\njarvis search dentist\njarvis agents`}</pre>

      <h3>{t("Python")}</h3>
      <p className="dim small">From the sdk folder: <code>pip install .</code></p>
      <pre className="code-block">{`from jarvis_sdk import Jarvis\n\nprint(Jarvis().ask("What's on tomorrow?"))`}</pre>

      <h3>{t("MCP server")}</h3>
      <p className="dim small">
        Add this to any app that uses MCP servers, such as Claude Desktop, VS Code or Cursor, and it can ask JARVIS and search what JARVIS
        knows. Everything it asks JARVIS to do waits for your yes here, even with Full control on.
      </p>
      <pre className="code-block">{mcp}</pre>

      <div className="card-head">
        <h3>{t("Other agents (A2A)")}</h3>
        <label className="switch">
          <input type="checkbox" checked={view.a2a_on} onChange={(e) => toggle(e.target.checked)} />
          <span>{view.a2a_on ? t("On") : t("Off")}</span>
        </label>
      </div>
      <p className="dim small">
        Lets AI agents on this PC send JARVIS requests at <code>{view.a2a_url}</code>, using a key. Every action they ask for waits for your yes.
        Turning it off cancels the key. JARVIS can always ask other agents, and asks you first each time.
      </p>
      {key && (
        <>
          <p className="ok small">{t("Give this key to the other agent. It's shown only now.")}</p>
          <pre className="code-block">{key}</pre>
        </>
      )}
      {error && <p className="bad">{error}</p>}
    </section>
  );
}
