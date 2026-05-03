# Local AI Stack — native Windows mode

A self-hosted multi-model LLM workflow that runs entirely on a single Windows
machine. A FastAPI backend routes each chat request to one of six llama.cpp
tiers (four chat, one vision, one embedding) with a VRAM-aware scheduler, a
multi-agent orchestrator, per-user RAG + memory, and an OpenAI-compatible
SSE endpoint.

**No Docker. No browser dependency. No Electron.** Ships as a single
PowerShell launcher (`LocalAIStack.ps1`) and a native PySide6 desktop app.

<p align="center">
  <img src="docs/images/launcher-window.svg" alt="LocalAIStack.ps1 console output during -Start"
       width="700"/>
</p>

## Quickstart

**Requires PowerShell 7 or higher.** The launcher refuses to run under
Windows PowerShell 5.1 (em-dashes in string literals break 5.1's
Windows-1252 default parser). If `pwsh` isn't installed:

```powershell
winget install --id Microsoft.PowerShell --source winget
```

Then:

```powershell
pwsh .\LocalAIStack.ps1 -InitEnv   # write a default .env (edit it: secrets, optional Brave key)
pwsh .\LocalAIStack.ps1 -Setup     # install prereqs, download binaries, create venvs, pull models
pwsh .\LocalAIStack.ps1            # start everything and launch the native Qt GUI
```

After cloning, opt in to the auto-refresh git hook so future `git pull`s
restart the backend with the new code:

```powershell
git config core.hooksPath .githooks
```

This wires up [`.githooks/post-merge`](.githooks/post-merge), which calls
[`scripts/refresh-backend.ps1`](scripts/refresh-backend.ps1) — diffs the
pull, `pip install`s any `requirements.txt` changes, and bounces the
backend so the new tool registry + admin endpoints are live without
manual intervention. The hook is gated to fire **only on pulls from
`origin`** (it ignores local merges, rebases, and pulls from other
remotes).

To **auto-pull on every PR squash+merge**, register the polling task —
it checks origin every 2 minutes and ff-pulls when a new commit is up:

```powershell
pwsh .\scripts\install-auto-pull.ps1                 # default: every 2 min
pwsh .\scripts\install-auto-pull.ps1 -IntervalMinutes 5
pwsh .\scripts\install-auto-pull.ps1 -Uninstall      # remove the task
```

The end-to-end chain is then:

> PR squash+merge on GitHub → `poll-origin.ps1` (≤2 min) → `git pull --ff-only` →
> `.githooks/post-merge` → `refresh-backend.ps1` → backend restarts on new code

The chat web UI ([`backend/static/chat.html`](backend/static/chat.html))
detects the brief restart window via a `/healthz` heartbeat and shows a
banner with an ETA so users see "*backend is refreshing — resuming in
~Xs*" instead of a confusing "Failed to fetch" error.

All operator instructions — daily commands, cloudflared ingress snippet,
log locations, model update policy, uninstall — live in:

```powershell
pwsh .\LocalAIStack.ps1 -Help
```

## What ships

| Path | What it is |
|---|---|
| [`LocalAIStack.ps1`](LocalAIStack.ps1) | One-file launcher: `-InitEnv`, `-Setup`, `-Start`, `-Stop`, `-Build`, `-BuildInstaller`, `-CheckUpdates`, `-Admin`, `-Test`, `-Help` |
| [`backend/`](backend/) | FastAPI app on `:18000` (chat SSE, auth, admin, RAG, memory, VRAM scheduler) |
| [`gui/`](gui/) | PySide6 native desktop app (chat, admin, diagnostics, metrics, setup wizard, system tray) |
| [`config/`](config/) | YAML-driven configuration (tiers, sources, router, VRAM, auth, tools) |
| [`tools/`](tools/) | 90+ discoverable tool modules (web, finance, science, data, dev) |
| [`tests/`](tests/) | Pytest suite + `local_health.py` health check (CI runs without GPU) |
| [`installer/`](installer/) | Inno Setup script + PyInstaller spec for the Windows installer |
| [`scripts/steps/`](scripts/steps/) | Helpers dot-sourced by `LocalAIStack.ps1` |
| `vendor/` *(generated)* | Pinned Qdrant + llama-server binaries and three Python venvs |
| `data/` *(generated)* | SQLite, encrypted histories, Qdrant storage, resolved-model cache, `.env` |

---

## GUI overview

The native desktop app lives in [`gui/`](gui/) and is built on PySide6
(no embedded browser, no JavaScript). Six windows cover the complete
operator surface; below is each one with its current visual.

### Setup wizard — first run

[`gui/windows/setup_wizard.py`](gui/windows/setup_wizard.py) — a 7-page
QWizard that runs automatically when `.env` is missing or no admin user
exists. Walks the operator through prerequisite checks (Python 3.12+,
cloudflared, NVIDIA driver), admin account creation, auto-generated
secrets, optional Cloudflare Tunnel provisioning, optional SMTP, and
the initial model pull. State is persisted to `data/.wizard_state.json`
so a crash mid-wizard doesn't lose typed input.

