import { useEffect, useState } from "react";
import { Permission, Policy, getPermissions, putSettings } from "../api";
import type { Connection } from "../shell";

const CHOICES: { id: Policy; label: string }[] = [
  { id: "allow", label: "Allow" },
  { id: "ask", label: "Ask" },
  { id: "block", label: "Block" },
];

/** Allow / Ask / Block for each kind of action. Locked ones can be blocked but never allowed without asking. */
export default function PermissionsCard({ conn, fullControl, onChanged }: { conn: Connection | null; fullControl: boolean; onChanged: () => void }) {
  const [rows, setRows] = useState<Permission[] | null>(null);
  const [note, setNote] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => {
    if (conn) getPermissions(conn).then(setRows).catch((e) => setNote({ tone: "bad", text: (e as Error).message }));
  };
  useEffect(load, [conn, fullControl]);

  const set = async (body: Parameters<typeof putSettings>[1], okText: string) => {
    if (!conn) return;
    setBusy(true);
    try {
      await putSettings(conn, body);
      setNote({ tone: "ok", text: okText });
      onChanged();
      load();
    } catch (e) {
      setNote({ tone: "bad", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card">
      <h2>Permissions</h2>
      <p className="dim small">What JARVIS may do on this PC. Ask shows an Allow / Deny prompt (or asks for a spoken yes).</p>
      <label className="switch">
        <input
          type="checkbox"
          checked={fullControl}
          disabled={busy}
          onChange={(e) =>
            set({ full_control: e.target.checked },
              e.target.checked ? "Full control: everyday changes run without asking. Locked ones still ask." : "Full control is off.")
          }
        />
        <span>Full control (only on your own PC): files, commands, processes and windows default to Allow</span>
      </label>
      <ul className="perms">
        {(rows ?? []).map((p) => (
          <li key={p.id}>
            <span className="perm-label">
              {p.label}
              {p.locked && <em className="perm-lock" title="Can be blocked, never allowed without asking">always asks</em>}
            </span>
            <span className="seg" role="radiogroup" aria-label={p.label}>
              {CHOICES.map((c) => (
                <button
                  key={c.id}
                  role="radio"
                  aria-checked={p.policy === c.id}
                  className={p.policy === c.id ? "on" : ""}
                  disabled={busy || (p.locked && c.id === "allow")}
                  onClick={() => p.policy !== c.id && set({ permissions: { [p.id]: c.id } }, `${p.label}: ${c.label}.`)}
                >
                  {c.label}
                </button>
              ))}
            </span>
          </li>
        ))}
      </ul>
      {note && <p className={`${note.tone} small`}>{note.text}</p>}
    </section>
  );
}
