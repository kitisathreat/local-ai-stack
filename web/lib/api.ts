export interface ModelProfile {
  profile: string;
  name: string;
  description: string;
  context: number;
  supports_thinking: boolean;
  supports_vision: boolean;
}

export interface Chat {
  id: string;
  title: string;
  model_id: string;
  thinking: number;
  created_at: number;
  updated_at: number;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  tokens?: number | null;
  tokens_per_sec?: number | null;
  created_at: number;
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const api = {
  models:   () => fetch("/api/models").then(json<{ default: string; profiles: ModelProfile[] }>),
  chats:    () => fetch("/api/chats").then(json<{ chats: Chat[] }>),
  createChat: (profile: string, thinking: boolean) =>
    fetch("/api/chats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile, thinking }),
    }).then(json<{ id: string; profile: string }>),
  renameChat: (id: string, title: string) =>
    fetch(`/api/chats/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }).then(json<{ ok: true }>),
  deleteChat: (id: string) =>
    fetch(`/api/chats/${id}`, { method: "DELETE" }).then(json<{ ok: true }>),
  messages: (id: string) => fetch(`/api/chats/${id}/messages`).then(json<{ messages: Message[] }>),
};
