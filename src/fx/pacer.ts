// Shared frame pacing for the ambient background (particles, scan, core).
// The glass panels blur whatever is behind them, so every background frame costs a full
// re-blur. While nothing is happening the background drifts slowly and 30 fps is visually
// identical; any interaction switches back to full rate for a moment.

const IDLE_FRAME_MS = 1000 / 30;
const BOOST_MS = 1500;

let boostUntil = 0;
const boost = () => {
  boostUntil = performance.now() + BOOST_MS;
};
for (const ev of ["pointermove", "pointerdown", "keydown", "jarvis:send", "jarvis:pulse", "jarvis:levels"]) {
  window.addEventListener(ev, boost, { passive: true });
}

/** Whether a loop should draw on this animation frame. Every loop calling this in the same
 *  frame gets the same answer (they share rAF's timestamp), so they stay in step and the
 *  compositor only produces a frame when they all draw. */
export function frameDue(now: number, lastSlot: { v: number }): boolean {
  if (now < boostUntil) return true;
  const slot = Math.floor(now / IDLE_FRAME_MS);
  if (slot === lastSlot.v) return false;
  lastSlot.v = slot;
  return true;
}
