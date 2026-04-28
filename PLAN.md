# Plan: Merge `non-docker-dependent` Branch into `master`

## Context

PR #96 (`non-docker-dependent`) already removed Docker and replaced it with a
native Windows stack (PowerShell launcher + PySide6 GUI + vendored binaries).
This plan tracks the remaining work needed before that branch can land on
`master` as the new default.

What is **already done** on `non-docker-dependent`:

- `docker-compose.yml`, `backend/Dockerfile`, `frontend/`, `launcher/`,
  `setup.ps1`, all `scripts/start*` and `scripts/setup-*` deleted.
- Single root launcher `LocalAIStack.ps1` with subcommands
  (`-InitEnv`, `-Setup`, `-Start`, `-Stop`, `-Build`, `-CheckUpdates`,
  `-Admin`, `-Help`).
- Step helpers in `scripts/steps/` (`prereqs.ps1`, `download.ps1`,
  `process.ps1`, `venvs.ps1`).
- Native Qt GUI under `gui/` (PySide6 + QtCharts):
  - `gui/windows/chat.py` — chat (SSE, `QTextBrowser`, no WebEngine)
  - `gui/windows/login.py`
  - `gui/windows/admin.py` (read-only)
  - `gui/windows/metrics.py` (QtCharts VRAM plots)
  - `gui/widgets/tray.py` (`QSystemTrayIcon`)
- Backend defaults swapped from `http://ollama:11434` etc. to `127.0.0.1`.
- `backend/model_resolver.py` + `config/model-sources.yaml` for HF/Ollama
  registry polling with offline fallback.
- Web search via `BraveSearchProvider` / `DuckDuckGoProvider`
  (SearXNG removed entirely).
- `backend/seed_admin.py` for first-run admin bootstrap.
- `tests/test_docker_setup.py` deleted; `test_config.py` and
  `test_diagnostics.py` rewritten for the native layout.

What is **still missing** (the subject of this plan):

1. The launcher's `-InitEnv` only writes a template; user requirements say
   nothing should ever be hand-edited. We need a true setup wizard.
2. Admin auth is still magic-link only; user requirements say the wizard must
   collect an admin **email + password**.
3. Cloudflare Tunnel is documented as "BYO native cloudflared"; user
   requirements say the wizard must launch the Cloudflare dashboard, drive
   `cloudflared tunnel login`, and provision a tunnel automatically.
4. There is no end-user installer — only the developer flow
   (`git clone` + `LocalAIStack.ps1 -Setup`). User requirements say it must
   ship as an installable `.exe` with an install wizard.
5. The admin Qt window is read-only; the old Preact admin panel had write
   parity that we should not regress.

This plan addresses 1–5 in order, calls out sticking points, and sequences
the merge so nothing on `master` breaks during the transition.

---

## Phase 1 — Audit & sticking points

Before writing any new code, walk the diff between `master` and
`non-docker-dependent` and tag every callsite that still assumes Docker. The
PR description covers the obvious deletions; this phase is about finding the
**non-obvious** survivors.

### 1.1 Code paths to re-audit

| Area | What to verify | Why it matters |
|------|----------------|----------------|
| `backend/main.py` startup | All service URLs default to `127.0.0.1`, not container DNS | Backend boots before services? Health-wait already in `LocalAIStack.ps1` — confirm timeout is generous enough for cold Ollama starts (first model load can take 60–90 s). |
| `backend/diagnostics.py` | `check_searxng` is gone; web-search check uses provider abstraction | A leftover SearXNG probe will fail closed on every `-Start`. |
| `backend/rag.py` | Qdrant URL reads `QDRANT_URL` env (default `http://127.0.0.1:6333`) | RAG was the most container-coupled module. |
| `backend/admin.py` | `_admin_emails()` still keys off `ADMIN_EMAILS` env; needs a path that also accepts the wizard-seeded admin record from SQLite. | Wizard writes to DB, not env. |
| `backend/auth.py` | Magic-link path must keep working for non-admin users; password path is **additive**, not a replacement. | Don't break end-user login. |
| `backend/db.py` | Confirm SQLite path resolves to `data/lai.db` on Windows (no Docker volume mount). | Path quoting on Windows is the usual offender. |
| `backend/history_store.py` | AES-GCM key derivation reads `HISTORY_SECRET_KEY` then falls back to `AUTH_SECRET_KEY`. Both must be set by the wizard before backend boots. | Boot order: wizard writes `.env` → launcher loads `.env` → uvicorn starts. |
| `tools/jupyter_tool.py` | `JUPYTER_TOKEN` now generated per-run by the launcher (random GUID). | Old code hardcoded `local-ai-stack-token`. |
| `tools/web_search.py` | Routes through `middleware/web_search.py` provider, not SearXNG. | Already done in PR #96; verify no orphan `searxng` imports. |
| `config/models.yaml` | `tiers.vision.endpoint` points at `http://127.0.0.1:8001/v1`. | Same. |
| `tests/test_config.py` | New positive assertions for native layout (compose file gone, root launcher present). | Already done. |
| `tests/test_backends_live.py` | Gated by `LIVE_BACKEND_TESTS=1`; rewrite probes to use loopback URLs. | Currently still references container hostnames. |

