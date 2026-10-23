import { useEffect, useRef } from "react";
import type { Activity } from "./activity";
import { frameDue } from "./pacer";

interface P {
  x: number;
  y: number;
  vx: number;
  vy: number;
  r: number;
}

const LINK = 110;
const LINK_LEVELS = 10; // links are batched into this many opacity steps: one stroke per step

/** Full-window particle field. Particles drift, link up near each other and the cursor,
 *  scatter from clicks, and get pulled toward the core while JARVIS is working.
 *  Also draws the slow scan sweep, so the whole background is one paint. */
export default function Particles({ color, activity }: { color: string; activity: Activity }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const activityRef = useRef(activity);
  const colorRef = useRef(color);
  activityRef.current = activity;
  colorRef.current = color;

  useEffect(() => {
    const cv = canvas.current!;
    const ctx = cv.getContext("2d")!;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let w = 0;
    let h = 0;
    let dpr = 1;
    let parts: P[] = [];
    const mouse = { x: -9999, y: -9999 };
    let raf = 0;
    let last = performance.now();
    const slot = { v: -1 };
    const links: number[][] = Array.from({ length: LINK_LEVELS }, () => []);

    const seed = () => {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = cv.clientWidth;
      h = cv.clientHeight;
      cv.width = w * dpr;
      cv.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const n = Math.min(170, Math.round((w * h) / 8500));
      parts = Array.from({ length: n }, () => ({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.25,
        vy: (Math.random() - 0.5) * 0.25,
        r: Math.random() * 1.6 + 0.4,
      }));
    };

    const burst = (x: number, y: number) => {
      for (const p of parts) {
        const dx = p.x - x;
        const dy = p.y - y;
        const d = Math.hypot(dx, dy) || 1;
        if (d < 260) {
          const f = (1 - d / 260) * 6;
          p.vx += (dx / d) * f;
          p.vy += (dy / d) * f;
        }
      }
    };

    const frame = (now: number) => {
      if (!frameDue(now, slot)) {
        if (!reduced && !document.hidden) raf = requestAnimationFrame(frame);
        return;
      }
      const dt = Math.min((now - last) / 16.67, 3);
      last = now;
      const act = activityRef.current;
      const pull = act === "thinking" ? 0.012 : act === "speaking" ? 0.006 : 0;
      const cx = w / 2;
      const cy = h / 2;
      ctx.clearRect(0, 0, w, h);
      const col = colorRef.current;

      // scan sweep: a 260px band crossing the window every 9s
      const sy = -260 + ((now % 9000) / 9000) * (h + 520);
      const band = ctx.createLinearGradient(0, sy, 0, sy + 260);
      band.addColorStop(0, "transparent");
      band.addColorStop(0.5, col);
      band.addColorStop(1, "transparent");
      ctx.globalAlpha = 0.035;
      ctx.fillStyle = band;
      ctx.fillRect(0, sy, w, 260);

      for (const p of parts) {
        if (pull) {
          const dx = cx - p.x;
          const dy = cy - p.y;
          const d = Math.hypot(dx, dy) || 1;
          if (d > 120) {
            p.vx += (dx / d) * pull * dt;
            p.vy += (dy / d) * pull * dt;
          } else {
            // reached the core: re-emerge at the edge so the field never collapses
            const a = Math.random() * Math.PI * 2;
            p.x = cx + Math.cos(a) * Math.max(w, h) * 0.6;
            p.y = cy + Math.sin(a) * Math.max(w, h) * 0.6;
          }
        }
        const mdx = p.x - mouse.x;
        const mdy = p.y - mouse.y;
        const md = Math.hypot(mdx, mdy);
        if (md < 120 && md > 0) {
          p.vx += (mdx / md) * 0.05 * dt;
          p.vy += (mdy / md) * 0.05 * dt;
        }
        p.vx *= 0.985;
        p.vy *= 0.985;
        const speed = Math.hypot(p.vx, p.vy);
        if (speed < 0.08) {
          p.vx += (Math.random() - 0.5) * 0.04;
          p.vy += (Math.random() - 0.5) * 0.04;
        }
        p.x += p.vx * dt;
        p.y += p.vy * dt;
        if (p.x < -20) p.x = w + 20;
        if (p.x > w + 20) p.x = -20;
        if (p.y < -20) p.y = h + 20;
        if (p.y > h + 20) p.y = -20;
      }

      ctx.lineWidth = 0.6;
      ctx.strokeStyle = col;
      for (const l of links) l.length = 0;
      for (let i = 0; i < parts.length; i++) {
        const a = parts[i];
        for (let j = i + 1; j < parts.length; j++) {
          const b = parts[j];
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const d2 = dx * dx + dy * dy;
          if (d2 < LINK * LINK) {
            const k = Math.min(LINK_LEVELS - 1, Math.floor((1 - Math.sqrt(d2) / LINK) * LINK_LEVELS));
            links[k].push(a.x, a.y, b.x, b.y);
          }
        }
      }
      links.forEach((l, k) => {
        if (!l.length) return;
        ctx.globalAlpha = ((k + 0.5) / LINK_LEVELS) * 0.22;
        ctx.beginPath();
        for (let n = 0; n < l.length; n += 4) {
          ctx.moveTo(l[n], l[n + 1]);
          ctx.lineTo(l[n + 2], l[n + 3]);
        }
        ctx.stroke();
      });
      for (const a of parts) {
        const md = Math.hypot(a.x - mouse.x, a.y - mouse.y);
        if (md < 170) {
          ctx.globalAlpha = (1 - md / 170) * 0.5;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(mouse.x, mouse.y);
          ctx.stroke();
        }
      }
      ctx.fillStyle = col;
      ctx.globalAlpha = 0.75;
      ctx.beginPath();
      for (const p of parts) {
        ctx.moveTo(p.x + p.r, p.y);
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      }
      ctx.fill();
      ctx.globalAlpha = 1;
      if (!reduced && !document.hidden) raf = requestAnimationFrame(frame);
    };

    const onMove = (e: PointerEvent) => {
      mouse.x = e.clientX;
      mouse.y = e.clientY;
    };
    const onLeave = () => {
      mouse.x = mouse.y = -9999;
    };
    const onDown = (e: PointerEvent) => {
      if ((e.target as HTMLElement).closest("button, input, textarea, select, a, .glass, .msg")) return;
      burst(e.clientX, e.clientY);
    };
    const onSend = () => burst(w / 2, h / 2);
    const onVis = () => {
      if (!document.hidden && !reduced) {
        last = performance.now();
        raf = requestAnimationFrame(frame);
      }
    };

    seed();
    raf = requestAnimationFrame(frame);
    window.addEventListener("resize", seed);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerleave", onLeave);
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("jarvis:send", onSend);
    document.addEventListener("visibilitychange", onVis);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", seed);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerleave", onLeave);
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("jarvis:send", onSend);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  return <canvas ref={canvas} className="particles" aria-hidden="true" />;
}
