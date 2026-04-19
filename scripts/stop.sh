#!/usr/bin/env bash
# Bring down the local AI stack. Canonical stop — .ps1 wrappers call this via WSL.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

printf "\033[36m>> docker compose down\033[0m\n"
docker compose down
printf "\033[32m   OK: stack stopped\033[0m\n"
