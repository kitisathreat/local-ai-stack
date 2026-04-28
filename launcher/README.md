# LocalAIStack launcher

Single-click Windows launcher for the stack. Double-click `LocalAIStack.exe` and the whole stack comes up silently with a progress window — no visible PowerShell or Docker console windows.

## Build

```powershell
pwsh -File launcher\build.ps1
```

Outputs `launcher\dist\LocalAIStack.exe` and `launcher\dist\AirgapChat.exe`. Requires `ps2exe` (auto-installed on first build).

## What it wraps

The launcher is a thin silent orchestrator over the existing repo scripts + docker compose. It assumes the stack architecture defined in [`docker-compose.yml`](../docker-compose.yml) and [`backend/`](../backend/):

- **backend** (FastAPI) on `:18000` (compose maps host 18000 → container 8000) — waits for `/healthz`
- **frontend** (Preact + nginx) on `:3000` — where the user is sent
- **cloudflared** — only when `CLOUDFLARE_TUNNEL_TOKEN` is set in `.env.local` (started via `--profile public`)
- **ollama** + optional **llama-server** — inference tiers (containerized; no host-side LM Studio dependency)

## First-time setup

1. Run `setup.ps1` from the repo root. It installs WSL2 + Ubuntu + Docker Engine **inside WSL** (not Docker Desktop), the NVIDIA Container Toolkit, generates secrets, and builds the launcher EXEs. Docker Desktop is not used by this stack — if you have it installed, leave it alone, but the launcher won't talk to it.
2. *(Optional, public access)* run `bash scripts/setup-cloudflared.sh` to provision a tunnel token, then paste it into `.env.local` as `CLOUDFLARE_TUNNEL_TOKEN`.
3. *(Optional, public URL)* set `PUBLIC_BASE_URL=https://your-tunnel-hostname` in `.env.local` so the launcher opens the public URL instead of `http://localhost:3000`.
4. Double-click `LocalAIStack.exe`.

## What the launcher does

1. Checks Docker Engine is reachable inside the WSL2 distro (`Ubuntu` by default); starts `docker.service` via systemd if not (40s timeout).
2. Checks `.env.local` for `CLOUDFLARE_TUNNEL_TOKEN`; if present, enables the `public` compose profile.
3. Runs `docker compose up -d` (+ `--profile public` when tunnel is configured).
4. Polls `http://localhost:18000/healthz` (backend) and `http://localhost:3000/` (frontend) until both are healthy (120s max).
5. Opens `$PUBLIC_BASE_URL` (or `http://localhost:3000`) in the default browser.
6. Minimizes to the system tray.

All output is logged to `%APPDATA%\LocalAIStack\launcher.log` (rotated at 2 MB, keeps 5 files). Dialogs appear **only** when user action is required (e.g., WSL is missing — re-run `setup.ps1`).

## Tray menu

- **Open Chat** — reopens the chat URL (browser)
- **Open Airgap Chat (desktop)** — launches the native desktop chat window (`AirgapChat.exe`)
- **View Logs** — opens `launcher.log` in Notepad
- **Restart** — relaunches the executable
- **Stop & Exit** — runs `scripts\stop.ps1` (if present) + `docker compose down` silently and exits

## Airgap desktop chat (`AirgapChat.exe`)

A standalone WinForms chat window intended for use when airgap mode is ON. It opens in its own top-level window — separate from the browser UI and the admin dashboard — and talks directly to the backend at `http://localhost:18000` (override via `-BackendUrl <url>` or `LAI_BACKEND_URL`).

- Streams token-by-token via SSE from `POST /v1/chat/completions`
- Tier picker populated from `GET /v1/models`
- Airgap status indicator polls `GET /airgap` every 15s (green = airgap ON, red = OFF or backend unreachable)
- `Ctrl+Enter` to send, `New chat` to reset, `Send` doubles as `Stop` while streaming
- Logs to `%APPDATA%\LocalAIStack\airgap-chat.log`

Run directly during development: `pwsh -File launcher\AirgapChat.ps1`.

## Troubleshooting

- Logs: `%APPDATA%\LocalAIStack\launcher.log`
- Manual run for debugging: `pwsh -File launcher\LocalAIStack.ps1 -DevMode`
- Skip the tunnel for a given run: unset `CLOUDFLARE_TUNNEL_TOKEN` in `.env.local` (the launcher detects the empty value and runs local-only).
