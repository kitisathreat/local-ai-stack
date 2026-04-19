#!/usr/bin/env bash
# Substitute the CLOUDFLARE_HOSTNAME placeholder in squarespace-embed.html.
# Usage:
#   bash scripts/render-embed.sh                  # reads CLOUDFLARE_HOSTNAME from .env.local
#   bash scripts/render-embed.sh ai.mydomain.tld  # explicit hostname
#
# Writes the rendered HTML to: squarespace-embed.rendered.html
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/squarespace-embed.html"
OUT="$ROOT/squarespace-embed.rendered.html"

HOSTNAME="${1:-}"
if [ -z "$HOSTNAME" ] && [ -f "$ROOT/.env.local" ]; then
  HOSTNAME=$(grep -E '^CLOUDFLARE_HOSTNAME=' "$ROOT/.env.local" | cut -d= -f2- || true)
fi
if [ -z "$HOSTNAME" ]; then
  echo "ERROR: no hostname. Pass it as an argument or set CLOUDFLARE_HOSTNAME in .env.local" >&2
  exit 1
fi

# Strip any protocol the user may have included.
HOSTNAME="${HOSTNAME#https://}"
HOSTNAME="${HOSTNAME#http://}"
HOSTNAME="${HOSTNAME%/}"

sed "s|__CLOUDFLARE_HOSTNAME__|https://${HOSTNAME}|g" "$SRC" > "$OUT"
echo "Rendered $OUT with hostname: https://$HOSTNAME"
echo "Paste the contents into a Squarespace Code Block."
