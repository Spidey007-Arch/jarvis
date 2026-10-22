import { useCallback, useEffect, useState } from "react";
import { Conversation, deleteConversation, listConversations, patchConversation } from "../api";
import type { Connection } from "../shell";

const ago = (t: number) => {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
};

export default function HistoryPanel({
  conn,
  current,
  version,
  onOpen,
}: {
  conn: Connection | null;
  current: string | null;
  version: number;
  onOpen: (id: string | null) => void;
}) {
  const [convs, setConvs] = useState<Conversation[]>([]);
  const [search, setSearch] = useState("");
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!conn) return;
    try {
      setConvs(await listConversations(conn, search.trim() || undefined));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [conn, search]);

  useEffect(() => {
    refresh();
  }, [refresh, version]);

  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    }
    refresh();
  };

  return (
    <div className="history">
      <button className="newchat" onClick={() => onOpen(null)}>
        <span>+</span> New conversation
      </button>
      <input className="search" placeholder="Search conversations" value={search} onChange={(e) => setSearch(e.target.value)} />
      {error && <p className="notice bad">{error}</p>}
      <ul className="convs">
        {convs.map((c) => (
          <li key={c.id} className={c.id === current ? "on" : ""}>
            {renaming === c.id ? (
              <input
                autoFocus
                value={renameText}
                onChange={(e) => setRenameText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && renameText.trim() && conn) act(() => patchConversation(conn, c.id, { title: renameText.trim() })).then(() => setRenaming(null));
                  if (e.key === "Escape") setRenaming(null);
                }}
                onBlur={() => setRenaming(null)}
              />
            ) : confirmDelete === c.id ? (
              <div className="confirm">
                <span>Delete permanently?</span>
                <button className="danger" onClick={() => conn && act(() => deleteConversation(conn, c.id)).then(() => { setConfirmDelete(null); if (current === c.id) onOpen(null); })}>
                  Delete
                </button>
                <button className="ghost" onClick={() => setConfirmDelete(null)}>Keep</button>
              </div>
            ) : (
              <>
                <button className="conv" onClick={() => onOpen(c.id)} title={c.title}>
                  <span className="conv-title">
                    {c.pinned ? <b className="pin">◆</b> : null}
                    {c.title}
                  </span>
                  <span className="conv-when">{ago(c.updated_at)}</span>
                </button>
                <span className="conv-actions">
                  <button title={c.pinned ? "Unpin" : "Pin"} onClick={() => conn && act(() => patchConversation(conn, c.id, { pinned: !c.pinned }))}>◆</button>
                  <button title="Rename" onClick={() => { setRenaming(c.id); setRenameText(c.title); }}>✎</button>
                  <button title="Delete" onClick={() => setConfirmDelete(c.id)}>✕</button>
                </span>
              </>
            )}
          </li>
        ))}
        {convs.length === 0 && <li className="dim small">{search ? "No matches." : "No conversations yet."}</li>}
      </ul>
    </div>
  );
}
