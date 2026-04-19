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
};

// ── SSE chat stream ────────────────────────────────────────────────────

export async function* streamChat(params: {
  model: string;
  messages: Message[];
  think?: boolean | null;
  multi_agent?: boolean | null;
  signal?: AbortSignal;
}): AsyncGenerator<SseEnvelope> {
  const body = {
    model: params.model,
    messages: params.messages,
    stream: true,
    think: params.think ?? null,
    multi_agent: params.multi_agent ?? null,
  };
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
