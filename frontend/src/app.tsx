/** @jsxImportSource preact */
import { render } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";

import {
  api, adminApi, Message, Tier, ConversationSummary, streamChat,
} from "./api";
import { AdminDashboard } from "./admin";

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
  me, chats, activeId, onSelect, onNew, onDelete, onLogout, onSettings,
}: {
  me: { email: string };
  chats: ConversationSummary[];
  activeId: number | null;
  onSelect: (id: number) => void;
  onNew: () => void;
  onDelete: (id: number) => void;
  onLogout: () => void;
  onSettings: () => void;
}) {
  return (
    <aside class="sidebar">
      <div class="sidebar-header">
        <h1>Local AI Stack</h1>
        <div style="display:flex; gap:0.3rem;">
          <button onClick={onSettings} title="Settings (docs + memory)">⚙︎</button>
          <button onClick={onNew} title="New chat">+</button>
        </div>
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

/* ── Settings panel: RAG docs + memories ────────────────────────────── */

function SettingsPanel({ onClose }: { onClose: () => void }) {
  const [docs, setDocs] = useState<any[]>([]);
  const [mems, setMems] = useState<any[]>([]);
  const [uploading, setUploading] = useState(false);
  const [status, setStatus] = useState<string>("");
  const fileInput = useRef<HTMLInputElement | null>(null);

  async function refresh() {
    try {
      const [d, m] = await Promise.all([api.listRAG(), api.listMemory()]);
      setDocs(d.data); setMems(m.data);
    } catch (e: any) { setStatus(e?.message || "Failed to load"); }
  }
  useEffect(() => { refresh(); }, []);

  async function onUpload(e: Event) {
    const f = (e.target as HTMLInputElement).files?.[0];
    if (!f) return;
    setUploading(true); setStatus("Uploading…");
    try {
      const r = await api.uploadRAG(f);
      setStatus(`Uploaded ${r.filename} (${r.chunks} chunks)`);
      await refresh();
    } catch (e: any) {
      setStatus(`Upload failed: ${e?.message}`);
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function delDoc(id: number) {
    if (!confirm("Delete this document from your knowledge base?")) return;
    await api.deleteRAG(id);
    await refresh();
  }
  async function delMem(id: number) {
    if (!confirm("Forget this memory?")) return;
    await api.deleteMemory(id);
    await refresh();
  }

  return (
    <div style="position:fixed; inset:0; background:rgba(0,0,0,0.6); z-index:50; display:flex; align-items:flex-start; justify-content:center; padding:2rem;" onClick={onClose}>
      <div class="signin-card" style="max-width:640px; width:100%; max-height:85vh; overflow-y:auto;"
           onClick={(e) => e.stopPropagation()}>
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <h2 style="margin:0;">Settings</h2>
          <button onClick={onClose}>✕</button>
        </div>

        <h3 style="margin-top:1.5rem; font-size:1rem;">Knowledge base</h3>
        <p style="color:var(--fg-dim); font-size:0.85rem; margin-top:0.2rem;">
          Upload PDFs, Markdown, or text files. The assistant will retrieve
          relevant passages when you ask related questions.
        </p>
        <div>
          <input ref={fileInput} type="file" accept=".pdf,.md,.txt,.html,.htm"
                 onChange={onUpload} disabled={uploading} />
        </div>
        <ul style="padding-left:0; list-style:none; margin-top:0.8rem;">
          {docs.length === 0 && <li style="color:var(--fg-dim); font-size:0.85rem;">No documents uploaded yet.</li>}
          {docs.map((d) => (
            <li key={d.id} style="display:flex; justify-content:space-between; padding:0.4rem 0; font-size:0.9rem;">
              <span>{d.filename} <span style="color:var(--fg-dim);">· {d.chunk_count} chunks</span></span>
              <button onClick={() => delDoc(d.id)}>Delete</button>
            </li>
          ))}
        </ul>

        <h3 style="margin-top:1.5rem; font-size:1rem;">Memories</h3>
        <p style="color:var(--fg-dim); font-size:0.85rem; margin-top:0.2rem;">
          Long-term facts the assistant remembers about you across conversations.
        </p>
        <ul style="padding-left:0; list-style:none; margin-top:0.4rem;">
          {mems.length === 0 && <li style="color:var(--fg-dim); font-size:0.85rem;">No memories yet. They're distilled after each chat.</li>}
          {mems.map((m) => (
            <li key={m.id} style="display:flex; justify-content:space-between; padding:0.4rem 0; gap:0.6rem; font-size:0.9rem;">
              <span>{m.content}</span>
              <button onClick={() => delMem(m.id)}>Forget</button>
            </li>
          ))}
        </ul>

        {status && <div class="status" style="margin-top:1rem;">{status}</div>}
      </div>
    </div>
  );
}

/* ── Tool picker (modal) ────────────────────────────────────────────── */

function ToolPicker({
  selected, onClose, onApply,
}: {
  selected: Set<string>;
  onClose: () => void;
  onApply: (next: Set<string>) => void;
}) {
  const [tools, setTools] = useState<Array<{ name: string; description: string; default_enabled: boolean; requires_service: string | null }>>([]);
  const [pick, setPick] = useState<Set<string>>(new Set(selected));
  const [q, setQ] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const r = await api.listTools();
        setTools(r.data);
      } catch { /* ignore */ }
    })();
  }, []);

  const toggle = (name: string) => {
    const next = new Set(pick);
    if (next.has(name)) next.delete(name); else next.add(name);
    setPick(next);
  };
  const filtered = tools.filter((t) =>
    !q || t.name.toLowerCase().includes(q.toLowerCase()) ||
    (t.description || "").toLowerCase().includes(q.toLowerCase())
  );

  return (
    <div style="position:fixed; inset:0; background:rgba(0,0,0,0.6); z-index:60; display:flex; align-items:flex-start; justify-content:center; padding:2rem;" onClick={onClose}>
      <div class="signin-card" style="max-width:640px; width:100%; max-height:80vh; overflow-y:auto;" onClick={(e) => e.stopPropagation()}>
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <h2 style="margin:0;">Tools & connectors</h2>
          <button onClick={onClose}>✕</button>
        </div>
        <p style="color:var(--fg-dim); font-size:0.85rem; margin-top:0.3rem;">
          Pick which tools the assistant may call for your next message. An
          empty selection falls back to the server defaults.
        </p>
        <input type="text" placeholder="Filter…"
               value={q}
               onInput={(e) => setQ((e.target as HTMLInputElement).value)}
               style="margin-bottom:0.8rem;" />
        <ul style="padding-left:0; list-style:none; margin:0;">
          {filtered.map((t) => (
            <li key={t.name} class="tool-row">
              <label style="display:flex; gap:0.6rem; align-items:flex-start; cursor:pointer;">
                <input type="checkbox"
                       checked={pick.has(t.name)}
                       onChange={() => toggle(t.name)} />
                <span>
                  <code>{t.name}</code>
                  {t.requires_service && (
                    <span class="admin-badge" style="background:var(--bg-alt); color:var(--fg-dim); border:1px solid var(--border);">{t.requires_service}</span>
                  )}
                  <div style="color:var(--fg-dim); font-size:0.85rem;">{t.description}</div>
                </span>
              </label>
            </li>
          ))}
          {filtered.length === 0 && <li style="color:var(--fg-dim); font-size:0.9rem;">No tools match.</li>}
        </ul>
        <div style="display:flex; gap:0.5rem; margin-top:1rem;">
          <button class="primary" onClick={() => { onApply(pick); onClose(); }}>
            Apply ({pick.size})
          </button>
          <button onClick={() => setPick(new Set())}>Clear</button>
        </div>
      </div>
    </div>
  );
}

