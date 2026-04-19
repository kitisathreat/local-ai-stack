#!/usr/bin/env bash
# Provision a Cloudflare Tunnel for Local AI Stack.
#
# What this does:
#   1. Walks you through creating (or reusing) a Cloudflare Tunnel.
#   2. Writes the tunnel token to .env.local as CLOUDFLARE_TUNNEL_TOKEN.
#   3. Optionally creates a public hostname (DNS record) pointing the tunnel
#      at http://frontend:80 inside the compose network.
#
# Prerequisites:
#   - A Cloudflare account (free tier is fine).
#   - A domain added to that account (any domain — the tunnel can also use
#     the free <name>.trycloudflare.com subdomain if you skip DNS setup).
#   - `cloudflared` CLI installed locally, OR run via docker.
#
# Usage:
#   bash scripts/setup-cloudflared.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENVFILE="$ROOT/.env.local"

cyan()  { printf "\033[36m>> %s\033[0m\n" "$*"; }
green() { printf "\033[32m   OK: %s\033[0m\n" "$*"; }
warn()  { printf "\033[33m   WARN: %s\033[0m\n" "$*"; }

upsert() {
  local key="$1" val="$2"
  touch "$ENVFILE"
  if grep -qE "^${key}=" "$ENVFILE"; then
    sed -i.bak -E "s|^${key}=.*|${key}=${val//|/\\|}|" "$ENVFILE"
    rm -f "$ENVFILE.bak"
  else
    echo "${key}=${val}" >> "$ENVFILE"
  fi
}

cyan "Cloudflare Tunnel setup"

echo ""
echo "Cloudflare Tunnel gives you a public HTTPS endpoint without opening"
echo "ports on your router. You'll need:"
echo "  1. A Cloudflare account (free)"
echo "  2. A zero-trust 'Tunnel' created in the Cloudflare dashboard"
echo ""
echo "Easiest path:"
echo "  - Go to https://one.dash.cloudflare.com/"
echo "  - Networks -> Tunnels -> Create a tunnel"
echo "  - Name it (e.g. 'local-ai-stack')"
echo "  - In 'Install connector', copy the TOKEN (the long string after --token)"
echo ""
read -rsp "Paste your CLOUDFLARE_TUNNEL_TOKEN (input hidden): " TOKEN || true
echo
if [ -z "$TOKEN" ]; then
  warn "No token entered — skipping. Run this script again once you have one."
  exit 0
fi

upsert "CLOUDFLARE_TUNNEL_TOKEN" "$TOKEN"
green "Wrote CLOUDFLARE_TUNNEL_TOKEN to $ENVFILE"

echo ""
cyan "Configure the public hostname"
echo ""
echo "In the Cloudflare dashboard, in your tunnel's 'Public Hostname' tab:"
echo "  - Subdomain: (your choice, e.g. 'ai')"
echo "  - Domain:    (one of your CF-managed domains)"
echo "  - Path:      (blank)"
echo "  - Service type: HTTP"
echo "  - URL:       frontend:80"
echo ""
read -rp "What public hostname will you use? (e.g. ai.mydomain.tld): " HOSTNAME || true
if [ -n "$HOSTNAME" ]; then
  upsert "CLOUDFLARE_HOSTNAME" "$HOSTNAME"
  upsert "PUBLIC_BASE_URL" "https://$HOSTNAME"
  upsert "ALLOWED_ORIGINS" "https://$HOSTNAME"
  green "Set CLOUDFLARE_HOSTNAME + PUBLIC_BASE_URL + ALLOWED_ORIGINS"
else
  warn "Skipping hostname setup — magic-link emails will use localhost defaults"
fi

echo ""
cyan "Start the tunnel"
echo "   docker compose --profile public up -d cloudflared"
echo ""
echo "After it starts, your site will be live at: ${HOSTNAME:+https://$HOSTNAME}"
echo "Check tunnel status:  docker logs lai-cloudflared"
