import { useEffect, useRef } from "react";
import type { Activity } from "./fx/activity";
import { frameDue } from "./fx/pacer";

const COILS = 10;
const TICKS = 60;

// How lit the coils are, and how fast the chase light runs around them (deg/s), per activity.
const LIGHT: Record<Activity, number> = { idle: 0.28, typing: 0.4, listening: 0.95, thinking: 0.75, speaking: 0.6, error: 0.12, offline: 0.05 };
const CHASE: Record<Activity, number> = { idle: 18, typing: 30, listening: 60, thinking: 220, speaking: 90, error: 6, offline: 0 };
// How hard the instrument rings spin up, relative to idle.
const DRIVE: Record<Activity, number> = { idle: 1, typing: 1.4, listening: 2.2, thinking: 4.5, speaking: 2.8, error: 0.4, offline: 0 };

const rad = (deg: number) => ((deg - 90) * Math.PI) / 180;
const pt = (r: number, deg: number) => `${(200 + r * Math.cos(rad(deg))).toFixed(2)} ${(200 + r * Math.sin(rad(deg))).toFixed(2)}`;

/** Equilateral triangle centred on the reactor, pointing down like the Mark VI chest piece. */
function triangle(r: number): string {
  return [180, 300, 60].map((deg) => pt(r, deg).replace(" ", ",")).join(" ");
}

/** A ring sector from r0 to r1 spanning `span` degrees, starting at `from` (0 = 12 o'clock). */
function sector(r0: number, r1: number, span: number, from = 0): string {
  const large = span > 180 ? 1 : 0;
  return `M ${pt(r1, from)} A ${r1} ${r1} 0 ${large} 1 ${pt(r1, from + span)} L ${pt(r0, from + span)} A ${r0} ${r0} 0 ${large} 0 ${pt(r0, from)} Z`;
}

/** An open arc of radius r from `from` spanning `span` degrees. */
function arc(r: number, from: number, span: number): string {
  return `M ${pt(r, from)} A ${r} ${r} 0 ${span > 180 ? 1 : 0} 1 ${pt(r, from + span)}`;
}

const range = (n: number) => Array.from({ length: n }, (_, i) => i);
const BARS = 64;

/** The arc reactor. Every moving part is its own layer moved by CSS transform/opacity, so the glow
 *  filters are painted once and the compositor does the rest. Outer instrument rings turn at their
 *  own speeds and spin up with activity, a light chases round the coils while JARVIS thinks, the
 *  lens turns in the hub, the core surges with each streamed chunk and fires a shockwave on send.
 *  While anyone talks, the real loudness of the voice ("jarvis:levels", 50 ms apart) drives the core,
 *  a ripple ring and a ring of sound bars; while JARVIS thinks, comets orbit the hub and a scanner sweeps. */
