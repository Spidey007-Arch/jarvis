import { useCallback, useEffect, useState } from "react";
import {
  CatalogPlugin, InstalledPlugin, PluginPreview, PluginSource, exportSkills, getCatalog, getPlugins, installPlugin, previewPlugin, removePlugin,
} from "../api";
import { t } from "../i18n";
import type { Connection } from "../shell";

type Note = { tone: "ok" | "bad"; text: string } | null;
const CATALOG_REPO = "https://github.com/Spidey007-Arch/jarvis/tree/main/community";

/** Plugins: bundles of skills and connectors, from the community catalog or a file, shown in full before anything is added. */
export default function PluginsCard({ conn }: { conn: Connection | null }) {
  const [installed, setInstalled] = useState<InstalledPlugin[]>([]);
  const [catalog, setCatalog] = useState<CatalogPlugin[] | null>(null);
  const [catalogError, setCatalogError] = useState("");
  const [preview, setPreview] = useState<{ p: PluginPreview; source: PluginSource } | null>(null);
  const [fileText, setFileText] = useState("");
  const [note, setNote] = useState<Note>(null);
  const [busy, setBusy] = useState(false);
  const fail = (e: unknown) => setNote({ tone: "bad", text: (e as Error).message });

  const load = useCallback(async () => {
    if (!conn) return;
    setInstalled(await getPlugins(conn).catch((e) => { fail(e); return []; }));
    getCatalog(conn).then((c) => { setCatalog(c); setCatalogError(""); }).catch((e) => setCatalogError((e as Error).message));
  }, [conn]);

  useEffect(() => {
    load();
  }, [load]);

  const act = async (fn: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try {
      setNote(null);
      await fn();
      if (done) setNote({ tone: "ok", text: done });
      await load();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const view = (source: PluginSource) =>
    conn && act(async () => setPreview({ p: await previewPlugin(conn, source), source }), "");

  const readFile = (f: File | undefined) => f && f.text().then(setFileText).catch(fail);

  return (
    <>
      {preview && (
        <section className="card plugin-preview">
          <div className="card-head">
            <h2>{preview.p.name} <span className="dim small">{preview.p.version && `v${preview.p.version}`}{preview.p.author && ` · ${preview.p.author}`}</span></h2>
          </div>
          {preview.p.description && <p>{preview.p.description}</p>}
          {preview.p.homepage && <p className="dim small">{preview.p.homepage}</p>}
          {!!preview.p.skills.length && (
            <>
              <h3>Skills it adds ({preview.p.skills.length})</h3>
              <ul className="skills">
                {preview.p.skills.map((s) => (
                  <li key={s.name}>
                    <details className="skill-main">
                      <summary><strong>{s.name}</strong>{s.when_to && <span className="dim small"> · use when: {s.when_to}</span>}</summary>
                      <pre className="plugin-body">{s.body}</pre>
                    </details>
                  </li>
                ))}
              </ul>
            </>
          )}
          {!!preview.p.commands.length && (
            <>
              <h3>Connectors it adds ({preview.p.commands.length}), turned off</h3>
              <p className="dim small">
                A connector runs a program on your PC or talks to a web service. Each is added switched off: check it, then turn it on in
                Connectors and add any token it needs there.
              </p>
              <ul className="skills">
                {preview.p.commands.map((c) => (
                  <li key={c.connector}>
                    <div className="skill-main">
                      <strong>{c.connector}</strong>
                      <code className="small">{c.runs}</code>
                      {!!c.env_names.length && <span className="dim small">Needs: {c.env_names.join(", ")}</span>}
                      {c.needs_token && <span className="dim small">Needs a token</span>}
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
          {!!preview.p.clashes.length && <p className="bad small">Already there: {preview.p.clashes.join(", ")}. Rename or remove those first.</p>}
          <div className="field-row">
            <button disabled={busy || !!preview.p.clashes.length}
              onClick={() => conn && act(async () => { await installPlugin(conn, preview.source); setPreview(null); setFileText(""); },
                `Installed ${preview.p.name}.${preview.p.commands.length ? " Turn its connectors on in Connectors when you're ready." : ""}`)}>
              {preview.p.installed ? t("UPDATE") : t("INSTALL")}
            </button>
            <button className="ghost" disabled={busy} onClick={() => setPreview(null)}>{t("CANCEL")}</button>
          </div>
        </section>
      )}

      <section className="card">
        <h2>{t("Installed")}</h2>
        <p className="dim small">A plugin bundles skills and connectors that someone made and shared. Removing one removes everything it added.</p>
        {!installed.length && <p className="dim small">No plugins yet.</p>}
        <ul className="skills">
          {installed.map((p) => (
            <li key={p.id}>
              <div className="skill-main">
                <strong>{p.name}</strong>
                <span className="dim small">
                  {[p.skills.length && `${p.skills.length} skill${p.skills.length === 1 ? "" : "s"}`,
                    p.connectors.length && `connectors: ${p.connectors.join(", ")}`, p.author].filter(Boolean).join(" · ")}
                </span>
              </div>
              <div className="skill-actions">
                <button className="ghost" disabled={busy}
                  onClick={() => conn && window.confirm(`Remove ${p.name} and everything it added?`) && act(() => removePlugin(conn, p.id), `Removed ${p.name}.`)}>
                  {t("REMOVE")}
                </button>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className="card">
        <h2>{t("Community catalog")}</h2>
        <p className="dim small">
          Plugins people shared, each reviewed before it's listed. To add yours, use SHARE MY SKILLS below and send the file as a pull request
          to <a href={CATALOG_REPO} target="_blank" rel="noreferrer">community/</a> in the JARVIS repository.
        </p>
        {catalogError && <p className="bad small">{catalogError}</p>}
        {catalog && !catalog.length && <p className="dim small">The catalog is empty.</p>}
        <ul className="skills">
          {catalog?.map((p) => (
            <li key={p.id}>
              <div className="skill-main">
                <strong>{p.name}</strong>
                <span className="dim small">{p.description}</span>
                <span className="dim small">{[p.author, ...p.has].filter(Boolean).join(" · ")}{p.installed ? " · installed" : ""}</span>
              </div>
              <div className="skill-actions">
                <button className="ghost" disabled={busy} onClick={() => view({ catalog_id: p.id })}>{t("VIEW")}</button>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className="card">
        <h2>{t("Install from a file")}</h2>
        <p className="dim small">A <code>.jarvis-plugin.json</code> file someone sent you. You'll see exactly what it adds before anything happens.</p>
        <input type="file" accept=".json,application/json" onChange={(e) => readFile(e.target.files?.[0])} />
        <textarea rows={4} value={fileText} placeholder='{"format": "jarvis-plugin", ...}' onChange={(e) => setFileText(e.target.value)} />
        <div className="field-row">
          <button disabled={busy || !fileText.trim()} onClick={() => view({ text: fileText })}>{t("VIEW")}</button>
          <button className="ghost" disabled={busy}
            onClick={() => conn && act(async () => {
              const r = await exportSkills(conn, "My skills");
              setNote({ tone: "ok", text: `Saved ${r.skills} skill${r.skills === 1 ? "" : "s"} as a plugin: ${r.path}` });
            }, "")}>
            {t("SHARE MY SKILLS")}
          </button>
        </div>
        {note && note.text && <p className={note.tone}>{note.text}</p>}
      </section>
    </>
  );
}
