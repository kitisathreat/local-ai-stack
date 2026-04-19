/** @jsxImportSource preact */
import { useEffect, useRef, useState } from "preact/hooks";
import {
  adminApi, api, AdminOverview, AdminSeries, AdminTierStat, AdminUser,
  AdminUserStat, AdminConfigSnapshot, Tier, streamChat,
  MultiAgentOptions, InteractionMode,
} from "./api";

type Tab =
  | "overview" | "usage" | "users" | "models" | "router" | "multi_agent"
  | "vram" | "concurrency" | "auth" | "tools";

const WINDOW_OPTS: Array<{ label: string; s: number }> = [
  { label: "1h", s: 3600 },
  { label: "24h", s: 86400 },
  { label: "7d", s: 7 * 86400 },
  { label: "30d", s: 30 * 86400 },
];

function fmt(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return n.toString();
}

function fmtBytes(b: number): string {
  if (b >= 1 << 30) return (b / (1 << 30)).toFixed(1) + " GB";
  if (b >= 1 << 20) return (b / (1 << 20)).toFixed(1) + " MB";
  if (b >= 1 << 10) return (b / (1 << 10)).toFixed(1) + " KB";
  return b + " B";
}

function fmtTime(ts: number | null): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

function Sparkline({
  values, color = "var(--accent)", height = 42,
}: { values: number[]; color?: string; height?: number }) {
  const w = 240, h = height;
  if (!values.length) return <svg width={w} height={h} />;
  const max = Math.max(1, ...values);
  const step = w / Math.max(1, values.length - 1);
  const pts = values.map((v, i) => `${i * step},${h - (v / max) * (h - 4) - 2}`).join(" ");
  const area = `0,${h} ${pts} ${w},${h}`;
  return (
    <svg width={w} height={h} class="spark">
      <polygon points={area} fill={color} opacity={0.15} />
      <polyline points={pts} fill="none" stroke={color} stroke-width={1.5} />
    </svg>
  );
}

function StatCard({
  label, value, spark, color,
}: { label: string; value: string; spark?: number[]; color?: string }) {
  return (
    <div class="admin-stat">
      <div class="admin-stat-label">{label}</div>
      <div class="admin-stat-value">{value}</div>
      {spark && <Sparkline values={spark} color={color} />}
    </div>
  );
}

/* ── Overview ──────────────────────────────────────────────────────── */

function OverviewTab({ window }: { window: number }) {
  const [data, setData] = useState<AdminOverview | null>(null);
  const [series, setSeries] = useState<AdminSeries | null>(null);
  const [err, setErr] = useState<string>("");

  async function load() {
    try {
      const [o, s] = await Promise.all([
        adminApi.overview(window), adminApi.usage(window, 48),
      ]);
      setData(o); setSeries(s);
    } catch (e: any) { setErr(e?.message || "Failed to load"); }
  }
  useEffect(() => { load(); const t = setInterval(load, 10_000); return () => clearInterval(t); }, [window]);

  if (err) return <div class="admin-error">{err}</div>;
  if (!data || !series) return <div class="admin-dim">Loading…</div>;

  return (
    <div>
      <div class="admin-grid">
        <StatCard label="Requests" value={fmt(data.requests)} spark={series.requests} />
        <StatCard label="Tokens out" value={fmt(data.tokens_out)} spark={series.tokens_out} color="var(--success)" />
        <StatCard label="Tokens in" value={fmt(data.tokens_in)} spark={series.tokens_in} color="var(--warn)" />
        <StatCard label="Avg latency" value={`${Math.round(data.latency_ms_avg)} ms`} spark={series.latency_ms_avg} color="var(--accent)" />
        <StatCard label="Errors" value={fmt(data.errors)} spark={series.errors} color="var(--danger)" />
        <StatCard label="Active users" value={fmt(data.active_users)} />
      </div>

      <h3 class="admin-h3">Totals</h3>
      <div class="admin-grid">
        <StatCard label="Users" value={fmt(data.total_users)} />
        <StatCard label="Conversations" value={fmt(data.total_conversations)} />
        <StatCard label="Messages" value={fmt(data.total_messages)} />
        <StatCard label="RAG docs" value={`${fmt(data.total_rag_docs)} · ${fmtBytes(data.total_rag_bytes)}`} />
        <StatCard label="Memories" value={fmt(data.total_memories)} />
      </div>
    </div>
  );
}

/* ── Usage ─────────────────────────────────────────────────────────── */

