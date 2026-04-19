#!/usr/bin/env bash
# Interactive SMTP + AUTH_SECRET_KEY config for Local AI Stack's magic-link auth.
# Writes values to .env.local at the repo root. Idempotent — re-running
# overwrites only the keys it prompts for.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENVFILE="$ROOT/.env.local"

cyan()  { printf "\033[36m>> %s\033[0m\n" "$*"; }
green() { printf "\033[32m   OK: %s\033[0m\n" "$*"; }

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

cyan "AUTH_SECRET_KEY"
if grep -q "^AUTH_SECRET_KEY=.\+" "$ENVFILE" 2>/dev/null; then
  green "Already set — keeping existing value"
else
  SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
  upsert "AUTH_SECRET_KEY" "$SECRET"
  green "Generated new AUTH_SECRET_KEY"
fi

cyan "Public base URL (where users will click the magic link)"
read -rp "   [http://localhost:3000]: " PUBLIC_URL || true
PUBLIC_URL="${PUBLIC_URL:-http://localhost:3000}"
upsert "PUBLIC_BASE_URL" "$PUBLIC_URL"

cyan "SMTP host (leave blank to log links to backend stdout instead of emailing)"
read -rp "   SMTP_HOST: " SMTP_HOST || true
if [ -z "$SMTP_HOST" ]; then
  upsert "SMTP_HOST" ""
  green "Skipping SMTP — magic links will be logged in 'docker logs lai-backend'"
  exit 0
fi
upsert "SMTP_HOST" "$SMTP_HOST"

read -rp "   SMTP_PORT [587]: " SMTP_PORT || true
upsert "SMTP_PORT" "${SMTP_PORT:-587}"

read -rp "   SMTP_USER: " SMTP_USER || true
upsert "SMTP_USER" "${SMTP_USER:-}"

read -rsp "   SMTP_PASS: " SMTP_PASS || true
echo
upsert "SMTP_PASS" "${SMTP_PASS:-}"

read -rp "   AUTH_EMAIL_FROM [noreply@yourdomain.tld]: " EMAIL_FROM || true
upsert "AUTH_EMAIL_FROM" "${EMAIL_FROM:-noreply@yourdomain.tld}"

green "Wrote SMTP config to $ENVFILE"
echo "   Restart the backend to pick up changes: docker compose up -d --force-recreate backend"