export default function Reactor({ activity, label }: { activity: Activity; label: string }) {
  const halo = useRef<SVGSVGElement>(null);
  const coilsLit = useRef<SVGSVGElement>(null);
  const chase = useRef<SVGSVGElement>(null);
  const ticks = useRef<SVGSVGElement>(null);
  const ringA = useRef<SVGSVGElement>(null);
  const ringB = useRef<SVGSVGElement>(null);
  const ringC = useRef<SVGSVGElement>(null);
  const inner = useRef<SVGSVGElement>(null);
  const lens = useRef<SVGSVGElement>(null);
  const core = useRef<SVGSVGElement>(null);
  const waves = useRef<SVGGElement>(null);
  const bars = useRef<SVGSVGElement>(null);
  const ripple = useRef<SVGSVGElement>(null);
  const think = useRef<SVGSVGElement>(null);
  const scan = useRef<SVGSVGElement>(null);
  const activityRef = useRef(activity);
  activityRef.current = activity;

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const angle = { chase: 0, tick: 0, a: 0, b: 0, c: 0, inner: 0, lens: 0, think: 0, scan: 0 };
    // voice levels waiting to be shown, each at its own moment, so the core moves in time with the voice
    const levelQueue: { at: number; v: number; who: string }[] = [];
    let voice = 0;
    let voiceTarget = 0;
    let voiceWho = "jarvis";
    let lastLevelAt = 0;
    let thinking = 0;
    const barEls = () => (bars.current ? Array.from(bars.current.querySelectorAll<SVGRectElement>("rect")) : []);
    let barCache: SVGRectElement[] = [];
    let chaseSpeed = CHASE.idle;
    let light = LIGHT.idle;
    let drive = DRIVE.idle;
    let energy = 0;
    let last = performance.now();
    let raf = 0;
    const slot = { v: -1 };
    const spin = (el: SVGSVGElement | null, deg: number) => {
      if (el) el.style.transform = `rotate(${deg.toFixed(2)}deg)`;
    };

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
      drive += (DRIVE[act] - drive) * k;
      energy = Math.max(0, energy - dt * 1.4);
      const d = drive + energy * 3;
      angle.chase = (angle.chase + chaseSpeed * dt) % 360;
      angle.tick = (angle.tick + 3 * d * dt) % 360;
      angle.a = (angle.a + 4 * d * dt) % 360;
      angle.b = (angle.b - 7 * d * dt) % 360;
      angle.c = (angle.c + 11 * d * dt) % 360;
      angle.inner = (angle.inner - 9 * d * dt) % 360;
      angle.lens = (angle.lens + 16 * d * dt) % 360;

      while (levelQueue.length && levelQueue[0].at <= now) {
        const l = levelQueue.shift()!;
        voiceTarget = l.v;
        voiceWho = l.who;
        lastLevelAt = now;
      }
      if (now - lastLevelAt > 160) voiceTarget = 0;  // the voice stopped
      voice += (voiceTarget - voice) * Math.min(dt * (voiceTarget > voice ? 22 : 9), 1);
      thinking += ((act === "thinking" ? 1 : 0) - thinking) * Math.min(dt * 3, 1);
      angle.think = (angle.think + 150 * dt) % 360;
      angle.scan = (angle.scan - 70 * dt) % 360;

      const pulse = act === "thinking" ? Math.sin(now / 160) * 0.5 + 0.5 : Math.sin(now / 1100) * 0.5 + 0.5;
      const flicker = 0.94 + Math.sin(now / 47) * 0.03 + Math.sin(now / 131) * 0.03; // plasma, never still
      if (chase.current) {
        chase.current.style.transform = `rotate(${angle.chase.toFixed(2)}deg)`;
        chase.current.style.opacity = Math.min(1, light * 1.25 + energy * 0.4).toFixed(2);
      }
      spin(ticks.current, -angle.tick);
      spin(ringA.current, angle.a);
      spin(ringB.current, angle.b);
      spin(ringC.current, angle.c);
      spin(inner.current, angle.inner);
      spin(lens.current, angle.lens);
      if (coilsLit.current) coilsLit.current.style.opacity = Math.min(1, light * (0.75 + pulse * 0.35) + energy * 0.5).toFixed(2);
      if (core.current) {
        core.current.style.transform = `scale(${(1 + pulse * (act === "thinking" ? 0.12 : 0.05) + energy * 0.35 + voice * 0.32).toFixed(3)})`;
        core.current.style.opacity = (act === "offline" ? 0.35 : flicker).toFixed(3);
      }
      if (halo.current) halo.current.style.opacity = Math.min(1, 0.45 + light * 0.5 + energy * 0.4 + voice * 0.5).toFixed(2);
      if (ripple.current) {
        ripple.current.style.opacity = Math.min(1, voice * 1.6).toFixed(2);
        ripple.current.style.transform = `scale(${(1 + voice * 0.14).toFixed(3)})`;
        ripple.current.classList.toggle("user", voiceWho === "user");
      }
      if (bars.current) {
        bars.current.style.opacity = Math.min(1, voice * 2.2).toFixed(2);
        bars.current.classList.toggle("user", voiceWho === "user");
        if (voice > 0.01) {
          if (barCache.length !== BARS) barCache = barEls();
          const t = now / 1000;
          for (let i = 0; i < barCache.length; i++) {
            // a lively spectrum from one loudness value: each bar wobbles at its own rate
            const shape = 0.3 + 0.7 * Math.abs(Math.sin(i * 0.83 + t * (5 + (i % 7))) * Math.cos(i * 0.31 - t * 3.1));
            barCache[i].style.transform = `scaleY(${(0.08 + voice * shape * 1.1).toFixed(3)})`;
          }
        }
      }
      if (think.current) {
        think.current.style.opacity = thinking.toFixed(2);
        think.current.style.transform = `rotate(${angle.think.toFixed(2)}deg)`;
      }
      if (scan.current) {
        scan.current.style.opacity = (thinking * 0.9).toFixed(2);
        scan.current.style.transform = `rotate(${angle.scan.toFixed(2)}deg)`;
      }
      if (!document.hidden) raf = requestAnimationFrame(frame);
    };

    const onPulse = () => {
      energy = Math.min(1, energy + 0.3);
    };
    const onLevels = (e: Event) => {
      const { who, v } = (e as CustomEvent<{ who: string; v: number[] }>).detail;
      // a batch covers the last 250 ms: play it back over the next 250 ms
      const start = Math.max(performance.now(), levelQueue.length ? levelQueue[levelQueue.length - 1].at + 50 : 0);
      v.forEach((x, i) => levelQueue.push({ at: start + i * 50, v: x, who }));
      if (levelQueue.length > 40) levelQueue.splice(0, levelQueue.length - 40);  // never fall behind the voice
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
    window.addEventListener("jarvis:pulse", onPulse);
    window.addEventListener("jarvis:levels", onLevels);
    window.addEventListener("jarvis:send", onSend);
    document.addEventListener("visibilitychange", onVis);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("jarvis:pulse", onPulse);
      window.removeEventListener("jarvis:levels", onLevels);
      window.removeEventListener("jarvis:send", onSend);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  const coil = (i: number, lit: boolean) => (
    <g key={i} transform={`rotate(${(i * 360) / COILS} 200 200)`}>
      <rect x="183" y="42" width="34" height="56" rx="5" className={lit ? "coil-lit" : "coil"} />
      {!lit && (
        <>
          {range(13).map((w) => <line key={w} x1="185.5" x2="214.5" y1={46 + w * 4} y2={46 + w * 4} className="winding" />)}
          <rect x="180" y="38" width="40" height="7" rx="2" className="coil-cap" />
          <rect x="180" y="95" width="40" height="7" rx="2" className="coil-cap" />
          <circle cx="185" cy="41.5" r="1.4" className="rivet" />
          <circle cx="215" cy="41.5" r="1.4" className="rivet" />
          <circle cx="185" cy="98.5" r="1.4" className="rivet" />
          <circle cx="215" cy="98.5" r="1.4" className="rivet" />
          <line x1="200" x2="200" y1="102" y2="110" className="lead" />
        </>
      )}
    </g>
  );

  return (
    <div className={`reactor ${activity}`} role="img" aria-label={`JARVIS arc reactor: ${label}`}>
      <svg ref={halo} className="layer" viewBox="0 0 400 400" style={{ opacity: 0.6 }}>
        <defs>
          <filter id="glow" x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="3.5" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
          <filter id="softglow" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="1.6" result="b" />
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
          <radialGradient id="glass" cx="38%" cy="32%" r="70%">
            <stop offset="0%" stopColor="#ffffff" stopOpacity="0.55" />
            <stop offset="30%" stopColor="#ffffff" stopOpacity="0.08" />
            <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
          </radialGradient>
          <linearGradient id="metal" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#4a5d69" />
            <stop offset="30%" stopColor="#1a2731" />
            <stop offset="55%" stopColor="#33444f" />
            <stop offset="100%" stopColor="#141f27" />
          </linearGradient>
          <linearGradient id="metal2" x1="1" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#2c3b45" />
            <stop offset="50%" stopColor="#0d161c" />
            <stop offset="100%" stopColor="#26343e" />
          </linearGradient>
          <linearGradient id="copper" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#3d2a1c" />
            <stop offset="35%" stopColor="#8a5d3a" />
            <stop offset="55%" stopColor="#b07a4d" />
            <stop offset="100%" stopColor="#3d2a1c" />
          </linearGradient>
          <radialGradient id="sweep" cx="50%" cy="0%" r="100%">
            <stop offset="0%" stopColor="#ffffff" stopOpacity="0.95" />
            <stop offset="100%" stopColor="var(--core)" stopOpacity="0" />
          </radialGradient>
          <pattern id="hex" width="8" height="13.856" patternUnits="userSpaceOnUse" patternTransform="translate(200 200)">
            <path d="M4 0 L8 2.309 L8 6.928 L4 9.237 L0 6.928 L0 2.309 Z M4 9.237 V13.856" fill="none" stroke="var(--core)" strokeWidth="0.45" />
          </pattern>
          <clipPath id="hub">
            <circle cx="200" cy="200" r="76" />
          </clipPath>
        </defs>
        <circle cx="200" cy="200" r="198" fill="url(#halo)" />
      </svg>

      {/* sound bars: the voice's loudness, all round the reactor */}
      <svg ref={bars} className="layer bars" viewBox="0 0 400 400" style={{ opacity: 0 }}>
        {range(BARS).map((i) => (
          <g key={i} transform={`rotate(${(i * 360) / BARS} 200 200)`}>
            <rect x="198.6" y="-42" width="2.8" height="26" rx="1.4" />
          </g>
        ))}
      </svg>
      <svg ref={ripple} className="layer spin ripple" viewBox="0 0 400 400" style={{ opacity: 0 }}>
        <circle cx="200" cy="200" r="203" fill="none" strokeWidth="2.5" filter="url(#softglow)" />
        <circle cx="200" cy="200" r="108" fill="none" strokeWidth="1.5" opacity="0.8" />
      </svg>

      {/* outermost HUD: segmented arcs and a dotted track, turning slowly */}
      <svg ref={ringA} className="layer spin" viewBox="0 0 400 400">
        <g fill="none" stroke="var(--core)" filter="url(#softglow)">
          {[0, 95, 140, 230, 300].map((from, i) => (
            <path key={i} d={arc(214, from, [70, 30, 75, 50, 40][i])} strokeWidth={i % 2 ? 1 : 2.2} opacity={i % 2 ? 0.45 : 0.7} />
          ))}
          <circle cx="200" cy="200" r="220" strokeWidth="1" strokeDasharray="1 6" opacity="0.45" />
        </g>
        {[20, 140, 260].map((deg) => (
          <path key={deg} d={`M ${pt(225, deg)} L ${pt(231, deg - 2)} L ${pt(231, deg + 2)} Z`} fill="var(--core)" opacity="0.75" />
        ))}
      </svg>

      {/* counter-rotating scale ring with numbered graduations */}
      <svg ref={ringB} className="layer spin" viewBox="0 0 400 400">
        <g stroke="var(--core)">
          {range(72).map((i) => (
            <line key={i} x1="200" x2="200" y1={i % 6 === 0 ? -4 : -1} y2="3" strokeWidth={i % 6 === 0 ? 1.4 : 0.6} opacity={i % 6 === 0 ? 0.75 : 0.35}
                  transform={`rotate(${i * 5} 200 200)`} />
          ))}
        </g>
        <g fill="var(--core)" opacity="0.55" fontSize="6" fontFamily="ui-monospace, Consolas, monospace" textAnchor="middle">
          {range(12).map((i) => (
            <text key={i} x="200" y="-8" transform={`rotate(${i * 30} 200 200)`}>{String(i * 30).padStart(3, "0")}</text>
          ))}
        </g>
        <path d={arc(206, 200, 110)} fill="none" stroke="var(--core)" strokeWidth="3" opacity="0.35" strokeLinecap="round" />
      </svg>

      {/* housing: bevelled metal ring, graduations, notches and screws */}
      <svg className="layer" viewBox="0 0 400 400">
        <circle cx="200" cy="200" r="182" fill="none" stroke="url(#metal)" strokeWidth="20" />
        <circle cx="200" cy="200" r="191.5" fill="none" stroke="#6d8592" strokeWidth="0.8" opacity="0.6" />
        <circle cx="200" cy="200" r="172.5" fill="none" stroke="#000" strokeWidth="1.2" opacity="0.7" />
        <g stroke="#93abb8" opacity="0.28">
          {range(120).map((i) => (
            <line key={i} x1="200" x2="200" y1="20" y2={i % 10 === 0 ? 27 : 23} strokeWidth="0.6" transform={`rotate(${i * 3} 200 200)`} />
          ))}
        </g>
        {range(24).map((i) => (
          <rect key={i} x="198" y="29" width="4" height="8" rx="1" fill="#05090c" stroke="#3a4b56" strokeWidth="0.5" transform={`rotate(${i * 15 + 7.5} 200 200)`} />
        ))}
        <g filter="url(#glow)" fill="none" stroke="var(--core)">
          <circle cx="200" cy="200" r="195" strokeWidth="1.2" opacity="0.55" />
          <circle cx="200" cy="200" r="169" strokeWidth="1" opacity="0.4" />
        </g>
        {range(12).map((i) => (
          <g key={i} transform={`rotate(${i * 30 + 15} 200 200)`}>
            <circle cx="200" cy="15.5" r="3.6" fill="url(#metal2)" stroke="#8aa3b0" strokeWidth="0.6" />
            <path d="M197.6 15.5 H202.4 M200 13.1 V17.9" stroke="#0b1217" strokeWidth="0.9" transform={`rotate(${i * 17} 200 15.5)`} />
          </g>
        ))}
        {/* coil bed */}
        <circle cx="200" cy="200" r="166" fill="none" stroke="#2a3944" strokeWidth="1.5" />
        <circle cx="200" cy="200" r="135" fill="none" stroke="#0a141a" strokeWidth="62" opacity="0.8" />
        <g stroke="#1e2c35" strokeWidth="0.8">
          {range(40).map((i) => (
            <line key={i} x1="200" x2="200" y1="36" y2="96" transform={`rotate(${i * 9 + 4.5} 200 200)`} />
          ))}
        </g>
        {range(COILS).map((i) => (
          <g key={i} transform={`rotate(${(i * 360) / COILS + 18} 200 200)`}>
            <path d="M193 40 L207 40 L204 100 L196 100 Z" fill="url(#metal2)" stroke="#3c4e59" strokeWidth="0.6" />
            <circle cx="200" cy="50" r="2" fill="#0c151b" stroke="#6f8794" strokeWidth="0.6" />
            <circle cx="200" cy="90" r="1.6" fill="#0c151b" stroke="#6f8794" strokeWidth="0.6" />
            <line x1="200" x2="200" y1="56" y2="84" stroke="var(--core)" strokeWidth="0.8" opacity="0.5" />
          </g>
        ))}
        <circle cx="200" cy="200" r="104" fill="#071118" stroke="#24333d" strokeWidth="2" />
      </svg>

      {/* rotating instrument ticks */}
      <svg ref={ticks} className="layer spin" viewBox="0 0 400 400">
        {range(TICKS).map((i) => (
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

      {/* copper coils with windings, caps and leads */}
      <svg className="layer" viewBox="0 0 400 400">
        {range(COILS).map((i) => coil(i, false))}
      </svg>
      <svg ref={coilsLit} className="layer spin" viewBox="0 0 400 400" style={{ opacity: 0.3 }}>
        <g filter="url(#bloom)">{range(COILS).map((i) => coil(i, true))}</g>
      </svg>
      {/* the chase light that runs round the coils while JARVIS works */}
      <svg ref={chase} className="layer spin" viewBox="0 0 400 400" style={{ opacity: 0.3 }}>
        <path d={sector(104, 160, 48)} fill="url(#sweep)" filter="url(#bloom)" />
      </svg>

      {/* data ring just inside the coils: broken arcs that race while JARVIS thinks */}
      <svg ref={ringC} className="layer spin" viewBox="0 0 400 400">
        <g fill="none" stroke="var(--core)" filter="url(#softglow)" strokeLinecap="round">
          {[0, 60, 105, 180, 250, 290].map((from, i) => (
            <path key={i} d={arc(110, from, [40, 25, 55, 45, 20, 50][i])} strokeWidth={i % 3 === 0 ? 2 : 1} opacity={i % 3 === 0 ? 0.85 : 0.5} />
          ))}
        </g>
      </svg>

      {/* hub: bright ring, radiating fins, honeycomb field and the triangle */}
      <svg className="layer" viewBox="0 0 400 400">
        <circle cx="200" cy="200" r="76" fill="url(#hex)" clipPath="url(#hub)" opacity="0.22" />
        <g stroke="var(--core)" opacity="0.55">
          {range(48).map((i) => (
            <line key={i} x1="200" x2="200" y1="106" y2={i % 4 === 0 ? 120 : 114} strokeWidth={i % 4 === 0 ? 1.4 : 0.7} transform={`rotate(${i * 7.5} 200 200)`} />
          ))}
        </g>
        <g filter="url(#glow)" fill="none">
          <circle cx="200" cy="200" r="98" stroke="var(--core)" strokeWidth="5" />
          <circle cx="200" cy="200" r="90" stroke="#dff8ff" strokeWidth="1.2" opacity="0.8" />
          <circle cx="200" cy="200" r="78" stroke="var(--core)" strokeWidth="0.8" opacity="0.6" />
          <polygon points={triangle(74)} stroke="var(--core)" strokeWidth="7" strokeLinejoin="round" fill="color-mix(in srgb, var(--core) 12%, transparent)" />
          <polygon points={triangle(60)} stroke="#e8fbff" strokeWidth="1.6" strokeLinejoin="round" opacity="0.85" />
          <polygon points={triangle(48)} stroke="var(--core)" strokeWidth="0.8" strokeLinejoin="round" opacity="0.6" strokeDasharray="3 3" />
        </g>
        {[180, 300, 60].map((deg) => {
          const [x, y] = pt(74, deg).split(" ");
          return (
            <g key={deg}>
              <circle cx={x} cy={y} r="5.5" fill="#06121a" stroke="var(--core)" strokeWidth="1.4" />
              <circle cx={x} cy={y} r="2" fill="#e8fbff" />
            </g>
          );
        })}
      </svg>

      {/* counter-rotating dashed ring inside the hub */}
      <svg ref={inner} className="layer spin" viewBox="0 0 400 400">
        <circle cx="200" cy="200" r="84" fill="none" stroke="#dff8ff" strokeWidth="1.6" strokeDasharray="10 4 2 4" opacity="0.55" />
        {[0, 120, 240].map((deg) => (
          <path key={deg} d={sector(80, 88, 18, deg)} fill="var(--core)" opacity="0.6" />
        ))}
      </svg>

      <svg className="layer" viewBox="0 0 400 400">
        <g ref={waves} filter="url(#glow)" />
      </svg>

      {/* thinking: three comets orbit the hub, and a scanner sweeps the coil bed */}
      <svg ref={scan} className="layer spin" viewBox="0 0 400 400" style={{ opacity: 0 }}>
        <path d={sector(104, 166, 26)} fill="url(#sweep)" opacity="0.55" />
        <path d={arc(170, 0, 26)} fill="none" stroke="#e8fbff" strokeWidth="1.6" strokeLinecap="round" />
      </svg>
      <svg ref={think} className="layer spin" viewBox="0 0 400 400" style={{ opacity: 0 }}>
        <g fill="none" strokeLinecap="round" filter="url(#softglow)">
          {[0, 120, 240].map((deg, i) => (
            <g key={deg} transform={`rotate(${deg} 200 200)`}>
              <path d={arc(64 + i * 7, 0, 70)} stroke="var(--core)" strokeWidth="3" opacity="0.25" />
              <path d={arc(64 + i * 7, 40, 30)} stroke="var(--core)" strokeWidth="3" opacity="0.6" />
              <path d={arc(64 + i * 7, 62, 8)} stroke="#ffffff" strokeWidth="3.4" />
            </g>
          ))}
        </g>
      </svg>

      {/* the lens over the core: spokes and an aperture ring that turn */}
      <svg ref={lens} className="layer spin" viewBox="0 0 400 400">
        <g stroke="#e8fbff" fill="none" opacity="0.75">
          <circle cx="200" cy="200" r="32" strokeWidth="1.1" />
          <circle cx="200" cy="200" r="36" strokeWidth="0.6" strokeDasharray="2 3" />
          {range(8).map((i) => (
            <line key={i} x1="200" x2="200" y1="168" y2="178" strokeWidth="1" transform={`rotate(${i * 45} 200 200)`} />
          ))}
        </g>
        {range(6).map((i) => (
          <path key={i} d={sector(26, 31, 34, i * 60 + 8)} fill="var(--core)" opacity="0.45" />
        ))}
      </svg>
      <svg ref={core} className="layer spin" viewBox="0 0 400 400">
        <circle cx="200" cy="200" r="40" fill="url(#hot)" filter="url(#bloom)" opacity="0.9" />
        <circle cx="200" cy="200" r="22" fill="url(#hot)" />
        <circle cx="200" cy="200" r="9" fill="#ffffff" />
      </svg>
      {/* glass reflection on top of everything */}
      <svg className="layer" viewBox="0 0 400 400">
        <circle cx="200" cy="200" r="100" fill="url(#glass)" opacity="0.35" />
        <path d={arc(150, 300, 70)} fill="none" stroke="#ffffff" strokeWidth="2" opacity="0.12" strokeLinecap="round" />
      </svg>
    </div>
  );
}
