// Minimal fetch wrappers that talk to the FastAPI backend via the /api/*
// prefix that nginx proxies to `backend:8000`. SSE chat stream uses
// `fetch + ReadableStream`; we decode text chunks and split on SSE framing.

export interface Tier {
  id: string;          // "tier.versatile"
  name: string;
  description: string;
  backend: string;
  context_window: number;
  think_supported: boolean;
  vram_estimate_gb: number;
}

export interface ConversationSummary {
  id: number;
  title: string;
  tier: string | null;
  created_at: number;
  updated_at: number;
}

export interface Message {
  id?: number;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  tier?: string | null;
  think?: boolean | null;
  tokens_in?: number | null;
  tokens_out?: number | null;
  created_at?: number;
}

// SSE event envelope (route/agent/token/error).
export interface SseEnvelope {
  kind: "token" | "agent" | "done" | "error";
  text?: string;               // for kind=token
  agent?: { type: string; data: Record<string, unknown> };  // for kind=agent
  error?: string;
}

async function j<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, {
    credentials: "include",
    headers: { "content-type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${await r.text()}`);
  return r.json() as Promise<T>;
}

export const api = {
  // ── Auth ────────────────────────────────────────────────────────────
  requestMagicLink: (email: string) =>
    j<{ ok: boolean; message: string }>("/api/auth/request", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  logout: () => j<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  me: () => j<{ id: number; email: string }>("/api/me"),

  // ── Tiers ───────────────────────────────────────────────────────────
  listTiers: async (): Promise<Tier[]> => {
    const r = await j<{ data: Tier[] }>("/api/v1/models");
    return r.data;
  },

  // ── Chats ───────────────────────────────────────────────────────────
  listChats: async (): Promise<ConversationSummary[]> => {
    const r = await j<{ data: ConversationSummary[] }>("/api/chats");
    return r.data;
  },
  createChat: (title: string, tier: string | null) =>
    j<ConversationSummary>("/api/chats", {
      method: "POST",
      body: JSON.stringify({ title, tier }),
    }),
  getChat: (id: number) =>
    j<{
      id: number; title: string; tier: string | null;
      created_at: number; updated_at: number; messages: Message[];
    }>(`/api/chats/${id}`),
  renameChat: (id: number, title: string) =>
    j<ConversationSummary>(`/api/chats/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),
  deleteChat: (id: number) => j<{ ok: boolean }>(`/api/chats/${id}`, { method: "DELETE" }),

  // ── VRAM (debug) ─────────────────────────────────────────────────────
  vramStatus: () => j<any>("/api/vram"),

  // ── System telemetry (VRAM + RAM, for the chat status panel) ────────
  systemStatus: () => j<{
    vram: { total_gb: number; free_gb: number; used_gb: number; loaded_tiers: string[] };
    ram:  { total_gb: number; used_gb: number; free_gb: number };
    ts: number;
  }>("/api/system"),

  // ── Health ping (for RTT measurement) ───────────────────────────────
  healthz: () => j<{ ok: boolean }>("/api/healthz"),

  // ── Tools ────────────────────────────────────────────────────────────
  listTools: () => j<{ data: Array<{ name: string; description: string; default_enabled: boolean }> }>("/api/tools"),

  // ── RAG ──────────────────────────────────────────────────────────────
  uploadRAG: async (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch("/api/rag/upload", {
      method: "POST",
      body: fd,
      credentials: "include",
    });
    if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
    return r.json() as Promise<{ ok: boolean; chunks: number; filename: string }>;
  },
  listRAG: () => j<{ data: Array<{ id: number; filename: string; chunk_count: number; size_bytes: number; created_at: number }> }>("/api/rag/docs"),
  deleteRAG: (id: number) => j<{ ok: boolean }>(`/api/rag/docs/${id}`, { method: "DELETE" }),

  // ── Memory ───────────────────────────────────────────────────────────
  listMemory: () => j<{ data: Array<{ id: number; content: string; source_conv: number | null; created_at: number; updated_at: number }> }>("/api/memory"),
  deleteMemory: (id: number) => j<{ ok: boolean }>(`/api/memory/${id}`, { method: "DELETE" }),
};

// ── Admin dashboard ────────────────────────────────────────────────────

export interface AdminOverview {
  window_seconds: number;
  requests: number;
  tokens_in: number;
  tokens_out: number;
  latency_ms_avg: number;
  errors: number;
  active_users: number;
  total_users: number;
  total_conversations: number;
  total_messages: number;
  total_rag_docs: number;
  total_rag_bytes: number;
  total_memories: number;
}

export interface AdminSeries {
  start: number;
  end: number;
  bucket_seconds: number;
  labels: number[];
  requests: number[];
  tokens_in: number[];
  tokens_out: number[];
  latency_ms_avg: number[];
  errors: number[];
}

export interface AdminTierStat {
  tier: string;
  requests: number;
  tokens_in: number;
  tokens_out: number;
  latency_ms_avg: number;
}

export interface AdminUserStat {
  id: number;
  email: string;
  created_at: number;
  last_login_at: number | null;
  n: number;
  tin: number;
  tout: number;
  convs: number;
}

export interface AdminUser {
  id: number;
  email: string;
  created_at: number;
  last_login_at: number | null;
  conversations: number;
  memories: number;
  rag_docs: number;
  is_admin: boolean;
}

export interface AdminConfigSnapshot {
  vram: {
    total_vram_gb: number;
    headroom_gb: number;
    poll_interval_sec: number;
    eviction: {
      policy: string;
      min_residency_sec: number;
      pin_orchestrator: boolean;
      pin_vision: boolean;
    };
    ollama: { keep_alive_default: string; keep_alive_pinned: number };
  };
  router: {
    auto_thinking_signals: {
      enable_when_any: Array<{ regex: string }>;
      disable_when_any: Array<{ regex: string }>;
    };
    multi_agent: {
      max_workers: number;
      worker_tier: string;
      orchestrator_tier: string;
    };
  };
  auth: {
    magic_link_expiry_minutes: number;
    allowed_email_domains: string[];
    rate_limits: {
      requests_per_hour_per_email: number;
      requests_per_hour_per_ip: number;
    };
    session: { cookie_ttl_days: number };
  };
  tiers: Record<string, {
    name: string;
    description: string;
    backend: string;
    model_tag: string;
    context_window: number;
    think_default: boolean;
    vram_estimate_gb: number;
    params: Record<string, any>;
  }>;
}

export const adminApi = {
  me: () => j<{ email: string; is_admin: boolean; admin_configured: boolean }>("/api/admin/me"),
  overview: (window = 86400) => j<AdminOverview>(`/api/admin/overview?window=${window}`),
  usage: (window = 86400, buckets = 48) =>
    j<AdminSeries>(`/api/admin/usage?window=${window}&buckets=${buckets}`),
  byTier: (window = 86400) =>
    j<{ data: AdminTierStat[] }>(`/api/admin/usage/by_tier?window=${window}`),
  byUser: (window = 86400, limit = 50) =>
    j<{ data: AdminUserStat[] }>(`/api/admin/usage/by_user?window=${window}&limit=${limit}`),
  errors: (limit = 25) =>
    j<{ data: Array<{ ts: number; tier: string; user_id: number | null; error: string }> }>(
      `/api/admin/errors?limit=${limit}`,
    ),
  users: () => j<{ data: AdminUser[] }>("/api/admin/users"),
  deleteUser: (id: number) =>
    j<{ ok: boolean }>(`/api/admin/users/${id}`, { method: "DELETE" }),
  vram: () => j<any>("/api/admin/vram"),
  tools: () => j<{ data: Array<{ name: string; description: string; default_enabled: boolean; enabled: boolean; requires_service: string | null }> }>("/api/admin/tools"),
  toggleTool: (name: string, enabled: boolean) =>
    j<{ ok: boolean }>(`/api/admin/tools/${encodeURIComponent(name)}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),
  getConfig: () => j<AdminConfigSnapshot>("/api/admin/config"),
  patchConfig: (patch: Partial<AdminConfigSnapshot>) =>
    j<{ ok: boolean; changes: string[] }>("/api/admin/config", {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  reload: () => j<{ ok: boolean }>("/api/admin/reload", { method: "POST" }),
};

// ── SSE chat stream ────────────────────────────────────────────────────

export type ResponseMode = "immediate" | "plan" | "clarify" | "approval" | "manual_plan";

export async function* streamChat(params: {
  model: string;
  messages: Message[];
  think?: boolean | null;
  multi_agent?: boolean | null;
  tools?: Array<Record<string, unknown>> | null;
  response_mode?: ResponseMode | null;
  plan_text?: string | null;
  signal?: AbortSignal;
}): AsyncGenerator<SseEnvelope> {
  const body: Record<string, unknown> = {
    model: params.model,
    messages: params.messages,
    stream: true,
    think: params.think ?? null,
    multi_agent: params.multi_agent ?? null,
  };
  if (params.tools && params.tools.length) body.tools = params.tools;
  if (params.response_mode && params.response_mode !== "immediate") {
    body.response_mode = params.response_mode;
  }
  if (params.plan_text && params.plan_text.trim()) {
    body.plan_text = params.plan_text;
  }
  const r = await fetch("/api/v1/chat/completions", {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal: params.signal,
  });
  if (!r.ok || !r.body) {
    yield { kind: "error", error: `${r.status} ${r.statusText}` };
    return;
  }

  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE events are separated by blank lines. Each event may have multiple
    // `event:` / `data:` lines.
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const ev = parseSseChunk(chunk);
      if (ev) yield ev;
    }
  }

  yield { kind: "done" };
}

function parseSseChunk(chunk: string): SseEnvelope | null {
  let evName: string | null = null;
  const dataLines: string[] = [];
  for (const line of chunk.split("\n")) {
    if (line.startsWith("event:")) evName = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  const data = dataLines.join("\n");
  if (!data) return null;
  if (data === "[DONE]") return { kind: "done" };

  // Named SSE events carry our typed AgentEvent payload; default (nameless)
  // events are OpenAI-style chat.completion.chunk with a token in delta.
  if (evName === "agent") {
    try {
      const parsed = JSON.parse(data);
      return { kind: "agent", agent: parsed };
    } catch { return null; }
  }

  try {
    const parsed = JSON.parse(data);
    const delta = parsed?.choices?.[0]?.delta?.content;
    if (typeof delta === "string" && delta.length > 0) {
      return { kind: "token", text: delta };
    }
  } catch { /* ignore */ }
  return null;
}
