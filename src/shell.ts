import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

export type Overall = "starting" | "online" | "degraded" | "reconnecting" | "failed";

export interface ShellStatus {
  overall: Overall;
  backend: "starting" | "online" | "offline";
  agent: "waiting" | "starting" | "online" | "failed";
  port: number;
  detail: string;
  backend_restarts: number;
  agent_restarts: number;
  attached_to_existing: boolean;
  failed: boolean;
  ever_online: boolean;
}

export interface Connection {
  port: number;
  token: string;
  pid: number;
}

export const insideShell = (): boolean => "__TAURI_INTERNALS__" in window;

export const getStatus = () => invoke<ShellStatus>("get_status");
export const getConnection = () => invoke<Connection | null>("get_connection");
export const restartServices = () => invoke<void>("restart_services");

export const onStatus = (cb: (s: ShellStatus) => void) => listen<ShellStatus>("status", (e) => cb(e.payload));

export interface PingResult {
  ok: boolean;
  round_trip_ms?: number;
  agent?: { agent_version: string; pid: number; user: string; host: string; os: string };
  message?: string;
}

/** Real round trip: UI -> backend -> agent -> backend -> UI. */
export async function pingAgent(conn: Connection): Promise<PingResult> {
  try {
    const res = await fetch(`http://127.0.0.1:${conn.port}/api/agent/ping`, {
      method: "POST",
      headers: { Authorization: `Bearer ${conn.token}` },
    });
    const body = await res.json();
    if (!res.ok) return { ok: false, message: body.message ?? body.detail ?? `HTTP ${res.status}` };
    return body as PingResult;
  } catch (e) {
    return { ok: false, message: `Could not reach the backend: ${String(e)}` };
  }
}
