# Local AI Stack

Self-hosted multi-model LLM stack for a single Windows machine. A FastAPI
backend (`:18000`) routes each request to one of six llama.cpp tiers via a
VRAM-aware scheduler, with a multi-agent orchestrator, per-user RAG +
memory, and an OpenAI-compatible SSE endpoint. Ships as a PowerShell
launcher (`LocalAIStack.ps1`) plus a PySide6 desktop app. No Docker, no
embedded browser, no Electron.

<p align="center">
  <img src="docs/images/launcher-window.svg" alt="LocalAIStack.ps1 console output" width="700"/>
</p>

## Requirements

- Windows 10/11 with an NVIDIA GPU (reference rig: RTX Pro 4000 SFF, 24 GB).
- 24 GB+ VRAM for the default chat tiers; 125 GB+ system RAM for the
  large MoE reasoning tiers (`reasoning_xl`, `frontier`); NVMe spillover
  for `frontier`.
- **PowerShell 7+** (`pwsh`). Windows PowerShell 5.1 is unsupported —
  the launcher uses em-dashes that 5.1's Windows-1252 parser breaks on.
  `winget install --id Microsoft.PowerShell` if missing.
- Python 3.12+, NVIDIA driver, optional `cloudflared` for tunnel ingress.

## What it does

- **Chat over six tiers** (four chat, one vision, one embedding). Tier
  picked automatically by router rules or via slash commands (`/tier`,
  `/coder`, `/think`, `/solo`, `/swarm`).