<p align="center">
  <img src="docs/images/setup-wizard.svg" alt="Setup wizard — Public access page" width="780"/>
</p>

### Chat — web (default) and native (airgap)

The canonical chat surface is the FastAPI-served web UI at
[`backend/static/chat.html`](backend/static/chat.html), reached over the
Cloudflare tunnel at `chat.<your-domain>` once provisioned. The Qt
[`ChatWindow`](gui/windows/chat.py) shows a guidance card pointing
users there.

When **airgap mode** is toggled on from the admin dashboard, the Qt
window swaps in-place to a full local chat UI: tier picker, reasoning
toggle, streaming markdown via [`MarkdownView`](gui/widgets/markdown_view.py)
(60 ms batched flush so token streams don't re-parse the whole document),
multi-agent visibility, and per-chat overrides. A `QTimer` polls
`/api/airgap` every 5 s and swaps modes live.

<p align="center">
  <img src="docs/images/frontend-chat.svg" alt="Chat UI mid-collaborative-multi-agent run" width="900"/>
</p>

### Admin dashboard

[`gui/windows/admin.py`](gui/windows/admin.py) is a full-fidelity
operator console — direct write-parity with what was previously a
Preact admin SPA. Nine tabs:

| Tab | What it controls |
|---|---|
| **Users** | Add / edit / promote / delete accounts; per-user password reset |
| **Models** | Live pull progress per tier, sourced from `/admin/model-pull-status` (5 s poll); replace any tier with a different GGUF |
| **Tools** | Toggle each of the 90+ tools on/off; see manifest + default-enabled set from [`config/tools.yaml`](config/tools.yaml) |
| **Airgap** | Switch between hosted (`chat.<domain>`) and on-device chat |
| **VRAM** | Per-tier residency table; mirrors `/vram` |
| **Router** | Multi-agent settings (min/max workers, tier choices, interaction mode, refinement rounds) and slash-command rules |
| **Auth** | Allowed email domains, session TTLs, rate limits |
| **Errors** | Recent backend exceptions (4-column timestamped log) |
| **Reload** | Hot-reload `config/*.yaml` without restarting the backend |

<p align="center">
  <img src="docs/images/admin-dashboard.svg" alt="Admin dashboard — Models tab + VRAM card" width="900"/>
</p>

#### Multi-agent orchestration (Router tab)

The Versatile MoE tier acts as an orchestrator: complex prompts are
decomposed into 2–5 parallel subtasks executed on the Fast tier and
synthesized back. Two interaction modes:

- **Independent** — classic parallel fan-out
- **Collaborative** — workers see each other's drafts and refine over
  N rounds before synthesis

<p align="center">
  <img src="docs/images/admin-multi-agent.svg" alt="Multi-agent workflow + live test runner" width="900"/>
</p>

### Metrics — live VRAM chart

[`gui/windows/metrics.py`](gui/windows/metrics.py) opens a QtCharts
window that polls `/api/vram` every 2 s, keeps a 60-sample sliding
window per tier, and renders one `QLineSeries` per tier on a 0–48 GB
y-axis. The polling task cancels cleanly on close.

<p align="center">
  <img src="docs/images/metrics.svg" alt="Metrics window — VRAM per tier line chart" width="780"/>
</p>

### Diagnostics — health check viewer

[`gui/windows/diagnostics.py`](gui/windows/diagnostics.py) is spawned by
`tests/local_health.py` after the suite finishes. Color-coded tree
(green PASS / amber WARN / red FAIL / grey SKIP), selecting a row
reveals full detail and a fix hint. Failures with a registered fix
hook are auto-fixable from the toolbar.

<p align="center">
  <img src="docs/images/diagnostics.svg" alt="Diagnostics window — health results with auto-fix" width="900"/>
</p>

Run it directly with `.\LocalAIStack.ps1 -Test` (add `-Fix` to auto-apply
known fixes; add `-Area cloudflared` to scope to one area).

### Login dialog

[`gui/windows/login.py`](gui/windows/login.py) — a modal `QDialog` shown
before any window that requires an admin session. Authentication runs on
a `QThread` (not asyncio) to avoid deadlocking Qt's modal event loop.
QSettings persists the last username; the password is never stored.

<p align="center">
  <img src="docs/images/login-dialog.svg" alt="Admin sign-in dialog" width="520"/>
</p>

### System tray

[`gui/widgets/tray.py`](gui/widgets/tray.py) installs a `QSystemTrayIcon`
with shortcuts to open Chat, Admin, Metrics, view logs, and quit. The
tray icon swaps between **airgap OFF** and **airgap ON** every 5 s so
the operator always knows which mode is live without opening a window.

---

## Architecture

Services that used to run in containers now run as tracked subprocesses.
PIDs are written to `%APPDATA%\LocalAIStack\pids.json` so `-Stop`
terminates exactly what was started.

| Service | Port | How it runs |
|---|---|---|
| `backend` (FastAPI) | 18000 | uvicorn (venv-backend) |
| `llama-server` (vision) | 8001 | Vendored binary, **pre-spawned** at boot |
| `llama-server` (embedding) | 8090 | Vendored binary, **pre-spawned** at boot, `--embedding` |
| `llama-server` (chat tiers) | 8010-8013 | Vendored binary, **cold-spawned** by `VRAMScheduler` on first request |
| `qdrant` | 6333 | Vendored binary (`vendor/qdrant/`) |
| `jupyter` | 8888 | venv-jupyter subprocess (sandbox for the `jupyter_tool`) |
| `cloudflared` | — | Optional Windows service (installed by the wizard) |
| `gui` | — | PySide6 app, no listening port |

The launcher dot-sources [`scripts/steps/`](scripts/steps/) for setup
helpers (prereq install, binary downloads, venv creation, CUDA runtime
provisioning). Pinned versions (`b8992` llama-server, `v1.12.4` Qdrant)
are SHA256-verified.

## Tiers

All six tiers live in [`config/models.yaml`](config/models.yaml). Every
tier runs with `--cache-type-k q8_0 --cache-type-v q8_0 -fa --jinja`,
so context windows are pushed to each model's native max within a 24 GB
card budget. Vision and embedding are pinned and pre-spawned; chat
tiers cold-spawn on first request via the
[`VRAMScheduler`](backend/vram_scheduler.py), with `versatile` and
`fast` warmed sequentially on user connect (login or page-mount via
`POST /api/warm`). All `.gguf` files are also pre-warmed into the OS
page cache by [`scripts/warm-page-cache.ps1`](scripts/warm-page-cache.ps1)
at `-Start`, so even tiers that aren't auto-warmed into VRAM avoid
disk-read latency on cold-spawn.

Tiers are grouped in the chat UI's tier dropdown by `category` (set on
each tier in `models.yaml`). Currently two groups exist:

