# local-ai-stack — overview

Self-hosted multi-model LLM workflow for a single Windows machine. A FastAPI
backend routes each chat request to one of six llama.cpp-hosted model tiers
(four chat, one vision, one embedding), with a VRAM-aware scheduler, a
multi-agent orchestrator, per-user RAG + memory, and an OpenAI-compatible
SSE endpoint. Ships as a PowerShell launcher (`LocalAIStack.ps1`) — no
Docker, no browser.

## What it does

- **Tier routing.** Every request is classified and dispatched to a specific
  model tier — images go to the vision tier, code blocks to the coding tier,
  ambiguous questions to the Versatile MoE default. Users can override with
  slash commands (`/tier`, `/think`, `/solo`, `/swarm`).
- **VRAM scheduling.** A reference-counted scheduler with LRU eviction and
  observed-cost tracking keeps multiple tiers co-resident on a single GPU,
  spawning per-tier `llama-server` subprocesses on demand and SIGTERMing
  them when VRAM pressure rises.
- **Multi-agent orchestration.** For complex prompts, the Versatile tier
  acts as an orchestrator, decomposing the request into 2–5 parallel
  subtasks run on the Fast tier, then synthesizing. Workers can call tools.
  Two interaction modes: **independent** (classic parallel) and
  **collaborative** (workers see each other's drafts and refine over N
  rounds before synthesis).
- **Tools + RAG + memory.** 100+ discoverable tools (web search, finance,
  science, data repos), per-user Qdrant RAG over uploaded documents, and
  memory distillation that extracts durable facts from chat history every
  Nth turn. The embedding pipeline is served by an always-on
  `llama-server --embedding` on port 8090.

## How it ships

A single PowerShell launcher at the repo root:

```powershell
.\LocalAIStack.ps1 -InitEnv     # write a default .env
.\LocalAIStack.ps1 -Setup       # install prereqs, vendor binaries, GGUFs
.\LocalAIStack.ps1 -Start       # launch services + native Qt GUI
.\LocalAIStack.ps1 -Stop        # terminate everything tracked
```

No Docker, no browser. Native Windows from end to end.

## Tier table

| Tier | Model | Port | `--ctx-size` | VRAM | Role |
|---|---|---|---|---|---|
| `highest_quality` | Qwen3 72B | 8010 | 32 768 | ~24 GB | Hardest reasoning |
| `versatile` | Qwen3.6 35B-A3B (MoE) | 8011 | 65 536 (YaRN ×2) | ~21 GB | Default + orchestrator |
| `fast` | Qwen3.5 9B | 8012 | 65 536 | ~7 GB | Multi-agent workers |
| `coding` | Qwen3-Coder-Next 80B-A3B | 8013 | 131 072 (YaRN ×4) | ~24 GB | SWE-bench trained |
| `vision` | Qwen3.6 35B + mmproj | 8001 | 16 384 | ~21 GB | Images / charts |
| `embedding` | nomic-embed-text-v1.5 | 8090 | 8 192 | ~1 GB | RAG + memory distillation |

All tiers run with `--cache-type-k q8_0 --cache-type-v q8_0 -fa --jinja`,
so context windows are pushed to each model's native max within the 24 GB
card budget. Vision and embedding tiers are pinned and pre-spawned at
boot; chat tiers cold-spawn on first request via the `VRAMScheduler`.

## Services on a running stack

- **backend** (FastAPI on 18000) — API + SSE chat + health
- **qdrant** (6333) — vector store for RAG + memory
- **llama-server** (8001) — vision tier, pinned, pre-spawned
- **llama-server** (8090) — embedding tier, pinned, pre-spawned
- **llama-server** (8010-8013) — chat tiers, cold-spawned on demand
- **jupyter** (8888) — code-interpreter sandbox
- **gui** — PySide6 native desktop app (no listening port)
- **cloudflared** — optional Windows service for HTTPS tunneling

## Phase history

- **Phase 0** — Docker-compose + Preact scaffolding (later removed)
- **Phase 1** — Backend-agnostic tier router + VRAM scheduler + multi-agent
- **Phase 4** — Auth + per-user storage
- **Phase 5** — Tool registry, per-user RAG, memory distillation
- **Phase 6** — Cloudflare Tunnel, middleware migration
- **Phase 7** — Native Windows migration: no Docker, PySide6 GUI, setup
  wizard, Inno Setup installer, local health-check suite
- **Phase 8** — Migration from Ollama to native llama.cpp for all tiers,
  unlocking native-max context windows via KV-cache quantization