- **Multi-agent orchestration.** The Versatile MoE tier decomposes
  complex prompts into 2–5 subtasks on the Fast tier and synthesizes the
  result. Independent (parallel fan-out) or collaborative (workers see
  each other's drafts over N rounds).
- **Per-user RAG + memory.** Documents go into per-user Qdrant
  collections via `/rag/upload`; durable facts are distilled from chat
  history every Nth turn and re-injected.
- **OpenAI-compatible API.** `POST /v1/chat/completions` with SSE
  streaming. `GET /v1/models` lists the tier IDs.
- **90+ tool modules.** Web search, finance, science, dev utils, data
  repos. A separate cluster of `host_*` tools (filesystem, app launcher,
  KiCad, Blender, Fusion 360, FL Studio, Synthesizer V) bridges into the
  host machine; off by default.
- **Speculative decoding (lossless).** Every Qwen3-family chat tier
  drafts against Qwen3-0.6B; output distribution is identical to running
  the target alone (Leviathan et al. 2023). 1.5–2× speedup typical on
  single-slot tiers. `reasoning_max` (GPT-OSS-120B) is target-only —
  tokenizer mismatch.
- **Cloudflare Tunnel ingress (optional).** Web chat at
  `chat.<your-domain>` provisioned by the setup wizard.
- **Airgap mode.** Toggle from the admin dashboard. Strips host-touching
  tools from the schema and swaps the chat surface to fully local.

## What it does not do

- **No Linux / macOS support.** Windows-only; the launcher and several
  paths are Windows-specific. CI does run pytest on Linux without GPU.
- **No Docker.** Services run as tracked subprocesses; PIDs go to
  `%APPDATA%\LocalAIStack\pids.json`.
- **No multi-host / cluster mode.** Single machine.
- **No fine-tuning or training.** Inference only.
- **No GPU other than NVIDIA.** llama.cpp CUDA build; no ROCm path
  shipped.
- **`reasoning_max` skips speculative decoding** (different tokenizer
  from the Qwen3 draft).
- **`frontier` is not interactive.** Pagefile-gated at IQ1_S; expect
  0.5–2 t/s. The REAP-345B variant fits in 125 GB RAM but is still
  fire-and-forget.
- **Embedding tier swaps require a re-embed.** Existing Qdrant
  collections at the old dim are incompatible — run
  `scripts/reembed_knowledge.py`.

## Operating

All operator commands run through `LocalAIStack.ps1`. Run from the
repo root in `pwsh`. `pwsh .\LocalAIStack.ps1 -Help` prints the
canonical reference; the snippets below are the day-to-day workflows.

### First-time setup

```powershell
pwsh .\LocalAIStack.ps1 -InitEnv   # write default .env at repo root
# Edit .env: AUTH_SECRET_KEY, HISTORY_SECRET_KEY, optional BRAVE_API_KEY / HF_TOKEN
pwsh .\LocalAIStack.ps1 -Setup     # install prereqs, download binaries, create venvs, pull GGUFs
pwsh .\LocalAIStack.ps1            # alias for -Start; opens the Qt GUI
```

`-Setup` installs (with one-time UAC prompts): Git, Python 3.12,
PowerShell 7, cloudflared. CUDA 12 runtime is bundled. NVIDIA driver
≥550 is detected but never auto-installed. Vendored binaries
(`qdrant.exe`, `llama-server.exe`) are downloaded + SHA256-verified
into `vendor/`. Three Python venvs (`venv-backend`, `venv-gui`,
`venv-jupyter`) are created under `vendor/`. The setup wizard runs on
first `-Start` if `.env` is missing or no admin user exists.

### Start / stop the stack

```powershell
pwsh .\LocalAIStack.ps1                    # start everything + Qt GUI
pwsh .\LocalAIStack.ps1 -Start -NoGui      # start backend + services without the GUI
pwsh .\LocalAIStack.ps1 -Start -NoUpdateCheck   # skip HF model-update polling
pwsh .\LocalAIStack.ps1 -Start -Offline    # also skip cloudflared + upstream model polls (CI / airgap)
pwsh .\LocalAIStack.ps1 -Stop              # terminate every tracked PID from pids.json
```

`-Start` brings up qdrant, the pre-spawned llama-servers (vision,
embedding, reranker), jupyter-lab, the FastAPI backend, and (unless
`-NoGui`) the Qt app. The four chat tiers cold-spawn on first request
via the `VRAMScheduler` — no pre-spawn at boot. PIDs land in
`%APPDATA%\LocalAIStack\pids.json` so `-Stop` only kills what was
started.

### Reach the chat UI

The web chat UI (`backend/static/chat.html`) is served by the FastAPI
backend on `:18000` and is the canonical surface. Three ways in:

```powershell
# 1. Public hostname via Cloudflare Tunnel (set up below): https://chat.<your-domain>
# 2. Native desktop window — the Qt app rendering the same web UI:
pwsh .\LocalAIStack.ps1 -DesktopChat
# 3. Loopback (admin only; chat is host-gated):
#    http://127.0.0.1:18000/  works only when CHAT_HOSTNAME=localhost or airgap mode is on
```

The `-Start` Qt window is the operator console (admin / metrics /
diagnostics / setup wizard). For everyday chat, use the browser at
`chat.<your-domain>` or `-DesktopChat`. **In airgap mode**, the Qt
chat window swaps in-place to a fully local chat surface.

### Cloudflare Tunnel

`-Start` brings up cloudflared automatically: as a Windows service if
the setup wizard installed it that way, otherwise inline via
`cloudflared tunnel run` reading `~/.cloudflared/config.yml`.

Chat is host-gated, so the ingress hostname **must** match the
`CHAT_HOSTNAME` env var (default `chat.mylensandi.com`). List the chat
hostname **before** any wildcard or `http_status:404` fallback —
cloudflared evaluates rules top-to-bottom.

```yaml
# ~/.cloudflared/config.yml
ingress:
  - hostname: chat.your-domain.com
    service: http://localhost:18000
  - service: http_status:404
```

The setup wizard's "Public access" page can provision a tunnel +
DNS record automatically using your Cloudflare API token. To do it
manually, follow `cloudflared tunnel login` →
`cloudflared tunnel create local-ai-stack` → write the config above →
`cloudflared tunnel route dns local-ai-stack chat.your-domain.com` →
`cloudflared service install` (if you want it to run as a service).

### Restart the backend after a code change

```powershell
git pull                                   # if you set core.hooksPath, the post-merge hook does the rest
.\scripts\refresh-backend.ps1              # diff requirements.txt, pip install, bounce backend
pwsh .\LocalAIStack.ps1 -Stop              # full stop
pwsh .\LocalAIStack.ps1                    # full restart
```

Opt into the auto-refresh git hook so future `git pull`s restart the
backend with the new code:

```powershell
git config core.hooksPath .githooks
```

This wires up [`.githooks/post-merge`](.githooks/post-merge) →
[`scripts/refresh-backend.ps1`](scripts/refresh-backend.ps1): diffs the
pull, `pip install`s any `requirements.txt` changes, bounces the
backend. Gated to fire only on pulls from `origin`.

To auto-pull on every PR squash+merge, register the polling task — it
checks origin every 2 minutes and ff-pulls when a new commit lands:

```powershell
pwsh .\scripts\install-auto-pull.ps1                 # default: every 2 min
pwsh .\scripts\install-auto-pull.ps1 -IntervalMinutes 5
pwsh .\scripts\install-auto-pull.ps1 -Uninstall
```

The chat web UI detects the restart window via a `/healthz` heartbeat
and shows a refresh banner instead of "Failed to fetch".

### After a Windows reboot

Nothing is registered as a Windows autostart service except cloudflared
(when installed via the wizard). On boot:

```powershell
pwsh .\LocalAIStack.ps1                    # bring everything back up
```

Tunnel and DNS persist across reboots. Encrypted SQLite history,
RAG documents, and per-user memory all live under `data/` and survive.
The auto-pull scheduled task (if installed) starts itself.

### Update model weights

```powershell
pwsh .\LocalAIStack.ps1 -CheckUpdates      # poll Hugging Face for new revisions per tier
```

`-Start` also runs the resolver against `config/model-sources.yaml` on
every boot. `MODEL_UPDATE_POLICY` in `.env` controls the verdict:

- `auto` — download immediately
- `prompt` — GUI dialog asks the user (default)
- `skip` — note it, don't download until next `-Setup`

Set `OFFLINE=1` in `.env` to never poll upstream and always use the
pinned revisions in `config/model-sources.yaml`.

### Uninstall / reset

```powershell
pwsh .\LocalAIStack.ps1 -Stop
Remove-Item -Recurse $env:APPDATA\LocalAIStack
Remove-Item -Recurse .\vendor .\data       # blows away models, DB, history, RAG, .env
```

`data/lai.db` holds users, chats, memories, and RAG metadata. Deleting
it resets the install — re-run `-Setup` to re-seed an admin.

### Logs

```
%APPDATA%\LocalAIStack\logs\<service>.log    Per-service stdout/stderr
%APPDATA%\LocalAIStack\pids.json             Tracked PIDs (used by -Stop)
data/eval/tier-bench-<ts>.json               Bench output
```

## Architecture

| Service | Port | How it runs |
|---|---|---|
| `backend` (FastAPI) | 18000 | uvicorn (venv-backend) |
| `llama-server` (vision) | 8001 | Vendored binary, pre-spawned at boot |
| `llama-server` (embedding) | 8090 | Vendored binary, pre-spawned at boot, `--embedding` |
| `llama-server` (reranker) | 8091 | Vendored binary, pre-spawned at boot |
| `llama-server` (chat tiers) | 8010-8016 | Cold-spawned by `VRAMScheduler` on first request |
| `qdrant` | 6333 | Vendored binary (`vendor/qdrant/`) |
| `jupyter` | 8888 | venv-jupyter subprocess (sandbox for the `jupyter_tool`) |
| `cloudflared` | — | Optional Windows service (installed by the wizard) |
| `gui` | — | PySide6 app, no listening port |

Pinned versions (`b9012` llama-server, `v1.12.4` Qdrant) are SHA256-verified.

## Tiers

Defined in [`config/models.yaml`](config/models.yaml). All run with
`--cache-type-k q8_0 --cache-type-v q8_0 -fa --jinja` to push context to
each model's native max within a 24 GB budget.

| Category | Tier | Model | Quant | Disk | Port | Ctx | VRAM + RAM | Role |
|---|---|---|---|---:|---|---|---|---|
| Reasoning | `highest_quality` 💾 | Qwen3-Next 80B-A3B Thinking | UD-Q4_K_XL | 43 GB | 8010 | 131072 | ~14.5 GB + 33 GB | Default heavy reasoning |
| Reasoning | `reasoning_max` 💾 | OpenAI GPT-OSS-120B | UD-Q4_K_XL ×2 | 59 GB | 8014 | 131072 | ~14 GB + 50 GB | Highest peak quality. No spec decode |
| Reasoning | `reasoning_xl` 💾 | Qwen3.5 397B-A17B | UD-IQ2_M ×4 | 115 GB | 8015 | 65536 | ~14 GB + 110 GB | Top open-weight reasoning |
| Reasoning | `frontier` 💾⚠️ | DeepSeek V3.2 (UD-IQ1_S ×4) **or** REAP-345B IQ1_S_L ×236 | — | 171 / 93 GB | 8016 | 32768 | ~7 GB + 110 GB | Aspirational, NVMe-paged |
| Coding | `coding` | Qwen3-Coder 30B-A3B (`/coder small`, default) / 80B-A3B (`/coder big`) | UD-Q4_K_XL | 18 / 49 GB | 8013 | 131072 | ~6.5 / 14.5 GB | Coding tier |
| (top) | `versatile` | Qwen3.6 35B-A3B MoE | UD-Q4_K_XL | 20 GB | 8011 | 131072 | ~6.5 GB | Default chat + orchestrator |
| (top) | `fast` | Qwen3.5 9B | UD-Q4_K_XL | 5.3 GB | 8012 | 65536 | ~7.5 GB | Multi-agent workers |
| (top) | `vision` | Qwen3.6 35B + mmproj | UD-Q4_K_XL + BF16 | 21 GB | 8001 | 65536 | ~6.5 GB | Images / charts |
| (hidden) | `embedding` | Qwen3-Embedding-4B | Q4_K_M | 2.4 GB | 8090 | 32768 | ~2.8 GB | RAG + memory (2560-dim) |
| (draft) | `draft_qwen3_06b` | Qwen3-0.6B | UD-Q4_K_XL | 387 MB | — | — | ~0.5 GB | Speculative draft for Qwen3 tiers |
| (rerank) | `reranker` | Qwen3-Reranker-0.6B | Q8_0 | 610 MB | 8091 | — | ~1 GB | RAG retrieval reranker |

Legend: 💾 = MoE expert offload to RAM via `-ot`. ⚠️ = NVMe spillover required.

UD = Unsloth Dynamic — keeps attention/embed/output layers at higher
bpw than the headline quant suggests.

### Residency cascade

When a tier doesn't fit free VRAM,
[`backend/model_residency.py`](backend/model_residency.py) walks a
cascade in order of least-perf-impact, stopping when it fits:

1. **Reduce GPU layers** — push transformer blocks (or MoE experts via
   `-ot`) onto CPU.
2. **Move KV cache to system RAM** (`--no-kv-offload`).
3. **Halve `--ctx-size`** down to `vram.residency.min_context_window`
   (default 4096).

Knobs in [`config/vram.yaml`](config/vram.yaml).

### VRAM probe + orphan reaper

The scheduler cross-checks its in-process projection against NVML's
actual free-VRAM reading on every fit decision. At startup it
SIGTERMs any `llama-server` PID not in its own registry (preserving
the launcher's pinned vision/embedding/reranker processes). Same
sweep is exposed as `POST /admin/vram/kill-orphans`; diagnostics via
`GET /admin/vram/probe`.

When a chat is archived/deleted the backend evicts the tier if no
other non-archived conversation references it (unless it's in the
auto-warm set).

## Configuration

All runtime config in [`config/`](config/):

- `models.yaml` — tier definitions + aliases
- `model-sources.yaml` — Hugging Face GGUF resolver
- `router.yaml` — auto-thinking / multi-agent / specialist rules
- `vram.yaml` — scheduler policy
- `auth.yaml` — session TTL, allowed domains, rate limits
- `tools.yaml` — tool manifest + default-enabled set
- `runtime.yaml` — backend + llama-server runtime knobs

Secrets (`AUTH_SECRET_KEY`, `HISTORY_SECRET_KEY`, SMTP creds, HF token)
in `.env`. Never committed.

### Environment variables

- `OFFLINE=1` — skip upstream HuggingFace polling; use pinned revisions only.
- `MODEL_UPDATE_POLICY` — `auto`, `prompt`, or `skip` for upstream model updates.
- `RAG_EMBED_DIM` (default `2560`) — output dim of the embedding tier.
  Override only if you swap to a different embedding model. **Existing
  Qdrant collections at the old dim are incompatible after a change** —
  run `python scripts/reembed_knowledge.py`.
- `LAI_RESIDENCY_PLANNER` — force-enables the residency planner even
  when `vram.residency.enable: false`. Parity escape hatch; planner is
  on by default.

## API endpoints

```
GET    /healthz                     status
GET    /v1/models                   OpenAI-compatible tier list
POST   /v1/chat/completions         SSE streaming chat (OpenAI-compatible)

POST   /auth/login                  username + password → JWT cookie
POST   /auth/logout
POST   /auth/change-password
GET    /me
GET    /api/airgap

POST   /rag/upload                  upload a doc into per-user RAG
GET    /rag/docs
DELETE /rag/docs/{doc_id}
GET    /memory                      list distilled memories
DELETE /memory/{id}

GET    /vram                        tier residency snapshot
GET    /system                      host info (CPU, RAM, GPU)
GET    /tools                       tool manifest

GET    /chats                       list conversations
POST   /chats
GET    /chats/{id}
PATCH  /chats/{id}                  rename / pin
DELETE /chats/{id}

# Admin (require admin session)
GET    /admin/overview
GET    /admin/users  POST  PATCH /{id}  DELETE /{id}
GET    /admin/model-pull-status
GET    /admin/usage
GET    /admin/errors
GET    /admin/vram
GET    /admin/vram/probe            NVML vs scheduler-tracked, orphan PIDs
POST   /admin/vram/kill-orphans
GET    /admin/tools
PATCH  /admin/tools/{name}
GET    /admin/config
PATCH  /admin/config
POST   /admin/reload                hot-reload all YAML
GET    /admin/airgap
PATCH  /admin/airgap
```

## GUI

PySide6 native app under [`gui/`](gui/). Six windows:

- **Setup wizard** ([`gui/windows/setup_wizard.py`](gui/windows/setup_wizard.py)) —
  7-page QWizard run when `.env` is missing or no admin user exists.
  Prerequisite checks, admin account, secrets, optional Cloudflare
  Tunnel + SMTP, initial model pull. State at `data/.wizard_state.json`.
- **Chat** ([`gui/windows/chat.py`](gui/windows/chat.py)) — guidance
  card pointing at the web UI in default mode; full local chat surface
  with tier picker, reasoning toggle, multi-agent visibility when
  airgap mode is on.
- **Admin** ([`gui/windows/admin.py`](gui/windows/admin.py)) — nine
  tabs: Users, Models, Tools, Airgap, VRAM, Router, Auth, Errors,
  Reload.
- **Metrics** ([`gui/windows/metrics.py`](gui/windows/metrics.py)) —
  QtCharts polling `/api/vram` every 2 s; per-tier `QLineSeries` over
  a 60-sample window.
- **Diagnostics** ([`gui/windows/diagnostics.py`](gui/windows/diagnostics.py)) —
  health-check viewer; auto-fix toolbar for failures with a registered
  fix hook.
- **Login** ([`gui/windows/login.py`](gui/windows/login.py)) — modal
  `QDialog`; auth runs on a `QThread` to avoid Qt modal deadlock.

The web chat UI ([`backend/static/chat.html`](backend/static/chat.html))
is the canonical chat surface, served by FastAPI and reached via the
Cloudflare tunnel.

## Tools, RAG, memory

- **Tools.** [`tools/`](tools/) holds 90+ self-contained modules.
  Registry driven by [`config/tools.yaml`](config/tools.yaml); each
  tool is enable/disable-able from the admin Tools tab.
- **RAG.** Per-user collections in Qdrant via `/rag/upload`. Embeddings
  computed on the always-on `llama-server --embedding` at port 8090.
- **Memory.** Every Nth turn the orchestrator distills durable facts
  from chat history; relevant memories are injected into prompts on
  subsequent turns.

### Desktop / host integration

Seven `host_*`-tagged tools reach into the host machine. **Off by
default**; enable per-account from admin → Tools, then expand each
row's Valves to set executable paths and writability. Airgap mode
strips them from the schema mid-flight.

| Tool | What the model can do |
|---|---|
| [`filesystem.py`](tools/filesystem.py) | Browse / read / search / hash / copy / move / write / append / delete files. Allow-list of root dirs (default `C:\`, `D:\`, `~`); blocks `Windows\`, `WindowsApps\`, `$Recycle.Bin\`, etc. Writes/deletes need `WRITE_ENABLED` / `DELETE_ENABLED`. |
| [`app_launcher.py`](tools/app_launcher.py) | Launch programs in `APPS` (or arbitrary executables when `ALLOW_ARBITRARY_EXEC` is on); list / terminate processes. Spawns are detached; model gets a PID. |
| [`kicad.py`](tools/kicad.py) | Open `.kicad_pro/.kicad_sch/.kicad_pcb`; headless `kicad-cli` (run_erc, run_drc, export_gerbers, export_drill, export_step, export_schematic_pdf, export_bom, export_netlist). |
| [`blender.py`](tools/blender.py) | Open `.blend` files; run arbitrary `bpy` Python headlessly. Wrappers for `render_frame`, `render_animation`, `export_model` (glb/gltf/fbx/obj/stl/usd/abc), `scene_info`. |
| [`fusion360.py`](tools/fusion360.py) | Open `.f3d/.f3z`; install scripts and add-ins into Fusion's standard API folders (with manifests; auto-load on launch optional). |
| [`fl_studio.py`](tools/fl_studio.py) | Open `.flp` / `.mid`; render to WAV/MP3/OGG/FLAC via `FL64.exe /R`; install MIDI Scripting controller surfaces; pipe live MIDI via `mido` + `python-rtmidi`. |
| [`synthv_studio.py`](tools/synthv_studio.py) | Open `.svp/.s5p`; batch-render WAV via `synthv-cli`; install JS automation scripts. |

A second cluster (Phase 9b) covers entertainment / media: `steam.py`,
`musicbee.py`, `spotify.py`, `torrent_search.py` (YTS / EZTV / Nyaa /
apibay / Internet Archive, optional Jackett/Prowlarr meta-search),
`qbittorrent.py`, `free_music.py` (FMA / Internet Archive Audio /
Jamendo). Same posture: `default_enabled: false`, `host_*`-declared
where applicable.

#### Safety posture

- **Allow-list, not deny-list.** Filesystem refuses paths outside
  `ALLOWED_ROOTS`; app launcher refuses executables outside `APPS`
  unless explicitly opened.
- **Off by default.** Fresh deploy can't accidentally hand the model
  `C:\` write access.
- **Airgap-aware.** All `host_*` tools stripped from schema in airgap
  mode; dispatcher refuses calls.
- **Dual opt-in for mutation.** Tool must be enabled *and*
  `WRITE_ENABLED` / `DELETE_ENABLED` flipped.

## Development

```powershell
.\LocalAIStack.ps1 -Start -NoGui            # backend reload mode (needs Qdrant + embedding llama-server)
python -m pytest tests/                     # pytest suite (Linux CI, no GPU required)
.\LocalAIStack.ps1 -Test                    # local health check (every area)
.\LocalAIStack.ps1 -Test -Area cloudflared  # one area
.\LocalAIStack.ps1 -Test -Fix               # auto-apply known fixes
.\LocalAIStack.ps1 -Build                   # PyInstaller build
.\LocalAIStack.ps1 -BuildInstaller          # Inno Setup installer
```

CI: [`ci.yml`](.github/workflows/ci.yml) runs pytest on Linux;
[`install-and-startup.yml`](.github/workflows/install-and-startup.yml)
exercises `-Setup` → `-Start` on a Windows runner.

### Tier benchmarks

[`scripts/bench_tiers.py`](scripts/bench_tiers.py) measures cold-spawn
latency and steady-state generation tok/s per tier. Forces a cold spawn
between runs by acquiring `--evict-tier` first. Results land in
`data/eval/tier-bench-<ts>.json`. Run each tier in its own invocation
for apples-to-apples numbers; `--tiers a,b,c,d` mode suppresses real
throughput because each subsequent tier acquires while the scheduler is
still cleaning up the previous spawn.

`coding_80b` is selected via the `coding` tier with `variant: "80b"`
in the request body (or `/coder big`); the bench script does not yet
support `--variant`.

### Project layout

```
LocalAIStack.ps1      Root launcher (setup / start / stop / build / test / help)
backend/              FastAPI app
  main.py             Endpoints, SSE producers, middleware pipeline
  admin.py            Admin endpoints (users, models, tools, config, reload)
  router.py           Tier selection + slash commands
  vram_scheduler.py   GPU residency manager (LRU + ref-count)
  orchestrator.py     Multi-agent plan/synthesize
  rag.py              Per-user Qdrant retrieval
  memory.py           Distillation + injection
  auth.py             Password auth + JWT cookies
  airgap.py           Airgap state + middleware
  diagnostics.py      Health-check primitives
  history_store.py    Encrypted SQLite chat history (per-user key)
  kv_cache_manager.py llama-server KV-cache lifecycle
  model_resolver.py   HF GGUF resolver
  model_residency.py  Pin/evict policy
  metrics.py          Prometheus-style counters
  middleware/         Auth, host gate, request logging, rate limiting
  backends/           llama.cpp + future provider adapters
  static/chat.html    Web chat UI served by FastAPI
  tools/              Backend-side tool plumbing
gui/                  PySide6 desktop app
config/               YAML runtime config
tools/                Discoverable tools (one file per tool, 90+)
scripts/
  steps/              Dot-sourced helpers (prereqs, downloads, venvs, CUDA)
  prompts/            Prompt templates
installer/            Inno Setup script + PyInstaller spec
tests/
  local_health.py     Operator-facing health check + fix hooks
  health_areas/       One file per area
  test_*.py           Pytest suite (Linux CI, no GPU)
docs/
  overview.md         Architecture + tier table
  manual-setup.md     Manual install
  backend-startup.md  What happens between launcher and ready-state
```

## Roadmap

- [#34 Admin platform & config](https://github.com/kitisathreat/local-ai-stack/issues/34)
- [#36 Scaling & performance](https://github.com/kitisathreat/local-ai-stack/issues/36)
- [#37 Tooling quality & tests](https://github.com/kitisathreat/local-ai-stack/issues/37)
- [#38 Docs & security](https://github.com/kitisathreat/local-ai-stack/issues/38)
- [#39 Stability & correctness](https://github.com/kitisathreat/local-ai-stack/issues/39)

## License

See repository settings.

---

## History

### Phases

- **Phase 0** — Docker-compose + Preact scaffolding (later removed).
- **Phase 1** — Backend-agnostic tier router, VRAM scheduler, multi-agent orchestrator.
- **Phase 4** — Auth + per-user storage.
- **Phase 5** — Tool registry, per-user RAG, memory distillation.
- **Phase 6** — Cloudflare Tunnel, middleware migration, airgap toggle.
- **Phase 7** — Native Windows migration: no Docker, PySide6 GUI, setup
  wizard, Inno Setup installer, local health-check suite.
- **Phase 8** — Migration from Ollama to native llama.cpp for all
  tiers; native-max context windows via KV-cache quantization.
- **Phase 9a** — Desktop/host integration tools (filesystem, app
  launcher, KiCad, Blender, Fusion 360, FL Studio, Synthesizer V).
- **Phase 9b** — Entertainment / media tools (Steam, MusicBee,
  Spotify, torrent search, qBittorrent, free music).

### Recent fixes (b9012 baseline, 2026-05)

- **`reasoning_xl` warmup OOM** ([#211](https://github.com/kitisathreat/local-ai-stack/pull/211)) —
  `--no-warmup` skips the empty-run warmup that was OOM'ing the
  scratch buffers (575-split graph from Gated DeltaNet hybrid attention
  + 60 layers + kv_offload). First-token latency ~100 s on cold cache;
  warmup happens implicitly during the first real request.
- **`reasoning_max` sharded spawn** ([#210](https://github.com/kitisathreat/local-ai-stack/pull/210)) —
  `_resolve_for_llama()` now resolves the canonical `<tier>.gguf`
  symlink to its `<base>-00001-of-MMMMM.gguf` target before passing
  to llama-server, so shard-pattern discovery works.
- **`highest_quality` VRAM gate** ([#210](https://github.com/kitisathreat/local-ai-stack/pull/210),
  [#215](https://github.com/kitisathreat/local-ai-stack/pull/215)) —
  `kv_offload: true` pushes KV to CPU RAM; `vram_estimate_gb` rebased
  to 13.
- **VRAM scheduler EMA poisoning** ([#202](https://github.com/kitisathreat/local-ai-stack/pull/202)) —
  observed-cost measurements clamp at 1.5× the YAML estimate, so a
  single bad reading under transient pressure can't drift the EMA into
  permanent "Cannot fit" 503s.
- **`frontier` — two unblock paths**:
  - **(a) Pagefile growth + original UD-IQ1_S** (171 GB sharded ×4).
    `pwsh .\scripts\grow-pagefile.ps1` (default 220 GB on largest free
    drive) + reboot. Post-reboot ~0.5–2 t/s (NVMe-paged active
    experts, no GPU offload).
  - **(b) REAP-345B IQ1_S_L** (92.8 GB sharded ×236, expert-pruned 671B
    → 345B from `lovedheart/DeepSeek-V3.2-REAP-345B-A37B-GGUF-Experimental`).
    Fits 125 GB RAM cleanly with 30 GB headroom; no pagefile growth
    needed. Quality cost vs full V3.2 is "experimental" per the
    publisher.

  Both land at "fire-and-forget for one hard reasoning prompt", not
  interactive. (b) is operationally cleaner.
- **llama.cpp `b8992` → `b9012`** — 5/7 tiers improved, 1 tied, 1
  marginal regression (`coding_80b` -13%, likely single-run variance).
  Pin bumped in `LocalAIStack.ps1`.

### Reference benchmarks (RTX Pro 4000 SFF 24 GB · llama.cpp b9012 · 2026-05-03)

`--tokens 220`, warm OS page cache, evicted-via-`fast` between runs
(or via `versatile` for `fast` itself).

| Tier | Cold (s) | Warm-first (s) | tok/s @ slot=1 | Δ vs b8992 |
|---|---:|---:|---:|---:|
| `fast`             |  8.4 |   0.58 | 71.0 | +24% |
| `versatile`        | 11.7 |   1.62 | 40.2 | +10% |
| `coding`           | 15.4 |   0.99 | 28.8 | +11% |
| `coding_80b`       | 31.4 |  24.00 | 17.5 | -13% |
| `highest_quality`  | 12.3 | 110.51 | 18.7 |    = |
| `reasoning_max`    | 22.4 |  24.60 | 11.0 | +13% |
| `reasoning_xl`     | 13.5 |  90.99 |  3.1 |  -6% |
| `frontier`         | depends — see "Recent fixes" above |
