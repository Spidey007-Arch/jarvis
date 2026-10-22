/** What JARVIS is doing right now; drives the core and particle animations. */
export type Activity = "idle" | "typing" | "thinking" | "speaking" | "error" | "offline";

/** Fired on window when a message is sent (shockwave) and on each streamed chunk (pulse). */
export const emitSend = () => window.dispatchEvent(new Event("jarvis:send"));
export const emitPulse = () => window.dispatchEvent(new Event("jarvis:pulse"));