/* ── Telemetry panel ────────────────────────────────────────────────── */

type TelemetryState = {
  ping_ms: number | null;
  tps: number | null;
  vram_used_gb: number;
  vram_total_gb: number;
  ram_used_gb: number;
  ram_total_gb: number;
  ctx_used: number;
  ctx_total: number;
};

function pct(n: number, d: number): number {
  if (!d) return 0;
  return Math.max(0, Math.min(100, (n / d) * 100));
}

function TelemetryPanel({
  state, open, onToggle,
}: { state: TelemetryState; open: boolean; onToggle: () => void }) {
  const vramP = pct(state.vram_used_gb, state.vram_total_gb);
  const ramP = pct(state.ram_used_gb, state.ram_total_gb);
  const ctxP = pct(state.ctx_used, state.ctx_total);
  return (
    <div class={"telemetry" + (open ? " open" : "")}>
      <button class="telemetry-toggle" onClick={onToggle}
              title="Toggle telemetry">
        <span class="dot"
              style={state.ping_ms == null
                ? "background:var(--fg-dim)"
                : state.ping_ms > 500
                  ? "background:var(--danger)"
                  : state.ping_ms > 150
                    ? "background:var(--warn)"
                    : "background:var(--success)"} />
        <span class="telemetry-compact">
          {state.ping_ms != null ? `${Math.round(state.ping_ms)} ms` : "—"}
          {state.tps != null && <> · {state.tps.toFixed(1)} tok/s</>}
        </span>
        <span class="telemetry-chev">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div class="telemetry-body">
          <div class="telemetry-row">
            <span class="telemetry-label">Ping</span>
            <span class="telemetry-value">
              {state.ping_ms != null ? `${Math.round(state.ping_ms)} ms` : "—"}
            </span>
          </div>
          <div class="telemetry-row">
            <span class="telemetry-label">Tokens / sec</span>
            <span class="telemetry-value">
              {state.tps != null ? state.tps.toFixed(1) : "—"}
            </span>
          </div>
          <TelemetryBar
            label="VRAM"
            used={state.vram_used_gb} total={state.vram_total_gb}
            unit="GB" pct={vramP}
          />
          <TelemetryBar
            label="RAM"
            used={state.ram_used_gb} total={state.ram_total_gb}
            unit="GB" pct={ramP}
          />
          <TelemetryBar
            label="Context"
            used={state.ctx_used} total={state.ctx_total}
            unit="tok" pct={ctxP}
            danger={ctxP > 90}
          />
        </div>
      )}
    </div>
  );
}

