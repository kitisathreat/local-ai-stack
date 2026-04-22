# Manual Setup Checklist

`setup.ps1` handles almost everything automatically, but a few things require
steps only you can take (external accounts, GPU drivers, model downloads).
This document lists them with exact instructions.

## 1. NVIDIA GPU drivers (required for GPU acceleration)

The WSL2 install gives Docker access to the GPU via the host's driver.

- Open **Device Manager -> Display adapters** and confirm an NVIDIA GPU is
  listed.
- Download the latest **Game Ready** or **Studio** driver from
  https://www.nvidia.com/drivers/ -- version 550 or newer is required for
  WSL CUDA.
- After install, reboot, then run `nvidia-smi` in PowerShell to confirm.
- Inside WSL, `wsl -d Ubuntu -- nvidia-smi` must also work before `setup.ps1`
  will enable GPU passthrough. If it doesn't, you need the separate
  **CUDA driver for WSL**: https://developer.nvidia.com/cuda/wsl

No special "CUDA Toolkit" install is needed -- the driver alone is enough.

## 2. Vision tier model files (optional)

`llama-server` stays in a restart loop until these two files exist in
`./models/`. The vision tier won't work without them; the text tiers are
unaffected. Skip this section if you don't need image understanding.

1. Open https://huggingface.co/Qwen/Qwen3.6-35B-A3B-Instruct-GGUF
2. Download:
   - `qwen3.6-35b-a3b-Q4_K_M.gguf` (the quantised weights, ~20 GB)
   - `mmproj-qwen3.6-35b-F16.gguf` (the multimodal projector, ~1 GB)
3. Put both files in `./models/` at the repo root.
4. `docker compose restart llama-server` (inside WSL) to pick them up.

## 3. Cloudflare Tunnel (optional, for public HTTPS)

Skip unless you want to access the chat UI from outside your LAN.

1. Go to https://one.dash.cloudflare.com
2. Navigate to **Networks -> Tunnels -> Create a tunnel**
3. Choose "Cloudflared" as the connector, give it any name
4. Copy the "install token" on the "Install and run a connector" page
   (a long base64 string)
5. Under **Public Hostname**, point your chosen subdomain (e.g.
   `chat.example.com`) to `http://frontend:3000`
6. Run `setup.ps1 -Interactive` (or paste the token and hostname into
   `.env.local` manually as `CLOUDFLARE_TUNNEL_TOKEN` and `CLOUDFLARE_HOSTNAME`)
7. Restart: `scripts\start.ps1` -- the `cloudflared` container will start
   because the `public` compose profile is now active.

## 4. SMTP for magic-link emails (optional)

If unset, magic links are written to `docker logs lai-backend` instead of
emailed -- fine for local-only use. Configure SMTP when you want the email
delivery to actually happen.

### Gmail
1. Enable 2-Step Verification on your Google account
   (https://myaccount.google.com/security)
2. Create an App Password: https://myaccount.google.com/apppasswords
3. In `setup.ps1 -Interactive`, enter:
   - Host: `smtp.gmail.com`
   - Port: `587`
   - Username: your full Gmail address
   - Password: the 16-character App Password (not your normal password)
   - From: same as username

### Outlook / Microsoft 365
- Host: `smtp.office365.com`, Port: `587`
- Use your full email and your account password (or App Password if MFA is on)

### Other providers
Check your provider's SMTP documentation for host, port, and auth method.
The stack uses STARTTLS on port 587 by default.

## 5. Admin dashboard access (handled by the launcher)

The first time you run `launcher\dist\LocalAIStack.exe`, a dialog asks for the
email address(es) that should have admin privileges. Those are saved to
`ADMIN_EMAILS` in `.env.local`.

Admin login flow:
1. Open `http://localhost:3000/admin`
2. Enter your admin email
3. Check your SMTP inbox (or `docker logs lai-backend`) for the magic link
4. Click the link -- you're in

To add or remove admins later, edit `ADMIN_EMAILS=` in `.env.local` (comma
separated) and restart the backend: `docker compose restart backend`.

## 6. n8n workflow editor (optional auth)

n8n runs on port 5678 and is **unauthenticated by default**. If you expose it
beyond localhost, enable basic auth:

- `setup.ps1 -Interactive` offers to set this up
- Or manually: set `N8N_BASIC_AUTH_ACTIVE=true`, `N8N_ADMIN_USER`, and
  `N8N_ADMIN_PASSWORD` in `.env.local`

## 7. Ollama text-tier models

### Automated (recommended)

Run setup.ps1 with the `-PullModels` flag. It starts Ollama, pulls the chosen
group, and shows GPU status so you know whether inference will use the GPU:

```powershell
# Pull minimal tier (~7 GB): qwen3.5:9b + nomic-embed-text
powershell -ExecutionPolicy Bypass -File setup.ps1 -PullModels

# Pull all backend tiers (~80 GB: 72B, 35B, 9B, coder, embed)
powershell -ExecutionPolicy Bypass -File setup.ps1 -PullModels -ModelGroup tiers

# Pull minimal + download vision GGUFs (~21 GB additional)
powershell -ExecutionPolicy Bypass -File setup.ps1 -DownloadVision
```

### Manual (inside WSL)

```bash
# Check what's already pulled
wsl -d Ubuntu -- docker exec ollama ollama list

# Pull a specific group (edit config/ollama-models.yaml to add models)
wsl -d Ubuntu -- bash scripts/setup-models.sh minimal
wsl -d Ubuntu -- bash scripts/setup-models.sh tiers --skip-vision
```

### Why is my CPU maxed out?

Ollama and llama-server run inference on CPU when no GPU is detected. A 7B model
uses 100% of every core; a 35B model can take minutes per reply. To fix this:

1. Install NVIDIA Game Ready/Studio driver >= 550 from https://www.nvidia.com/drivers/
2. Install the WSL CUDA driver: https://developer.nvidia.com/cuda/wsl
3. Reboot Windows, then re-run `setup.ps1`

Verify with: `wsl -d Ubuntu -- nvidia-smi` (should show your GPU name)

## 8. Port conflicts

Known conflicts you might hit on Windows:

| Port | Used by this stack | Common conflict |
|------|--------------------|-----------------|
| 18000 | backend | (previously 8000, now 18000 because IncrediBuild's Coordinator squats on 8000) |
| 3000 | frontend | many dev servers |
| 11434 | Ollama | standalone Ollama installs |
| 6333, 6334 | Qdrant | |
| 5678 | n8n | |
| 8888 | Jupyter | |
| 4000 | SearXNG | |

If any of these clashes, edit the `ports:` mapping for that service in
`docker-compose.yml` (change the **host** side, keep the container side the
same so the internal docker network continues to work).

## Quick verification

After setup, these should all succeed from a Windows terminal:
```
curl http://localhost:3000/           # frontend -> HTTP 200
curl http://localhost:18000/healthz    # backend  -> {"ok":true}
curl http://localhost:11434/api/version # ollama  -> {"version":"..."}
```
And from WSL:
```
wsl -d Ubuntu -- nvidia-smi             # GPU visible
wsl -d Ubuntu -- docker ps              # all services running
```
