import { useEffect, useRef } from "react";
import type { Activity } from "./fx/activity";
import { frameDue } from "./fx/pacer";

const COILS = 10;
const TICKS = 60;

// How lit the coils are, and how fast the chase light runs around them (deg/s), per activity.
const LIGHT: Record<Activity, number> = { idle: 0.28, typing: 0.4, thinking: 0.75, speaking: 0.6, error: 0.12, offline: 0.05 };
const CHASE: Record<Activity, number> = { idle: 18, typing: 30, thinking: 220, speaking: 90, error: 6, offline: 0 };

/** Equilateral triangle centred on the reactor, pointing down like the Mark VI chest piece. */
function triangle(r: number): string {
  return [90, 210, 330]
    .map((deg) => {
      const a = (deg * Math.PI) / 180;
      return `${(200 + r * Math.cos(a)).toFixed(2)},${(200 + r * Math.sin(a)).toFixed(2)}`;
    })
    .join(" ");
}

/** A ring sector from r0 to r1 spanning `span` degrees, starting at 12 o'clock. */
function sector(r0: number, r1: number, span: number): string {
  const p = (r: number, deg: number) => {
    const a = ((deg - 90) * Math.PI) / 180;
    return `${(200 + r * Math.cos(a)).toFixed(2)} ${(200 + r * Math.sin(a)).toFixed(2)}`;
  };
  return `M ${p(r1, 0)} A ${r1} ${r1} 0 0 1 ${p(r1, span)} L ${p(r0, span)} A ${r0} ${r0} 0 0 0 ${p(r0, 0)} Z`;
}

/** The arc reactor. Every moving part is its own layer moved by CSS transform/opacity, so the glow
 *  filters are painted once. It breathes when idle, a light chases round the coils while JARVIS
 *  thinks, it surges with each streamed chunk, leans toward the cursor and fires a shockwave on send. */
