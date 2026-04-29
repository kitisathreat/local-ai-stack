# Local AI Stack — native mode

A self-hosted multi-model LLM workflow that runs entirely on your Windows
machine. No Docker, no browser.

## Quickstart

```powershell
.\LocalAIStack.ps1 -InitEnv     # write a default .env (edit it: secrets, optional Brave key)
.\LocalAIStack.ps1 -Setup       # install prereqs, download binaries, create venvs, pull models
.\LocalAIStack.ps1              # start everything and open the native Qt GUI
```

All operator instructions — daily commands, cloudflared ingress snippet,
log locations, model update policy, uninstall — live in:

```powershell
.\LocalAIStack.ps1 -Help
```

## What's here

- `LocalAIStack.ps1` — setup + launcher + build + help, all in one file.
- `.env` — single environment file (created by `-InitEnv`).
- `backend/` — FastAPI API server (localhost:18000).
- `gui/` — PySide6 native desktop app (chat + admin + QtCharts metrics).
- `config/` — tier and tool YAML (`model-sources.yaml` drives the
  Hugging Face / Ollama registry resolver on every start).
- `scripts/steps/` — helpers dot-sourced by `LocalAIStack.ps1`.
- `vendor/` — created by `-Setup`: pinned Qdrant + llama-server binaries
  and three Python venvs (backend, gui, jupyter).
- `data/` — SQLite, encrypted histories, Qdrant storage, resolved-model cache.
- `docs/` — architecture overview, API reference, troubleshooting.

## Why native?

Docker and the Preact SPA have been removed in favour of a fully native Windows
stack. Services that used to run in containers now run as tracked subprocesses:

| Service | Port | How it runs |
|---|---|---|
| `backend` | 18000 | FastAPI via uvicorn (venv-backend) |
| `ollama` | 11434 | Native Windows install via winget |
| `llama-server` | 8001 | Vendored binary (`vendor/llama-server/`) |
| `qdrant` | 6333 | Vendored binary (`vendor/qdrant/`) |
| `jupyter` | 8888 | venv-jupyter subprocess |
| `cloudflared` | — | Windows service (installed by wizard) |

## Tiers

All five tiers are defined in [`config/models.yaml`](config/models.yaml).

| Tier | Model | Backend | VRAM | Role |
|---|---|---|---|---|
| `highest_quality` | Qwen3 72B | Ollama | ~24 GB | Hardest reasoning |
| `versatile` | Qwen3.6 35B-A3B (MoE) | Ollama | ~21 GB | Default + orchestrator |
| `fast` | Qwen3.5 9B | Ollama | ~7 GB | Multi-agent workers |
| `coding` | Qwen3-Coder-Next 80B-A3B | Ollama | ~24 GB | SWE-bench trained |
| `vision` | Qwen3.6 35B + mmproj | llama.cpp | ~21 GB | Images / charts |

## Configuration

All runtime config lives in [`config/`](config/):

- [`models.yaml`](config/models.yaml) — tier definitions + aliases
- [`model-sources.yaml`](config/model-sources.yaml) — HuggingFace / Ollama registry resolver
- [`router.yaml`](config/router.yaml) — auto-thinking / multi-agent / specialist rules
- [`vram.yaml`](config/vram.yaml) — scheduler policy (headroom, eviction, pinning)
- [`auth.yaml`](config/auth.yaml) — magic-link TTL, allowed domains, rate limits
- [`tools.yaml`](config/tools.yaml) — tool manifest + default-enabled set

Secrets live in `data/.env` (written by the setup wizard; never committed).

## API endpoints

```
GET    /healthz                     → {status: "ok"|"degraded"}
GET    /v1/models                   → OpenAI-compatible tier list
POST   /v1/chat/completions         → SSE streaming chat (OpenAI-compatible)
POST   /auth/request                → Send magic link
GET    /auth/verify?token=...       → Exchange for session cookie
POST   /auth/password               → Admin password login
GET    /me                          → Current user
GET    /memory                      → List distilled memories
DELETE /memory/{id}                 → Forget a memory
POST   /rag/upload                  → Upload a document into per-user RAG
GET    /rag/docs                    → List uploaded documents
GET    /vram                        → Current tier residency snapshot
GET    /chats                       → List conversations
GET    /chats/{id}                  → Conversation history
```

## Development

```powershell
# Start backend in reload mode (requires Ollama + Qdrant running)
.\LocalAIStack.ps1 -Start -NoGui

# Run tests (Linux CI — no GPU required)
python -m pytest tests/

# Local health check (on the actual machine after setup)
.\LocalAIStack.ps1 -Test
```

### Project layout

```
LocalAIStack.ps1      Root launcher (setup / start / stop / build / test)
backend/              FastAPI app
  main.py             Endpoints, SSE producers, middleware pipeline
  router.py           Tier selection + slash commands
  vram_scheduler.py   GPU residency manager
  orchestrator.py     Multi-agent plan/synthesize
  rag.py              Per-user Qdrant retrieval
  memory.py           Distillation + injection
  auth.py             Magic-link + password auth + JWT cookies
  model_resolver.py   HF / Ollama registry resolver
gui/                  PySide6 native desktop app
  windows/            chat.py, admin.py, login.py, diagnostics.py, setup_wizard.py
  widgets/            tray.py, markdown_view.py
  cloudflare_setup.py Tunnel provisioning helpers
config/               YAML-driven configuration
installer/            Inno Setup script + PyInstaller spec
scripts/steps/        Dot-sourced helpers for the launcher
tests/                Pytest suite + local health-check areas
tools/                Discoverable tools (one file per tool)
vendor/               Created by -Setup: binaries + Python venvs
data/                 SQLite, histories, Qdrant storage (gitignored)
```

## Roadmap + contributing

- [#34 Admin platform & config](https://github.com/kitisathreat/local-ai-stack/issues/34)
- [#36 Scaling & performance](https://github.com/kitisathreat/local-ai-stack/issues/36)
- [#37 Tooling quality & tests](https://github.com/kitisathreat/local-ai-stack/issues/37)
- [#38 Docs & security](https://github.com/kitisathreat/local-ai-stack/issues/38)
- [#39 Stability & correctness](https://github.com/kitisathreat/local-ai-stack/issues/39)

## Phase history

- **Phase 0** — Docker compose scaffolding, Ollama + Qdrant + SearXNG + Jupyter services
- **Phase 1** — Backend-agnostic tier router + VRAM scheduler + multi-agent orchestrator
- **Phase 4** — Custom Preact frontend, magic-link auth, per-user storage
- **Phase 5** — Tool registry, per-user RAG, memory distillation
- **Phase 6** — Cloudflare Tunnel, middleware migration
- **Phase 7** — Native Windows migration (this branch): no Docker, PySide6 GUI, setup wizard, Inno Setup installer, local health-check suite

## License

See repository settings.