function TelemetryBar({
  label, used, total, unit, pct, danger,
}: { label: string; used: number; total: number; unit: string; pct: number; danger?: boolean }) {
  return (
    <div class="telemetry-bar-row">
      <span class="telemetry-label">{label}</span>
      <div class="telemetry-bar">
        <div style={`width:${pct}%; background:${danger ? "var(--danger)" : pct > 80 ? "var(--warn)" : "var(--accent)"}`} />
      </div>
      <span class="telemetry-value">
        {unit === "GB"
          ? `${used.toFixed(1)} / ${total.toFixed(1)} ${unit}`
          : `${used} / ${total} ${unit}`}
        <span class="telemetry-pct"> ({Math.round(pct)}%)</span>
      </span>
    </div>
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
  me, tiers, isAdmin, onOpenAdmin,
}: { me: { email: string }; tiers: Tier[]; isAdmin: boolean; onOpenAdmin: () => void }) {
  const [chats, setChats] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [pendingAsst, setPendingAsst] = useState<string>("");
  const [agentSteps, setAgentSteps] = useState<AgentStep[]>([]);
  const [tier, setTier] = useState<string>("tier.versatile");
  const [reasoning, setReasoning] = useState<"auto" | "on" | "off">("auto");
  const [draft, setDraft] = useState<string>("");
  const [sending, setSending] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showTools, setShowTools] = useState(false);
  const [selectedTools, setSelectedTools] = useState<Set<string>>(new Set());
  const [uploadStatus, setUploadStatus] = useState<string>("");
  const [telemetryOpen, setTelemetryOpen] = useState(false);
  const [telemetry, setTelemetry] = useState<TelemetryState>({
    ping_ms: null, tps: null,
    vram_used_gb: 0, vram_total_gb: 0,
    ram_used_gb: 0, ram_total_gb: 0,
    ctx_used: 0, ctx_total: 8192,
  });
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const uploadInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => { refreshChats(); }, []);
  useEffect(() => { scrollRef.current?.scrollTo(0, 1e9); }, [messages, pendingAsst, agentSteps]);

  // Telemetry polling: ping + VRAM/RAM. Runs always so the indicator dot
  // is meaningful even before the first message. Cadence is intentionally
  // gentle (3s) to stay off the hot path.
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      const t0 = performance.now();
      try { await api.healthz(); }
      catch { if (!cancelled) setTelemetry((s) => ({ ...s, ping_ms: null })); return; }
      const ping = performance.now() - t0;
      try {
        const sys = await api.systemStatus();
        if (cancelled) return;
        setTelemetry((s) => ({
          ...s,
          ping_ms: ping,
          vram_used_gb: sys.vram.used_gb,
          vram_total_gb: sys.vram.total_gb,
          ram_used_gb: sys.ram.used_gb,
          ram_total_gb: sys.ram.total_gb,
        }));
      } catch {
        if (cancelled) return;
        setTelemetry((s) => ({ ...s, ping_ms: ping }));
      }
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Context-window fill %: whitespace-word proxy against the active tier's
  // context_window. Mirrors how the backend records tokens_in, so the ratio
  // stays honest even if it's not tokenizer-accurate.
  useEffect(() => {
    const ctxTotal = tiers.find((t) => t.id === tier)?.context_window ?? 8192;
    const used = messages.reduce((n, m) => {
      const c = typeof m.content === "string" ? m.content : "";
      return n + (c ? c.split(/\s+/).length : 0);
    }, 0) + (draft ? draft.split(/\s+/).length : 0);
    setTelemetry((s) => ({ ...s, ctx_used: used, ctx_total: ctxTotal }));
  }, [messages, draft, tier, tiers]);

  async function onUploadClick() {
    uploadInputRef.current?.click();
  }

  async function onUploadChange(e: Event) {
    const f = (e.target as HTMLInputElement).files?.[0];
    if (!f) return;
    setUploadStatus(`Uploading ${f.name}…`);
    try {
      const r = await api.uploadRAG(f);
      setUploadStatus(`✓ ${r.filename} (${r.chunks} chunks) — available as RAG context.`);
      setTimeout(() => setUploadStatus(""), 6000);
    } catch (err: any) {
      setUploadStatus(`Upload failed: ${err?.message || "error"}`);
    } finally {
      if (uploadInputRef.current) uploadInputRef.current.value = "";
    }
  }

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
    let firstTokenAt = 0;
    let tokenCount = 0;
    try {
      for await (const ev of streamChat({
        model: tier,
        messages: newMsgs,
        think: reasoning === "auto" ? null : reasoning === "on",
        tools: selectedTools.size
          ? Array.from(selectedTools).map((n) => ({
              type: "function", function: { name: n },
            }))
          : null,
        signal: ctrl.signal,
      })) {
        if (ev.kind === "token" && ev.text) {
          if (!firstTokenAt) firstTokenAt = performance.now();
          assembled += ev.text;
          tokenCount += Math.max(1, ev.text.split(/\s+/).filter(Boolean).length);
          const elapsed = (performance.now() - firstTokenAt) / 1000;
          if (elapsed > 0.15) {
            const tps = tokenCount / elapsed;
            setTelemetry((s) => ({ ...s, tps }));
          }
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
      setTelemetry((s) => ({ ...s, tps: null }));
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
        onSettings={() => setShowSettings(true)}
      />
      {showSettings && <SettingsPanel onClose={() => setShowSettings(false)} />}
      <section class="main">
        <header class="chat-header">
          <TierPicker tiers={tiers} activeId={tier} onPick={setTier} />
          <ReasoningToggle mode={reasoning} onChange={setReasoning} />
          <div style="flex:1" />
          {isAdmin && (
            <button onClick={onOpenAdmin} title="Admin dashboard" style="font-size:0.85rem;">Admin</button>
          )}
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
        <TelemetryPanel
          state={telemetry}
          open={telemetryOpen}
          onToggle={() => setTelemetryOpen((v) => !v)}
        />
        {uploadStatus && (
          <div class="composer-status">{uploadStatus}</div>
        )}
        <div class="composer">
          <div class="composer-actions">
            <input ref={uploadInputRef} type="file"
                   accept=".pdf,.md,.txt,.html,.htm"
                   style="display:none;" onChange={onUploadChange} />
            <button class="icon-btn" onClick={onUploadClick}
                    title="Upload a document to your knowledge base"
                    disabled={sending}>📎</button>
            <button class={"icon-btn" + (selectedTools.size ? " active" : "")}
                    onClick={() => setShowTools(true)}
                    title="Pick tools / connectors for this message"
                    disabled={sending}>🧰{selectedTools.size > 0 && <span class="icon-count">{selectedTools.size}</span>}</button>
          </div>
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
        {showTools && (
          <ToolPicker
            selected={selectedTools}
            onClose={() => setShowTools(false)}
            onApply={(s) => setSelectedTools(s)}
          />
        )}
      </section>
    </div>
  );
}

/* ── Root ───────────────────────────────────────────────────────────── */

function useHashRoute(): [string, (h: string) => void] {
  const [hash, setHash] = useState<string>(location.hash || "");
  useEffect(() => {
    const on = () => setHash(location.hash || "");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const nav = (h: string) => {
    location.hash = h;
    setHash(h);
  };
  return [hash, nav];
}

function App() {
  const [me, setMe] = useState<null | { id: number; email: string }>(null);
  const [loaded, setLoaded] = useState(false);
  const [tiers, setTiers] = useState<Tier[]>([]);
  const [isAdmin, setIsAdmin] = useState(false);
  const [hash, nav] = useHashRoute();

  useEffect(() => {
    (async () => {
      try {
        const [u, ts] = await Promise.all([
          api.me().catch(() => null),
          api.listTiers().catch(() => []),
        ]);
        setMe(u);
        setTiers(ts);
        if (u) {
          // adminApi.me() returns is_admin=false for non-admin users and
          // throws 503 when ADMIN_EMAILS is unset — both collapse to false.
          try {
            const r = await adminApi.me();
            setIsAdmin(!!r.is_admin);
          } catch { setIsAdmin(false); }
        }
      } finally {
        setLoaded(true);
      }
    })();
  }, []);

  if (!loaded) return <div style="padding:2rem; color:var(--fg-dim);">Loading…</div>;
  if (!me) return <SignIn onHint={() => {}} />;
  if (hash === "#/admin") {
    // Server 403s non-admins, but block the route here too so the bundle
    // doesn't even render the config forms.
    if (!isAdmin) {
      return (
        <div class="admin-root">
          <div class="admin-gate">
            <h2>Admin dashboard</h2>
            <p>Signed in as <code>{me.email}</code> — not an admin account.</p>
            <button onClick={() => nav("")}>Back to chat</button>
          </div>
        </div>
      );
    }
    return <AdminDashboard onExit={() => nav("")} />;
  }
  return <ChatView me={me} tiers={tiers} isAdmin={isAdmin} onOpenAdmin={() => nav("#/admin")} />;
}

render(<App />, document.getElementById("app")!);
