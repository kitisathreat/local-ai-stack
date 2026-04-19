/** @jsxImportSource preact */
import { render } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";

import {
  api, Message, Tier, ConversationSummary, streamChat,
} from "./api";

/* ── Magic-link sign-in ─────────────────────────────────────────────── */

function SignIn({ onHint }: { onHint: () => void }) {
  const [email, setEmail] = useState("");
  const [status, setStatus] = useState<null | { ok: boolean; msg: string }>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: Event) {
    e.preventDefault();
    if (!email) return;
    setBusy(true);
    setStatus(null);
    try {
      const r = await api.requestMagicLink(email);
      setStatus({ ok: true, msg: r.message });
      onHint();
    } catch (e: any) {
      setStatus({ ok: false, msg: e.message || "Request failed" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="signin">
      <div class="signin-card">
        <h2>Sign in</h2>
        <p>Enter your email and we'll send you a magic link.</p>
        <form onSubmit={submit}>
          <input
            type="email"
            value={email}
            required
            placeholder="you@example.com"
            onInput={(e) => setEmail((e.target as HTMLInputElement).value)}
            disabled={busy}
          />
          <button type="submit" class="primary" disabled={busy || !email}>
            {busy ? "Sending…" : "Send magic link"}
          </button>
        </form>
        {status && (
          <div class={"status " + (status.ok ? "ok" : "error")}>{status.msg}</div>
        )}
      </div>
    </div>
  );
}

/* ── Tier picker + reasoning toggle ─────────────────────────────────── */

function TierPicker({
  tiers, activeId, onPick,
}: { tiers: Tier[]; activeId: string; onPick: (id: string) => void }) {
  return (
    <div class="tier-picker" role="tablist" aria-label="Model tier">
      {tiers.map((t) => (
        <button
          key={t.id}
          class={"tier-btn" + (t.id === activeId ? " active" : "")}
          onClick={() => onPick(t.id)}
          title={t.description}
        >
          {shortName(t.name)}
        </button>
      ))}
    </div>
  );
}

function shortName(full: string): string {
  // "Versatile (Qwen3.6 35B-A3B)" -> "Versatile"
  const p = full.indexOf(" (");
  return p > 0 ? full.slice(0, p) : full;
}

function ReasoningToggle({
  mode, onChange,
}: { mode: "auto" | "on" | "off"; onChange: (m: "auto" | "on" | "off") => void }) {
  return (
    <div class={"reason-toggle" + (mode === "auto" ? " auto" : "")}>
      <label style="margin:0; cursor:pointer;">
        🧠 reasoning:
        <select
          value={mode}
          onChange={(e) =>
            onChange((e.target as HTMLSelectElement).value as any)
          }
          style="margin-left:0.3rem; background:transparent; border:none; color:inherit; cursor:pointer;"
        >
          <option value="auto">auto</option>
          <option value="on">on</option>
          <option value="off">off</option>
        </select>
      </label>
    </div>
  );
}

/* ── History sidebar ────────────────────────────────────────────────── */

function HistorySidebar({
  me, chats, activeId, onSelect, onNew, onDelete, onLogout,
}: {
  me: { email: string };
  chats: ConversationSummary[];
  activeId: number | null;
  onSelect: (id: number) => void;
  onNew: () => void;
  onDelete: (id: number) => void;
  onLogout: () => void;
}) {
  return (
    <aside class="sidebar">
      <div class="sidebar-header">
        <h1>Local AI Stack</h1>
        <button onClick={onNew} title="New chat">+</button>
      </div>
      <div class="sidebar-body">
        <div class="conv-list">
          {chats.length === 0 && (
            <div style="padding:0.75rem; color:var(--fg-dim); font-size:0.9rem;">
              No chats yet. Start one on the right.
            </div>
          )}
          {chats.map((c) => (
            <div
              key={c.id}
              class={"conv-item" + (c.id === activeId ? " active" : "")}
              onClick={() => onSelect(c.id)}
            >
              <span class="title">{c.title}</span>
              <button
                class="del-btn"
                title="Delete"
                onClick={(e) => { e.stopPropagation(); onDelete(c.id); }}
              >✕</button>
            </div>
          ))}
        </div>
      </div>
      <div class="sidebar-footer">
        {me.email} · <a href="#" onClick={(e) => { e.preventDefault(); onLogout(); }}>sign out</a>
      </div>
    </aside>
  );
}

/* ── Agent panel (multi-agent visualization) ────────────────────────── */

type AgentStep = { label: string; state: "pending" | "active" | "done" };

function AgentPanel({ steps }: { steps: AgentStep[] }) {
  if (steps.length === 0) return null;
  return (
    <div class="agent-panel">
      {steps.map((s, i) => (
        <div key={i} class={"step " + s.state}>
          <span class="dot" /> {s.label}
        </div>
      ))}
    </div>
  );
}

/* ── Message rendering ──────────────────────────────────────────────── */

function MessageBubble({ m }: { m: Message }) {
  const { thinking, body } = splitThinking(m.content || "");
  return (
    <div class={"msg " + m.role}>
      {m.role !== "user" && (
        <div class="meta">{m.tier ?? "assistant"}{m.think ? " · reasoning" : ""}</div>
      )}
      {thinking && (
        <details class="think-block">
          <summary>Reasoning ({thinking.split("\n").length} lines)</summary>
          <div style="margin-top:0.4rem; white-space:pre-wrap;">{thinking}</div>
        </details>
      )}
      <div class="body">{body || (m.role === "assistant" ? "…" : "")}</div>
    </div>
  );
}

function splitThinking(content: string): { thinking: string | null; body: string } {
  // Extract <think>...</think> blocks and surface them separately.
  const m = content.match(/<think>([\s\S]*?)<\/think>/i);
  if (!m) return { thinking: null, body: content };
  const thinking = m[1].trim();
  const body = content.replace(m[0], "").trim();
  return { thinking: thinking || null, body };
}

/* ── Main chat view ─────────────────────────────────────────────────── */

function ChatView({
  me, tiers,
}: { me: { email: string }; tiers: Tier[] }) {
  const [chats, setChats] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [pendingAsst, setPendingAsst] = useState<string>("");
  const [agentSteps, setAgentSteps] = useState<AgentStep[]>([]);
  const [tier, setTier] = useState<string>("tier.versatile");
  const [reasoning, setReasoning] = useState<"auto" | "on" | "off">("auto");
  const [draft, setDraft] = useState<string>("");
  const [sending, setSending] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => { refreshChats(); }, []);
  useEffect(() => { scrollRef.current?.scrollTo(0, 1e9); }, [messages, pendingAsst, agentSteps]);

  async function refreshChats() {
    try {
      const cs = await api.listChats();
      setChats(cs);
      if (activeId == null && cs.length > 0) selectChat(cs[0].id);
    } catch (e) { console.error(e); }
  }

  async function selectChat(id: number) {
    setActiveId(id);
    try {
      const c = await api.getChat(id);
      setMessages(c.messages);
      if (c.tier) setTier(c.tier.startsWith("tier.") ? c.tier : `tier.${c.tier}`);
    } catch (e) { console.error(e); }
  }

  async function newChat() {
    const c = await api.createChat("New chat", tier);
    setChats((prev) => [c, ...prev]);
    setActiveId(c.id);
    setMessages([]);
  }

  async function delChat(id: number) {
    if (!confirm("Delete this conversation?")) return;
    await api.deleteChat(id);
    setChats((prev) => prev.filter((c) => c.id !== id));
    if (activeId === id) {
      setActiveId(null);
      setMessages([]);
    }
  }

  async function send() {
    if (!draft.trim() || sending) return;
    let convId = activeId;
    if (convId == null) {
      const c = await api.createChat(draft.slice(0, 50), tier);
      setChats((prev) => [c, ...prev]);
      setActiveId(c.id);
      convId = c.id;
    }
    const userMsg: Message = { role: "user", content: draft };
    const newMsgs = [...messages, userMsg];
    setMessages(newMsgs);
    setDraft("");
    setSending(true);
    setPendingAsst("");
    setAgentSteps([]);

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let assembled = "";
    try {
      for await (const ev of streamChat({
        model: tier,
        messages: newMsgs,
        think: reasoning === "auto" ? null : reasoning === "on",
        signal: ctrl.signal,
      })) {
        if (ev.kind === "token" && ev.text) {
          assembled += ev.text;
          setPendingAsst(assembled);
        } else if (ev.kind === "agent" && ev.agent) {
          updateAgentSteps(ev.agent.type, ev.agent.data);
        } else if (ev.kind === "error") {
          assembled += `\n[Error: ${ev.error}]`;
          setPendingAsst(assembled);
        }
      }
    } catch (e: any) {
      console.error(e);
      assembled += `\n[Connection error: ${e?.message}]`;
    } finally {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: assembled, tier, think: reasoning === "on" },
      ]);
      setPendingAsst("");
      setAgentSteps((s) => s.map((x) => ({ ...x, state: x.state === "active" ? "done" : x.state })));
      setSending(false);
      abortRef.current = null;
      refreshChats();  // updated_at changed
    }
  }

  function updateAgentSteps(type: string, _data: Record<string, unknown>) {
    setAgentSteps((prev) => {
      const steps = [...prev];
      const set = (label: string, state: AgentStep["state"]) => {
        const i = steps.findIndex((s) => s.label === label);
        if (i === -1) steps.push({ label, state });
        else steps[i] = { label, state };
      };
      const markPrevDone = () => {
        steps.forEach((s) => { if (s.state === "active") s.state = "done"; });
      };

      switch (type) {
        case "route.decision": /* route events are noisy; skip */ break;
        case "agent.plan_start":
          set("Planning subtasks", "active"); break;
        case "agent.plan_done":
          set("Planning subtasks", "done"); break;
        case "agent.workers_start":
          set("Spawning parallel workers", "active"); break;
        case "agent.worker_done":
          set("Spawning parallel workers", "done"); break;
        case "agent.synthesis_start":
          markPrevDone(); set("Synthesizing", "active"); break;
        case "agent.synthesis_done":
          set("Synthesizing", "done"); break;
      }
      return steps;
    });
  }

  function cancel() { abortRef.current?.abort(); }

  const pendingBubble = pendingAsst ? (
    <MessageBubble
      m={{ role: "assistant", content: pendingAsst, tier, think: reasoning === "on" }}
    />
  ) : null;

  return (
    <div class="app-root">
      <HistorySidebar
        me={me}
        chats={chats}
        activeId={activeId}
        onSelect={selectChat}
        onNew={newChat}
        onDelete={delChat}
        onLogout={async () => { await api.logout(); location.reload(); }}
      />
      <section class="main">
        <header class="chat-header">
          <TierPicker tiers={tiers} activeId={tier} onPick={setTier} />
          <ReasoningToggle mode={reasoning} onChange={setReasoning} />
          <div style="flex:1" />
          <div style="color:var(--fg-dim); font-size:0.85rem;">{me.email}</div>
        </header>
        <div class="chat-body" ref={scrollRef}>
          {messages.length === 0 && !pendingAsst && (
            <div style="text-align:center; color:var(--fg-dim); margin-top:4rem;">
              <div style="font-size:1.4rem;">👋</div>
              <div style="margin-top:0.5rem;">Ask anything. Slash commands: <code>/think on|off</code>, <code>/solo</code>, <code>/tier coding</code>.</div>
            </div>
          )}
          {messages.map((m, i) => <MessageBubble key={i} m={m} />)}
          {pendingBubble}
          <AgentPanel steps={agentSteps} />
        </div>
        <div class="composer">
          <textarea
            value={draft}
            placeholder={"Message " + shortName(tiers.find((t) => t.id === tier)?.name || tier) + "…"}
            onInput={(e) => setDraft((e.target as HTMLTextAreaElement).value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
            }}
            disabled={sending}
          />
          {sending
            ? <button class="send-btn" onClick={cancel}>Stop</button>
            : <button class="send-btn primary" onClick={send} disabled={!draft.trim()}>Send</button>}
        </div>
      </section>
    </div>
  );
}

/* ── Root ───────────────────────────────────────────────────────────── */

function App() {
  const [me, setMe] = useState<null | { id: number; email: string }>(null);
  const [loaded, setLoaded] = useState(false);
  const [tiers, setTiers] = useState<Tier[]>([]);

  useEffect(() => {
    (async () => {
      try {
        const [u, ts] = await Promise.all([
          api.me().catch(() => null),
          api.listTiers().catch(() => []),
        ]);
        setMe(u);
        setTiers(ts);
      } finally {
        setLoaded(true);
      }
    })();
  }, []);

  if (!loaded) return <div style="padding:2rem; color:var(--fg-dim);">Loading…</div>;
  if (!me) return <SignIn onHint={() => {}} />;
  return <ChatView me={me} tiers={tiers} />;
}

render(<App />, document.getElementById("app")!);
