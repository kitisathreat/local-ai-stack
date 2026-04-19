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
  fail "Docker is not running. Start Docker Desktop and re-run."
fi
green "Docker is running"

# ── env file ─────────────────────────────────────────────────────────────
if [ -f "$ROOT/.env.local" ]; then
  cyan "Loading .env.local"
  set -a; . "$ROOT/.env.local"; set +a
fi

# ── Compose up ───────────────────────────────────────────────────────────
cyan "docker compose up -d"
docker compose up -d

# ── Wait for backend health ──────────────────────────────────────────────
cyan "Waiting for backend (http://localhost:8000/healthz)"
for i in $(seq 1 30); do
  if curl -fsS --max-time 2 http://localhost:8000/healthz >/dev/null 2>&1; then
    green "Backend is ready"
    break
  fi
  sleep 2
  if [ "$i" = "30" ]; then
    warn "Backend did not come up in 60s — check: docker compose logs backend"
  fi
done

# ── Warm-pull Ollama tier models (non-blocking) ──────────────────────────
if [ "${AUTO_PULL_MODELS:-true}" = "true" ]; then
  cyan "Running setup-models.sh (background)"
  (bash "$ROOT/scripts/setup-models.sh" --skip-vision >/dev/null 2>&1 &)
fi

# ── Done ─────────────────────────────────────────────────────────────────
green "Stack is up"
echo "   Backend:    http://localhost:8000/healthz"
echo "   Open WebUI: http://localhost:3000"
echo "   VRAM stats: http://localhost:8000/api/vram"
