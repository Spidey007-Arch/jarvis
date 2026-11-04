import { useCallback, useEffect, useState } from "react";
import { BackupView, exportBackup, getBackup, restoreBackup, saveSync, syncNow } from "../api";
import { t } from "../i18n";
import type { Connection } from "../shell";

type Note = { tone: "ok" | "bad"; text: string } | null;
const when = (s?: number) => (s ? new Date(s * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "never");
const counts = (c: Record<string, number> | undefined) =>
  Object.entries(c ?? {}).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(", ") || "nothing new";

function base64(buf: ArrayBuffer): string {
  let s = "";
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

/** Encrypted backup to a file, and sync between the user's PCs through a folder their cloud drive already syncs. */
export default function BackupCard({ conn }: { conn: Connection | null }) {
  const [view, setView] = useState<BackupView | null>(null);
  const [pass, setPass] = useState("");
  const [withKeys, setWithKeys] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [restorePass, setRestorePass] = useState("");
  const [sync, setSync] = useState({ folder: "", include_secrets: false, passphrase: "" });
  const [note, setNote] = useState<Note>(null);
  const [busy, setBusy] = useState(false);
  const fail = (e: unknown) => setNote({ tone: "bad", text: (e as Error).message });

  const load = useCallback(async () => {
    if (!conn) return;
    const v = await getBackup(conn).catch((e) => { fail(e); return null; });
    setView(v);
    if (v) setSync((s) => ({ ...s, folder: s.folder || v.folder || v.suggested[0] || "", include_secrets: v.include_secrets }));
  }, [conn]);

  useEffect(() => {
    load();
  }, [load]);

  const act = async (fn: () => Promise<string>) => {
    setBusy(true);
    setNote(null);
    try {
      setNote({ tone: "ok", text: await fn() });
      await load();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  if (!view) return <section className="card"><h2>{t("Backup & sync")}</h2>{note && <p className={note.tone}>{note.text}</p>}</section>;

  return (
    <>
      <section className="card">
        <h2>{t("Backup & sync")}</h2>
        <p className="dim small">
          A backup holds your settings, instructions, memories, skills, automations, JARVIS's calendar, plugins and heartbeat list,
          encrypted with a passphrase only you know. Without it nobody can open the file, including you: write it down somewhere safe.
        </p>
        <div className="grid2">
          <label>
            <span>{t("Passphrase")}</span>
            <input type="password" autoComplete="new-password" value={pass}
              placeholder={view.has_passphrase ? "Empty: use the sync passphrase" : "At least 8 characters"} onChange={(e) => setPass(e.target.value)} />
          </label>
        </div>
        <label className="switch">
          <input type="checkbox" checked={withKeys} onChange={(e) => setWithKeys(e.target.checked)} />
          <span>{t("Include API keys and tokens")} (AI keys, connector tokens, linked calendar addresses)</span>
        </label>
        <div className="field-row">
          <button disabled={busy || (!pass && !view.has_passphrase)}
            onClick={() => conn && act(async () => {
              const r = await exportBackup(conn, pass, withKeys);
              setPass("");
              return `Saved to ${r.path}: ${r.memories} memories, ${r.skills} skills, ${r.automations} automations, ${r.events} events, ${r.plugins} plugins.`;
            })}>
            {t("Back up now")}
          </button>
        </div>

        <h3>{t("Restore")}</h3>
        <p className="dim small">Brings a backup in: adds what's missing and updates what changed. Nothing here is deleted.</p>
        <input type="file" accept=".jarvisbackup" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <div className="grid2">
          <label>
            <span>{t("Passphrase")}</span>
            <input type="password" autoComplete="off" value={restorePass} onChange={(e) => setRestorePass(e.target.value)} />
          </label>
        </div>
        <div className="field-row">
          <button disabled={busy || !file || !restorePass}
            onClick={() => conn && file && act(async () => {
              const r = await restoreBackup(conn, base64(await file.arrayBuffer()), restorePass);
              setRestorePass("");
              return `Restored${r.from ? ` from ${r.from}` : ""}: ${counts(r.restored)}.`;
            })}>
            {t("Restore")}
          </button>
        </div>
      </section>

      <section className="card">
        <div className="card-head">
          <h2>{t("Sync between my PCs")}</h2>
          <label className="switch">
            <input type="checkbox" checked={view.enabled} disabled={busy}
              onChange={(e) => conn && act(async () => {
                await saveSync(conn, { enabled: e.target.checked, folder: sync.folder, include_secrets: sync.include_secrets,
                  ...(sync.passphrase ? { passphrase: sync.passphrase } : {}) });
                setSync({ ...sync, passphrase: "" });
                return e.target.checked ? "Sync is on: JARVIS checks the folder every 10 minutes." : "Sync is off.";
              })} />
            <span>{view.enabled ? t("On") : t("Off")}</span>
          </label>
        </div>
        <p className="dim small">
          No server or account: pick a folder your OneDrive, Google Drive or Dropbox already syncs (or a USB stick), and turn sync on with the
          same folder and passphrase on each PC. Each PC writes its own encrypted file there and reads the others'. Deleting something on one PC
          doesn't delete it on the others.
        </p>
        <label>
          <span>{t("Sync folder")}</span>
          <input value={sync.folder} placeholder="C:\Users\you\OneDrive\JARVIS" onChange={(e) => setSync({ ...sync, folder: e.target.value })} />
        </label>
        {!!view.suggested.length && (
          <div className="chips">
            {view.suggested.map((f) => <button key={f} className="ghost" onClick={() => setSync({ ...sync, folder: f })}>{f}</button>)}
          </div>
        )}
        <div className="grid2">
          <label>
            <span>{t("Passphrase")}</span>
            <input type="password" autoComplete="new-password" value={sync.passphrase}
              placeholder={view.has_passphrase ? "Saved (type to change it)" : "At least 8 characters"} onChange={(e) => setSync({ ...sync, passphrase: e.target.value })} />
          </label>
        </div>
        <label className="switch">
          <input type="checkbox" checked={sync.include_secrets} onChange={(e) => setSync({ ...sync, include_secrets: e.target.checked })} />
          <span>{t("Include API keys and tokens")}</span>
        </label>
        <div className="field-row">
          <button disabled={busy || !sync.folder.trim() || (!sync.passphrase && !view.has_passphrase)}
            onClick={() => conn && act(async () => {
              await saveSync(conn, { enabled: view.enabled, folder: sync.folder, include_secrets: sync.include_secrets,
                ...(sync.passphrase ? { passphrase: sync.passphrase } : {}) });
              setSync({ ...sync, passphrase: "" });
              return "Saved.";
            })}>
            {t("SAVE")}
          </button>
          <button className="ghost" disabled={busy || !view.enabled}
            onClick={() => conn && act(async () => {
              const r = await syncNow(conn);
              return `Synced: brought in ${counts(r.brought)}${r.other_pcs?.length ? ` from ${r.other_pcs.join(", ")}` : " (no other PC has synced yet)"}.`;
            })}>
            {t("SYNC NOW")}
          </button>
        </div>
        <p className="dim small">
          This PC: {view.device.name} · last sync {when(view.last.at)}
          {view.last.error ? ` · ${view.last.error}` : view.last.at ? ` · brought in ${counts(view.last.brought)}` : ""}
        </p>
        {note && <p className={note.tone}>{note.text}</p>}
      </section>
    </>
  );
}
