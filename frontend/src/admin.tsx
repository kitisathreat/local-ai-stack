/** @jsxImportSource preact */
import { useEffect, useState } from "preact/hooks";
import {
  adminApi, AdminOverview, AdminSeries, AdminTierStat, AdminUser,
  AdminUserStat, AdminConfigSnapshot,
} from "./api";

type Tab = "overview" | "usage" | "users" | "models" | "router" | "vram" | "auth" | "tools";

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

      <div class="admin-actions">
        <button class="primary" disabled={busy} onClick={() => save({ vram: draft })}>Save</button>
        <button disabled={busy} onClick={() => cfg && setDraft(JSON.parse(JSON.stringify(cfg.vram)))}>Revert</button>
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

      <h3 class="admin-h3">Multi-agent</h3>
      <p class="admin-dim" style="margin-top:-0.4rem; font-size:0.85rem;">
        Defaults that apply when a chat doesn't override them. Admins can
        also tweak these per chat from the chat header (won't persist).
      </p>
      <div class="admin-form-grid">
        <Field label="Min workers" type="number" value={draft.multi_agent.min_workers}
               hint="Lower bound the orchestrator targets when decomposing."
               onChange={(v) => setDraft({ ...draft, multi_agent: { ...draft.multi_agent, min_workers: v } })} />
        <Field label="Max workers" type="number" value={draft.multi_agent.max_workers}
               hint="Hard cap on parallel subtasks (1..8)."
               onChange={(v) => setDraft({ ...draft, multi_agent: { ...draft.multi_agent, max_workers: v } })} />
        <Field label="Worker tier (size & quantization)"
               value={draft.multi_agent.worker_tier}
               hint='e.g. "fast" — each tier embeds model size + quantization.'
               onChange={(v) => setDraft({ ...draft, multi_agent: { ...draft.multi_agent, worker_tier: v } })} />
        <Field label="Orchestrator tier" value={draft.multi_agent.orchestrator_tier}
               hint="Plans subtasks and synthesizes the final answer."
               onChange={(v) => setDraft({ ...draft, multi_agent: { ...draft.multi_agent, orchestrator_tier: v } })} />
        <Bool label="Workers reason (think mode) by default"
              value={!!draft.multi_agent.reasoning_workers}
              onChange={(v) => setDraft({ ...draft, multi_agent: { ...draft.multi_agent, reasoning_workers: v } })} />
        <label class="admin-field">
          <span>Interaction mode</span>
          <select value={draft.multi_agent.interaction_mode || "independent"}
                  onChange={(e) => setDraft({
                    ...draft,
                    multi_agent: {
                      ...draft.multi_agent,
                      interaction_mode: (e.target as HTMLSelectElement).value,
                    },
                  })}>
            <option value="independent">Independent (parallel only)</option>
            <option value="collaborative">Collaborative (peers refine)</option>
          </select>
          <small class="admin-dim">
            Collaborative mode lets workers see each other's drafts and refine
            their own answers between rounds before synthesis. Higher rigor,
            higher cost.
          </small>
        </label>
        <Field label="Refinement rounds (collaborative only)" type="number"
               value={draft.multi_agent.interaction_rounds}
               hint="Extra rounds where workers cross-read drafts (0..4)."
               onChange={(v) => setDraft({ ...draft, multi_agent: { ...draft.multi_agent, interaction_rounds: v } })} />
      </div>

      <div class="admin-actions">
        <button class="primary" disabled={busy} onClick={() => save({ router: draft })}>Save</button>
        <button disabled={busy} onClick={() => cfg && setDraft(JSON.parse(JSON.stringify(cfg.router)))}>Revert</button>
        {status && <span class="admin-dim">{status}</span>}
      </div>
    </div>
  );
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
    { id: "vram", label: "VRAM" },
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
        {tab === "vram" && <VramConfigTab />}
        {tab === "auth" && <AuthConfigTab />}
        {tab === "tools" && <ToolsTab />}
      </main>
    </div>
  );
}
