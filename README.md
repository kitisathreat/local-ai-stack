# local-ai-stack

Self-hosted multi-model LLM workflow. A FastAPI backend routes each chat request to one of five GPU-resident model tiers, with a VRAM-aware scheduler, a multi-agent orchestrator, per-user RAG + memory, and an OpenAI-compatible SSE endpoint. Ships as a single `docker compose up`.

## What it does

- **Tier routing.** Every request is classified and dispatched to a specific model tier — images go to the vision tier, code blocks to the coding tier, ambiguous questions to the Versatile MoE default. Users can override with slash commands (`/tier`, `/think`, `/solo`, `/swarm`).
- **VRAM scheduling.** A reference-counted scheduler with LRU eviction and observed-cost tracking keeps multiple tiers co-resident on a single GPU, evicting idle models when headroom is needed.
- **Multi-agent orchestration.** For complex prompts, the Versatile tier acts as an orchestrator, decomposing the request into 2–5 parallel subtasks run on the Fast tier, then synthesizing. Workers can call tools. Two interaction modes: **independent** (classic parallel) and **collaborative** (workers see each other's drafts and refine over N rounds before synthesis). All knobs — worker count, tier, reasoning, mode, refinement rounds — are tunable globally from the admin dashboard and per-chat by elevated users without persisting.
- **Tools + RAG + memory.** 100+ discoverable tools (web search, finance, science, data repos), per-user Qdrant RAG over uploaded documents, and memory distillation that extracts durable facts from chat history every Nth turn.
- **Public access.** OpenAI-compatible `/v1/chat/completions` endpoint (works with Open WebUI, Cline, etc.) plus an opt-in Cloudflare Tunnel profile for HTTPS exposure without opening router ports.

## Architecture

```
                      ┌───────────────────┐
User ──► Frontend ──► │   FastAPI         │ ──► Router + middleware
                      │   backend         │
                      │                   │ ──► VRAM scheduler ──► Ollama
                      │                   │                    └── llama.cpp (vision)
                      │                   │
                      │                   │ ──► Orchestrator ──► N × Fast workers
                      │                   │
                      │                   │ ──► Tool executor ──► 100+ tools
                      │                   │                       (Jupyter, SearXNG, APIs)
                      │                   │
                      │                   │ ──► Qdrant ──► per-user RAG + memory
                      │                   │
                      │                   │ ──► SQLite ──► chats, memories, users
                      └───────────────────┘
```

## Interfaces

Three GUIs ship with the stack. The chat SPA and admin dashboard are the same Preact bundle; the launcher is a separate Windows orchestrator.

> The chat and admin mockups below are **animated SVGs** — GitHub renders them inline like GIFs, no binary assets required. They show the live agent step panel, worker cards transitioning through states, and the streaming event timeline as they actually appear during a multi-agent run.

### Chat (Preact SPA, `:3000`)

User-facing chat at `http://localhost:3000`. The sidebar lists conversations; the header hosts the tier picker, reasoning toggle, **response-mode picker** (Immediate · Plan first · Clarify · Step approval · My plan), and — for users in `ADMIN_EMAILS` — a **🤝 multi-agent** pill that opens a per-chat overrides panel (workers, worker tier, orchestrator tier, reasoning, interaction mode, refinement rounds). Per-chat tweaks reset on every chat switch and never persist. The composer has 📎 upload and 🧰 tool-picker buttons; a collapsible telemetry strip above it shows ping, tokens/sec, VRAM, RAM, and context-window fill.

![Chat UI animated mockup](docs/images/frontend-chat.svg)

Code: [`frontend/src/app.tsx`](frontend/src/app.tsx). Theme tokens in [`frontend/src/styles.css`](frontend/src/styles.css); light mode auto-engages via `prefers-color-scheme`. Response modes are steered server-side by [`backend/middleware/response_mode.py`](backend/middleware/response_mode.py); the multi-agent panel posts `multi_agent_options` on `/v1/chat/completions` (handled in [`backend/orchestrator.py`](backend/orchestrator.py)).

### Admin dashboard (`#/admin`)

Same bundle, gated by the `ADMIN_EMAILS` env var. Tabs for live usage (requests, tokens, latency, error sparklines), by-tier and by-user breakdowns, users, VRAM residency, and editable config (models · router · **multi-agent** · VRAM · auth · tools). Saves write back to `config/*.yaml` atomically and hot-reload without a restart.

![Admin dashboard mockup](docs/images/admin-dashboard.svg)

Code: [`frontend/src/admin.tsx`](frontend/src/admin.tsx) · [`backend/admin.py`](backend/admin.py) · [`backend/metrics.py`](backend/metrics.py).

#### Multi-agent tab

Dedicated tab for visualizing and tuning the orchestrator → workers → synthesis pipeline. Three sections: a **workflow diagram** rendered live from the unsaved draft (so admins can preview a tweak before saving), the **defaults form** (min/max workers, worker + orchestrator tier dropdowns, reasoning, interaction mode, refinement rounds), and a **live test runner** that submits a prompt with the draft settings and animates one card per worker as it runs — pending → running → done/error, with `round N` tags during collaborative refinement. A relative-timestamped event log streams every plan / workers_start / refine_start / worker_done / synthesis SSE event side-by-side. Doesn't pollute any conversation.

![Admin Multi-agent tab animated mockup](docs/images/admin-multi-agent.svg)

Code: `MultiAgentTab` in [`frontend/src/admin.tsx`](frontend/src/admin.tsx); orchestrator collaborative mode in [`backend/orchestrator.py`](backend/orchestrator.py); per-request schema in [`backend/schemas.py`](backend/schemas.py) (`MultiAgentOptions`).

### Windows launcher (`LocalAIStack.exe`)

PowerShell + WinForms one-shot orchestrator. Starts Docker Desktop, brings the compose stack up, and exposes a tray icon with *Open Chat · View Logs · Restart · Stop & Exit*. Compiled from [`launcher/LocalAIStack.ps1`](launcher/LocalAIStack.ps1) via [`launcher/build.ps1`](launcher/build.ps1).

![Launcher window mockup](docs/images/launcher-window.svg)

## Tiers

All five tiers are defined in [`config/models.yaml`](config/models.yaml) and addressable as `tier.<name>` virtual model ids.

| Tier | Model | Backend | VRAM | Role |
|---|---|---|---|---|
| `highest_quality` | Qwen3 72B | Ollama | ~24 GB | Hardest reasoning, slow. |
| `versatile` | Qwen3.6 35B-A3B (MoE) | Ollama | ~21 GB | Default + multi-agent orchestrator. |
| `fast` | Qwen3.5 9B | Ollama | ~7 GB | Multi-agent workers (3× parallel fit on 24 GB). |
| `coding` | Qwen3-Coder-Next 80B-A3B | Ollama | ~24 GB | SWE-bench trained, native tool use. |
| `vision` | Qwen3.6 35B + mmproj | llama.cpp | ~21 GB | Images, charts, screenshots. Pinned. |

## Quick start

```bash
git clone https://github.com/kitisathreat/local-ai-stack
cd local-ai-stack
cp .env.example .env.local
python -c 'import secrets; print(secrets.token_urlsafe(48))' >> .env.local   # set AUTH_SECRET_KEY
bash scripts/setup-models.sh        # pull Ollama tags + optionally download vision GGUFs
docker compose up -d
```

Then open http://localhost:3000.

**Hardware minimums:** NVIDIA GPU with 24 GB VRAM (RTX 3090 / 4090 / A5000). CPU-only works but the 35B+ tiers will be unusably slow. Vision tier can be skipped by removing GGUF files from `models/`.

## Services (docker-compose.yml)

| Service | Port | Purpose |
|---|---|---|
| `backend` | 8000 | FastAPI — router, auth, RAG, memory, admin |
| `frontend` | 3000 | Preact SPA served by nginx |
| `ollama` | 11434 | Primary inference backend |
| `llama-server` | 8001 | Vision-tier inference (llama.cpp, optional) |
| `qdrant` | 6333 | Vector DB for RAG + memory |
| `searxng` | 4000 | Meta-search backing the web-search middleware |
| `jupyter` | 8888 | Code interpreter tool sandbox |
| `n8n` | 5678 | Workflow automation (optional) |
| `cloudflared` | — | Public HTTPS tunnel (opt-in via `--profile public`) |

## Configuration

All runtime config lives in [`config/`](config/):

- [`models.yaml`](config/models.yaml) — tier definitions + aliases
- [`router.yaml`](config/router.yaml) — auto-thinking / multi-agent / specialist regex rules
- [`vram.yaml`](config/vram.yaml) — scheduler policy (headroom, eviction, pinning)
- [`auth.yaml`](config/auth.yaml) — magic-link TTL, allowed domains, rate limits
- [`tools.yaml`](config/tools.yaml) — tool manifest + default-enabled set
- [`ollama-models.yaml`](config/ollama-models.yaml) — what `scripts/setup-models.sh` pulls

Secrets go in `.env.local` (gitignored). See [`.env.example`](.env.example) for the full list.

Several operationally-meaningful values are still hardcoded in `backend/*.py` — tracked by [#29](https://github.com/kitisathreat/local-ai-stack/issues/29).

### Deeper docs

- [`docs/tiers.md`](docs/tiers.md) — tier roster, reasoning toggle, slash commands, multi-agent orchestration
- [`docs/auth.md`](docs/auth.md) — magic-link flow, SMTP, session cookies, domain allow-list, per-user preferences
- [`docs/vram.md`](docs/vram.md) — scheduler policy, slot cap + wait queue, observed-cost tuning, headroom tuning
- [`docs/public-access.md`](docs/public-access.md) — Cloudflare Tunnel setup (token + credentials.json paths), `X-Forwarded-For` trust, metrics, revocation

## API endpoints

```
GET  /healthz                     → {ok: true}
GET  /v1/models                   → OpenAI-compatible tier list
POST /v1/chat/completions         → SSE streaming chat (OpenAI-compatible)
POST /auth/request                → Send magic link
GET  /auth/verify?token=...       → Exchange for session cookie
GET  /me                          → Current user
GET  /api/memory                  → List distilled memories
DELETE /api/memory/{id}           → Forget a memory
POST /api/rag/upload              → Upload a document into per-user RAG
GET  /api/rag/docs                → List uploaded documents
GET  /api/vram                    → Current tier residency snapshot
GET  /chats                       → List conversations
GET  /chats/{id}                  → Conversation history
```

## Development

```bash
# Run backend locally (requires Ollama/Qdrant up)
uv run --directory backend uvicorn main:app --reload

# Frontend dev server
cd frontend && npm run dev

# Run tests
uv run pytest
# Live-backend tests (gated — requires compose up)
LIVE_BACKEND_TESTS=1 uv run pytest tests/test_backends_live.py    # planned, see #22
```

### Project layout

```
backend/              FastAPI app
  main.py             Endpoints, SSE producers, middleware pipeline
  router.py           Tier selection + slash commands
  vram_scheduler.py   GPU residency manager (LRU + refcount + pinning)
  orchestrator.py     Multi-agent plan/synthesize
  rag.py              Per-user Qdrant retrieval
  memory.py           Distillation + injection
  auth.py             Magic-link + JWT cookies
  backends/           ollama.py, llama_cpp.py
  middleware/         context, clarification, web_search, rate_limit
  tools/              registry, executor
frontend/             Preact SPA
config/               YAML-driven tier + router + vram + auth + tools config
tests/                Pytest suite
scripts/              setup-models.sh, setup-cloudflared.sh, code_assist.py, ...
tools/                Discoverable tools (one file per tool; auto-registered)
```

## Roadmap + contributing

Work is organized as **epics** (tracking issues with child issues linked as sub-issues) grouped by theme:

- [#34 Admin platform & config](https://github.com/kitisathreat/local-ai-stack/issues/34) — live parameter tuning GUI, per-user preferences, config externalization
- [#35 Frontend UX polish](https://github.com/kitisathreat/local-ai-stack/issues/35) — `conversation_id` plumbing, tool-call cards, richer memory UI
- [#36 Scaling & performance](https://github.com/kitisathreat/local-ai-stack/issues/36) — Redis rate limiter, lazy tool-registry load
- [#37 Tooling quality & tests](https://github.com/kitisathreat/local-ai-stack/issues/37) — multi-agent tool tests, live-backend tests, vision-tier tools
- [#38 Docs & security](https://github.com/kitisathreat/local-ai-stack/issues/38) — user-facing docs, Cloudflare hardening
- [#39 Stability & correctness](https://github.com/kitisathreat/local-ai-stack/issues/39) — verified-bug backlog

A living catalog of current / deprecated / anticipated features lives in [#33](https://github.com/kitisathreat/local-ai-stack/issues/33).

### Labels

- **Priority:** `p0` (urgent) → `p3` (nice-to-have)
- **Group:** `group:admin-platform`, `group:user-polish`, `group:scaling`, `group:tooling-quality`, `group:docs-security`, `group:correctness`
- **Status:** `status:ready` (pick it up), `status:blocked` (waiting on another issue), `status:needs-design` (scoping required)
- **Type:** `bug`, `enhancement`, `refactor`, `documentation`, `tests`, `security`, `performance`, `observability`, `configuration`, `epic`

Before closing an epic, confirm its "Review gate before closing" checklist is satisfied (every epic body has one).

## Phase history

- **Phase 0** — Docker compose scaffolding, Ollama + Qdrant + SearXNG + Jupyter + n8n services
- **Phase 1** — Backend-agnostic tier router + VRAM scheduler + multi-agent orchestrator
- **Phase 4** — Custom Preact frontend, magic-link auth, per-user storage
- **Phase 5** — Tool registry, per-user RAG, memory distillation
- **Phase 6** — Cloudflare Tunnel, middleware migration, tools-through-workers

## License

See repository settings.