export default function Reactor({ activity, label }: { activity: Activity; label: string }) {
  const wrap = useRef<HTMLDivElement>(null);
  const halo = useRef<SVGSVGElement>(null);
  const coilsLit = useRef<SVGSVGElement>(null);
  const chase = useRef<SVGSVGElement>(null);
  const ticks = useRef<SVGSVGElement>(null);
  const core = useRef<SVGSVGElement>(null);
  const waves = useRef<SVGGElement>(null);
  const activityRef = useRef(activity);
  activityRef.current = activity;

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let chaseAngle = 0;
    let tickAngle = 0;
    let chaseSpeed = CHASE.idle;
    let light = LIGHT.idle;
    let energy = 0;
    let tiltX = 0;
    let tiltY = 0;
    let targetX = 0;
    let targetY = 0;
    let last = performance.now();
    let raf = 0;
    const slot = { v: -1 };

    const frame = (now: number) => {
      if (!frameDue(now, slot)) {
        if (!document.hidden) raf = requestAnimationFrame(frame);
        return;
      }
      const dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      const act = activityRef.current;
      const k = Math.min(dt * 2.5, 1);
      chaseSpeed += (CHASE[act] - chaseSpeed) * k;
      light += (LIGHT[act] - light) * k;
      energy = Math.max(0, energy - dt * 1.4);
      chaseAngle = (chaseAngle + chaseSpeed * dt) % 360;
      tickAngle = (tickAngle + (act === "thinking" ? 14 : 3) * dt) % 360;

      const pulse = act === "thinking" ? Math.sin(now / 160) * 0.5 + 0.5 : Math.sin(now / 1100) * 0.5 + 0.5;
      if (chase.current) {
        chase.current.style.transform = `rotate(${chaseAngle.toFixed(2)}deg)`;
        chase.current.style.opacity = Math.min(1, light * 1.25 + energy * 0.4).toFixed(2);
      }
      if (ticks.current) ticks.current.style.transform = `rotate(${(-tickAngle).toFixed(2)}deg)`;
      if (coilsLit.current) coilsLit.current.style.opacity = Math.min(1, light * (0.75 + pulse * 0.35) + energy * 0.5).toFixed(2);
      if (core.current) core.current.style.transform = `scale(${(1 + pulse * (act === "thinking" ? 0.12 : 0.05) + energy * 0.35).toFixed(3)})`;
      if (halo.current) halo.current.style.opacity = Math.min(1, 0.45 + light * 0.5 + energy * 0.4).toFixed(2);

      tiltX += (targetX - tiltX) * Math.min(dt * 4, 1);
      tiltY += (targetY - tiltY) * Math.min(dt * 4, 1);
      if (wrap.current) {
        wrap.current.style.transform = `translate(-50%, -50%) perspective(900px) rotateX(${tiltX.toFixed(2)}deg) rotateY(${tiltY.toFixed(2)}deg)`;
      }
      if (!document.hidden) raf = requestAnimationFrame(frame);
    };

    const onMove = (e: PointerEvent) => {
      targetY = (e.clientX / window.innerWidth - 0.5) * 14;
      targetX = -(e.clientY / window.innerHeight - 0.5) * 14;
    };
    const onPulse = () => {
      energy = Math.min(1, energy + 0.3);
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

  const coil = (i: number, lit: boolean) => (
    <g key={i} transform={`rotate(${(i * 360) / COILS} 200 200)`}>
      <rect x="183" y="42" width="34" height="56" rx="5" className={lit ? "coil-lit" : "coil"} />
      {!lit &&
        [0, 1, 2, 3, 4, 5, 6].map((w) => <line key={w} x1="186" x2="214" y1={49 + w * 7} y2={49 + w * 7} className="winding" />)}
    </g>
  );

  return (
    <div ref={wrap} className={`reactor ${activity}`} role="img" aria-label={`JARVIS arc reactor: ${label}`}>
      <svg ref={halo} className="layer" viewBox="0 0 400 400" style={{ opacity: 0.6 }}>
        <defs>
          <filter id="glow" x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="3.5" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
          <filter id="bloom" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="9" />
          </filter>
          <radialGradient id="halo" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--core)" stopOpacity="0.38" />
            <stop offset="55%" stopColor="var(--core)" stopOpacity="0.08" />
            <stop offset="100%" stopColor="var(--core)" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="hot" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#ffffff" />
            <stop offset="45%" stopColor="#e6fbff" />
            <stop offset="75%" stopColor="var(--core)" />
            <stop offset="100%" stopColor="var(--core)" stopOpacity="0" />
          </radialGradient>
          <linearGradient id="metal" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#3b4b55" />
            <stop offset="50%" stopColor="#141f27" />
            <stop offset="100%" stopColor="#2c3a44" />
          </linearGradient>
          <linearGradient id="copper" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#3d2a1c" />
            <stop offset="50%" stopColor="#7a5233" />
            <stop offset="100%" stopColor="#3d2a1c" />
          </linearGradient>
          <radialGradient id="sweep" cx="50%" cy="0%" r="100%">
            <stop offset="0%" stopColor="#ffffff" stopOpacity="0.95" />
            <stop offset="100%" stopColor="var(--core)" stopOpacity="0" />
          </radialGradient>
        </defs>
        <circle cx="200" cy="200" r="198" fill="url(#halo)" />
      </svg>

      {/* housing: metal ring, notches, outlines */}
      <svg className="layer" viewBox="0 0 400 400">
        <circle cx="200" cy="200" r="182" fill="none" stroke="url(#metal)" strokeWidth="18" />
        <g filter="url(#glow)" fill="none" stroke="var(--core)">
          <circle cx="200" cy="200" r="193" strokeWidth="1.2" opacity="0.55" />
          <circle cx="200" cy="200" r="171" strokeWidth="1" opacity="0.4" />
        </g>
        {Array.from({ length: 12 }, (_, i) => (
          <circle key={i} cx="200" cy="18" r="2.6" fill="#8aa3b0" opacity="0.8" transform={`rotate(${i * 30 + 15} 200 200)`} />
        ))}
        <circle cx="200" cy="200" r="104" fill="#071118" stroke="#24333d" strokeWidth="2" />
      </svg>

      {/* rotating instrument ticks */}
      <svg ref={ticks} className="layer spin" viewBox="0 0 400 400">
        {Array.from({ length: TICKS }, (_, i) => (
          <line
            key={i}
            x1="200"
            x2="200"
            y1={i % 5 === 0 ? 2 : 4}
            y2="9"
            stroke="var(--core)"
            strokeWidth={i % 5 === 0 ? 1.6 : 0.8}
            opacity={i % 5 === 0 ? 0.8 : 0.4}
            transform={`rotate(${(i * 360) / TICKS} 200 200)`}
          />
        ))}
      </svg>

      {/* copper coils with windings */}
      <svg className="layer" viewBox="0 0 400 400">
        {Array.from({ length: COILS }, (_, i) => coil(i, false))}
      </svg>
      <svg ref={coilsLit} className="layer spin" viewBox="0 0 400 400" style={{ opacity: 0.3 }}>
        <g filter="url(#bloom)">{Array.from({ length: COILS }, (_, i) => coil(i, true))}</g>
      </svg>
      {/* the chase light that runs round the coils while JARVIS works */}
      <svg ref={chase} className="layer spin" viewBox="0 0 400 400" style={{ opacity: 0.3 }}>
        <path d={sector(104, 160, 48)} fill="url(#sweep)" filter="url(#bloom)" />
      </svg>

      {/* inner ring, plate and triangle */}
      <svg className="layer" viewBox="0 0 400 400">
        <g filter="url(#glow)" fill="none">
          <circle cx="200" cy="200" r="98" stroke="var(--core)" strokeWidth="5" />
          <circle cx="200" cy="200" r="90" stroke="#dff8ff" strokeWidth="1.2" opacity="0.8" />
          <polygon points={triangle(74)} stroke="var(--core)" strokeWidth="7" strokeLinejoin="round" fill="color-mix(in srgb, var(--core) 12%, transparent)" />
          <polygon points={triangle(60)} stroke="#e8fbff" strokeWidth="1.6" strokeLinejoin="round" opacity="0.85" />
        </g>
      </svg>
      <svg className="layer" viewBox="0 0 400 400">
        <g ref={waves} filter="url(#glow)" />
      </svg>
      <svg ref={core} className="layer spin" viewBox="0 0 400 400">
        <circle cx="200" cy="200" r="40" fill="url(#hot)" filter="url(#bloom)" opacity="0.9" />
        <circle cx="200" cy="200" r="22" fill="url(#hot)" />
      </svg>
    </div>
  );
}