function UsageTab({ window }: { window: number }) {
  const [series, setSeries] = useState<AdminSeries | null>(null);
  const [tiers, setTiers] = useState<AdminTierStat[]>([]);
  const [users, setUsers] = useState<AdminUserStat[]>([]);
  const [errs, setErrs] = useState<any[]>([]);

  async function load() {
    const [s, t, u, e] = await Promise.all([
      adminApi.usage(window, 60),
      adminApi.byTier(window),
      adminApi.byUser(window, 25),
      adminApi.errors(15),
    ]);
    setSeries(s); setTiers(t.data); setUsers(u.data); setErrs(e.data);
  }
  useEffect(() => { load(); }, [window]);

  if (!series) return <div class="admin-dim">Loading…</div>;

  return (
    <div>
      <div class="admin-chart">
        <div class="admin-chart-label">Requests</div>
        <Sparkline values={series.requests} height={100} />
      </div>
      <div class="admin-chart">
        <div class="admin-chart-label">Tokens out</div>
        <Sparkline values={series.tokens_out} height={100} color="var(--success)" />
      </div>
      <div class="admin-chart">
        <div class="admin-chart-label">Avg latency (ms)</div>
        <Sparkline values={series.latency_ms_avg} height={100} color="var(--warn)" />
      </div>

      <h3 class="admin-h3">By tier</h3>
      <table class="admin-table">
        <thead><tr>
          <th>Tier</th><th>Requests</th><th>Tokens in</th>
          <th>Tokens out</th><th>Avg latency</th>
        </tr></thead>
        <tbody>
          {tiers.map((t) => (
            <tr key={t.tier}>
              <td><code>{t.tier}</code></td>
              <td>{fmt(t.requests)}</td>
              <td>{fmt(t.tokens_in)}</td>
              <td>{fmt(t.tokens_out)}</td>
              <td>{Math.round(t.latency_ms_avg)} ms</td>
            </tr>
          ))}
          {tiers.length === 0 && <tr><td colspan={5} class="admin-dim">No traffic in window.</td></tr>}
        </tbody>
      </table>

      <h3 class="admin-h3">Top users</h3>
      <table class="admin-table">
        <thead><tr><th>Email</th><th>Requests</th><th>Tokens out</th><th>Conversations</th></tr></thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td>{u.email}</td>
              <td>{fmt(u.n)}</td>
              <td>{fmt(u.tout)}</td>
              <td>{u.convs}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3 class="admin-h3">Recent errors</h3>
      {errs.length === 0 && <div class="admin-dim">None.</div>}
      {errs.map((e, i) => (
        <div key={i} class="admin-error-row">
          <span class="admin-dim">{fmtTime(e.ts)}</span>
          <code>{e.tier}</code>
          <span>{e.error}</span>
        </div>
      ))}
    </div>
  );
}

/* ── Users ─────────────────────────────────────────────────────────── */

