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
  startup_briefing: boolean;
  full_control: boolean;
  presence_greetings: boolean;
  onboarded: boolean;
  followups: boolean;
  permissions: Record<string, Policy>;
  provider: string;
  base_url: string;
  chat_model: string;
  embed_model: string;
  has_api_key: boolean;
  ready: boolean;
  voice_mode?: "live" | "local";
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

export type SettingsUpdate = Partial<Pick<Settings, "user_name" | "address" | "proactive_alerts" | "startup_briefing" | "full_control" | "permissions" | "presence_greetings" | "followups" | "onboarded" | "provider" | "base_url" | "chat_model" | "embed_model">> & {
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

export interface QuotaModel {
  model: string;
  used: number;
  limit: number | null; // learned from the provider's own quota errors; null until one says
  state: "ready" | "out";
  reason: string | null;
  until: number | null; // epoch seconds
  tokens_in?: number;
  tokens_out?: number;
  tokens_cached?: number; // read from the provider's cache: cheaper and faster
}
export interface Quota {
  provider: string;
  provider_name: string;
  day: string;
  resets_at: number;
  models: QuotaModel[];
  tokens?: { input: number; output: number; cached: number };
}
export const getQuota = (c: Connection) => call<Quota>(c, "/api/quota");

// ---------- voice ----------

export interface VoiceEvent {
  seq: number;
  at: number;
  event:
    | "listening" | "transcribing" | "transcript" | "speaking" | "idle" | "error"
    | "live_connecting" | "live_start" | "user_text" | "jarvis_text" | "live_tool" | "live_turn" | "live_end" | "briefing" | "levels" | "emergency_stop" | "saying";
  conversation_id?: string | null;
  tool?: string;
  model?: string;
  text?: string;
  message?: string;
  who?: "jarvis" | "user"; // levels: whose voice
  v?: number[]; // levels: loudness 0..1, one per 50 ms
  reason?: string;
  seconds?: number;
}
export interface VoiceStatus {
  enabled: boolean;
  ready: boolean;
  missing: string[];
  error?: string;
  asr?: string; // offline speech recognizer in use, e.g. "NVIDIA sherpa-onnx-nemo-parakeet..." or "Whisper base"
  speaker_model?: boolean; // the speaker-recognition model is downloaded
  voiceprint?: boolean; // the user's voice is enrolled: only it wakes JARVIS
}
export interface Enrollment {
  enrolled: boolean;
  pieces?: number;
  consistency?: number;
  threshold?: number;
}
export const getVoiceEvents = (c: Connection, after: number, signal?: AbortSignal) =>
  call<VoiceEvent[]>(c, `/api/voice/events?after=${after}`, { signal });
export const setVoiceMode = (c: Connection, mode: "live" | "local") =>
  call<{ mode: string }>(c, "/api/voice/mode", { method: "POST", body: JSON.stringify({ mode }) });
export const getVoiceSeq = (c: Connection) => call<{ seq: number }>(c, "/api/voice/seq");
export const listenNow = (c: Connection) => call<{ listening: boolean }>(c, "/api/voice/listen", { method: "POST", body: "{}" });
/** The spoken start-up briefing; the backend gives it once per launch. */
export const startBriefing = (c: Connection) => call<{ started: boolean; mode?: string; text?: string }>(c, "/api/briefing", { method: "POST", body: "{}" });
export const getVoice = (c: Connection) => call<VoiceStatus>(c, "/api/voice");
export const setVoice = (c: Connection, enabled: boolean) =>
  call<VoiceStatus>(c, "/api/voice", { method: "POST", body: JSON.stringify({ enabled }) });
/** Records 15 s of the user reading aloud and makes their voiceprint. */
export const enrollVoice = (c: Connection) => call<Enrollment>(c, "/api/voice/enroll", { method: "POST", body: "{}" });
export const forgetVoice = (c: Connection) => call<Enrollment>(c, "/api/voice/enroll", { method: "DELETE" });
export interface ApiKeys {
  saved: string[]; // public APIs with a saved key (names only)
  catalog: number; // how many APIs JARVIS can call
  need_key: string[]; // APIs that take an API key, for the name picker
}
export interface TaskView {
  id: string;
  goal: string;
  status: string;
  plan: string[];
  current: number;
  actions: { label: string; status: string }[];
  steps_taken: number;
  thought?: string; // the model's latest reasoning summary
  advice?: string; // the advisor's latest review
}
export interface TaskOffer {
  id: string;
  goal: string;
  plan: string[];
  current: number;
  done_steps: string[];
}
export const getTask = (c: Connection) => call<{ task: TaskView | null; offer: TaskOffer | null }>(c, "/api/task");
export const controlTask = (c: Connection, id: string, what: string) =>
  call<Partial<TaskView> & { prompt?: string }>(c, `/api/task/${encodeURIComponent(id)}/${what}`, { method: "POST" });

export const getAutostart = (c: Connection) => call<{ starts_with_windows: boolean }>(c, "/api/autostart");
export const setAutostart = (c: Connection, enabled: boolean) =>
  call<{ starts_with_windows: boolean }>(c, "/api/autostart", { method: "PUT", body: JSON.stringify({ enabled }) });
export const stopEverything = (c: Connection) => call<{ stopped: boolean }>(c, "/api/stop-all", { method: "POST" });
export const startGateway = (c: Connection) =>
  call<{ running: boolean; started?: boolean; error?: string }>(c, "/api/gateway/start", { method: "POST" });

export interface ModelStatus {
  name: string;
  display_name: string;
  cooling_s: number;
  recent_failures: number;
  used_today?: number | null;
}
export const getModelStatus = (c: Connection) => call<{ current: string; models: ModelStatus[] }>(c, "/api/models/status");
export const testModel = (c: Connection, model: string) =>
  call<{ model: string; ok: boolean; ms?: number; error?: string }>(c, "/api/models/test", { method: "POST", body: JSON.stringify({ model }) });

export interface Connector {
  name: string;
  kind: "local" | "web";
  command?: string;
  args?: string[];
  url?: string;
  enabled: boolean;
  has_token: boolean;
  env_names: string[];
  state: string; // connected | error | not started | off
  tools?: number;
  server?: string;
  error?: string;
}
export const getConnectors = (c: Connection) => call<Connector[]>(c, "/api/connectors");
export const saveConnector = (c: Connection, body: Record<string, unknown>) =>
  call<{ name: string; state: string; tools?: number; error?: string }>(c, "/api/connectors", { method: "PUT", body: JSON.stringify(body) });
export const retryConnector = (c: Connection, name: string) =>
  call<{ state: string }>(c, `/api/connectors/${encodeURIComponent(name)}/connect`, { method: "POST" });
export const deleteConnector = (c: Connection, name: string) =>
  call<{ removed: string }>(c, `/api/connectors/${encodeURIComponent(name)}`, { method: "DELETE" });

export interface BackgroundJob {
  name: string;
  every_hours: number;
  last_run: number | null;
  next_due: number;
  last_result: string;
}
export const getJobs = (c: Connection) => call<BackgroundJob[]>(c, "/api/jobs");

export type Policy = "allow" | "ask" | "block";
export interface Permission {
  id: string;
  label: string;
  locked: boolean; // can be blocked, never allowed without asking
  policy: Policy;
}
export const getPermissions = (c: Connection) => call<Permission[]>(c, "/api/permissions");

export const getApiKeys = (c: Connection) => call<ApiKeys>(c, "/api/apikeys");
export const saveApiKey = (c: Connection, service: string, key: string) =>
  call<{ saved: string[] }>(c, "/api/apikeys", { method: "PUT", body: JSON.stringify({ service, key }) });
export const deleteApiKey = (c: Connection, service: string) =>
  call<{ saved: string[] }>(c, `/api/apikeys/${encodeURIComponent(service)}`, { method: "DELETE" });
/** queue: follow on from what is already being said (a reply spoken sentence by sentence as it streams). */
export const speak = (c: Connection, text: string, queue = false) =>
  call<{ spoken: boolean }>(c, "/api/voice/speak", { method: "POST", body: JSON.stringify({ text, queue }) });
export const stopSpeaking = (c: Connection) => call<{ stopped: boolean }>(c, "/api/voice/stop", { method: "POST", body: "{}" });

/** A reply as it should sound: the whole of it, without markdown symbols, link targets or list bullets.
 *  A code block is not read out symbol by symbol; it is announced instead. */
export function forSpeech(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, " The code is on screen. ")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/https?:\/\/\S+/g, "")
    .replace(/^#+\s*/gm, "")
    .replace(/^\s*[-*•]\s+/gm, "")
    .replace(/[*_`>#|]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

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
  quiet?: boolean; // an API lookup: shown as a light "via ..." line
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
  body: { message: string; conversation_id?: string | null; voice?: boolean },
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
