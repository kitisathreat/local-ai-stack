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

This branch removes all Docker dependencies so that users running
`cloudflared` natively on Windows don't get tunnel conflicts from a
second containerised connector. The main-branch Docker path is preserved
on `master`; see `docs/overview.md` for the original architecture.
