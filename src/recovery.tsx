import { Component, ReactNode } from "react";
import { getConnection } from "./shell";

// Crash recovery for the window. A page error is written to logs/crash.log through the backend; a render
// error reloads the page instead of leaving it blank (at most RELOADS_PER_MIN times a minute, then it
// stays on the message so a broken build can't loop); and the window checks in with the backend, so the
// shell can reload a window that has frozen or never finished loading.

const RELOADS_PER_MIN = 3;
const RELOAD_KEY = "jarvis.reloads";

async function post(path: string, body?: unknown): Promise<void> {
  try {
    const c = await getConnection();
    if (!c) return;
    await fetch(`http://127.0.0.1:${c.port}${path}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${c.token}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    /* the backend is restarting; nothing to do */
  }
}

export function reportError(where: string, error: unknown): void {
  const e = error instanceof Error ? error : new Error(String(error));
  post("/ui/error", { where, message: e.message, stack: e.stack ?? "" });
}

function mayReload(): boolean {
  try {
    const now = Date.now();
    const recent = (JSON.parse(sessionStorage.getItem(RELOAD_KEY) ?? "[]") as number[]).filter((t) => now - t < 60_000);
    if (recent.length >= RELOADS_PER_MIN) return false;
    sessionStorage.setItem(RELOAD_KEY, JSON.stringify([...recent, now]));
    return true;
  } catch {
    return true;
  }
}

export function startRecovery(): void {
  window.addEventListener("error", (e) => reportError("window", e.error ?? e.message));
  window.addEventListener("unhandledrejection", (e) => reportError("promise", e.reason));
  const alive = () => post("/ui/alive");
  alive();
  window.setInterval(alive, 10_000);
}

export class Recover extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: unknown) {
    reportError("render", error);
    if (mayReload()) window.setTimeout(() => window.location.reload(), 1500);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="recover">
        <div className="recover-box glass">
          <strong>JARVIS hit a problem and is reloading…</strong>
          <button onClick={() => window.location.reload()}>RELOAD NOW</button>
        </div>
      </div>
    );
  }
}
