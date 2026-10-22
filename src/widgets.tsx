import type { ReactNode } from "react";

export function Stat({ k, v, tone, cap }: { k: string; v: ReactNode; tone?: "ok" | "warn" | "bad"; cap?: boolean }) {
  return (
    <div className="stat">
      <span className="k">{k}</span>
      <span className={`v ${tone ?? ""} ${cap ? "cap" : ""}`}>{v}</span>
    </div>
  );
}

export function Spark({ data, max = 100, current, unit }: { data: number[]; max?: number; current?: number; unit: string }) {
  const w = 200;
  const h = 44;
  const pts = data.map((v, i) => {
    const x = data.length <= 1 ? w : (i / (data.length - 1)) * w;
    const y = h - Math.min(Math.max(v / max, 0), 1) * (h - 2) - 1;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return (
    <div className="spark">
      <div className="spark-now">
        {current === undefined ? "—" : current.toFixed(0)}
        <small>{unit}</small>
      </div>
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-hidden="true">
        {pts.length > 1 ? (
          <>
            <polyline points={`0,${h} ${pts.join(" ")} ${w},${h}`} className="spark-fill" />
            <polyline points={pts.join(" ")} className="spark-line" />
          </>
        ) : (
          <line x1="0" y1={h - 1} x2={w} y2={h - 1} className="spark-line" opacity="0.4" />
        )}
      </svg>
    </div>
  );
}