- **Reasoning** — `highest_quality`, `reasoning_max`, `reasoning_xl`. Picked
  when you need maximum capability and accept slower inference.
- **Coding** — `coding` (with switchable 30B / 80B sub-variants via the
  `/coder small | big` slash commands).

Everything else (`versatile`, `fast`, `vision`) renders at the top level
of the dropdown.

| Category | Tier | Model | Quant | Port | `--ctx-size` | VRAM | Role |
|---|---|---|---|---|---|---|---|
| **Reasoning** | `highest_quality` | Qwen3-Next 80B-A3B Thinking | UD-Q4_K_XL (Unsloth Dynamic) | 8010 | 131 072 (YaRN ×4) | ~14.5 GB VRAM + ~33 GB RAM | Default heavy-reasoning. MoE w/ expert offload + spec decode |
| **Reasoning** | `reasoning_max` | OpenAI GPT-OSS-120B | Q4_K_M (sharded ×2) | 8014 | 131 072 | ~14 GB VRAM + ~50 GB RAM | Opt-in. Highest peak quality on hard reasoning, slower (no spec decode — different tokenizer) |
| **Reasoning** | `reasoning_xl` | Qwen3.5 397B-A17B | UD-IQ2_M (Unsloth Dynamic, sharded ×4) | 8015 | 65 536 | ~14 GB VRAM + ~110 GB RAM | Top open-weight reasoning at IQ2_M. Active 17 B / 397 B w/ expert offload + spec decode |
| **Coding** | `coding` | Qwen3-Coder 30B-A3B (default) / Qwen3-Coder-Next 80B-A3B (`/coder big`) | UD-Q4_K_XL (30B) / UD-Q4_K_XL sharded (80B) | 8013 | 131 072 (YaRN ×4) | ~6.5 / ~14.5 GB | Coding tier with switchable 30 B / 80 B variants |
| (top-level) | `versatile` | Qwen3.6 35B-A3B (MoE) | UD-Q4_K_XL (Unsloth Dynamic) | 8011 | 131 072 (YaRN ×4) | ~6.5 GB | Default + orchestrator (3 slots, expert offload, spec decode) |
| (top-level) | `fast` | Qwen3.5 9B | UD-Q4_K_XL (Unsloth Dynamic) | 8012 | 65 536 | ~7.5 GB | Multi-agent workers (4 slots, dense + spec decode) |
| (top-level) | `vision` | Qwen3.6 35B + mmproj | UD-Q4_K_XL + BF16 mmproj | 8001 | 65 536 (YaRN ×2) | ~6.5 GB | Images / charts (expert offload, spec decode) |
| (hidden) | `embedding` | Qwen3-Embedding-4B | Q4_K_M | 8090 | 32 768 | ~2.8 GB | RAG + memory distillation (2 560-dim, MTEB ~70.0) — hidden from chat dropdown |
| (draft) | `draft_qwen3_06b` | Qwen3-0.6B | UD-Q4_K_XL | — | — | ~0.5 GB | Universal speculative-decode draft for every Qwen3-family chat tier |
| (rerank) | `reranker` | Qwen3-Reranker-0.6B | Q8_0 | 8091 | — | ~1 GB | RAG retrieval reranker (`--reranking --pooling rank`) |

