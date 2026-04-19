#!/usr/bin/env bash
# Pull Ollama tier models and download vision GGUF + mmproj.
# Idempotent — safe to re-run.
#
# Usage:
#   bash scripts/setup-models.sh              # full tier group
#   bash scripts/setup-models.sh minimal      # fast tier only
#   bash scripts/setup-models.sh --skip-vision
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
GROUP="${1:-tiers}"
SKIP_VISION=false
for arg in "$@"; do
  case "$arg" in
    --skip-vision) SKIP_VISION=true ;;
  esac
done

cyan()  { printf "\033[36m>> %s\033[0m\n" "$*"; }
green() { printf "\033[32m   OK: %s\033[0m\n" "$*"; }
warn()  { printf "\033[33m   WARN: %s\033[0m\n" "$*"; }
fail()  { printf "\033[31m   FAIL: %s\033[0m\n" "$*" >&2; exit 1; }

# ── Ollama reachability ──────────────────────────────────────────────────
cyan "Checking Ollama at $OLLAMA_URL"
if ! curl -fsS --max-time 5 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  fail "Ollama not reachable at $OLLAMA_URL. Start it with: docker compose up -d ollama"
fi
green "Ollama is up"

# ── Parse group from ollama-models.yaml ──────────────────────────────────
YAML="$ROOT/config/ollama-models.yaml"
TAGS=$(python3 - <<PY
import yaml, sys
with open("$YAML") as f:
    data = yaml.safe_load(f)
group = data.get("groups", {}).get("$GROUP") or data.get("auto_pull", [])
for tag in group:
    print(tag)
PY
)

if [ -z "$TAGS" ]; then
  fail "Group '$GROUP' not found in $YAML"
fi

# ── Pull each tag ────────────────────────────────────────────────────────
while IFS= read -r tag; do
  [ -z "$tag" ] && continue
  cyan "Pulling $tag"
  if curl -fsS --max-time 3600 "$OLLAMA_URL/api/pull" \
       -H 'Content-Type: application/json' \
       -d "{\"name\":\"$tag\",\"stream\":false}" | grep -q '"status":"success"'; then
    green "Pulled $tag"
  else
    warn "Pull may have failed or streamed; verify with 'curl $OLLAMA_URL/api/tags'"
  fi
done <<< "$TAGS"

# ── Vision tier: GGUF + mmproj download ──────────────────────────────────
if [ "$SKIP_VISION" = true ]; then
  cyan "Skipping vision tier download (--skip-vision)"
  exit 0
fi

MODELS_DIR="$ROOT/models"
mkdir -p "$MODELS_DIR"

VISION_GGUF="$MODELS_DIR/qwen3.6-35b-a3b-Q4_K_M.gguf"
VISION_MMPROJ="$MODELS_DIR/mmproj-qwen3.6-35b-F16.gguf"

# NOTE: Official Qwen3.6 mmproj release URL — when the community builds
# settle, hardcode the actual HuggingFace URL here. For now document the
# manual-download path.
if [ ! -f "$VISION_GGUF" ]; then
  warn "Vision GGUF missing: $VISION_GGUF"
  echo "   Download from HuggingFace (Qwen/Qwen3.6-35B-A3B-Instruct-GGUF)"
  echo "   and place Q4_K_M.gguf at: $VISION_GGUF"
fi
if [ ! -f "$VISION_MMPROJ" ]; then
  warn "Vision mmproj missing: $VISION_MMPROJ"
  echo "   Download mmproj-F16.gguf from the same repo and place at: $VISION_MMPROJ"
fi

if [ -f "$VISION_GGUF" ] && [ -f "$VISION_MMPROJ" ]; then
  green "Vision tier files present"
fi

cyan "Done."
