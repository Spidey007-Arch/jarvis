import type { Connection } from "./shell";

export class ApiError extends Error {}

async function call<T>(conn: Connection, path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`http://127.0.0.1:${conn.port}${path}`, {
      ...init,
      headers: { Authorization: `Bearer ${conn.token}`, "Content-Type": "application/json", ...(init.headers ?? {}) },
    });
  } catch (e) {
    throw new ApiError(`Could not reach the backend: ${String(e)}`);
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof body.detail === "string" ? body.detail : body.message;
    throw new ApiError(detail ?? `Request failed (HTTP ${res.status}).`);
  }
  return body as T;
}

// ---------- settings ----------

export interface Settings {
  user_name: string;
  address: string;
  proactive_alerts: boolean;
  provider: string;
  base_url: string;
  chat_model: string;
  embed_model: string;
  has_api_key: boolean;
  api_key_hint: string;
  providers_with_keys: string[];
  load_error: string | null;
}

export interface ProviderPreset {
  id: string;
  name: string;
  needs_key: boolean;
  base_url: string;
  key_hint: string;
  embeddings: boolean;
  featured?: boolean;
  badge?: string;
}

export interface ModelInfo {
  name: string;
  display_name: string;
  kind: "chat" | "embed";
}

export type SettingsUpdate = Partial<Pick<Settings, "user_name" | "address" | "proactive_alerts" | "provider" | "base_url" | "chat_model" | "embed_model">> & {
  api_key?: string;
};

export const getSettings = (c: Connection) => call<Settings>(c, "/api/settings");
export const getProviders = (c: Connection) => call<ProviderPreset[]>(c, "/api/providers");
export const putSettings = (c: Connection, body: SettingsUpdate) =>
  call<Settings>(c, "/api/settings", { method: "PUT", body: JSON.stringify(body) });
export interface Alert {
  at: number;
  kind: string;
  message: string;
}

export const getAlerts = (c: Connection, since: number) => call<Alert[]>(c, `/api/alerts?since=${since}`);

export const getModels = (c: Connection) => call<{ chat: ModelInfo[]; embed: ModelInfo[] }>(c, "/api/models");

// ---------- conversations ----------

export interface Conversation {
  id: string;
  title: string;
  updated_at: number;
  pinned: number;
}

export type ActionStatus = "running" | "awaiting_confirmation" | "done" | "failed" | "declined";

/** One thing JARVIS did (or asked to do) on the PC while answering. */
export interface ActionEvent {
  id: string;
  tool: string;
  label: string;
  status: ActionStatus;
  error?: string;
  detail?: string;
}

export interface Message {
  id: string;
  role: "user" | "model";
  content: string;
  actions?: ActionEvent[];
}

export const listConversations = (c: Connection, q?: string) =>
  call<Conversation[]>(c, `/api/conversations${q ? `?q=${encodeURIComponent(q)}` : ""}`);
export const getMessages = async (c: Connection, id: string): Promise<Message[]> => {
  const rows = await call<(Omit<Message, "actions"> & { actions: string | null })[]>(c, `/api/conversations/${id}/messages`);
  return rows.map((r) => ({ ...r, actions: r.actions ? (JSON.parse(r.actions) as ActionEvent[]) : undefined }));
};
export const answerConfirmation = (c: Connection, id: string, approve: boolean) =>
  call<{ ok: boolean }>(c, `/api/confirm/${id}`, { method: "POST", body: JSON.stringify({ approve }) });
export const patchConversation = (c: Connection, id: string, body: { title?: string; pinned?: boolean }) =>
  call<Conversation>(c, `/api/conversations/${id}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteConversation = (c: Connection, id: string) =>
  call<{ ok: boolean }>(c, `/api/conversations/${id}`, { method: "DELETE" });

export interface ChatHandlers {
  meta?: (d: { conversation_id: string; user_message_id: string; reply_id: string }) => void;
  memory?: (d: { core: number; relevant: number }) => void;
  notice?: (d: { message: string }) => void;
  delta?: (d: { text: string }) => void;
  done?: (d: { reply_id: string }) => void;
  error?: (d: { message: string }) => void;
  tool?: (d: ActionEvent) => void;
}

/** Streams a reply over server-sent events. Abort the signal to stop generation. */
export async function streamChat(
  c: Connection,
  body: { message: string; conversation_id?: string | null },
  signal: AbortSignal,
  on: ChatHandlers,
): Promise<void> {
  const res = await fetch(`http://127.0.0.1:${c.port}/api/chat`, {
    method: "POST",
    headers: { Authorization: `Bearer ${c.token}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    const j = await res.json().catch(() => ({}));
    throw new ApiError(j.message ?? (typeof j.detail === "string" ? j.detail : `Chat failed (HTTP ${res.status}).`));
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let i: number;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, i);
      buf = buf.slice(i + 2);
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      const handler = (on as Record<string, ((d: unknown) => void) | undefined>)[event];
      if (handler && data) handler(JSON.parse(data));
    }
  }
}

// ---------- memory ----------

export const MEMORY_TYPES = ["identity", "preference", "goal", "project", "relationship", "event", "behavior"] as const;
export type MemoryType = (typeof MEMORY_TYPES)[number];

export interface Memory {
  id: string;
  type: MemoryType;
  subject: string;
  content: string;
  importance: number;
  source: string | null;
  created_at: number;
  updated_at: number;
  access_count: number;
}

export interface MemoryStats {
  total: number;
  by_type: Record<MemoryType, number>;
  last_updated: number | null;
}

export const listMemories = (c: Connection, type?: string, q?: string) => {
  const p = new URLSearchParams();
  if (type) p.set("type", type);
  if (q) p.set("q", q);
  return call<Memory[]>(c, `/api/memories?${p}`);
};
export const memoryStats = (c: Connection) => call<MemoryStats>(c, "/api/memories/stats");
export const patchMemory = (c: Connection, id: string, body: Partial<Pick<Memory, "type" | "subject" | "content" | "importance">>) =>
  call<Memory>(c, `/api/memories/${id}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteMemory = (c: Connection, id: string) => call<{ ok: boolean }>(c, `/api/memories/${id}`, { method: "DELETE" });
export const exportMemoriesToFile = (c: Connection) =>
  call<{ path: string; count: number }>(c, "/api/memories/export-file", { method: "POST" });
export const importMemories = (c: Connection, memories: unknown[]) =>
  call<{ added: number; skipped: number }>(c, "/api/memories/import", { method: "POST", body: JSON.stringify({ memories }) });
