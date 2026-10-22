import { useEffect, useRef, useState } from "react";
import type { Connection } from "./shell";
import type { MemoryStats } from "./api";

export interface AgentStats {
  cpu_percent: number;
  ram_percent: number;
  ram_used_gb?: number;
  ram_total_gb?: number;
  disk_percent?: number | null;
  battery_percent?: number | null;
  battery_plugged?: boolean | null;
  net_down_kbps?: number;
  net_up_kbps?: number;
}

export interface AgentDetails {
  connected: boolean;
  info: { version?: string; pid?: number; os?: string };
  stats: Partial<AgentStats>;
  heartbeat_age_s: number | null;
}

export interface BackendHealth {
  version: string;
  pid: number;
  uptime_s: number;
  agent_connected: boolean;
}

export interface LogLine {
  ts: string;
  level: string;
  msg: string;
}

export type LogFile = "backend" | "agent" | "error";

const HISTORY = 60;

export interface Telemetry {
  reachable: boolean;
  health: BackendHealth | null;
  agent: AgentDetails | null;
  cpu: number[];
  ram: number[];
  logs: LogLine[];
  memory: MemoryStats | null;
}

const empty: Telemetry = { reachable: false, health: null, agent: null, cpu: [], ram: [], logs: [], memory: null };

/** Polls the backend while it is online. Everything returned is a real reading. */
export function useTelemetry(conn: Connection | null, logFile: LogFile): Telemetry {
  const [t, setT] = useState<Telemetry>(empty);
  const lastAge = useRef<number | null>(null);
  const logFileRef = useRef(logFile);
  logFileRef.current = logFile;

  useEffect(() => {
    if (!conn) {
      setT(empty);
      lastAge.current = null;
      return;
    }
    let stopped = false;
    const base = `http://127.0.0.1:${conn.port}`;
    const auth = { Authorization: `Bearer ${conn.token}` };

    const tick = async () => {
      try {
        const [h, a, l, m] = await Promise.all([
          fetch(`${base}/health`).then((r) => r.json() as Promise<BackendHealth>),
          fetch(`${base}/api/agent`, { headers: auth }).then((r) => r.json() as Promise<AgentDetails>),
          fetch(`${base}/api/logs?file=${logFileRef.current}&n=60`, { headers: auth }).then(
            (r) => r.json() as Promise<{ lines: LogLine[] }>,
          ),
          fetch(`${base}/api/memories/stats`, { headers: auth }).then((r) =>
            r.ok ? (r.json() as Promise<MemoryStats>) : null,
          ),
        ]);
        if (stopped) return;
        setT((prev) => {
          // Only record a sample when a new heartbeat has arrived.
          const age = a.heartbeat_age_s;
          const fresh = a.connected && age !== null && (lastAge.current === null || age < lastAge.current);
          lastAge.current = age;
          const push = (arr: number[], v: number | undefined) =>
            fresh && typeof v === "number" ? [...arr, v].slice(-HISTORY) : arr;
          return {
            reachable: true,
            health: h,
            agent: a,
            cpu: push(prev.cpu, a.stats.cpu_percent),
            ram: push(prev.ram, a.stats.ram_percent),
            logs: l.lines,
            memory: m,
          };
        });
      } catch {
        if (!stopped) setT((prev) => ({ ...prev, reachable: false }));
      }
    };

    tick();
    const id = window.setInterval(tick, 2000);
    return () => {
      stopped = true;
      window.clearInterval(id);
    };
  }, [conn]);

  return t;
}
