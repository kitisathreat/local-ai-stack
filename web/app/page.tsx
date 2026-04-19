"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Sidebar } from "@/components/Sidebar";
import { Header } from "@/components/Header";
import { MessageList } from "@/components/MessageList";
import { Composer, type UploadRef } from "@/components/Composer";
import { TelemetryPanel } from "@/components/TelemetryPanel";
import { api, type Chat, type Message, type ModelProfile } from "@/lib/api";

export default function Home() {
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [defaultProfile, setDefaultProfile] = useState<string>("");
  const [activeProfileId, setActiveProfileId] = useState<string>("");
  const [thinking, setThinking] = useState(false);

  const [chats, setChats]         = useState<Chat[]>([]);
  const [activeId, setActiveId]   = useState<string | null>(null);
  const [messages, setMessages]   = useState<Message[]>([]);
  const [streaming, setStreaming] = useState("");
  const [sending, setSending]     = useState(false);
  const [tps, setTps]             = useState<number | null>(null);

  const activeProfile = useMemo(
    () => profiles.find((p) => p.profile === activeProfileId) ?? null,
    [profiles, activeProfileId]
  );

  const refreshChats = useCallback(async () => {
    const { chats } = await api.chats();
    setChats(chats);
  }, []);

  useEffect(() => {
    (async () => {
      const { default: def, profiles: list } = await api.models();
      setProfiles(list);
      setDefaultProfile(def);
      setActiveProfileId(def);
      await refreshChats();
    })().catch(console.error);
  }, [refreshChats]);

  useEffect(() => {
    if (!activeId) { setMessages([]); return; }
    api.messages(activeId).then((r) => setMessages(r.messages)).catch(console.error);
  }, [activeId]);

  async function handleNewChat() {
    const { id } = await api.createChat(activeProfileId || defaultProfile, thinking);
    setActiveId(id);
    await refreshChats();
  }

  async function handleSend(text: string, _attachments: UploadRef[]) {
    let chatId = activeId;
    if (!chatId) {
      const created = await api.createChat(activeProfileId || defaultProfile, thinking);
      chatId = created.id;
      setActiveId(chatId);
      await refreshChats();
    }

    const userMsg: Message = {
      id: `tmp-${Date.now()}`,
      role: "user",
      content: text,
      created_at: Date.now(),
    };
    setMessages((m) => [...m, userMsg]);
    setStreaming("");
    setSending(true);
    setTps(null);

    const started = Date.now();
    let tokens = 0;
    let buf = "";

    const res = await fetch(`/api/chats/${chatId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: text }),
    });

    if (!res.body) { setSending(false); return; }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let carry = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      carry += decoder.decode(value, { stream: true });
      const events = carry.split("\n\n");
      carry = events.pop() ?? "";

      for (const evt of events) {
        const lines = evt.split("\n");
        const nameLine = lines.find((l) => l.startsWith("event:"));
        const dataLine = lines.find((l) => l.startsWith("data:"));
        if (!nameLine || !dataLine) continue;
        const name = nameLine.slice(6).trim();
        const data = JSON.parse(dataLine.slice(5).trim());
        if (name === "token") {
          buf += data.delta;
          tokens += 1;
          setStreaming(buf);
          const elapsed = (Date.now() - started) / 1000;
          if (elapsed > 0) setTps(tokens / elapsed);
        } else if (name === "done") {
          setMessages((m) => [...m, {
            id: data.id,
            role: "assistant",
            content: buf,
            tokens: data.tokens,
            tokens_per_sec: data.tokens_per_sec,
            created_at: Date.now(),
          }]);
          setStreaming("");
          setTps(data.tokens_per_sec);
        } else if (name === "error") {
          setStreaming("");
          console.error(data.message);
        }
      }
    }
    setSending(false);
    await refreshChats();
  }

  async function handleDelete(id: string) {
    await api.deleteChat(id);
    if (activeId === id) setActiveId(null);
    await refreshChats();
  }

  return (
    <div className="flex h-screen w-screen bg-bg text-neutral-200 overflow-hidden">
      <Sidebar
        chats={chats}
        activeId={activeId}
        onSelect={setActiveId}
        onNew={handleNewChat}
        onDelete={handleDelete}
      />

      <main className="flex-1 flex flex-col min-w-0">
        <Header
          profiles={profiles}
          active={activeProfile}
          onChange={setActiveProfileId}
          thinking={thinking}
          onToggleThinking={setThinking}
        />
        <MessageList messages={messages} streaming={streaming} />
        <Composer
          contextWindow={activeProfile?.context ?? 4096}
          onSend={handleSend}
          disabled={sending}
        />
      </main>

      <TelemetryPanel tokensPerSec={tps} />
    </div>
  );
}