**Action:** open each file and grep for the regex
`(http://(ollama|qdrant|llama-server|searxng|jupyter|n8n|redis):)` —
any hit is a bug. Treat the audit as a one-pass checklist; do not
"fix while reading", just record findings to a follow-up task list.

### 1.2 Sticking points (genuine risks)

These are the items most likely to break something during the merge.

**S1. Magic-link → password auth migration is one-way.**
The DB schema needs a `password_hash` column on `users` and a new
`password_reset_tokens` table. Once a `master` install upgrades to the new
schema, you cannot downgrade without dropping `data/lai.db`. Mitigation:
ship the migration in `backend/db.py::_migrate_*` with a clear schema
version bump (`v3 → v4`) and document the irreversibility in
`LocalAIStack.ps1 -Help` (the existing v2→v3 note is the template).

**S2. `ADMIN_EMAILS` env var is no longer the source of truth.**
`backend/admin.py::is_admin_email` currently uses `ADMIN_EMAILS` to gate
the `/admin/*` endpoints. After the wizard, admin status is stored in the
`users` table (`is_admin BOOL`). The env var should become a *fallback*
seed only — if a row already has `is_admin = 1`, that wins. Don't delete
the env path; it's how CI tests admin endpoints without a wizard run.

**S3. Cloudflare cohabitation.**
The PR description warns that two cloudflared instances flap. The wizard
must check whether `cloudflared.exe` is already running (via
`Get-Process cloudflared`) and refuse to start a second copy. If the user
already has a tunnel, the wizard offers to **adopt** it (read existing
`%USERPROFILE%\.cloudflared\config.yml`) instead of provisioning a new one.

**S4. CHAT_HOSTNAME is hardcoded in the env template.**
`.env` ships with `CHAT_HOSTNAME=chat.mylensandi.com`, which is the
maintainer's personal domain. The wizard must overwrite this with the
hostname returned by the Cloudflare API after tunnel creation, or — if
the user picks "local-only" — set it to `localhost` and force-disable the
host-gating middleware path in `backend/main.py`.

**S5. `vendor/` and `data/` paths break under
`%PROGRAMFILES%\LocalAIStack`.**
The launcher computes `$RepoRoot` from its own location, which works for
git clones but not for an installed app where the launcher lives in
read-only `Program Files` and the data should live in
`%LOCALAPPDATA%\LocalAIStack`. The packaging step must split these:
`$RepoRoot` keeps code/binaries; introduce `$Script:UserDataRoot` for
`data\`, `.env`, and logs. This is the largest single change in the
plan — gate it behind `$env:LAI_INSTALLED -eq '1'` so the dev flow still
works from a checkout.

**S6. PySide6 wheel size.**
Full PySide6 is ~180 MB; Essentials is ~120 MB. The installer download is
already dominated by Ollama models (24–72 GB), so size is a non-issue.
Stick with full PySide6 to keep `QtCharts` available.

**S7. Test coverage during the transition.**
The CI matrix on `master` runs `pytest tests/ --ignore=tests/test_results.txt`
in a Linux container. After the merge, the same suite must pass without
Docker. PR #96 already adapted `test_config.py` and `test_diagnostics.py`;
verify the CI workflow file (`.github/workflows/*.yml`) doesn't `docker
build` anywhere. If it does, replace with a plain `pip install -r
backend/requirements.txt` + `pytest`.

---
