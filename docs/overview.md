# local-ai-stack — overview

A 2-minute tour. For tier specs, ports, bench numbers, and the full
architecture, see the project [`README.md`](../README.md).

Self-hosted multi-model LLM workflow for a single Windows machine. A
FastAPI backend routes each chat request to one of six llama.cpp-hosted
model tiers (four chat, one vision, one embedding), with a VRAM-aware
scheduler, a multi-agent orchestrator, per-user RAG + memory, and an
OpenAI-compatible SSE endpoint. Ships as a PowerShell launcher
(`LocalAIStack.ps1`) — no Docker, no browser.

## What it does

- **Tier routing.** Every request is classified and dispatched to a
  specific model tier — images go to the vision tier, code blocks to
  the coding tier, ambiguous questions to the Versatile MoE default.
  Users can override with slash commands (`/tier`, `/think`, `/solo`,
  `/swarm`).
- **VRAM scheduling.** A reference-counted scheduler with LRU eviction
  and observed-cost tracking keeps multiple tiers co-resident on a
  single GPU, spawning per-tier `llama-server` subprocesses on demand
  and SIGTERMing them when VRAM pressure rises.
- **Multi-agent orchestration.** For complex prompts, the Versatile
  tier acts as an orchestrator, decomposing the request into 2–5
  parallel subtasks run on the Fast tier, then synthesizing. Workers
  can call tools. Two interaction modes: **independent** (classic
  parallel) and **collaborative** (workers see each other's drafts and
  refine over N rounds before synthesis).
- **Tools + RAG + memory.** 100+ discoverable tools (web search,
  finance, science, data repos), per-user Qdrant RAG over uploaded
  documents, and memory distillation that extracts durable facts from
  chat history every Nth turn. The embedding pipeline is served by an
  always-on `llama-server --embedding` on port 8090.

## How it ships

A single PowerShell launcher at the repo root:

```powershell
.\LocalAIStack.ps1 -InitEnv     # write a default .env
.\LocalAIStack.ps1 -Setup       # install prereqs, vendor binaries, GGUFs
.\LocalAIStack.ps1 -Start       # launch services + native Qt GUI
.\LocalAIStack.ps1 -Stop        # terminate everything tracked
```

No Docker, no browser. Native Windows from end to end.

## Where to next

- **[`README.md`](../README.md)** — full project documentation: tier
  table with bench numbers, ports, GUI tour, configuration reference,
  API endpoints, tools/RAG/memory deep-dive.
- **[`docs/manual-setup.md`](manual-setup.md)** — manual install path
  (no installer): prerequisites, vendored binaries, venv layout, port
  map, GGUF pull procedure.
