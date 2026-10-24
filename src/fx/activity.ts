/** What JARVIS is doing right now; drives the arc reactor and the aurora. */
export type Activity = "idle" | "typing" | "listening" | "thinking" | "speaking" | "error" | "offline";

/** Fired on window when a message is sent (shockwave) and on each streamed chunk (pulse). */
export const emitSend = () => window.dispatchEvent(new Event("jarvis:send"));
export const emitPulse = () => window.dispatchEvent(new Event("jarvis:pulse"));
