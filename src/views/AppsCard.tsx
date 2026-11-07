import { useCallback, useEffect, useState } from "react";
import { AppView, SearchResult, disconnectApp, getApps, saveApp, searchEverything, signInApp, testApp } from "../api";
import { t } from "../i18n";
import type { Connection } from "../shell";

type Note = { tone: "ok" | "bad"; text: string } | null;
const GROUPS = ["Google and files", "Email", "Notes and work", "News", "Health"];

/** Apps JARVIS can read directly, how to connect each one, and one search across all of it. */
export default function AppsCard({ conn }: { conn: Connection | null }) {
  const [apps, setApps] = useState<AppView[]>([]);
  const [open, setOpen] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [query, setQuery] = useState("");
  const [found, setFound] = useState<SearchResult | null>(null);
  const [note, setNote] = useState<Note>(null);
  const [busy, setBusy] = useState(false);
  const fail = (e: unknown) => setNote({ tone: "bad", text: (e as Error).message });

  const load = useCallback(async () => {
    if (conn) setApps(await getApps(conn).catch((e) => { fail(e); return []; }));
  }, [conn]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {  // a sign-in finishes in the browser
    if (!apps.some((a) => a.status.startsWith("Waiting"))) return;
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [apps, load]);

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

  const edit = (a: AppView) => {
    setOpen(open === a.id ? "" : a.id);
    setValues(Object.fromEntries(a.fields.map((f) => [f.name, f.value])));
    setNote(null);
  };

  const search = () => {
    if (conn && query.trim()) act(async () => setFound(await searchEverything(conn, query.trim())), "");
  };

  return (
    <section className="card">
      <h2>{t("Apps")}</h2>
      <p className="dim small">
        Apps JARVIS can read by itself. It only reads: nothing here can send, change or delete anything in the app. Keys are stored
        encrypted on this PC and go only to that app.
      </p>

      <div className="field-row">
        <input value={query} placeholder={t("Search everything…")} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} />
        <button disabled={busy || !query.trim()} onClick={search}>{t("SEARCH")}</button>
      </div>
      {found && (
        <>
          <p className="dim small">{found.results.length} found in {found.searched.join(", ") || "nothing yet"}</p>
          <ul className="skills">
            {found.results.map((x) => (
              <li key={`${x.app}-${x.id}`}>
                <div className="skill-main">
                  <span>{x.title}</span>
                  <span className="dim small">{x.app}{x.when ? ` · ${x.when}` : ""}{x.snippet ? ` · ${x.snippet}` : ""}</span>
                </div>
              </li>
            ))}
          </ul>
          {Object.entries(found.problems).map(([name, why]) => <p key={name} className="dim small">{name}: {why}</p>)}
        </>
      )}

      {GROUPS.map((group) => (
        <div key={group}>
          <h3>{t(group)}</h3>
          <ul className="skills">
            {apps.filter((a) => a.group === group).map((a) => (
              <li key={a.id}>
                <div className="skill-main">
                  <span>{a.label}</span>
                  <span className={a.connected ? "ok small" : "dim small"}>{a.status || (a.connected ? t("Connected") : t("Not connected"))}</span>
                </div>
                <div className="row-actions">
                  {a.connected && <button className="ghost" disabled={busy} onClick={() => conn && act(() => testApp(conn, a.id), `${a.label} works.`)}>{t("TEST")}</button>}
                  <button className="ghost" disabled={busy} onClick={() => edit(a)}>{open === a.id ? t("CLOSE") : t("SET UP")}</button>
                </div>
                {open === a.id && (
                  <div className="app-setup">
                    <p className="dim small">{a.help}</p>
                    {a.fields.map((f) => (
                      <label key={f.name}>
                        <span>{f.label}</span>
                        {f.name === "feeds" ? (
                          <textarea rows={3} value={values[f.name] ?? ""} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })} />
                        ) : (
                          <input type={f.secret ? "password" : "text"} value={values[f.name] ?? ""} placeholder={f.saved && f.secret ? t("Saved") : ""} autoComplete="off"
                            onChange={(e) => setValues({ ...values, [f.name]: e.target.value })} />
                        )}
                      </label>
                    ))}
                    <div className="field-row">
                      {!!a.fields.length && <button disabled={busy} onClick={() => conn && act(() => saveApp(conn, a.id, values), "Saved.")}>{t("SAVE")}</button>}
                      {a.needs === "signin" && (
                        <button disabled={busy} onClick={() => conn && act(async () => { await saveApp(conn, a.id, values); await signInApp(conn, a.id); }, "Finish signing in in your browser.")}>
                          {t("SIGN IN")}
                        </button>
                      )}
                      {(a.connected || a.off) && (
                        <button className="ghost" disabled={busy} onClick={() => conn && (a.off ? act(() => saveApp(conn, a.id, {}), "Turned on.") : act(() => disconnectApp(conn, a.id), "Disconnected."))}>
                          {a.off ? t("TURN ON") : t("DISCONNECT")}
                        </button>
                      )}
                    </div>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
      {note && note.text && <p className={note.tone}>{note.text}</p>}
    </section>
  );
}
