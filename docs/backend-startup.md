# Backend startup guide

How to bring the FastAPI backend up, and a checklist of everything you still
need to do before the code will actually run.

---

## TL;DR — the five things you still need to do

1. **Create `.env.local`** with at minimum a generated `AUTH_SECRET_KEY` (and
   probably `ADMIN_EMAILS`, `HISTORY_SECRET_KEY`).
2. **Install Docker Desktop** and make sure it's running with the WSL 2
   backend + NVIDIA Container Toolkit (for GPU passthrough).
3. **Pull the Ollama tier models** via `bash scripts/setup-models.sh` — this is
   a 60+ GB download.
4. *(Optional)* **Download the vision GGUFs** (`qwen3.6-35b-a3b-Q4_K_M.gguf`
   and `mmproj-qwen3.6-35b-F16.gguf`) into `models/` — or skip the vision tier
   entirely.
5. **Run** `docker compose up -d` (or `bash scripts/start.sh`).

After that, `curl http://localhost:8000/healthz` should return `{"ok": true}`.

---

## 1. Prerequisites

### Hardware
- NVIDIA GPU with **≥ 24 GB VRAM** (RTX 3090 / 4090 / A5000). CPU-only works
  but the 35B+ tiers are unusably slow.
- **~120 GB free disk** for model weights + volumes.

### Software
- **Docker Desktop** with the WSL 2 backend (Windows) or a Docker Engine
  install (Linux/macOS).
- **NVIDIA Container Toolkit** — without it, `docker compose up` will fail on
  the `deploy.resources.reservations.devices[driver=nvidia]` blocks. Verify
  with:
  ```bash
  docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
  ```
- **Python 3** (only for the one-liner in step 2 below — the backend itself
  runs in a container).

---

## 2. Configure `.env.local`

The backend will refuse to start without `AUTH_SECRET_KEY`. Copy the example
and fill it in.

```bash
cp .env.example .env.local
```

Then edit `.env.local` and set these. The ones marked **required** must be
filled in before the container will start cleanly.

| Var | Required? | What to do |
|---|---|---|
| `AUTH_SECRET_KEY` | **yes** | Generate: `python -c 'import secrets; print(secrets.token_urlsafe(48))'` |
| `HISTORY_SECRET_KEY` | recommended | Same generator. If unset, history encryption derives from `AUTH_SECRET_KEY` — rotating the auth key would then orphan existing chat history. |
| `ADMIN_EMAILS` | required for admin UI | Comma-separated list of emails allowed into `/admin` and the multi-agent pill. Without this, admin endpoints return 503. *Note: this env var is consumed by `backend/admin.py` but is not in `.env.example` yet — add it manually.* |
| `PUBLIC_BASE_URL` | for magic-link login | Leave at `http://localhost:3000` for local; set to your Cloudflare Tunnel hostname for public. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` | optional | Without these, magic-link URLs are printed to `docker logs lai-backend` instead of emailed. Fine for local dev. |
| `AUTH_EMAIL_FROM` | if using SMTP | The `From:` address on magic-link emails. |
| `ALLOWED_ORIGINS` | recommended | CORS origin allowlist. `*` for dev; restrict in production. |
| `CLOUDFLARE_TUNNEL_TOKEN` / `CLOUDFLARE_HOSTNAME` | only for public tunnel | Run `bash scripts/setup-cloudflared.sh` once to provision. |
| `BACKEND_WORKERS` | optional | Uvicorn worker count. If `> 1`, make sure Redis is reachable at `REDIS_URL` (it ships as a compose service). |
| `OLLAMA_KEEP_ALIVE` / `OLLAMA_MAX_LOADED_MODELS` / `OLLAMA_NUM_PARALLEL` / `OLLAMA_FLASH_ATTENTION` | optional | Ollama tuning knobs. |
| `LOG_LEVEL` | optional | `DEBUG` / `INFO` / `WARNING`. |

`.env.local` is gitignored — don't commit it.

---

## 3. Pull the Ollama tier models

The backend won't route successfully until Ollama has the tier models pulled.
The `setup-models.sh` script reads the `tiers` group from
`config/ollama-models.yaml` and pulls each one.

```bash
# Start Ollama first (the script talks to it over HTTP)
docker compose up -d ollama

