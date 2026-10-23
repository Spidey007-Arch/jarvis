import { useEffect, useRef } from "react";
import type { Activity } from "./fx/activity";

// radius, stroke width, dash pattern, opacity, base speed (deg/s; negative = counter-clockwise)
const RINGS: [number, number, string, number, number][] = [
  [190, 6, "1.5 7", 0.55, 4],
  [164, 3, "70 18 6 18", 0.8, -9],
  [140, 16, "34 9", 0.35, 13],
  [108, 2, "3 5", 0.8, -4],
  [86, 12, "110 24", 0.65, 26],
  [52, 5, "45 14", 0.9, -15],
];

const SPEED: Record<Activity, number> = { idle: 1, typing: 1.8, thinking: 5, speaking: 2.6, error: 0.25, offline: 0 };

/** The core. Rings are driven frame by frame so speed changes are smooth; it leans toward
 *  the cursor, swells with each streamed chunk and fires a shockwave when a message is sent. */
export default function Reactor({ activity, label }: { activity: Activity; label: string }) {
  const wrap = useRef<HTMLDivElement>(null);
  const rings = useRef<(SVGGElement | null)[]>([]);
  const core = useRef<SVGCircleElement>(null);
  const halo = useRef<SVGCircleElement>(null);
  const waves = useRef<SVGGElement>(null);
  const activityRef = useRef(activity);
  activityRef.current = activity;

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const angles = RINGS.map(() => Math.random() * 360);
    let speed = 1;
    let energy = 0;
    let tiltX = 0;
    let tiltY = 0;
    let targetX = 0;
    let targetY = 0;
    let last = performance.now();
    let raf = 0;

    const frame = (now: number) => {
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      const act = activityRef.current;
      speed += (SPEED[act] - speed) * Math.min(dt * 2.5, 1);
      energy = Math.max(0, energy - dt * 1.6);
      RINGS.forEach((r, i) => {
        angles[i] = (angles[i] + r[4] * speed * dt) % 360;
        rings.current[i]?.setAttribute("transform", `rotate(${angles[i].toFixed(2)} 200 200)`);
      });
      const breathe = act === "thinking" ? Math.sin(now / 140) * 0.18 : Math.sin(now / 900) * 0.08;
      const s = 1 + breathe + energy * 0.6;
      core.current?.setAttribute("r", (16 * s).toFixed(2));
      halo.current?.setAttribute("opacity", (0.55 + energy * 0.45 + (act === "thinking" ? 0.2 : 0)).toFixed(2));
      tiltX += (targetX - tiltX) * Math.min(dt * 4, 1);
      tiltY += (targetY - tiltY) * Math.min(dt * 4, 1);
      if (wrap.current) {
        wrap.current.style.transform = `translate(-50%, -50%) perspective(900px) rotateX(${tiltX.toFixed(2)}deg) rotateY(${tiltY.toFixed(2)}deg)`;
      }
      if (!document.hidden) raf = requestAnimationFrame(frame);
    };

    const onMove = (e: PointerEvent) => {
      targetY = ((e.clientX / window.innerWidth) - 0.5) * 18;
      targetX = -((e.clientY / window.innerHeight) - 0.5) * 18;
    };
    const onPulse = () => {
      energy = Math.min(1, energy + 0.35);
    };
    const onSend = () => {
      energy = 1;
      const g = waves.current;
      if (!g || reduced) return;
      const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      c.setAttribute("cx", "200");
      c.setAttribute("cy", "200");
      c.setAttribute("r", "30");
      c.setAttribute("class", "shockwave");
      g.appendChild(c);
      window.setTimeout(() => c.remove(), 1300);
    };
    const onVis = () => {
      if (!document.hidden) {
        last = performance.now();
        raf = requestAnimationFrame(frame);
      }
    };

    if (!reduced) raf = requestAnimationFrame(frame);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("jarvis:pulse", onPulse);
    window.addEventListener("jarvis:send", onSend);
    document.addEventListener("visibilitychange", onVis);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("jarvis:pulse", onPulse);
      window.removeEventListener("jarvis:send", onSend);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  return (
    <div ref={wrap} className={`reactor ${activity}`}>
      <svg viewBox="0 0 400 400" role="img" aria-label={`JARVIS core: ${label}`}>
        <defs>
          <filter id="glow" x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="3.5" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
          <radialGradient id="halo" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--core)" stopOpacity="0.32" />
            <stop offset="60%" stopColor="var(--core)" stopOpacity="0.07" />
            <stop offset="100%" stopColor="var(--core)" stopOpacity="0" />
          </radialGradient>
        </defs>
        <circle ref={halo} cx="200" cy="200" r="198" fill="url(#halo)" />
        <g filter="url(#glow)" fill="none" stroke="var(--core)">
          <circle cx="200" cy="200" r="176" strokeWidth="1" opacity="0.5" />
          <circle cx="200" cy="200" r="122" strokeWidth="1.5" opacity="0.6" />
          <circle cx="200" cy="200" r="66" strokeWidth="1.5" opacity="0.8" />
          <circle cx="200" cy="200" r="34" strokeWidth="2" opacity="0.9" />
          {RINGS.map(([r, w, dash, op], i) => (
            <g key={r} ref={(el) => (rings.current[i] = el)}>
              <circle cx="200" cy="200" r={r} strokeWidth={w} strokeDasharray={dash} opacity={op} />
            </g>
          ))}
          <g ref={waves} />
          <circle ref={core} className="core" cx="200" cy="200" r="16" fill="var(--core)" stroke="none" />
        </g>
      </svg>
    </div>
  );
}
