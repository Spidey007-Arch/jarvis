import { useState } from "react";
import { PingResult, ShellStatus, pingAgent, restartServices } from "../shell";
import type { Connection } from "../shell";
import type { Telemetry } from "../telemetry";
import { Spark, Stat } from "../widgets";

const fmtUptime = (s: number) => {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${Math.floor(s % 60)}s` : `${Math.floor(s)}s`;
};

export default function SystemPanel({ status, conn, tel }: { status: ShellStatus | null; conn: Connection | null; tel: Telemetry }) {
  const [ping, setPing] = useState<PingResult | null>(null);
  const [busy, setBusy] = useState(false);
  const s = tel.agent?.stats ?? {};
  const agentOnline = status?.agent === "online";

  return (
    <div className="system">
      <section className="sys-block">
        <h3>Services</h3>
        <Stat k="Backend" cap v={status?.backend ?? "starting"} tone={status?.backend === "online" ? "ok" : "warn"} />
        <Stat k="Desktop agent" cap v={status?.agent ?? "waiting"} tone={agentOnline ? "ok" : "warn"} />
        <Stat k="Port" v={status?.port ? `${status.port}${status.attached_to_existing ? " (attached)" : ""}` : "—"} />
        <Stat k="Uptime" v={tel.health ? fmtUptime(tel.health.uptime_s) : "—"} />
        <Stat k="Agent heartbeat" v={tel.agent?.heartbeat_age_s != null ? `${tel.agent.heartbeat_age_s.toFixed(1)}s ago` : "—"} />
        {status && (status.backend_restarts > 0 || status.agent_restarts > 0) && (
          <Stat k="Auto-restarts" v={`backend ${status.backend_restarts} · agent ${status.agent_restarts}`} tone="warn" />
        )}
        {status?.detail && <pre className="sys-error">{status.detail}</pre>}
        <div className="sys-actions">
          <button
            disabled={!conn || !agentOnline || busy}
            onClick={async () => {
              if (!conn) return;
              setBusy(true);
              setPing(await pingAgent(conn));
              setBusy(false);
            }}
          >
            {busy ? "Pinging…" : "Ping agent"}
          </button>
          {(status?.failed || status?.overall === "degraded") && (
            <button className="ghost" onClick={() => restartServices()}>Retry startup</button>
          )}
        </div>
        {ping && <p className={ping.ok ? "ok small" : "bad small"}>{ping.ok ? `Round trip ${ping.round_trip_ms} ms` : ping.message}</p>}
      </section>

      <section className="sys-block">
        <h3>Processor</h3>
        <Spark data={tel.cpu} current={s.cpu_percent} unit="%" />
      </section>
      <section className="sys-block">
        <h3>Memory</h3>
        <Spark data={tel.ram} current={s.ram_percent} unit="%" />
        {s.ram_used_gb !== undefined && <div className="dim small">{s.ram_used_gb} / {s.ram_total_gb} GB in use</div>}
      </section>
      <section className="sys-block">
        <h3>Machine</h3>
        <Stat k="System drive" v={s.disk_percent != null ? `${s.disk_percent.toFixed(0)}% used` : "—"} />
        <Stat k="Battery" v={s.battery_percent != null ? `${s.battery_percent}%${s.battery_plugged ? " (charging)" : ""}` : "none"} />
        <Stat k="Network" v={s.net_down_kbps !== undefined ? `↓ ${s.net_down_kbps} · ↑ ${s.net_up_kbps} KB/s` : "—"} />
      </section>
    </div>
  );
}