# Then pull (this is the ~60 GB download step)
bash scripts/setup-models.sh
```

What gets pulled by default (`groups.tiers`):
- `qwen3:72b-q3_k_m` — Highest Quality tier (~33 GB)
- `qwen3.6:35b-a3b` — Versatile / orchestrator (~21 GB)
- `qwen3.5:9b` — Fast tier / workers (~7 GB)
- `qwen2.5-coder:32b` — Coding fallback (~19 GB)
- `nomic-embed-text` — RAG embeddings (~0.3 GB)

Smaller groups are available for testing: `bash scripts/setup-models.sh minimal`
pulls only the 9B Fast tier + embeddings.

---

## 4. *(Optional)* Vision tier setup

The `llama-server` container loads `models/qwen3.6-35b-a3b-Q4_K_M.gguf` and
`models/mmproj-qwen3.6-35b-F16.gguf` at boot. These files are **not** shipped
with the repo. Two options:

**Option A — skip vision.** Remove the `llama-server` service from
`docker-compose.yml` (or just let its boot fail; the VRAM scheduler will mark
the tier unavailable and route around it).

**Option B — download the GGUFs.** From HuggingFace
`Qwen/Qwen3.6-35B-A3B-Instruct-GGUF`:
1. Download the `Q4_K_M.gguf` variant → save as
   `models/qwen3.6-35b-a3b-Q4_K_M.gguf`
2. Download `mmproj-F16.gguf` → save as `models/mmproj-qwen3.6-35b-F16.gguf`

The `models/` directory is empty in this clone — create it if missing.

---

## 5. Start the stack

Easiest path:

```bash
bash scripts/start.sh
```

This runs `docker compose up -d`, waits for the backend healthcheck, and
triggers a background `setup-models.sh --skip-vision` pull.

Or directly:

```bash
docker compose up -d
```

### What comes up

| Service | Port | Purpose |
|---|---|---|
| `backend` | 8000 | FastAPI — the thing you're starting |
| `frontend` | 3000 | Preact SPA (nginx) |
| `ollama` | 11434 | Primary inference |
| `llama-server` | 8001 | Vision tier (fails silently if GGUFs missing) |
| `qdrant` | 6333 / 6334 | RAG + memory vector DB |
| `searxng` | 4000 | Web search for middleware |
| `jupyter` | 8888 | Code interpreter (token: `local-ai-stack-token`) |
| `n8n` | 5678 | Workflow automation (optional) |
| `redis` | internal | Cross-worker rate-limit coordination |
| `cloudflared` | — | Public HTTPS, opt-in via `--profile public` |

### Verify

```bash
curl http://localhost:8000/healthz          # → {"ok": true}
curl http://localhost:8000/v1/models        # → tier list
curl http://localhost:8000/api/vram         # → residency snapshot
```

Backend logs: `docker compose logs -f backend`
(the container name is `lai-backend`.)

---

## 6. First-time login

1. Open `http://localhost:3000`.
2. Enter your email → click the magic link.
3. If SMTP is unconfigured, the link shows up in
   `docker compose logs backend` — copy-paste it into the browser.
4. Put your email in `ADMIN_EMAILS` to unlock the admin dashboard at
   `http://localhost:3000/#/admin`.

---

## Running the backend without Docker (dev loop)

If you want to iterate on `backend/*.py` without a full container rebuild:

```bash
# Ollama + Qdrant + Redis still need to be up — keep those in compose
docker compose up -d ollama qdrant redis searxng

# Then run the backend on the host
export AUTH_SECRET_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(48))')
export OLLAMA_URL=http://localhost:11434
export LAI_CONFIG_DIR=$(pwd)/config

uv run --directory backend uvicorn main:app --reload --port 8000
```

`--reload` picks up file changes; the compose `backend` service should be
stopped first (`docker compose stop backend`) to free port 8000.

---

## Troubleshooting

- **`AUTH_SECRET_KEY env var is not set`** — fill it in `.env.local` (step 2).
- **Backend container keeps restarting** — `docker compose logs backend`.
  Usually a missing env var or an unreachable Ollama.
- **`nvidia-smi` fails inside containers** — NVIDIA Container Toolkit isn't
  installed / Docker Desktop GPU support isn't enabled.
- **`llama-server` won't stay up** — vision GGUFs missing from `models/`.
  Either download them (step 4) or remove the service.
- **Chat returns 503 / router errors** — the tier's model isn't in Ollama yet.
  Run `curl http://localhost:11434/api/tags` to check; re-run
  `scripts/setup-models.sh` if the tag's missing.
- **Admin dashboard returns 503** — `ADMIN_EMAILS` isn't set, or your email
  isn't in it.

---

## Stop / reset

```bash
bash scripts/stop.sh                  # docker compose down
docker compose down -v                # also wipes volumes (Ollama models, Qdrant index, chat history)
```
