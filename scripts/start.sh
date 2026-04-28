#!/usr/bin/env bash
# Bring up the local AI stack. Canonical launcher — the .ps1 versions wrap
# this via WSL on Windows.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

cyan()  { printf "\033[36m>> %s\033[0m\n" "$*"; }
green() { printf "\033[32m   OK: %s\033[0m\n" "$*"; }
warn()  { printf "\033[33m   WARN: %s\033[0m\n" "$*"; }
fail()  { printf "\033[31m   FAIL: %s\033[0m\n" "$*" >&2; exit 1; }

# ── Docker ──────────────────────────────────────────────────────────────
cyan "Checking Docker"
if ! docker ps >/dev/null 2>&1; then
  fail "Docker is not running. On Windows, the launcher invokes this from inside WSL — re-run via setup.ps1 or scripts\\start.ps1. On Linux/macOS, start your Docker Engine and re-run."
fi
green "Docker is running"

# ── env file ─────────────────────────────────────────────────────────────
# Bootstrap .env.local from .env.example on first run, and auto-fill any
# required secrets that are still blank. Mirrors what setup.ps1 does on
# Windows so the bash path is just as secure.
if [ ! -f "$ROOT/.env.local" ] && [ -f "$ROOT/.env.example" ]; then
  cyan "Creating .env.local from .env.example"
  cp "$ROOT/.env.example" "$ROOT/.env.local"
fi

if [ -f "$ROOT/.env.local" ]; then
  for key in AUTH_SECRET_KEY HISTORY_SECRET_KEY JUPYTER_TOKEN; do
    if ! grep -E "^${key}=.+" "$ROOT/.env.local" >/dev/null 2>&1; then
      secret=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
      if grep -E "^${key}=" "$ROOT/.env.local" >/dev/null 2>&1; then
        # Key present but empty — replace.
        if [ "$(uname)" = "Darwin" ]; then
          sed -i "" "s|^${key}=.*|${key}=${secret}|" "$ROOT/.env.local"
        else
          sed -i "s|^${key}=.*|${key}=${secret}|" "$ROOT/.env.local"
        fi
      else
        # Key absent — append.
        printf '%s=%s\n' "$key" "$secret" >> "$ROOT/.env.local"
      fi
      green "Generated $key"
    fi
  done
  cyan "Loading .env.local"
  set -a; . "$ROOT/.env.local"; set +a
fi

# docker-compose.yml gates the cloudflared service on --profile public, but
# Compose still interpolates its ${CLOUDFLARE_TUNNEL_TOKEN:?...} at validate
# time, so an empty token fails even when the profile is off. Export a
# harmless placeholder when the real value is unset.
export CLOUDFLARE_TUNNEL_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-_disabled_}"

# ── Compose up ───────────────────────────────────────────────────────────
cyan "docker compose up -d"
docker compose up -d

# ── Wait for backend health ──────────────────────────────────────────────
cyan "Waiting for backend (http://localhost:18000/healthz)"
for i in $(seq 1 30); do
  if curl -fsS --max-time 2 http://localhost:18000/healthz >/dev/null 2>&1; then
    green "Backend is ready"
    break
  fi
  sleep 2
  if [ "$i" = "30" ]; then
    warn "Backend did not come up in 60s — check: docker compose logs backend"
  fi
done

# ── Verify Ollama is up before backgrounding the model pull ──────────────
# setup-models.sh fails immediately if Ollama isn't reachable, so wait for
# /api/tags to respond instead of spawning a doomed background pull.
cyan "Waiting for Ollama (http://localhost:11434/api/tags)"
ollama_ready=false
for i in $(seq 1 30); do
  if curl -fsS --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
    green "Ollama is ready"
    ollama_ready=true
    break
  fi
  sleep 2
done
[ "$ollama_ready" = false ] && warn "Ollama did not come up in 60s — model pull skipped"

# ── Warm-pull Ollama tier models (non-blocking) ──────────────────────────
if [ "${AUTO_PULL_MODELS:-true}" = "true" ] && [ "$ollama_ready" = true ]; then
  cyan "Running setup-models.sh (background)"
  (bash "$ROOT/scripts/setup-models.sh" --skip-vision >/dev/null 2>&1 &)
fi

# ── Done ─────────────────────────────────────────────────────────────────
green "Stack is up"
echo "   Backend:    http://localhost:18000/healthz"
echo "   Open WebUI: http://localhost:3000"
echo "   VRAM stats: http://localhost:18000/api/vram"
