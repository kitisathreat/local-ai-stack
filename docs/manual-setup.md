# Manual setup (no installer)

This document covers what to do if you can't or don't want to run the
LocalAIStack Inno Setup installer. The supported path is still the
PowerShell launcher; this page just walks through the steps it automates.

## Prerequisites

Verified prerequisites — installed automatically by `-Setup` via winget:

- Git
- Python 3.12
- PowerShell 7
- Cloudflared (only needed for HTTPS tunnel; skip if you don't expose chat)

Detected but never auto-installed:

- NVIDIA driver ≥ 550 (for CUDA 12 inference). Without a GPU, llama-server
  falls back to CPU — usable for the embedding tier and the smallest chat
  tier (`fast`) only.

## Vendored binaries

`-Setup` pulls and SHA256-verifies these into `vendor/`:

- `vendor/qdrant/qdrant.exe` — vector store
- `vendor/llama-server/llama-server.exe` — inference server (CUDA build)

Versions are pinned in `LocalAIStack.ps1` (`$QdrantVersion` and
`$LlamaCppVersion`).

## Python venvs

Three isolated venvs live under `vendor/`:

- `venv-backend/` — FastAPI + httpx + qdrant-client + huggingface_hub
- `venv-gui/` — PySide6
- `venv-jupyter/` — JupyterLab for the code-interpreter tool

Created by `-Setup`. Backed by `pyproject.toml` / `requirements*.txt`.

## GGUF model files

`-Setup` runs `python -m backend.model_resolver resolve --pull`, which:

1. Polls Hugging Face for each tier in `config/model-sources.yaml`.
2. Downloads the chosen GGUF file to `data/models/<tier>.gguf` (and a
   `<tier>.mmproj.gguf` companion for vision).
3. Writes `data/resolved-models.json` so the backend knows what's on disk.

The vision tier GGUF (~25 GB) is gated behind a confirmation prompt; the
others (highest_quality, versatile, fast, coding, embedding) are always
pulled when missing.

## Verifying it works

```powershell
.\LocalAIStack.ps1 -Start -NoGui   # leave GUI off; just start services
```

Then check ports:

```powershell
curl http://127.0.0.1:18000/healthz   # backend → {"status":"ok"|"degraded"}
curl http://127.0.0.1:6333/healthz    # qdrant
curl http://127.0.0.1:8001/health     # vision llama-server
curl http://127.0.0.1:8090/v1/models  # embedding llama-server
```

The 4 chat tiers (8010–8013) start lazily on first request — they won't
appear in `tasklist` until you chat.

## Pulling extra / different models

Edit `config/model-sources.yaml` to change the upstream HF repo or the
`file:` glob pattern, then run:

```powershell
.\LocalAIStack.ps1 -CheckUpdates    # re-poll
.\LocalAIStack.ps1 -Setup           # download anything missing
```

For air-gapped installs, set `OFFLINE=1` in `.env` and pre-stage GGUFs into
`data/models/<tier>.gguf` yourself. The backend honours
`data/resolved-models.json` first, so you can hand-edit it to point at any
path.

## Ports summary

| Port | Service |
|---|---|
| 18000 | backend (FastAPI) |
| 8001 | vision llama-server |
| 8010 | highest_quality llama-server (lazy) |
| 8011 | versatile llama-server (lazy) |
| 8012 | fast llama-server (lazy) |
| 8013 | coding llama-server (lazy) |
| 8090 | embedding llama-server |
| 6333 | qdrant HTTP |
| 6334 | qdrant gRPC |
| 8888 | jupyter |
