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
