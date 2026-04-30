# Backend startup guide

This document used to cover Docker + Ollama bring-up. Both are gone — the
stack is native Windows now and inference is served by llama-server.

For current instructions, see:

- [`README.md`](../README.md) — overview, tier table, port map
- [`LocalAIStack.ps1 -Help`](../LocalAIStack.ps1) — full launcher reference
- [`docs/manual-setup.md`](manual-setup.md) — manual setup (no installer)

## Quick reference

```powershell
# First-time setup (writes .env, installs prereqs, downloads vendored
# binaries, creates Python venvs, pulls GGUFs from Hugging Face).
.\LocalAIStack.ps1 -Setup

# Day-to-day start (qdrant + vision + embedding llama-server + jupyter +
# backend + GUI). Chat tiers cold-spawn on first request.
.\LocalAIStack.ps1 -Start

# Stop everything tracked in pids.json.
.\LocalAIStack.ps1 -Stop
```

## Services on a running stack

| Service | Port | Role |
|---|---|---|
| backend (FastAPI) | 18000 | API + SSE chat + health |
| qdrant | 6333 | Vector store for RAG + memory |
| llama-server (vision) | 8001 | Pinned, pre-spawned |
| llama-server (embedding) | 8090 | Pinned, pre-spawned, `--embedding` |
| llama-server (chat) | 8010-8013 | Cold-spawned per-tier by VRAMScheduler |
| jupyter | 8888 | Code-interpreter sandbox |

## Environment variables that matter

| Var | Required | Purpose |
|---|---|---|
| `AUTH_SECRET_KEY` | yes | JWT signing |
| `HISTORY_SECRET_KEY` | yes | Encrypts on-disk chat history |
| `CHAT_HOSTNAME` | yes | Host-gate guard for chat endpoints |
| `QDRANT_URL` | optional | Defaults to `http://127.0.0.1:6333` |
| `HF_TOKEN` | optional | For gated/private HuggingFace repos |
| `OFFLINE` | optional | Skip upstream polling (uses pinned versions) |
| `MODEL_UPDATE_POLICY` | optional | `auto` / `prompt` / `skip` |
| `WEB_SEARCH_PROVIDER` | optional | `brave` / `ddg` / `none` |

The launcher writes a default `.env` via `-InitEnv`. Edit it and re-run.

## Troubleshooting

- **healthz returns degraded** — check `data/logs/embedding.log` and
  `data/logs/qdrant.log`. The embedding tier (port 8090) is required for
  RAG and memory; if its GGUF download didn't land, run `-CheckUpdates`.
- **Chat returns 503** — the cold-spawn for that tier failed. Check
  `data/logs/<tier>.log` and the corresponding entry in
  `data/resolved-models.json`.
- **VRAM exhausted** — lower `parallel_slots` for the largest tiers in
  `config/models.yaml` (admin UI also lets you do this without an edit).
