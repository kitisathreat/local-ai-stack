# LocalAIStack launcher

Single-click Windows launcher for the stack. Double-click `LocalAIStack.exe` and the whole stack comes up silently with a progress window.

## Build

```powershell
pwsh -File launcher\build.ps1
```

Outputs `launcher\dist\LocalAIStack.exe` and `launcher\dist\gpu-agent.exe`. Requires `ps2exe` (auto-installed on first build).

## First-time setup (one-time per machine)

1. Install prerequisites: Docker Desktop, LM Studio, `cloudflared`.
2. Authorize Cloudflare tunnel:
   ```
   cloudflared tunnel login
   cloudflared tunnel create local-ai-stack
   cloudflared tunnel route dns local-ai-stack chat.mylensandi.com
   ```
3. Copy the generated credentials JSON into `%USERPROFILE%\.cloudflared\`.
4. Edit `cloudflare/config.yml` — replace `TUNNEL_UUID_HERE` with your tunnel UUID.
5. Configure Cloudflare Access for `chat.mylensandi.com`:
   - Zero Trust dashboard → Access → Applications → Add application → Self-hosted
   - Policy: Allow if email ∈ your allowlist
   - Identity provider: One-time PIN (built-in)

Every subsequent launch is silent.

## What the launcher does

1. Checks Docker Desktop is running; starts it if not
2. Checks LM Studio server on :1234; starts it if not
3. Verifies cloudflared credentials exist
4. Runs `docker compose up -d`
5. Waits for `api` and `web` to be healthy
6. Starts `gpu-agent.exe` for VRAM telemetry
7. Opens `https://chat.mylensandi.com` in the default browser
8. Minimizes to system tray

All output is logged to `%APPDATA%\LocalAIStack\launcher.log` (rotated at 2 MB, keeps 5 files). Dialogs appear only when user action is required (e.g., a missing prerequisite).

## Tray menu

- **Open Chat** — reopens `chat.mylensandi.com`
- **View Logs** — opens `launcher.log` in Notepad
- **Restart** — relaunches the executable
- **Stop & Exit** — runs `scripts\stop.ps1` silently and exits

## Troubleshooting

- Logs: `%APPDATA%\LocalAIStack\launcher.log`
- Manual start for debugging: `pwsh -File launcher\LocalAIStack.ps1 -DevMode`
- Reset cloudflared auth: delete `%USERPROFILE%\.cloudflared\*.json` and re-run `cloudflared tunnel login`