function UsersTab() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  async function load() { setUsers((await adminApi.users()).data); }
  useEffect(() => { load(); }, []);

  async function del(u: AdminUser) {
    if (!confirm(`Delete user ${u.email}? This cascades to all their chats, memories, and docs.`)) return;
    try { await adminApi.deleteUser(u.id); await load(); }
    catch (e: any) { alert(e?.message || "Failed"); }
  }

  return (
    <table class="admin-table">
      <thead><tr>
        <th>Email</th><th>Created</th><th>Last login</th>
        <th>Chats</th><th>Memories</th><th>Docs</th><th></th>
      </tr></thead>
      <tbody>
        {users.map((u) => (
          <tr key={u.id}>
            <td>{u.email} {u.is_admin && <span class="admin-badge">admin</span>}</td>
            <td>{fmtTime(u.created_at)}</td>
            <td>{fmtTime(u.last_login_at)}</td>
            <td>{u.conversations}</td>
            <td>{u.memories}</td>
            <td>{u.rag_docs}</td>
            <td><button onClick={() => del(u)} disabled={u.is_admin}>Delete</button></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/* ── VRAM ──────────────────────────────────────────────────────────── */

function VramTab() {
  const [v, setV] = useState<any>(null);
  useEffect(() => {
    const load = async () => { try { setV(await adminApi.vram()); } catch {} };
    load(); const t = setInterval(load, 3000); return () => clearInterval(t);
  }, []);
  if (!v) return <div class="admin-dim">Loading…</div>;
  const used = Math.max(0, v.total_vram_gb - v.free_vram_gb_projected);
  const pct = Math.min(100, (used / v.total_vram_gb) * 100);
  return (
    <div>
      <div class="admin-grid">
        <StatCard label="Total VRAM" value={`${v.total_vram_gb.toFixed(1)} GB`} />
        <StatCard label="Free (actual)" value={`${v.free_vram_gb_actual?.toFixed?.(1) ?? "?"} GB`} />
        <StatCard label="Free (projected)" value={`${v.free_vram_gb_projected.toFixed(1)} GB`} />
        <StatCard label="Headroom" value={`${v.headroom_gb.toFixed(1)} GB`} />
      </div>
      <div class="admin-vram-bar"><div style={`width:${pct}%`} /></div>
      <h3 class="admin-h3">Loaded models</h3>
      <table class="admin-table">
        <thead><tr>
          <th>Tier</th><th>Tag</th><th>Backend</th><th>State</th>
          <th>Refs</th><th>Est</th><th>Observed</th><th>Last used</th>
        </tr></thead>
        <tbody>
          {(v.loaded || []).map((m: any) => (
            <tr key={m.tier_id}>
              <td><code>{m.tier_id}</code></td>
              <td><code>{m.model_tag}</code></td>
              <td>{m.backend}</td>
              <td>{m.state}</td>
              <td>{m.refcount}</td>
              <td>{m.vram_cost_gb.toFixed(1)} GB</td>
              <td>{m.observed_cost_gb?.toFixed?.(1) ?? "—"}</td>
              <td>{Math.round(m.last_used_sec_ago)}s ago</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ── Tools ─────────────────────────────────────────────────────────── */

function ToolsTab() {
  const [tools, setTools] = useState<any[]>([]);
  async function load() { setTools((await adminApi.tools()).data); }
  useEffect(() => { load(); }, []);
  async function toggle(name: string, enabled: boolean) {
    await adminApi.toggleTool(name, enabled); await load();
  }
  return (
    <table class="admin-table">
      <thead><tr><th>Name</th><th>Description</th><th>Service</th><th>Enabled</th></tr></thead>
      <tbody>
        {tools.map((t) => (
          <tr key={t.name}>
            <td><code>{t.name}</code></td>
            <td>{t.description}</td>
            <td>{t.requires_service || "—"}</td>
            <td>
              <input type="checkbox" checked={t.enabled}
                     onChange={(e) => toggle(t.name, (e.target as HTMLInputElement).checked)} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/* ── Config-editing tabs (models / router / vram / auth) ───────────── */

function useConfig() {
  const [cfg, setCfg] = useState<AdminConfigSnapshot | null>(null);
  const [status, setStatus] = useState<string>("");
  const [busy, setBusy] = useState(false);
  async function load() { setCfg(await adminApi.getConfig()); }
  useEffect(() => { load(); }, []);
  async function save(patch: Partial<AdminConfigSnapshot>) {
    setBusy(true); setStatus("Saving…");
    try {
      const r = await adminApi.patchConfig(patch);
      setStatus(r.changes.length ? `Saved ${r.changes.length} changes.` : "No changes.");
      await load();
    } catch (e: any) {
      setStatus("Error: " + (e?.message || "save failed"));
    } finally { setBusy(false); }
  }
  return { cfg, save, status, busy };
}

function Field({
  label, value, onChange, type = "text", step, hint,
}: {
  label: string; value: any; onChange: (v: any) => void;
  type?: string; step?: string; hint?: string;
}) {
  return (
    <label class="admin-field">
      <span>{label}</span>
      <input type={type} step={step} value={value ?? ""}
             onInput={(e) => {
               const raw = (e.target as HTMLInputElement).value;
               onChange(type === "number" ? (raw === "" ? null : Number(raw)) : raw);
             }} />
      {hint && <small class="admin-dim">{hint}</small>}
    </label>
  );
}

function Bool({ label, value, onChange }: { label: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <label class="admin-field admin-field-bool">
      <input type="checkbox" checked={value} onChange={(e) => onChange((e.target as HTMLInputElement).checked)} />
      <span>{label}</span>
    </label>
  );
}

function VramConfigTab() {
  const { cfg, save, status, busy } = useConfig();
  const [draft, setDraft] = useState<any>(null);
  useEffect(() => { if (cfg) setDraft(JSON.parse(JSON.stringify(cfg.vram))); }, [cfg]);
  if (!draft) return <div class="admin-dim">Loading…</div>;
  return (
    <div class="admin-form">
      <h3 class="admin-h3">Hardware</h3>
      <Field label="Total VRAM (GB)" type="number" step="0.1" value={draft.total_vram_gb}
             onChange={(v) => setDraft({ ...draft, total_vram_gb: v })} />
      <Field label="Headroom (GB)" type="number" step="0.1" value={draft.headroom_gb}
             onChange={(v) => setDraft({ ...draft, headroom_gb: v })}
             hint="Safety margin for KV cache growth." />
      <Field label="Poll interval (s)" type="number" value={draft.poll_interval_sec}
             onChange={(v) => setDraft({ ...draft, poll_interval_sec: v })} />

      <h3 class="admin-h3">Eviction</h3>
      <Field label="Policy" value={draft.eviction.policy}
             onChange={(v) => setDraft({ ...draft, eviction: { ...draft.eviction, policy: v } })} />
      <Field label="Min residency (s)" type="number" value={draft.eviction.min_residency_sec}
             onChange={(v) => setDraft({ ...draft, eviction: { ...draft.eviction, min_residency_sec: v } })} />
      <Bool label="Pin orchestrator" value={draft.eviction.pin_orchestrator}
            onChange={(v) => setDraft({ ...draft, eviction: { ...draft.eviction, pin_orchestrator: v } })} />
      <Bool label="Pin vision" value={draft.eviction.pin_vision}
            onChange={(v) => setDraft({ ...draft, eviction: { ...draft.eviction, pin_vision: v } })} />

      <h3 class="admin-h3">Ollama keep-alive</h3>
      <Field label="Default" value={draft.ollama.keep_alive_default}
             hint={'e.g. "30m"'}
             onChange={(v) => setDraft({ ...draft, ollama: { ...draft.ollama, keep_alive_default: v } })} />
      <Field label="Pinned" type="number" value={draft.ollama.keep_alive_pinned}
             hint="-1 = forever"
             onChange={(v) => setDraft({ ...draft, ollama: { ...draft.ollama, keep_alive_pinned: v } })} />

      <h3 class="admin-h3">Request queue (per tier)</h3>
      <Field label="Max depth per tier" type="number"
             value={draft.queue?.max_depth_per_tier ?? 10}
             hint="Requests beyond this are rejected with 503."
             onChange={(v) => setDraft({ ...draft, queue: { ...(draft.queue || {}), max_depth_per_tier: v } })} />
      <Field label="Max wait (seconds)" type="number"
             value={draft.queue?.max_wait_sec ?? 60}
             hint="Total time a request may spend queued before it times out."
             onChange={(v) => setDraft({ ...draft, queue: { ...(draft.queue || {}), max_wait_sec: v } })} />
      <Field label="Position update interval (s)" type="number"
             value={draft.queue?.position_update_interval_sec ?? 2}
             hint="How often queue progress is streamed to the client."
             onChange={(v) => setDraft({ ...draft, queue: { ...(draft.queue || {}), position_update_interval_sec: v } })} />

      <div class="admin-actions">
        <button class="primary" disabled={busy} onClick={() => save({ vram: draft })}>Save</button>
        <button disabled={busy} onClick={() => cfg && setDraft(JSON.parse(JSON.stringify(cfg.vram)))}>Revert</button>
        {status && <span class="admin-dim">{status}</span>}
      </div>
    </div>
  );
}

function ConcurrencyTab() {
  const { cfg, save, status, busy } = useConfig();
  const [authDraft, setAuthDraft] = useState<any>(null);
  const [concDraft, setConcDraft] = useState<any>(null);
  useEffect(() => {
    if (cfg) {
      setAuthDraft(JSON.parse(JSON.stringify(cfg.auth)));
      setConcDraft(JSON.parse(JSON.stringify(cfg.concurrency || {
        workers_target: 1, workers_running: 1, redis_url_set: false, redis_healthy: false,
      })));
    }
  }, [cfg]);
  if (!cfg || !authDraft || !concDraft) return <div class="admin-dim">Loading…</div>;

  const redisStatus = concDraft.redis_url_set
    ? (concDraft.redis_healthy ? "connected" : "configured but unreachable")
    : "disabled (single-worker in-memory mode)";

  return (
    <div class="admin-form">
      <h3 class="admin-h3">Per-user rate limits</h3>
      <Field label="Requests / minute / user" type="number"
             value={authDraft.rate_limits.requests_per_minute_per_user ?? 30}
             hint="Applies to the /v1/chat/completions endpoint. Shared across workers when Redis is connected."
             onChange={(v) => setAuthDraft({
               ...authDraft,
               rate_limits: { ...authDraft.rate_limits, requests_per_minute_per_user: v },
             })} />
      <Field label="Requests / day / user" type="number"
             value={authDraft.rate_limits.requests_per_day_per_user ?? 500}
             onChange={(v) => setAuthDraft({
               ...authDraft,
               rate_limits: { ...authDraft.rate_limits, requests_per_day_per_user: v },
             })} />

      <h3 class="admin-h3">Workers &amp; Redis</h3>
      <div class="admin-dim" style="margin-bottom:0.5rem;">
        Currently running: <strong>{concDraft.workers_running} worker(s)</strong>.
        Redis: <strong>{redisStatus}</strong>.
      </div>
      <Field label="Workers target (next restart)" type="number"
             value={concDraft.workers_target ?? 1}
             hint="Uvicorn --workers count. Requires a container restart to apply."
             onChange={(v) => setConcDraft({ ...concDraft, workers_target: v })} />
      <Field label="Redis URL" value={concDraft.redis_url ?? ""}
             hint='e.g. "redis://redis:6379/0". Leave blank to run in-memory (single-worker only). Requires a restart.'
             onChange={(v) => setConcDraft({ ...concDraft, redis_url: v })} />

      <div class="admin-actions">
        <button class="primary" disabled={busy}
                onClick={() => save({ auth: authDraft, concurrency: concDraft })}>
          Save
        </button>
        <button disabled={busy}
                onClick={() => {
                  setAuthDraft(JSON.parse(JSON.stringify(cfg.auth)));
                  setConcDraft(JSON.parse(JSON.stringify(cfg.concurrency || {})));
                }}>
          Revert
        </button>
        {status && <span class="admin-dim">{status}</span>}
      </div>
    </div>
  );
}

function RouterConfigTab() {
  const { cfg, save, status, busy } = useConfig();
  const [draft, setDraft] = useState<any>(null);
  useEffect(() => { if (cfg) setDraft(JSON.parse(JSON.stringify(cfg.router))); }, [cfg]);
  if (!draft) return <div class="admin-dim">Loading…</div>;

  const listEditor = (key: "enable_when_any" | "disable_when_any") => {
    const items = draft.auto_thinking_signals[key] as Array<{ regex: string }>;
    const setItems = (arr: any[]) =>
      setDraft({
        ...draft,
        auto_thinking_signals: { ...draft.auto_thinking_signals, [key]: arr },
      });
    return (
      <div class="admin-regex-list">
        {items.map((r, i) => (
          <div key={i} class="admin-regex-row">
            <input value={r.regex}
                   onInput={(e) => {
                     const arr = [...items];
                     arr[i] = { regex: (e.target as HTMLInputElement).value };
                     setItems(arr);
                   }} />
            <button onClick={() => setItems(items.filter((_, j) => j !== i))}>✕</button>
          </div>
        ))}
        <button onClick={() => setItems([...items, { regex: "" }])}>+ Add regex</button>
      </div>
    );
  };

  return (
    <div class="admin-form">
      <h3 class="admin-h3">Auto-thinking: enable when any match</h3>
      {listEditor("enable_when_any")}
      <h3 class="admin-h3">Auto-thinking: disable when any match</h3>
      {listEditor("disable_when_any")}

      <p class="admin-dim" style="font-size:0.85rem;">
        Multi-agent workflow defaults moved to the dedicated{" "}
        <strong>Multi-agent</strong> tab — including the workflow diagram and
        a live test runner. This tab now only edits the auto-thinking signal
        regexes.
      </p>

      <div class="admin-actions">
        <button class="primary" disabled={busy} onClick={() => save({ router: draft })}>Save</button>
        <button disabled={busy} onClick={() => cfg && setDraft(JSON.parse(JSON.stringify(cfg.router)))}>Revert</button>
        {status && <span class="admin-dim">{status}</span>}
      </div>
    </div>
  );
}

/* ── Multi-agent tab ───────────────────────────────────────────────── */
//
// Three sections, top to bottom:
//   1. Workflow diagram — schematic of the current orchestrator → workers
//      → synthesis pipeline, reflecting the *unsaved draft* settings so the
//      admin can see the shape of the pipeline before saving.
//   2. Defaults — same fields that used to live under Router. Saves to
//      router.yaml via PATCH /admin/config (hot-reload).
//   3. Live test — admin-only sandbox that submits a prompt with
//      multi_agent_options.enabled=true and renders a worker card per
//      subtask in real time. Useful for tuning min/max workers and
//      collaborative rounds without polluting a real chat.

type WorkerStatus = "pending" | "running" | "done" | "error";

interface WorkerCard {
  id: number;
  tier: string;
  task: string;
  status: WorkerStatus;
  chars: number;
  round: number;
  error?: string;
}

function tierLabel(tier: Tier | undefined, name: string): string {
  if (!tier) return name;
  // "Versatile (Qwen3.6 35B-A3B)" → "Versatile · 35B"
  const m = tier.name.match(/^([^(]+?)\s*\(.*?(\d+\.?\d*[Bb]).*\)/);
  return m ? `${m[1].trim()} · ${m[2]}` : tier.name;
}

function WorkflowDiagram({
  orchestratorTier, workerTier, maxWorkers, interactionMode,
  interactionRounds, reasoningWorkers, tiersByName,
}: {
  orchestratorTier: string;
  workerTier: string;
  maxWorkers: number;
  interactionMode: string;
  interactionRounds: number;
  reasoningWorkers: boolean;
  tiersByName: Record<string, Tier>;
}) {
  const orchTier = tiersByName[orchestratorTier];
  const wTier = tiersByName[workerTier];
  // Cap drawn workers at 6 so the row stays readable; show a "+N more"
  // chip beyond that.
  const drawn = Math.min(maxWorkers, 6);
  const overflow = Math.max(0, maxWorkers - drawn);
  const collaborative = interactionMode === "collaborative" && interactionRounds > 0;

  return (
    <div class="ma-flow">
      <div class="ma-flow-row">
        <div class="ma-node ma-node-orch">
          <div class="ma-node-label">Orchestrator · plan</div>
          <code>{orchestratorTier}</code>
          <small class="admin-dim">{tierLabel(orchTier, orchestratorTier)}</small>
        </div>
      </div>
      <div class="ma-flow-arrow">↓ decompose into ≤ {maxWorkers} subtasks</div>
      <div class="ma-flow-row ma-flow-row-workers">
        {Array.from({ length: drawn }).map((_, i) => (
          <div key={i} class="ma-node ma-node-worker">
            <div class="ma-node-label">Worker {i + 1}</div>
            <code>{workerTier}</code>
            <small class="admin-dim">{tierLabel(wTier, workerTier)}</small>
            <div class="ma-node-flags">
              {reasoningWorkers && <span class="ma-flag">🧠 reason</span>}
            </div>
          </div>
        ))}
        {overflow > 0 && (
          <div class="ma-node ma-node-overflow">+{overflow} more</div>
        )}
      </div>
      {collaborative && (
        <div class="ma-flow-collab">
          ↔ {interactionRounds} refinement round{interactionRounds === 1 ? "" : "s"} —
          peers share drafts and revise
        </div>
      )}
      <div class="ma-flow-arrow">↓ synthesize</div>
      <div class="ma-flow-row">
        <div class="ma-node ma-node-orch">
          <div class="ma-node-label">Orchestrator · synthesize</div>
          <code>{orchestratorTier}</code>
          <small class="admin-dim">{tierLabel(orchTier, orchestratorTier)}</small>
        </div>
      </div>
    </div>
  );
}

function MultiAgentTab() {
  const { cfg, save, status, busy } = useConfig();
  const [draft, setDraft] = useState<any>(null);
  const [tiers, setTiers] = useState<Tier[]>([]);

  useEffect(() => { if (cfg) setDraft(JSON.parse(JSON.stringify(cfg.router.multi_agent))); }, [cfg]);
  useEffect(() => {
    (async () => {
      try { setTiers(await api.listTiers()); } catch { /* ignore */ }
    })();
  }, []);

  // ── Live test runner state ───────────────────────────────────────────
  const [prompt, setPrompt] = useState(
    "Compare Python, Rust, and Go for building a high-throughput web service. " +
    "For each, cover concurrency model, ecosystem maturity, and deployment story.",
  );
  const [running, setRunning] = useState(false);
  const [workers, setWorkers] = useState<WorkerCard[]>([]);
  const [events, setEvents] = useState<Array<{ type: string; data: any; ts: number }>>([]);
  const [output, setOutput] = useState("");
  const [testErr, setTestErr] = useState<string>("");
  const abortRef = useRef<AbortController | null>(null);

  if (!draft) return <div class="admin-dim">Loading…</div>;

  const tiersByName: Record<string, Tier> = {};
  for (const t of tiers) tiersByName[t.id.replace(/^tier\./, "")] = t;
  const tierOptions = tiers.map((t) => t.id.replace(/^tier\./, ""));

  const setMA = (patch: any) => setDraft({ ...draft, ...patch });

  async function runTest() {
    if (running || !prompt.trim()) return;
    setRunning(true);
    setWorkers([]);
    setEvents([]);
    setOutput("");
    setTestErr("");
    const t0 = performance.now();
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    // Use the *unsaved* draft so admins can preview tweaks before persisting.
    const opts: MultiAgentOptions = {
      enabled: true,
      num_workers: draft.max_workers,
      worker_tier: draft.worker_tier,
      orchestrator_tier: draft.orchestrator_tier,
      reasoning_workers: !!draft.reasoning_workers,
      interaction_mode: (draft.interaction_mode as InteractionMode) || "independent",
      interaction_rounds: draft.interaction_rounds,
    };

    try {
      for await (const ev of streamChat({
        model: `tier.${draft.orchestrator_tier || "versatile"}`,
        messages: [{ role: "user", content: prompt }],
        multi_agent_options: opts,
        signal: ctrl.signal,
      })) {
        const ts = performance.now() - t0;
        if (ev.kind === "agent" && ev.agent) {
          const { type, data } = ev.agent;
          setEvents((prev) => [...prev, { type, data, ts }]);
          if (type === "agent.workers_start") {
            const ws: WorkerCard[] = ((data.workers as any[]) || []).map((w) => ({
              id: w.id, tier: w.tier, task: w.task,
              status: "running", chars: 0, round: 1,
            }));
            setWorkers(ws);
          } else if (type === "agent.refine_start") {
            const r = (data as any).round as number || 2;
            setWorkers((prev) => prev.map((w) => (
              { ...w, status: "running" as WorkerStatus, round: r }
            )));
          } else if (type === "agent.worker_done") {
            const d = data as { id: number; chars?: number; round?: number; error?: string | null };
            setWorkers((prev) => prev.map((w) => {
              if (w.id !== d.id) return w;
              const next: WorkerCard = {
                ...w,
                status: (d.error ? "error" : "done") as WorkerStatus,
                chars: Number(d.chars || 0),
                round: Number(d.round || w.round),
                error: d.error || undefined,
              };
              return next;
            }));
          } else if (type === "error") {
            setTestErr(String((data as any).message || "unknown error"));
          }
        } else if (ev.kind === "token" && ev.text) {
          setOutput((prev) => prev + ev.text);
        } else if (ev.kind === "error" && ev.error) {
          setTestErr(ev.error);
        }
      }
    } catch (e: any) {
      if (e?.name !== "AbortError") setTestErr(e?.message || String(e));
    } finally {
      setRunning(false);
      abortRef.current = null;
    }
  }

  function cancelTest() { abortRef.current?.abort(); }

  return (
    <div class="ma-tab">
      {/* ── 1. Workflow diagram ──────────────────────────────────── */}
      <h3 class="admin-h3">Workflow (preview from current draft)</h3>
      <WorkflowDiagram
        orchestratorTier={draft.orchestrator_tier || "versatile"}
        workerTier={draft.worker_tier || "fast"}
        maxWorkers={draft.max_workers || 3}
        interactionMode={draft.interaction_mode || "independent"}
        interactionRounds={draft.interaction_rounds || 0}
        reasoningWorkers={!!draft.reasoning_workers}
        tiersByName={tiersByName}
      />

      {/* ── 2. Tuning ────────────────────────────────────────────── */}
      <h3 class="admin-h3">Defaults (router.yaml)</h3>
      <p class="admin-dim" style="font-size:0.85rem; margin-top:-0.4rem;">
        Edits here apply globally. Per-chat overrides from the chat header
        sit on top of these without persisting.
      </p>
      <div class="admin-form-grid">
        <Field label="Min workers" type="number" value={draft.min_workers}
               hint="Lower bound the orchestrator targets when decomposing."
               onChange={(v) => setMA({ min_workers: v })} />
        <Field label="Max workers" type="number" value={draft.max_workers}
               hint="Hard cap on parallel subtasks (1..8)."
               onChange={(v) => setMA({ max_workers: v })} />
        <label class="admin-field">
          <span>Worker tier (size & quantization)</span>
          <select value={draft.worker_tier}
                  onChange={(e) => setMA({ worker_tier: (e.target as HTMLSelectElement).value })}>
            {tierOptions.length === 0 && <option value={draft.worker_tier}>{draft.worker_tier}</option>}
            {tierOptions.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <small class="admin-dim">Each tier embeds a model size and quantization.</small>
        </label>
        <label class="admin-field">
          <span>Orchestrator tier</span>
          <select value={draft.orchestrator_tier}
                  onChange={(e) => setMA({ orchestrator_tier: (e.target as HTMLSelectElement).value })}>
            {tierOptions.length === 0 && <option value={draft.orchestrator_tier}>{draft.orchestrator_tier}</option>}
            {tierOptions.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <small class="admin-dim">Plans subtasks and synthesizes the final answer.</small>
        </label>
        <Bool label="Workers reason (think mode) by default"
              value={!!draft.reasoning_workers}
              onChange={(v) => setMA({ reasoning_workers: v })} />
        <label class="admin-field">
          <span>Interaction mode</span>
          <select value={draft.interaction_mode || "independent"}
                  onChange={(e) => setMA({ interaction_mode: (e.target as HTMLSelectElement).value })}>
            <option value="independent">Independent (parallel only)</option>
            <option value="collaborative">Collaborative (peers refine)</option>
          </select>
          <small class="admin-dim">
            Collaborative shares peer drafts between rounds for higher rigor.
          </small>
        </label>
        <Field label="Refinement rounds (collaborative only)" type="number"
               value={draft.interaction_rounds}
               hint="Extra rounds where workers cross-read drafts (0..4)."
               onChange={(v) => setMA({ interaction_rounds: v })} />
      </div>
      <div class="admin-actions">
        <button class="primary" disabled={busy}
                onClick={() => save({ router: { multi_agent: draft } as any })}>Save</button>
        <button disabled={busy}
                onClick={() => cfg && setDraft(JSON.parse(JSON.stringify(cfg.router.multi_agent)))}>Revert</button>
        {status && <span class="admin-dim">{status}</span>}
      </div>

      {/* ── 3. Live test ─────────────────────────────────────────── */}
      <h3 class="admin-h3">Live test</h3>
      <p class="admin-dim" style="font-size:0.85rem; margin-top:-0.4rem;">
        Send a prompt through the orchestrator using the unsaved draft above
        and watch the workers run. Results aren't saved to any conversation.
      </p>
      <textarea
        class="ma-test-prompt"
        value={prompt}
        onInput={(e) => setPrompt((e.target as HTMLTextAreaElement).value)}
        disabled={running}
        rows={3}
      />
      <div class="admin-actions">
        {!running ? (
          <button class="primary" disabled={!prompt.trim()} onClick={runTest}>
            ▶ Run test
          </button>
        ) : (
          <button onClick={cancelTest}>■ Stop</button>
        )}
        <button disabled={running}
                onClick={() => { setWorkers([]); setEvents([]); setOutput(""); setTestErr(""); }}>
          Clear
        </button>
        {running && <span class="admin-dim">Streaming…</span>}
      </div>

      {testErr && <div class="admin-error" style="margin-top:0.6rem;">{testErr}</div>}

      {(workers.length > 0 || events.length > 0) && (
        <div class="ma-test-grid">
          <div>
            <h4 class="admin-h4">Workers</h4>
            {workers.length === 0 && <div class="admin-dim">Waiting for plan…</div>}
            <div class="ma-worker-grid">
              {workers.map((w) => (
                <div key={w.id} class={`ma-worker-card ma-status-${w.status}`}>
                  <div class="ma-worker-card-head">
                    <span>Worker {w.id}</span>
                    <span class="ma-worker-status">{w.status}</span>
                  </div>
                  <code class="ma-worker-tier">{w.tier}</code>
                  {w.round > 1 && <div class="ma-worker-round">round {w.round}</div>}
                  <div class="ma-worker-task">{w.task}</div>
                  <div class="ma-worker-meta">
                    {w.status === "done" && <>{w.chars.toLocaleString()} chars</>}
                    {w.status === "error" && <span class="ma-worker-err">{w.error}</span>}
                  </div>
                </div>
              ))}
            </div>

            <h4 class="admin-h4">Final output</h4>
            <div class="ma-output">
              {output || <span class="admin-dim">(no synthesis yet)</span>}
            </div>
          </div>
          <div>
            <h4 class="admin-h4">Event timeline</h4>
            <div class="ma-event-log">
              {events.length === 0 && <div class="admin-dim">No events yet.</div>}
              {events.map((e, i) => (
                <div key={i} class="ma-event-row">
                  <span class="ma-event-ts">{(e.ts / 1000).toFixed(2)}s</span>
                  <code class="ma-event-type">{e.type.replace(/^agent\./, "")}</code>
                  <span class="ma-event-data">{summarizeEventData(e.type, e.data)}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function summarizeEventData(type: string, data: any): string {
  if (!data) return "";
  if (type === "agent.plan_start") {
    return `tier=${data.tier} mode=${data.interaction_mode} max=${data.max_workers}`;
  }
  if (type === "agent.plan_done") return `${data.subtask_count} subtasks`;
  if (type === "agent.workers_start") {
    return `${data.count} workers · reasoning=${!!data.reasoning_workers}`;
  }
  if (type === "agent.worker_done") {
    return data.error
      ? `worker ${data.id} ERROR ${data.error}`
      : `worker ${data.id} → ${data.chars} chars (round ${data.round || 1})`;
  }
  if (type === "agent.refine_start") {
    return `round ${data.round} of ${data.total_rounds}`;
  }
  if (type === "agent.synthesis_start") return `tier=${data.tier}`;
  if (type === "agent.synthesis_done") return "complete";
  if (type === "route.decision") {
    return `tier=${data.tier} multi=${data.multi_agent} think=${data.think}`;
  }
  if (type === "error") return data.message || "";
  return JSON.stringify(data).slice(0, 80);
}

function AuthConfigTab() {
  const { cfg, save, status, busy } = useConfig();
  const [draft, setDraft] = useState<any>(null);
  useEffect(() => { if (cfg) setDraft(JSON.parse(JSON.stringify(cfg.auth))); }, [cfg]);
  if (!draft) return <div class="admin-dim">Loading…</div>;
  return (
    <div class="admin-form">
      <Field label="Magic link expiry (minutes)" type="number"
             value={draft.magic_link_expiry_minutes}
             onChange={(v) => setDraft({ ...draft, magic_link_expiry_minutes: v })} />
      <Field label="Allowed email domains (comma-separated, blank = any)"
             value={(draft.allowed_email_domains || []).join(", ")}
             hint='e.g. "example.com, other.org"'
             onChange={(v) => setDraft({ ...draft, allowed_email_domains: v })} />
      <h3 class="admin-h3">Rate limits</h3>
      <Field label="Requests / hour / email" type="number"
             value={draft.rate_limits.requests_per_hour_per_email}
             onChange={(v) => setDraft({ ...draft, rate_limits: { ...draft.rate_limits, requests_per_hour_per_email: v } })} />
      <Field label="Requests / hour / IP" type="number"
             value={draft.rate_limits.requests_per_hour_per_ip}
             onChange={(v) => setDraft({ ...draft, rate_limits: { ...draft.rate_limits, requests_per_hour_per_ip: v } })} />
      <h3 class="admin-h3">Session</h3>
      <Field label="Cookie TTL (days)" type="number"
             value={draft.session.cookie_ttl_days}
             onChange={(v) => setDraft({ ...draft, session: { ...draft.session, cookie_ttl_days: v } })} />
      <div class="admin-actions">
        <button class="primary" disabled={busy} onClick={() => save({ auth: draft })}>Save</button>
        <button disabled={busy} onClick={() => cfg && setDraft(JSON.parse(JSON.stringify(cfg.auth)))}>Revert</button>
        {status && <span class="admin-dim">{status}</span>}
      </div>
    </div>
  );
}

function ModelsConfigTab() {
  const { cfg, save, status, busy } = useConfig();
  const [draft, setDraft] = useState<any>(null);
  useEffect(() => { if (cfg) setDraft(JSON.parse(JSON.stringify(cfg.tiers))); }, [cfg]);
  if (!draft) return <div class="admin-dim">Loading…</div>;

  const setTier = (name: string, patch: any) =>
    setDraft({ ...draft, [name]: { ...draft[name], ...patch } });
  const setParam = (name: string, key: string, val: any) =>
    setDraft({
      ...draft,
      [name]: {
        ...draft[name],
        params: { ...(draft[name].params || {}), [key]: val },
      },
    });

  const tierNames = Object.keys(draft);
  return (
    <div class="admin-form">
      {tierNames.map((name) => {
        const t = draft[name];
        return (
          <details key={name} class="admin-tier">
            <summary><strong>{t.name}</strong> <code class="admin-dim">({name} · {t.backend} · {t.model_tag})</code></summary>
            <Field label="Description" value={t.description}
                   onChange={(v) => setTier(name, { description: v })} />
            <Field label="Context window" type="number" value={t.context_window}
                   onChange={(v) => setTier(name, { context_window: v })} />
            <Bool label="Thinking on by default" value={t.think_default}
                  onChange={(v) => setTier(name, { think_default: v })} />
            <Field label="VRAM estimate (GB)" type="number" step="0.1" value={t.vram_estimate_gb}
                   onChange={(v) => setTier(name, { vram_estimate_gb: v })} />
            <Field label="Parallel slots" type="number"
                   value={t.parallel_slots ?? 1}
                   hint={
                     "Concurrent requests this loaded model can serve (num_parallel). " +
                     "Effective per-request KV budget = context_window; total KV ≈ slots × context_window. " +
                     "Saving a change evicts and reloads the model."
                   }
                   onChange={(v) => setTier(name, { parallel_slots: v })} />
            <h4 class="admin-h4">Sampling params</h4>
            <Field label="temperature" type="number" step="0.05"
                   value={t.params?.temperature ?? ""}
                   onChange={(v) => setParam(name, "temperature", v)} />
            <Field label="top_p" type="number" step="0.05"
                   value={t.params?.top_p ?? ""}
                   onChange={(v) => setParam(name, "top_p", v)} />
            <Field label="top_k" type="number"
                   value={t.params?.top_k ?? ""}
                   onChange={(v) => setParam(name, "top_k", v)} />
            <Field label="num_ctx" type="number"
                   value={t.params?.num_ctx ?? ""}
                   onChange={(v) => setParam(name, "num_ctx", v)} />
          </details>
        );
      })}
      <div class="admin-actions">
        <button class="primary" disabled={busy} onClick={() => save({ tiers: draft })}>Save</button>
        <button disabled={busy} onClick={() => cfg && setDraft(JSON.parse(JSON.stringify(cfg.tiers)))}>Revert</button>
        {status && <span class="admin-dim">{status}</span>}
      </div>
    </div>
  );
}

/* ── Root ──────────────────────────────────────────────────────────── */

export function AdminDashboard({ onExit }: { onExit: () => void }) {
  const [tab, setTab] = useState<Tab>("overview");
  const [window, setWindow] = useState(86400);
  const [access, setAccess] = useState<"loading" | "ok" | "denied" | "disabled">("loading");
  const [email, setEmail] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const r = await adminApi.me();
        setEmail(r.email);
        if (!r.admin_configured) setAccess("disabled");
        else setAccess(r.is_admin ? "ok" : "denied");
      } catch { setAccess("denied"); }
    })();
  }, []);

  if (access === "loading") return <div class="admin-dim" style="padding:2rem;">Loading admin…</div>;
  if (access !== "ok") {
    return (
      <div class="admin-root">
        <div class="admin-gate">
          <h2>Admin dashboard</h2>
          {access === "disabled" ? (
            <p>The admin dashboard is disabled. Set the <code>ADMIN_EMAILS</code> env var on the backend (comma-separated emails) and restart.</p>
          ) : (
            <p>Signed in as <code>{email}</code> — not an admin. Ask an administrator to add you to <code>ADMIN_EMAILS</code>.</p>
          )}
          <button onClick={onExit}>Back to chat</button>
        </div>
      </div>
    );
  }

  const tabs: Array<{ id: Tab; label: string }> = [
    { id: "overview", label: "Overview" },
    { id: "usage", label: "Usage" },
    { id: "users", label: "Users" },
    { id: "models", label: "Models" },
    { id: "router", label: "Router" },
    { id: "multi_agent", label: "Multi-agent" },
    { id: "vram", label: "VRAM" },
    { id: "concurrency", label: "Concurrency" },
    { id: "auth", label: "Auth" },
    { id: "tools", label: "Tools" },
  ];
  const needsWindow = tab === "overview" || tab === "usage";

  return (
    <div class="admin-root">
      <header class="admin-header">
        <h1>Admin</h1>
        <nav class="admin-tabs">
          {tabs.map((t) => (
            <button key={t.id}
                    class={"admin-tab" + (tab === t.id ? " active" : "")}
                    onClick={() => setTab(t.id)}>{t.label}</button>
          ))}
        </nav>
        <div class="admin-spacer" />
        {needsWindow && (
          <div class="admin-window">
            {WINDOW_OPTS.map((w) => (
              <button key={w.s}
                      class={"admin-tab" + (window === w.s ? " active" : "")}
                      onClick={() => setWindow(w.s)}>{w.label}</button>
            ))}
          </div>
        )}
        <button onClick={onExit}>← Chat</button>
      </header>
      <main class="admin-main">
        {tab === "overview" && <OverviewTab window={window} />}
        {tab === "usage" && <UsageTab window={window} />}
        {tab === "users" && <UsersTab />}
        {tab === "models" && <ModelsConfigTab />}
        {tab === "router" && <RouterConfigTab />}
        {tab === "multi_agent" && <MultiAgentTab />}
        {tab === "vram" && <VramConfigTab />}
        {tab === "concurrency" && <ConcurrencyTab />}
        {tab === "auth" && <AuthConfigTab />}
        {tab === "tools" && <ToolsTab />}
      </main>
    </div>
  );
}
