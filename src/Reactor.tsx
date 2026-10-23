import { useEffect, useRef } from "react";
import type { Activity } from "./fx/activity";

// radius, stroke width, dash pattern, opacity, base speed (deg/s; negative = counter-clockwise)
const RINGS: [number, number, string, number, number][] = [
  [190, 6, "1.5 7", 0.55, 4],
  [164, 3, "70 18 6 18", 0.8, -9],
  [140, 16, "34 9", 0.35, 13],
  [108, 2, "3 5", 0.8, -4],
  [86, 12, "110 24", 0.65, 26],
];

const SPEED: Record<Activity, number> = { idle: 1, typing: 1.8, thinking: 5, speaking: 2.6, error: 0.25, offline: 0 };

/** Orb colour per state (RGB). */
export const ORB_COLOR: Record<Activity, [number, number, number]> = {
  idle: [41, 211, 255], // calm blue
  typing: [61, 252, 171], // listening green
  thinking: [178, 107, 255], // violet
  speaking: [255, 190, 70], // warm gold
  error: [255, 92, 92],
  offline: [255, 92, 92],
};

// How much the orb's surface ripples in each state.
const WOBBLE: Record<Activity, number> = { idle: 0.04, typing: 0.07, thinking: 0.1, speaking: 0.09, error: 0.02, offline: 0 };
const BLOB_POINTS = 64;

/** The core: rings plus a living orb. Colour, speed and surface motion follow what JARVIS is doing;
 *  it swells with each spoken word, leans toward the cursor, grows on hover and ripples when clicked. */
