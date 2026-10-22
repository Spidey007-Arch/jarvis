import { ChangeEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  MEMORY_TYPES,
  Memory,
  MemoryType,
  deleteMemory,
  exportMemoriesToFile,
  importMemories,
  listMemories,
  patchMemory,
} from "../api";
import type { Connection } from "../shell";

type Note = { tone: "ok" | "bad"; text: string } | null;

const fmtDate = (t: number) => new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });

export default function MemoryView({ conn }: { conn: Connection | null }) {
  const [items, setItems] = useState<Memory[]>([]);
  const [type, setType] = useState<MemoryType | "">("");
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ type: MemoryType; subject: string; content: string; importance: number } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [note, setNote] = useState<Note>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    if (!conn) return;
    try {
      setItems(await listMemories(conn, type || undefined, q.trim() || undefined));
    } catch (e) {
      setNote({ tone: "bad", text: (e as Error).message });
    }
  }, [conn, type, q]);

  useEffect(() => {
    load();
  }, [load]);

  const startEdit = (m: Memory) => {
    setEditing(m.id);
    setDraft({ type: m.type, subject: m.subject, content: m.content, importance: m.importance });
  };

  const saveEdit = async () => {
    if (!conn || !editing || !draft) return;
    try {
      await patchMemory(conn, editing, draft);
      setNote({ tone: "ok", text: "Memory updated." });
      setEditing(null);
      load();
    } catch (e) {
      setNote({ tone: "bad", text: (e as Error).message });
    }
  };

  const remove = async (id: string) => {
    if (!conn) return;
    try {
      await deleteMemory(conn, id);
      setNote({ tone: "ok", text: "Memory deleted permanently." });
      load();
    } catch (e) {
      setNote({ tone: "bad", text: (e as Error).message });
    }
    setConfirmDelete(null);
  };

  const doExport = async () => {
    if (!conn) return;
    try {
      const r = await exportMemoriesToFile(conn);
      setNote({ tone: "ok", text: `Exported ${r.count} memories to ${r.path}` });
    } catch (e) {
      setNote({ tone: "bad", text: (e as Error).message });
    }
  };

  const doImport = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!conn || !file) return;
    try {
      const parsed = JSON.parse(await file.text());
      const list = Array.isArray(parsed) ? parsed : parsed.memories;
      if (!Array.isArray(list)) throw new Error("That file does not contain a list of memories.");
      const r = await importMemories(conn, list);
      setNote({ tone: "ok", text: `Imported ${r.added}; skipped ${r.skipped} (duplicates or invalid).` });
      load();
    } catch (err) {
      setNote({ tone: "bad", text: `Import failed: ${(err as Error).message}` });
    }
  };

  if (!conn) return <div className="view-msg">Waiting for the backend to come online…</div>;

  return (
    <div className="memory">
      <div className="toolbar">
        <div className="chips" role="tablist">
          <button className={type === "" ? "on" : ""} onClick={() => setType("")}>ALL</button>
          {MEMORY_TYPES.map((t) => (
            <button key={t} className={type === t ? "on" : ""} onClick={() => setType(t)}>
              {t.toUpperCase()}
            </button>
          ))}
        </div>
        <input className="search" placeholder="Search memories" value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="ghost" onClick={doExport}>EXPORT</button>
        <button className="ghost" onClick={() => fileInput.current?.click()}>IMPORT</button>
        <input ref={fileInput} type="file" accept=".json,application/json" hidden onChange={doImport} />
      </div>
      {note && <p className={`notice ${note.tone}`}>{note.text}</p>}

      <ul className="mem-list">
        {items.map((m) =>
          editing === m.id && draft ? (
            <li key={m.id} className="mem editing">
              <div className="grid3">
                <select value={draft.type} onChange={(e) => setDraft({ ...draft, type: e.target.value as MemoryType })}>
                  {MEMORY_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
                <input value={draft.subject} onChange={(e) => setDraft({ ...draft, subject: e.target.value })} aria-label="Subject" />
                <label className="imp">
                  Importance
                  <input
                    type="number" min={0} max={1} step={0.1} value={draft.importance}
                    onChange={(e) => setDraft({ ...draft, importance: Number(e.target.value) })}
                  />
                </label>
              </div>
              <textarea rows={2} value={draft.content} onChange={(e) => setDraft({ ...draft, content: e.target.value })} aria-label="Content" />
              <div className="row-actions">
                <button onClick={saveEdit}>SAVE</button>
                <button className="ghost" onClick={() => setEditing(null)}>CANCEL</button>
              </div>
            </li>
          ) : (
            <li key={m.id} className="mem">
              <span className={`tag t-${m.type}`}>{m.type}</span>
              <div className="mem-body">
                <div className="mem-content">{m.content}</div>
                <div className="mem-meta">
                  {m.subject} · importance {m.importance.toFixed(1)} · updated {fmtDate(m.updated_at)} · used {m.access_count}×
                </div>
              </div>
              {confirmDelete === m.id ? (
                <span className="row-actions">
                  <button className="danger" onClick={() => remove(m.id)}>DELETE</button>
                  <button className="ghost" onClick={() => setConfirmDelete(null)}>KEEP</button>
                </span>
              ) : (
                <span className="row-actions">
                  <button className="ghost" onClick={() => startEdit(m)}>EDIT</button>
                  <button className="ghost" onClick={() => setConfirmDelete(m.id)}>DELETE</button>
                </span>
              )}
            </li>
          ),
        )}
        {items.length === 0 && (
          <li className="dim">
            {q || type ? "Nothing matches." : "Nothing remembered yet. JARVIS saves durable facts from your conversations."}
          </li>
        )}
      </ul>
    </div>
  );
}
