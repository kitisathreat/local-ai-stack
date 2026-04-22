#!/usr/bin/env bash
# Pull Ollama tier models and download vision GGUF + mmproj.
# Idempotent -- safe to re-run.
#
# Usage:
#   bash scripts/setup-models.sh              # full tier group
#   bash scripts/setup-models.sh minimal      # fast tier only
#   bash scripts/setup-models.sh --skip-vision
#   bash scripts/setup-models.sh --download-vision /path/to/repo/root
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
SKIP_VISION=false
DOWNLOAD_VISION=false
GROUP="tiers"

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-vision)    SKIP_VISION=true; shift ;;
    --download-vision) DOWNLOAD_VISION=true; shift
      if [[ $# -gt 0 && "$1" != --* ]]; then ROOT="$1"; shift; fi ;;
    --yes)            AUTO_YES=true; shift ;;
    -*) shift ;;
    *)  GROUP="$1"; shift ;;
  esac
done
AUTO_YES="${AUTO_YES:-false}"

cyan()  { printf "\033[36m>> %s\033[0m\n" "$*"; }
green() { printf "\033[32m   OK: %s\033[0m\n" "$*"; }
warn()  { printf "\033[33m   WARN: %s\033[0m\n" "$*"; }
fail()  { printf "\033[31m   FAIL: %s\033[0m\n" "$*" >&2; exit 1; }

# ── Ollama reachability ──────────────────────────────────────────────────────
cyan "Checking Ollama at $OLLAMA_URL"
if ! curl -fsS --max-time 5 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  fail "Ollama not reachable at $OLLAMA_URL. Start it with: docker compose up -d ollama"
fi
green "Ollama is up"

# ── Parse group from ollama-models.yaml ─────────────────────────────────────
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

# ── GPU awareness ────────────────────────────────────────────────────────────
GPU_NAME=""
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || true)
fi
if [ -n "$GPU_NAME" ]; then
  green "GPU detected: $GPU_NAME -- models will load on GPU"
else
  warn "No GPU detected. Models will run on CPU."
  warn "CPU inference for large models saturates all cores and is slow."
  warn "See docs/manual-setup.md section 1 to set up NVIDIA WSL CUDA driver."
fi

# ── Pull each tag ────────────────────────────────────────────────────────────
while IFS= read -r tag; do
  [ -z "$tag" ] && continue
  cyan "Pulling $tag"
  if curl -fsS --max-time 7200 "$OLLAMA_URL/api/pull" \
       -H 'Content-Type: application/json' \
       -d "{\"name\":\"$tag\",\"stream\":false}" | grep -q '"status":"success"'; then
    green "Pulled $tag"
  else
    warn "Pull may have streamed or partially failed; verify: curl $OLLAMA_URL/api/tags"
  fi
done <<< "$TAGS"

# ── Vision tier: GGUF + mmproj download ─────────────────────────────────────
if [ "$SKIP_VISION" = true ]; then
  cyan "Skipping vision tier download (--skip-vision)"
  cyan "Done."
  exit 0
fi

MODELS_DIR="$ROOT/models"
mkdir -p "$MODELS_DIR"

VISION_GGUF="$MODELS_DIR/qwen3.6-35b-a3b-Q4_K_M.gguf"
VISION_MMPROJ="$MODELS_DIR/mmproj-qwen3.6-35b-F16.gguf"

# HuggingFace CDN URLs (resolve/main = latest commit on default branch)
HF_REPO="https://huggingface.co/Qwen/Qwen3.6-35B-A3B-Instruct-GGUF/resolve/main"
GGUF_URL="$HF_REPO/qwen3.6-35b-a3b-Q4_K_M.gguf"
MMPROJ_URL="$HF_REPO/mmproj-qwen3.6-35b-F16.gguf"

files_present=true
[ -f "$VISION_GGUF" ]   || files_present=false
[ -f "$VISION_MMPROJ" ] || files_present=false

if [ "$files_present" = true ]; then
  green "Vision tier files already present -- skipping download"
  cyan "Done."
  exit 0
fi

if [ "$DOWNLOAD_VISION" = false ] && [ "$AUTO_YES" = false ]; then
  warn "Vision GGUF files not found in $MODELS_DIR"
  echo ""
  echo "   The vision tier needs two files (~21 GB total):"
  echo "     qwen3.6-35b-a3b-Q4_K_M.gguf  (~20 GB)"
  echo "     mmproj-qwen3.6-35b-F16.gguf  (~1 GB)"
  echo ""
  echo "   Download options:"
  echo "     A) Re-run with --download-vision to auto-download via wget (resumes on interrupt)"
  echo "     B) Download manually from:"
  echo "        https://huggingface.co/Qwen/Qwen3.6-35B-A3B-Instruct-GGUF"
  echo "        and place both files in: $MODELS_DIR"
  echo "     C) Skip -- text tiers work fine without vision"
  cyan "Done (vision skipped -- use --download-vision to auto-download)."
  exit 0
fi

# ── Automated wget download with resume support ──────────────────────────────
if ! command -v wget >/dev/null 2>&1; then
  warn "wget not found. Installing..."
  apt-get install -y wget >/dev/null 2>&1 || fail "Cannot install wget. Install manually: sudo apt-get install wget"
fi

cyan "Downloading vision GGUF files to $MODELS_DIR"
echo "   This will download ~21 GB. Progress is shown below."
echo "   If interrupted, re-run the same command -- wget resumes where it left off."
echo ""

if [ ! -f "$VISION_GGUF" ]; then
  cyan "Downloading weights (~20 GB): qwen3.6-35b-a3b-Q4_K_M.gguf"
  wget -c --show-progress \
       --tries=10 --waitretry=30 \
       -O "$VISION_GGUF" \
       "$GGUF_URL" || {
    warn "Download failed or was interrupted."
    warn "Resume by re-running: bash scripts/setup-models.sh --download-vision"
    exit 1
  }
  green "qwen3.6-35b-a3b-Q4_K_M.gguf downloaded"
fi

if [ ! -f "$VISION_MMPROJ" ]; then
  cyan "Downloading mmproj (~1 GB): mmproj-qwen3.6-35b-F16.gguf"
  wget -c --show-progress \
       --tries=10 --waitretry=30 \
       -O "$VISION_MMPROJ" \
       "$MMPROJ_URL" || {
    warn "Download failed or was interrupted."
    warn "Resume by re-running: bash scripts/setup-models.sh --download-vision"
    exit 1
  }
  green "mmproj-qwen3.6-35b-F16.gguf downloaded"
fi

green "Vision tier files present"
echo ""
echo "   Restart llama-server to pick them up:"
echo "     wsl -d Ubuntu -- docker compose restart llama-server"

cyan "Done."
