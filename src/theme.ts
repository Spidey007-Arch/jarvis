import { useSyncExternalStore } from "react";

/** The look of everything around the core (Settings > General > Appearance). Kept on this PC, applied before the first paint. */
export type Theme = "scifi" | "dark" | "light";
export const THEMES: { id: Theme; label: string; hint: string }[] = [
  { id: "scifi", label: "Sci-fi", hint: "Holographic glass and light" },
  { id: "dark", label: "Dark", hint: "Quiet graphite, no glow" },
  { id: "light", label: "Light", hint: "Clean paper and ink" },
];
const KEY = "jarvis.theme";
const listeners = new Set<() => void>();

function stored(): Theme {
  try {
    const t = localStorage.getItem(KEY);
    return THEMES.some((x) => x.id === t) ? (t as Theme) : "scifi";
  } catch {
    return "scifi";
  }
}

let current: Theme = stored();

/** Switch themes; colours cross-fade for a moment instead of jumping. */
export function setTheme(theme: Theme | undefined | null): void {
  const next = THEMES.some((x) => x.id === theme) ? (theme as Theme) : "scifi";
  const root = document.documentElement;
  if (next !== root.dataset.theme && root.dataset.theme) {
    root.classList.add("theming");
    window.setTimeout(() => root.classList.remove("theming"), 600);
  }
  root.dataset.theme = next;
  current = next;
  try {
    localStorage.setItem(KEY, next);
  } catch {
    /* remembered for this visit only */
  }
  listeners.forEach((l) => l());
}

export function initTheme(): void {
  document.documentElement.dataset.theme = current;
}

export function useTheme(): Theme {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => current,
  );
}