**On quants.** "UD" = Unsloth Dynamic — keeps critical layers
(attention, embed, output) at higher bpw than the headline quant
suggests. UD-Q4_K_XL ≈ Q4_K_M with attention layers up-quantized;
UD-IQ2_M ≈ IQ2_M with attention up-quantized. The result is measurably
better coherence than the plain quant at the same disk size, which
matters most on extreme low-bit MoE tiers like `reasoning_xl`.

**Heavier tiers we don't ship today:**
- **Kimi K2.6 (~1 T)** and **DeepSeek V3.2 / V4 (~671 B)** — only fit
  at sub-2-bit quants (IQ1_M / IQ1_S) where multi-step reasoning fails
  faster than the parameter count compensates. Tracked in
  [#186](https://github.com/kitisathreat/local-ai-stack/issues/186) for
  re-evaluation when better sub-2-bit quant schemes land or RAM grows.

GGUF resolution runs on every `-Start` against
[`config/model-sources.yaml`](config/model-sources.yaml); cached results
land in `data/models/`. Slash overrides at chat time: `/tier`, `/coder`,
`/think`, `/solo`, `/swarm`.

### Speculative decoding (lossless)

Every Qwen3-family chat tier (`highest_quality`, `versatile`, `fast`,
`coding`, `vision`) runs llama.cpp speculative decoding against a tiny
**Qwen3-0.6B** draft. Speedup typically lands in the 1.5–2× range on
single-slot tiers, less on parallel-slot tiers where batch capacity is
already amortized.

llama.cpp uses the standard rejection-sampling algorithm from
[Leviathan et al. 2023](https://arxiv.org/abs/2211.17192). For each
generation step:

1. The draft model proposes K tokens (`--draft-max`).
2. The target model evaluates those K positions in **one** parallel
   forward pass.
3. Rejection sampling accepts a prefix of the draft's tokens whose
   probabilities under the target match the draft's; on rejection the
   target's own next-token distribution is sampled at the rejection
   point (corrected by subtracting the draft's contribution).

The math guarantees the **joint output distribution is identical** to
running the target model alone. There is **no quality tradeoff**, in
either greedy or temperature-sampled mode. The speedup comes purely
from amortizing the target's memory bandwidth across K parallel-
evaluated tokens. We do *not* enable any approximate-decoding modes
(lookahead, Medusa, EAGLE) — those would change the output
distribution and lose the lossless guarantee.

The `reasoning_max` tier (GPT-OSS-120B) is the lone exception: GPT-OSS
uses OpenAI's `o200k_harmony` tokenizer, which is incompatible with
the Qwen3-0.6B draft. Tokenizer compatibility is a hard requirement
for the rejection-sampling math, so this tier runs target-only.

### `/coder` — coding-tier variant toggle

The `coding` tier hosts two interchangeable Qwen3-Coder MoE models.
The default is the smaller, faster 30B; users opt into the larger 80B
per turn:

```
/coder big       refactor this whole module    → loads Qwen3-Coder-Next-80B-A3B
/coder small     one-line bugfix              → loads Qwen3-Coder-30B-A3B
/coder 80b       …                            → explicit canonical name
```

Switching variants triggers a re-spawn of the coding tier's
`llama-server`. The scheduler treats variant mismatch like a
`parallel_slots` change: evict when idle, queue when busy. Both
variants share the same `-ot` expert offload pattern, the same
Qwen3-0.6B speculative draft, and the same `q8_0` KV cache.

### Residency cascade — fitting tiers into tight VRAM

When a tier doesn't fit in free VRAM, the spawn path
([`backend/model_residency.py`](backend/model_residency.py)) walks a
cascade in order of least-perf-impact, stopping as soon as it fits:

1. **Reduce GPU layers** — push transformer blocks (or MoE experts via
   `-ot`) onto CPU. Cheapest lever for hybrid-attention / MoE tiers
   because the bandwidth-bound bits stay on GPU.
2. **Move KV cache to system RAM** (`--no-kv-offload`) — frees several
   GB at long context, costs attention-step bandwidth. Only engaged
   when (1) alone is insufficient.
3. **Halve `--ctx-size`** — last resort, halving repeatedly until it
   fits or hits `vram.residency.min_context_window` (default 4096).

Knobs live in [`config/vram.yaml`](config/vram.yaml) under
`residency:` (`enable`, `enable_kv_offload`, `enable_ctx_shrink`,
`min_context_window`). The planner is on by default; the legacy
`LAI_RESIDENCY_PLANNER=1` env var still force-enables for parity with
older deployments. Each spawn logs the chosen plan, e.g.
`Residency plan for versatile: mode=partial layers=34/40 ctx=131072 kv_offload=False reason=headroom-ok+complex`.

### VRAM probe + orphan reaper

The scheduler cross-checks its in-process projection against NVML's
actual free-VRAM reading on every fit decision, so an orphan
`llama-server` (left from a previous backend bounce or crash) can't
fool the projection into a "fits" verdict followed by an OOM. At
startup the backend also walks live `llama-server` PIDs and
SIGTERMs anything not in its own process registry, preserving ports
that belong to the launcher's pinned vision/embedding/reranker
processes. The same sweep is exposed as
`POST /admin/vram/kill-orphans`, and `GET /admin/vram/probe` returns
the diagnostic snapshot (`nvml_free_gb`, `scheduler_tracked_used_gb`,
`orphan_drift_gb`, `orphan_llama_server_pids`).

When a chat is archived or deleted, the backend checks whether any
other non-archived conversation references the same tier; if not, the
tier is evicted (unless it's in the auto-warm set). Combined with the
no-eager-load policy this enforces "models loaded only during active
sessions."

## Configuration

All runtime configuration lives in [`config/`](config/):

- [`models.yaml`](config/models.yaml) — tier definitions + aliases
- [`model-sources.yaml`](config/model-sources.yaml) — Hugging Face GGUF resolver
- [`router.yaml`](config/router.yaml) — auto-thinking / multi-agent / specialist rules
- [`vram.yaml`](config/vram.yaml) — scheduler policy (headroom, eviction, pinning)
- [`auth.yaml`](config/auth.yaml) — session TTL, allowed domains, rate limits
- [`tools.yaml`](config/tools.yaml) — tool manifest + default-enabled set
- [`runtime.yaml`](config/runtime.yaml) — backend + llama-server runtime knobs

Secrets (`AUTH_SECRET_KEY`, `HISTORY_SECRET_KEY`, SMTP creds, Hugging
Face token) live in `.env` at the repo root, written by the setup
wizard or `-InitEnv`. Never committed.

### Optional environment variables

- `LAI_RESIDENCY_PLANNER` (default: unset) — Force-enables the
  residency planner (cascade in
  [`backend/model_residency.py`](backend/model_residency.py)) even
  when `vram.residency.enable: false` is set in YAML. The planner is
  on by default now; this flag exists as a parity escape hatch for
  older deployments that intentionally turned it off.
- `OFFLINE=1` — Skips upstream HuggingFace polling; uses pinned
  revisions only.
- `MODEL_UPDATE_POLICY` — `auto`, `prompt`, or `skip` for detected
  upstream model updates.
- `RAG_EMBED_DIM` (default: `2560`) — Output dimension of the embedding
  tier. Default matches Qwen3-Embedding-4B. Override only if you swap
  the embedding model for one with a different dim. Existing Qdrant
  collections at the old dim are **incompatible** after a change — run
  `python scripts/reembed_knowledge.py` to rebuild them.

### After upgrading the embedding tier

`scripts/reembed_knowledge.py` re-embeds every user's RAG and memory
collections after a model dimension change:

```bash
python scripts/reembed_knowledge.py --dry-run     # enumerate, show plan
python scripts/reembed_knowledge.py               # do the work
python scripts/reembed_knowledge.py --user 7      # restrict to one user
```

Requires the embedding tier to be running (which the launcher's `-Start`
brings up alongside Qdrant). Points without a `chunk_text` payload are
skipped and need manual handling.

## API endpoints

OpenAI-compatible streaming chat is the headline; the rest is the
operator surface that the admin window talks to.

```
GET    /healthz                     → {status: "ok"|"degraded"}
GET    /v1/models                   → OpenAI-compatible tier list
POST   /v1/chat/completions         → SSE streaming chat (OpenAI-compatible)

POST   /auth/login                  → username + password → JWT cookie
POST   /auth/logout                 → clear session
POST   /auth/change-password        → rotate password
GET    /me                          → current user
GET    /api/airgap                  → {enabled: bool}

POST   /rag/upload                  → upload a document into per-user RAG
GET    /rag/docs                    → list uploaded documents
DELETE /rag/docs/{doc_id}           → forget a document
GET    /memory                      → list distilled memories
DELETE /memory/{id}                 → forget a memory

GET    /vram                        → current tier residency snapshot
GET    /system                      → host info (CPU, RAM, GPU)
GET    /tools                       → tool manifest

GET    /chats                       → list conversations
POST   /chats                       → create a conversation
GET    /chats/{id}                  → conversation history
PATCH  /chats/{id}                  → rename / pin
DELETE /chats/{id}                  → delete

# Admin (require admin session)
GET    /admin/overview              → dashboard counts
GET    /admin/users                 → list users
POST   /admin/users                 → create user
PATCH  /admin/users/{id}            → update user
DELETE /admin/users/{id}            → delete user
GET    /admin/model-pull-status     → live pull progress per tier
GET    /admin/usage                 → request volume + token totals
GET    /admin/errors                → recent backend exceptions
GET    /admin/vram                  → per-tier residency
GET    /admin/vram/probe            → NVML vs scheduler-tracked, orphan PIDs
POST   /admin/vram/kill-orphans     → reap stray llama-server processes
GET    /admin/tools                 → tool toggle state
PATCH  /admin/tools/{name}          → enable/disable a tool
GET    /admin/config                → resolved YAML config
PATCH  /admin/config                → write-through update
POST   /admin/reload                → hot-reload all YAML
GET    /admin/airgap                → airgap state
PATCH  /admin/airgap                → toggle airgap
```

### Tier benchmarks

[`scripts/bench_tiers.py`](scripts/bench_tiers.py) measures cold-spawn
latency and steady-state generation tok/s per tier. The script forces
a cold spawn between runs by acquiring `--evict-tier` first. Results
are written to `data/eval/tier-bench-<ts>.json` for A/B tracking
across quant swaps, llama.cpp version bumps, hardware changes, etc.

Reference numbers from the RTX Pro 4000 SFF (24 GB) reference rig,
post-merge of [#169](https://github.com/kitisathreat/local-ai-stack/pull/169) +
[#170](https://github.com/kitisathreat/local-ai-stack/pull/170) +
[#173](https://github.com/kitisathreat/local-ai-stack/pull/173)
(NVML-aware probe + residency cascade + method-scope hotfix), warm OS
page cache:

| tier | cold-load (s) | warm-first-token (s) | tok/s @ slot=1 | notes |
|---|---:|---:|---:|---|
| `versatile` | 12.85 | 1.81 | **12.3** | Qwen3.6 35B-A3B MoE, expert offload, spec-decode (+43% tok/s vs pre-merge) |
| `fast`      |  8.93 | 0.58 | 22.0 | Qwen3.5 9B dense, spec-decode |
| `coding`    | 11.05 | 1.03 |  9.7 | Qwen3-Coder-30B-A3B, expert offload, spec-decode |

The cascade-driven KV→CPU spillover frees the equivalent of ~5 GB of
VRAM at full ctx for `versatile` (3 slots, q4_0 KV), which keeps the
attention path on GPU in the partial-residency regime that previously
forced a layer downscale. Cold-load latency is roughly stable —
weights still page in from the warm OS cache in similar wall time.

`highest_quality` (Qwen3-Next 80B-A3B Thinking, ~50 GB GGUF) and
`coding_80b` (Qwen3-Coder-Next 80B, ~50 GB GGUF) bench numbers will
be added once the resolver pull completes; the resolver auto-resumes
from the partial blob in `data/models/.cache/huggingface/download/`
across CDN drops, so an interrupted multi-hour pull continues
where it left off on the next `-Start`.

Reproduce with:

```powershell
python scripts/bench_tiers.py --tiers versatile,fast,coding
python scripts/bench_tiers.py --tiers versatile,fast,coding,highest_quality
```

## Tools, RAG, memory

- **Tools.** [`tools/`](tools/) holds 90+ self-contained modules (web
  search, finance, science, dev utils, data repos). The registry is
  driven by [`config/tools.yaml`](config/tools.yaml); each tool exposes
  its JSON schema and is enable/disable-able from the admin Tools tab.
  See [Desktop integration](#desktop-integration) below for the
  filesystem / app-launcher / KiCad / Blender / Fusion 360 / FL Studio /
  Synthesizer V Studio bridge.
- **RAG.** Per-user collections in Qdrant, populated via `/rag/upload`.
  Embeddings are computed on the always-on `llama-server --embedding`
  pinned to port 8090.
- **Memory.** Every Nth turn the orchestrator distills durable facts
  from chat history and stores them per-user; relevant memories are
  injected into prompts on subsequent turns.

## Development

```powershell
# Backend in reload mode (requires Qdrant + the embedding llama-server)
.\LocalAIStack.ps1 -Start -NoGui

# Pytest suite (Linux CI — no GPU required)
python -m pytest tests/

# Local health check on the actual machine after setup
.\LocalAIStack.ps1 -Test                    # runs every area
.\LocalAIStack.ps1 -Test -Area cloudflared  # one area
.\LocalAIStack.ps1 -Test -Fix               # auto-apply known fixes

# Build the desktop app (PyInstaller) + Inno Setup installer
.\LocalAIStack.ps1 -Build
.\LocalAIStack.ps1 -BuildInstaller
```

CI is in [`.github/workflows/`](.github/workflows/):
`ci.yml` runs the pytest suite on Linux; `install-and-startup.yml`
exercises the full `-Setup` → `-Start` flow on a Windows runner.

### Project layout

```
LocalAIStack.ps1      Root launcher (setup / start / stop / build / test / help)
backend/              FastAPI app
  main.py             Endpoints, SSE producers, middleware pipeline
  admin.py            Admin endpoints (users, models, tools, config, reload)
  router.py           Tier selection + slash commands
  vram_scheduler.py   GPU residency manager (LRU + ref-count)
  orchestrator.py     Multi-agent plan/synthesize (independent + collaborative)
  rag.py              Per-user Qdrant retrieval
  memory.py           Distillation + injection
  auth.py             Password auth + JWT cookies
  airgap.py           Airgap state + middleware
  diagnostics.py      Health-check primitives (consumed by tests/local_health.py)
  history_store.py    Encrypted SQLite chat history (per-user key)
  kv_cache_manager.py llama-server KV-cache lifecycle
  model_resolver.py   Hugging Face GGUF resolver
  model_residency.py  Pin/evict policy
  metrics.py          Prometheus-style counters
  middleware/         Auth, host gate, request logging, rate limiting
  backends/           llama.cpp + future provider adapters
  static/chat.html    Web chat UI served by FastAPI
  tools/              Backend-side tool plumbing (registry, dispatcher)
gui/                  PySide6 native desktop app
  main.py             Tray + window registry + asyncio integration
  api_client.py       Typed async client for backend endpoints
  cloudflare_setup.py Tunnel provisioning helpers
  windows/            chat.py · admin.py · login.py · diagnostics.py
                      · setup_wizard.py · metrics.py
  widgets/            tray.py · markdown_view.py
config/               YAML-driven runtime configuration
tools/                Discoverable tools (one file per tool, 90+)
scripts/
  steps/              Dot-sourced helpers (prereqs, downloads, venvs, CUDA)
  prompts/            Prompt templates
  code_assist.py      Repo helper utilities
installer/            Inno Setup script + PyInstaller spec
tests/
  local_health.py     Operator-facing health check + fix hooks
  health_areas/       One file per area (backend, vram, cloudflared, …)
  test_*.py           Pytest suite (no GPU required, runs in Linux CI)
docs/
  overview.md         Architecture + tier table
  manual-setup.md     Manual install (when you don't trust the wizard)
  backend-startup.md  What happens between launcher and ready-state
  images/             SVG mockups (referenced by this README)
.github/workflows/    ci.yml · install-and-startup.yml · update-project-fields.yml
```

## Desktop integration

The seven `host_*`-tagged tools in [`tools/`](tools/) let the model reach
out of the backend process and into the host machine: read and write
files anywhere on `C:\`/`D:\`, launch programs, and drive the major
design suites you actually use day to day. They are **off by default**
— flip them on per-account from the admin Tools tab once you've
reviewed the per-tool Valves. Every desktop tool is also automatically
suppressed when **airgap mode** is on (the model never sees an offering
it can't fulfil), so you can reach for the public chat surface without
worrying about the model trying to spawn `kicad.exe` over a tunnel.

| Tool file | What the model can do |
|---|---|
| [`tools/filesystem.py`](tools/filesystem.py) | Browse, read (text + binary base64), search, hash, copy, move, write, append, delete files on the host. Allow-list of root directories (default `C:\`, `D:\`, `~`); blocks `Windows\`, `Program Files\WindowsApps\`, `$Recycle.Bin\`, etc. Writes and deletes require flipping `WRITE_ENABLED` / `DELETE_ENABLED` in the Valves. |
| [`tools/app_launcher.py`](tools/app_launcher.py) | Launch any program registered in `APPS` (or arbitrary executables when `ALLOW_ARBITRARY_EXEC` is on), open files in the OS-default handler, list and terminate processes. Spawns are detached — the model gets a PID back. |
| [`tools/kicad.py`](tools/kicad.py) | Open `.kicad_pro` / `.kicad_sch` / `.kicad_pcb` in the GUI; run `kicad-cli` (KiCad 7+) headlessly: `run_erc`, `run_drc`, `export_gerbers`, `export_drill`, `export_step`, `export_schematic_pdf`, `export_bom`, `export_netlist`. |
| [`tools/blender.py`](tools/blender.py) | Open `.blend` files in Blender's GUI, or run arbitrary Python in Blender's bundled interpreter (full `bpy` API) headlessly via `blender -b -P`. Convenience wrappers for `render_frame`, `render_animation`, `export_model` (glb/gltf/fbx/obj/stl/usd/abc), and `scene_info`. |
| [`tools/fusion360.py`](tools/fusion360.py) | Open Fusion 360 (or `.f3d` / `.f3z` files), and install Python scripts and add-ins into Fusion's standard `%APPDATA%\Autodesk\Autodesk Fusion 360\API\Scripts` and `\AddIns` folders (with manifests). Add-ins can be set to auto-load on Fusion launch. |
| [`tools/fl_studio.py`](tools/fl_studio.py) | Open `.flp` projects and `.mid` files in FL Studio. Render projects to WAV/MP3/OGG/FLAC headlessly via `FL64.exe /R`. Install MIDI Scripting controller-surface scripts. Optionally pipe live MIDI to FL Studio's loopback port via `mido` + `python-rtmidi`. |
| [`tools/synthv_studio.py`](tools/synthv_studio.py) | Open Synthesizer V Studio Pro projects (`.svp` / `.s5p`); batch-render to WAV via `synthv-cli` (or `synthv-studio --batch-render`); install JavaScript automation scripts into the user `scripts/` directory so they appear under Scripts → User. |

### Enabling them

1. In the GUI, open the admin window → **Tools tab** → tick the seven
   `default_enabled: false` rows under `tools/filesystem.py`,
   `tools/app_launcher.py`, etc.
2. Click each tool's row to expand its Valves. Adjust `ALLOWED_ROOTS`,
   executable paths (`KICAD_EXE`, `BLENDER_EXE`, `FL_EXE`, …),
   `WRITE_ENABLED`, `DELETE_ENABLED`, and `ALLOW_ARBITRARY_EXEC` to
   match your install.
3. The settings persist across restarts via the same `config/tools.yaml`
   surface as the rest of the registry. No code changes needed.

### Entertainment & media tools (Phase 9b)

A second cluster of opt-in tools for the model to control gaming and
music software, search torrent indexers, drive a torrent client, and
pull legitimately-free music. Same posture as the design tools:
`default_enabled: false` and (for host-touching ones) declared with a
`host_*` service so airgap mode strips them.

| Tool file | What the model can do |
|---|---|
| [`tools/steam.py`](tools/steam.py) | Launch installed games via `steam://run/<appid>`, list installed games by parsing `libraryfolders.vdf` + `appmanifest_*.acf`, search the Steam Store, and (with a free Web API key) read a public profile's owned-games / recently-played / summary. |
| [`tools/musicbee.py`](tools/musicbee.py) | Drive MusicBee via its CLI: launch, play/pause/next/previous, mute, set volume, open or queue a file, list playlists, locate the library DB. |
| [`tools/spotify.py`](tools/spotify.py) | Spotify Web API. Two modes: client-credentials (public catalogue search, album/track lookup) and user OAuth (now-playing, play/pause, next/prev, queue, volume, my-playlists, add-to-playlist). Refresh tokens are rotated transparently and never logged. |
| [`tools/torrent_search.py`](tools/torrent_search.py) | Unified torrent discovery across YTS (movies, official JSON), EZTV (TV, JSON), Nyaa.si (anime, RSS), apibay.org (general / The Pirate Bay JSON), and the Internet Archive's public-domain torrent collection. Optional Jackett/Prowlarr meta-search hits 100+ trackers at once. Returns magnet URIs / `.torrent` URLs. |
| [`tools/qbittorrent.py`](tools/qbittorrent.py) | qBittorrent Web API client: add (magnet, URL, or local `.torrent` file), list+filter the queue, pause/resume/delete (optionally with file removal), set per-torrent download limits, fetch global stats. Cookie auth is refreshed automatically. |
| [`tools/free_music.py`](tools/free_music.py) | Search and download legitimately-free music: Free Music Archive (CC indie), Internet Archive Audio (public-domain + CC concerts, broadcasts, 78rpm — often FLAC), Jamendo (CC, 600k+ artists, FLAC with a free key). Streams audio straight to disk. |

Existing music-related tools `tools/qobuz_dl.py` (Qobuz Hi-Res via the
`qobuz-dl` CLI) and `tools/soulseek.py` (Soulseek P2P via `slskd`) are
still in place; the Phase 9b tools sit alongside them.

#### Where to find films / TV

`torrent_search.search_movies(query=…)` calls YTS's open JSON API and
returns top-seed releases per quality tier. `torrent_search.search_tv(imdb_id=…)`
calls EZTV's JSON. For anime, `search_anime` hits Nyaa's RSS. For
public-domain or CC-licensed films that you can grab without a
torrent-legality concern at all, use
`torrent_search.search_internet_archive(media_type="movies", …)` —
the result is a real `_archive.torrent` URL pointing at content the
Internet Archive distributes legally.

### Safety posture

- **Allow-list, not deny-list.** The filesystem tool refuses any path
  outside `ALLOWED_ROOTS`. The app launcher refuses any executable
  outside `APPS` unless explicitly opened up.
- **Off by default.** Every desktop tool ships with
  `default_enabled: false` so a fresh deploy can't accidentally hand
  the model `C:\` write access.
- **Airgap-aware.** All seven tools declare `requires_service:
  host_filesystem` or `host_processes`. Airgap mode strips them from
  the schema, and the dispatcher refuses calls to them mid-flight.
- **Writes / deletes require dual opt-in.** The tool must be enabled
  *and* `WRITE_ENABLED` / `DELETE_ENABLED` must be flipped before any
  mutation can run.

## Roadmap & contributing

- [#34 Admin platform & config](https://github.com/kitisathreat/local-ai-stack/issues/34)
- [#36 Scaling & performance](https://github.com/kitisathreat/local-ai-stack/issues/36)
- [#37 Tooling quality & tests](https://github.com/kitisathreat/local-ai-stack/issues/37)
- [#38 Docs & security](https://github.com/kitisathreat/local-ai-stack/issues/38)
- [#39 Stability & correctness](https://github.com/kitisathreat/local-ai-stack/issues/39)

## Phase history

- **Phase 0** — Docker-compose + Preact scaffolding (later removed)
- **Phase 1** — Backend-agnostic tier router + VRAM scheduler + multi-agent orchestrator
- **Phase 4** — Auth + per-user storage
- **Phase 5** — Tool registry, per-user RAG, memory distillation
- **Phase 6** — Cloudflare Tunnel, middleware migration, airgap toggle
- **Phase 7** — Native Windows migration: no Docker, PySide6 GUI, setup
  wizard, Inno Setup installer, local health-check suite
- **Phase 8** — Migration from Ollama to native llama.cpp for all tiers,
  unlocking native-max context windows via KV-cache quantization

## License

See repository settings.