export default function Reactor({ activity, label, override }: { activity: Activity; label: string; override?: [number, number, number] }) {
  const wrap = useRef<HTMLDivElement>(null);
  const rings = useRef<(SVGGElement | null)[]>([]);
  const blob = useRef<SVGPathElement>(null);
  const nucleus = useRef<SVGCircleElement>(null);
  const halo = useRef<SVGCircleElement>(null);
  const waves = useRef<SVGGElement>(null);
  const state = useRef({ activity, override });
  state.current = { activity, override };

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const angles = RINGS.map(() => Math.random() * 360);
    const phases = [Math.random() * 6, Math.random() * 6, Math.random() * 6, Math.random() * 6];
    const color = [...ORB_COLOR.idle] as [number, number, number];
    let speed = 1;
    let wobble = WOBBLE.idle;
    let energy = 0;
    let hover = 0;
    let hoverTarget = 0;
    let tiltX = 0;
    let tiltY = 0;
    let targetX = 0;
    let targetY = 0;
    let last = performance.now();
    let raf = 0;

    const shockwave = (strength = 1) => {
      energy = Math.min(1, energy + 0.6 * strength);
      const g = waves.current;
      if (!g || reduced) return;
      const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      c.setAttribute("cx", "200");
      c.setAttribute("cy", "200");
      c.setAttribute("r", "40");
      c.setAttribute("class", "shockwave");
      g.appendChild(c);
      window.setTimeout(() => c.remove(), 1300);
    };

    const frame = (now: number) => {
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      const { activity: act, override: ov } = state.current;
      const k = Math.min(dt * 3, 1);

      // smooth colour transition between states
      const target = ov ?? ORB_COLOR[act];
      for (let i = 0; i < 3; i++) color[i] += (target[i] - color[i]) * k;
      const rgb = `rgb(${color.map((c) => Math.round(c)).join(",")})`;
      wrap.current?.style.setProperty("--core", rgb);

      speed += (SPEED[act] - speed) * Math.min(dt * 2.5, 1);
      wobble += (WOBBLE[act] - wobble) * k;
      energy = Math.max(0, energy - dt * 1.8);
      hover += (hoverTarget - hover) * Math.min(dt * 6, 1);

      RINGS.forEach((r, i) => {
        angles[i] = (angles[i] + r[4] * speed * dt) % 360;
        rings.current[i]?.setAttribute("transform", `rotate(${angles[i].toFixed(2)} 200 200)`);
      });

      // organic orb surface: a circle perturbed by a few travelling waves
      const t = now / 1000;
      const breathe = act === "thinking" ? Math.sin(t * 7) * 0.05 : Math.sin(t * 1.1) * 0.03;
      const base = 46 * (1 + breathe + energy * 0.28 + hover * 0.12);
      const amp = wobble + energy * 0.12 + hover * 0.03;
      const pts: string[] = [];
      for (let i = 0; i < BLOB_POINTS; i++) {
        const a = (i / BLOB_POINTS) * Math.PI * 2;
        const n =
          Math.sin(a * 3 + t * 1.7 * speed + phases[0]) * 0.5 +
          Math.sin(a * 5 - t * 2.3 + phases[1]) * 0.3 +
          Math.sin(a * 2 + t * 0.9 + phases[2]) * 0.2;
        const r = base * (1 + amp * n);
        pts.push(`${(200 + Math.cos(a) * r).toFixed(1)},${(200 + Math.sin(a) * r).toFixed(1)}`);
      }
      blob.current?.setAttribute("d", `M${pts.join("L")}Z`);
      nucleus.current?.setAttribute("r", (13 * (1 + energy * 0.5 + breathe)).toFixed(2));
      halo.current?.setAttribute("opacity", (0.5 + energy * 0.5 + hover * 0.2 + (act === "thinking" ? 0.2 : 0)).toFixed(2));

      tiltX += (targetX - tiltX) * Math.min(dt * 4, 1);
      tiltY += (targetY - tiltY) * Math.min(dt * 4, 1);
      if (wrap.current) {
        wrap.current.style.transform = `translate(-50%, -50%) perspective(900px) rotateX(${tiltX.toFixed(2)}deg) rotateY(${tiltY.toFixed(2)}deg)`;
      }
      if (!document.hidden) raf = requestAnimationFrame(frame);
    };

    const nearOrb = (x: number, y: number) => {
      const el = wrap.current;
      if (!el) return false;
      const b = el.getBoundingClientRect();
      const cx = b.left + b.width / 2;
      const cy = b.top + b.height / 2;
      return Math.hypot(x - cx, y - cy) < b.width * 0.18;
    };
    const onMove = (e: PointerEvent) => {
      targetY = (e.clientX / window.innerWidth - 0.5) * 18;
      targetX = -(e.clientY / window.innerHeight - 0.5) * 18;
      hoverTarget = nearOrb(e.clientX, e.clientY) ? 1 : 0;
    };
    const onDown = (e: PointerEvent) => {
      if ((e.target as HTMLElement).closest("button, input, textarea, select, a, .glass, .msg")) return;
      if (nearOrb(e.clientX, e.clientY)) shockwave(0.8);
    };
    const onPulse = () => {
      energy = Math.min(1, energy + 0.22);
    };
    const onSend = () => shockwave(1);
    const onVis = () => {
      if (!document.hidden) {
        last = performance.now();
        raf = requestAnimationFrame(frame);
      }
    };

    if (!reduced) raf = requestAnimationFrame(frame);
    else frame(performance.now());
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("jarvis:pulse", onPulse);
    window.addEventListener("jarvis:send", onSend);
    document.addEventListener("visibilitychange", onVis);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("jarvis:pulse", onPulse);
      window.removeEventListener("jarvis:send", onSend);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  return (
    <div ref={wrap} className={`reactor ${activity}`}>
      <svg viewBox="0 0 400 400" role="img" aria-label={`JARVIS core: ${label}, ${activity}`}>
        <defs>
          <filter id="glow" x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="3.5" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
          <filter id="orbGlow" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="9" />
          </filter>
          <radialGradient id="halo" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--core)" stopOpacity="0.34" />
            <stop offset="60%" stopColor="var(--core)" stopOpacity="0.07" />
            <stop offset="100%" stopColor="var(--core)" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="orbFill" cx="42%" cy="38%" r="65%">
            <stop offset="0%" stopColor="#ffffff" stopOpacity="0.95" />
            <stop offset="28%" stopColor="var(--core)" stopOpacity="0.95" />
            <stop offset="75%" stopColor="var(--core)" stopOpacity="0.45" />
            <stop offset="100%" stopColor="var(--core)" stopOpacity="0.15" />
          </radialGradient>
        </defs>
        <circle ref={halo} cx="200" cy="200" r="198" fill="url(#halo)" />
        <g filter="url(#glow)" fill="none" stroke="var(--core)">
          <circle cx="200" cy="200" r="176" strokeWidth="1" opacity="0.5" />
          <circle cx="200" cy="200" r="122" strokeWidth="1.5" opacity="0.6" />
          <circle cx="200" cy="200" r="66" strokeWidth="1.2" opacity="0.55" />
          {RINGS.map(([r, w, dash, op], i) => (
            <g key={r} ref={(el) => (rings.current[i] = el)}>
              <circle cx="200" cy="200" r={r} strokeWidth={w} strokeDasharray={dash} opacity={op} />
            </g>
          ))}
          <g ref={waves} />
        </g>
        {/* the orb: blurred glow layer, crisp surface, bright nucleus */}
        <use href="#orb-surface" filter="url(#orbGlow)" opacity="0.85" />
        <path id="orb-surface" ref={blob} fill="url(#orbFill)" stroke="var(--core)" strokeWidth="1.5" strokeOpacity="0.9" />
        <circle ref={nucleus} cx="200" cy="200" r="13" fill="#ffffff" opacity="0.9" />
      </svg>
    </div>
  );
}
