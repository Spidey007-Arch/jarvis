import { useCallback, useEffect, useState } from "react";
import { PhoneStatus, getPhone, linkPhone, testPhone, unlinkPhone } from "../api";
import type { Connection } from "../shell";

type Note = { tone: "ok" | "bad"; text: string } | null;

/** Talk to JARVIS from Telegram: the user's own bot, paired to their one chat with a code. */
export default function PhoneCard({ conn, onStatus }: { conn: Connection | null; onStatus?: (s: PhoneStatus) => void }) {
  const [s, setS] = useState<PhoneStatus | null>(null);
  const [token, setToken] = useState("");
  const [note, setNote] = useState<Note>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!conn) return;
    try {
      const fresh = await getPhone(conn);
      setS(fresh);
      onStatus?.(fresh);
    } catch (e) {
      setNote({ tone: "bad", text: (e as Error).message });
    }
  }, [conn, onStatus]);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 3000); // notices the pairing as soon as the code is sent
    return () => window.clearInterval(t);
  }, [load]);

  const act = async (fn: () => Promise<unknown>, done: string) => {
    setBusy(true);
    setNote(null);
    try {
      await fn();
      setNote({ tone: "ok", text: done });
      await load();
    } catch (e) {
      setNote({ tone: "bad", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card">
      <h2>Phone</h2>
      <p className="dim small">
        Message JARVIS from anywhere through your own Telegram bot. It obeys only the one chat you pair, steps that need your OK come as Approve
        / Decline buttons, and automations can send their results there. JARVIS must be running on this PC.
      </p>

      {s?.linked ? (
        <>
          <p className="ok">Linked to {s.who}{s.bot ? ` through @${s.bot}` : ""}.</p>
          <p className="dim small">In the chat: any request, /stop to stop what JARVIS is doing, /new for a fresh conversation.</p>
          <div className="field-row">
            <button disabled={busy} onClick={() => conn && act(() => testPhone(conn), "Test message sent.")}>SEND A TEST</button>
            <button className="ghost" disabled={busy}
              onClick={() => conn && window.confirm("Unlink the phone and remove the bot token?") && act(() => unlinkPhone(conn), "Unlinked.")}>
              UNLINK
            </button>
          </div>
        </>
      ) : s?.has_token ? (
        <>
          <p>Now open <strong>@{s.bot || "your bot"}</strong> in Telegram and send it this code:</p>
          <p className="pair-code" aria-label="Pairing code">{s.pairing_code}</p>
          <p className="dim small">The code changes every 15 minutes. The first private chat that sends it becomes the only one JARVIS answers.</p>
          <div className="field-row">
            <button className="ghost" disabled={busy} onClick={() => conn && act(() => unlinkPhone(conn), "Removed.")}>CANCEL</button>
          </div>
        </>
      ) : (
        <>
          <ol className="steps small">
            <li>In Telegram, open <strong>@BotFather</strong>, send <code>/newbot</code> and pick a name.</li>
            <li>Paste the token it gives you below. It's stored encrypted on this PC and only ever sent to Telegram.</li>
            <li>Send your new bot the pairing code JARVIS shows next.</li>
          </ol>
          <div className="field-row">
            <input type="password" autoComplete="off" spellCheck={false} placeholder="Bot token, e.g. 123456789:AA…" value={token}
              onChange={(e) => setToken(e.target.value)} />
            <button disabled={busy || !token.trim()} onClick={() => conn && act(async () => { await linkPhone(conn, token.trim()); setToken(""); }, "Bot connected.")}>
              CONNECT
            </button>
          </div>
        </>
      )}
      {s?.error && <p className="bad">{s.error}</p>}
      {note && <p className={note.tone}>{note.text}</p>}
    </section>
  );
}
