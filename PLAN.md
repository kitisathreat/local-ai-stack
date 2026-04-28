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

## Phase 2 — First-run setup wizard

Replace the dumb `-InitEnv` template-writer with a real PySide6 `QWizard`
that collects every value the stack needs and writes a fully-populated
`.env` (mode 600). The user never opens `.env` themselves.

### 2.1 Where it lives

New module `gui/windows/setup_wizard.py`. Reuses the existing GUI venv
(`vendor\venv-gui`) and the existing `gui/api_client.py` for the final
"register admin" call. Spawned by the launcher when:

```
LocalAIStack.ps1 -Setup    # always run; idempotent
LocalAIStack.ps1 -Start    # only if .env is missing OR no admin user exists
```

The launcher decides whether to spawn the wizard via a fast SQLite check
in `backend/seed_admin.py --check-only` (already exists; widen its
contract to also report "no admin user").

### 2.2 Wizard pages

Each page is a `QWizardPage` with field validation; `Next` is disabled
until the page is valid. Pages 4 and 5 are skippable.

1. **Welcome** — one-paragraph explanation, no fields. Detects whether
   prereqs (Ollama, Python, NVIDIA driver, cloudflared) are installed
   and shows green/red icons. "Install missing" button shells out to
   `LocalAIStack.ps1 -Setup -SkipModels` in a worker thread with a
   progress log streamed via `QPlainTextEdit`.

2. **Admin account**
   - Email (`QLineEdit`, regex-validated)
   - Password (`QLineEdit` with `setEchoMode(Password)`, min 12 chars,
     zxcvbn score ≥ 3 — vendored copy in `vendor\venv-gui` from the
     `zxcvbn` PyPI package; falls back to a length+entropy heuristic
     if the import fails)
   - Confirm password
   - The only output of this page is two strings held in
     `wizard.field("admin_email")` and `wizard.field("admin_password")`.
     Nothing is written to disk yet.

3. **Secrets (no user input)**
   - Generates `AUTH_SECRET_KEY` and `HISTORY_SECRET_KEY` via
     `secrets.token_urlsafe(48)`.
   - Page renders as "Generating cryptographic keys… ✓" with a 200 ms
     fake delay so the user sees what's happening.
   - Values are held in wizard fields; not yet persisted.

4. **Public access (Cloudflare Tunnel) — skippable**
   - Radio buttons: "Local-only" / "Public via Cloudflare Tunnel".
   - "Public" reveals two fields: domain (e.g. `mylensandi.com`) and
     desired chat hostname (default `chat.<domain>`).
   - "Connect to Cloudflare" button kicks off the flow described in
     Phase 3. Wizard cannot advance until the flow either succeeds,
     errors out, or the user reverts to "Local-only".

5. **Email delivery (SMTP) — skippable**
   - Used only for magic-link emails to *non-admin* users. The admin
     user already has a password and never needs SMTP.
   - Default state: skip (radio "Skip — print magic links to logs").
   - Optional fields: SMTP host/port/user/pass/from-address/STARTTLS.
   - If skipped, sets `SMTP_HOST=` (empty) in `.env`; backend already
     handles this by logging links instead.

6. **Models — skippable but recommended**
   - List of tier groups from `config/model-sources.yaml` with size
     estimates and checkboxes (`minimal` ≈ 7 GB, `tiers` ≈ 60 GB,
     `vision` ≈ 25 GB).
   - "Download now" runs `python -m backend.model_resolver resolve
     --pull` in a worker thread; progress shown live.
   - Skipping is fine — the user can re-run from the admin panel later.

7. **Finish**
   - Atomically writes `.env` (write to `.env.tmp`, `os.replace`).
   - Calls `python -m backend.seed_admin --email --password --admin`
     to insert the admin row with `passlib`-hashed password.
   - Closes wizard; launcher continues into `-Start`.

### 2.3 Where values land

The wizard writes a single `.env` file at the repo root (or
`%LOCALAPPDATA%\LocalAIStack\.env` once Phase 5 splits user data — see
S5). Contents after a successful run:

```
AUTH_SECRET_KEY=<48-char base64>           # auto-generated
HISTORY_SECRET_KEY=<48-char base64>        # auto-generated
PUBLIC_BASE_URL=https://chat.<domain>      # or http://localhost:18000
CHAT_HOSTNAME=chat.<domain>                # or localhost
ADMIN_API_ALLOWED_HOSTS=127.0.0.1,localhost
WEB_SEARCH_PROVIDER=ddg                    # or brave if BRAVE_API_KEY supplied
BRAVE_API_KEY=<optional>
HF_TOKEN=<optional>
SMTP_HOST=...                              # blank if skipped
CLOUDFLARE_TUNNEL_ID=<uuid>                # written by Phase 3 flow
CLOUDFLARE_TUNNEL_NAME=local-ai-stack
MODEL_UPDATE_POLICY=prompt
```

Admin email + password go to **SQLite** (`users` table), not `.env`.

### 2.5 Backend changes required

- `backend/db.py::_migrate_*` — schema bump v3 → v4, adds
  `password_hash TEXT NULL` and `is_admin INTEGER NOT NULL DEFAULT 0`
  to `users`.
- `backend/auth.py` — new `verify_password()` and
  `POST /auth/password` route. Magic-link routes stay as-is for
  non-admin users. Add a `password_required` flag on the user row;
  admins are required to use a password, end-users still magic-link.
- `backend/seed_admin.py` — accept `--email`, `--password`, `--admin`
  flags; idempotent (`INSERT … ON CONFLICT DO UPDATE`).
- `backend/admin.py::is_admin_email` → renamed to `is_admin_user`,
  reads `is_admin` from SQLite first, falls back to `ADMIN_EMAILS` env
  for CI.
- New dep in `backend/requirements.txt`: `passlib[bcrypt]==1.7.4`.

---

## Phase 3 — Cloudflare auto-provisioning

The user's requirement: the wizard launches a browser to the Cloudflare
dashboard, the user logs in, and the wizard then auto-creates the tunnel.
This is the **only** browser launch in the entire app; everything else is
native Qt.

### 3.1 Why a browser is unavoidable here

Cloudflare's `cloudflared tunnel login` flow uses OAuth. There is no
machine-to-machine alternative for free-tier users (the API-token path
exists but requires the user to provision the token in the dashboard
first, which still puts them in a browser). Embedding the OAuth dance
in our own Qt window via `QtWebEngine` would technically work but
violates the "no embedded browser engine" decision in the PR (PySide6
ships without WebEngine to keep the venv at ~120 MB). So: shell out to
the system default browser exactly once, and only for Cloudflare.

### 3.2 The flow

Lives in a new helper `gui/cloudflare_setup.py`, called from
`gui/windows/setup_wizard.py` page 4.

1. **Pre-check.** If `%USERPROFILE%\.cloudflared\cert.pem` exists and is
   under 90 days old, jump to step 4 (the user already authorized us
   on a previous run). Otherwise continue.

2. **Spawn `cloudflared tunnel login`.** Worker thread runs:
   ```
   vendor\cloudflared\cloudflared.exe tunnel login
   ```
   `cloudflared` prints a URL to stdout and opens it in the system
   default browser automatically. We capture stdout and *also* show the
   URL inside the wizard ("If your browser didn't open, click here") via
   a `QLabel` with `setOpenExternalLinks(True)` — never as an embedded
   page.

3. **Poll for `cert.pem`.** While the worker thread waits on
   `cloudflared`, the wizard shows a `QProgressIndicator` with
   "Waiting for Cloudflare login… (you can close the browser tab once
   you see 'Tunnel certificate written')". A `QFileSystemWatcher` on
   `%USERPROFILE%\.cloudflared\` fires when `cert.pem` lands;
   `cloudflared` exits 0 shortly after.

4. **List zones.** With the cert in hand, hit
   `GET https://api.cloudflare.com/client/v4/zones` using the cert as
   bearer (`cloudflared` writes a JSON-encoded API token to the cert
   file; reuse it). Show the user a `QComboBox` of their zones; default
   to the one matching the domain they entered on page 4.

5. **Create the tunnel.** Worker thread runs:
   ```
   cloudflared.exe tunnel create local-ai-stack-<hostname-slug>
   ```
   This writes `<UUID>.json` (the credentials file) into
   `%USERPROFILE%\.cloudflared\` and prints the tunnel UUID. Capture
   the UUID.

6. **Route DNS.** Worker thread runs:
   ```
   cloudflared.exe tunnel route dns <UUID> chat.<domain>
   ```
   This is the moment the chat hostname becomes resolvable.

7. **Write `config.yml`.** Generate
   `%USERPROFILE%\.cloudflared\config.yml`:
   ```yaml
   tunnel: <UUID>
   credentials-file: C:\Users\<user>\.cloudflared\<UUID>.json
   ingress:
     - hostname: chat.<domain>
       service: http://localhost:18000
     - service: http_status:404
   ```
   The 404 fallback is critical — listed *after* the chat hostname so
   the host-gating order in `LocalAIStack.ps1 -Help` is preserved.

8. **Persist to `.env`.** The wizard sets:
   ```
   CLOUDFLARE_TUNNEL_ID=<UUID>
   CLOUDFLARE_TUNNEL_NAME=local-ai-stack-<slug>
   CHAT_HOSTNAME=chat.<domain>
   PUBLIC_BASE_URL=https://chat.<domain>
   ```

9. **Register cloudflared as a Windows service.** Worker thread runs:
   ```
   cloudflared.exe service install
   ```
   This makes the tunnel survive reboots without keeping
   `LocalAIStack.exe` open. Service runs as `LocalSystem`, which is
   required for the Windows service installer; document this in
   `-Help`. Add a `LocalAIStack.ps1 -DisableTunnel` subcommand that
   runs `cloudflared.exe service uninstall` for users who want to
   revert.

### 3.3 Failure & retry

- Network failure during steps 2/4/5/6 → wizard surfaces the captured
  stderr in a `QPlainTextEdit`, offers "Retry" and "Skip (local-only)".
- Step 9 fails (UAC declined) → wizard warns that the tunnel won't
  auto-start on boot; the user can finish setup and run `-DisableTunnel`
  / `-EnableTunnel` later.
- Existing `cloudflared` service from a previous install (S3) →
  detect via `Get-Service cloudflared`, offer "Adopt existing",
  "Replace", or "Cancel".

### 3.4 What never happens

- We never ask the user for a Cloudflare API key.
- We never embed `dash.cloudflare.com` in a Qt webview.
- We never write the tunnel token (the long opaque string from the
  legacy `--token` flow) anywhere — the credentials file is the
  modern equivalent and `cloudflared` reads it from
  `%USERPROFILE%\.cloudflared\` on its own.

---

## Phase 4 — Native admin GUI write parity

`gui/windows/admin.py` currently ships read-only (per PR #96 follow-ups).
The old Preact admin panel (`frontend/src/admin.tsx`, deleted) had write
parity; we must rebuild that in PySide6 so admins never need a browser.

### 4.1 Sections to port

Each becomes a `QTabWidget` page in `admin.py`. All edits hit existing
backend routes (`/admin/config/*`) — no new HTTP surface.

| Tab | Backing route | Widget |
|-----|---------------|--------|
| Models & tiers | `GET/PATCH /admin/config/models` | `QTableView` over `config/models.yaml` with inline aliases editor |
| Router | `GET/PATCH /admin/config/router` | Form: regex list, keyword counts, token thresholds |
| VRAM scheduler | `GET/PATCH /admin/config/vram` | Numeric spinboxes for headroom, eviction, pinning |
| Auth | `GET/PATCH /admin/config/auth` | Magic-link TTL, cookie TTL, allowed domains, rate limits |
| Tools | `GET/PATCH /admin/config/tools` | Two-column list: enabled / available, drag to enable |
| Users | `GET /admin/users`, `PATCH /admin/users/{id}` | Table with admin toggle, password reset trigger |
| Errors | `GET /admin/errors` | Read-only table of recent backend errors |
| Multi-agent test | `POST /admin/test/multi_agent` | Same SSE stream the old admin panel had, rendered with `QTextBrowser` |
| Live metrics | reuses `gui/windows/metrics.py` | Already done; embed as a tab here too |

### 4.2 Patterns

- Every tab has a **Reload** and **Save** button. Save is disabled until
  any field changes (Qt's `dataChanged` signal).
- Saves run through a single `gui/api_client.py::patch_config(section,
  payload)` helper with optimistic UI + rollback on HTTP error.
- Hot-reload: backend already supports YAML hot-reload on PATCH (see
  `backend/admin.py::_set_deep`). Confirm via a "Config reloaded ✓"
  toast in the status bar.
- Restart-required fields (`BACKEND_WORKERS`, `REDIS_URL`) get a yellow
  badge and a "Restart required" line under the field. Saving them
  flips the launcher tray icon to amber until the user clicks
  "Restart services" (which runs `LocalAIStack.ps1 -Stop` + `-Start`
  in a worker process).

### 4.3 What is **not** ported

- The old web admin had a "Run multi-agent benchmark" button that
  streamed events. Keep it functionally, but in Qt — `QTextBrowser`
  appending lines from an `httpx.AsyncClient.stream` reader. No
  embedded browser.
- The old web admin embedded a Grafana iframe for long-range metrics.
  Replace with QtCharts time-series in the metrics tab; if a user
  *really* wants Grafana, document that they can run it standalone
  and we'll surface a configurable URL field that opens in the system
  browser (one-shot, like the Cloudflare login).

### 4.4 Auth gate

`-Admin` already spawns the GUI in admin-only mode. Update
`gui/windows/login.py` to default to password mode when the target
user has `password_required=True` (admins do), and fall back to
magic-link otherwise. Backend route: new `POST /auth/password`
returning the same `lai_session` JWT cookie.

---

## Phase 5 — Installable `.exe` with install wizard

End state: a single `LocalAIStackInstaller-<version>.exe` the user
double-clicks. It installs the app under `%PROGRAMFILES%\LocalAIStack\`,
creates Start Menu + Desktop shortcuts, and on first launch runs the
Phase 2 setup wizard.

### 5.1 Installer toolchain

Use **Inno Setup 6** (free, MIT-style license, scriptable, native
Windows look-and-feel, signed-installer support). Reasons over
alternatives:

- **NSIS**: more painful scripting, weaker UAC handling.
- **MSIX/WiX**: enterprise-grade but overkill; needs MSI authoring
  expertise we don't have.
- **PyInstaller alone**: produces an `.exe` but no installer, no Start
  Menu integration, no UAC elevation, no service registration.

Inno Setup script lives at `installer\LocalAIStack.iss`. Build via a
new launcher subcommand:

```
LocalAIStack.ps1 -BuildInstaller    # produces dist\LocalAIStackInstaller-<ver>.exe
```

That subcommand:
1. Runs `-Build` (ps2exe → `LocalAIStack.exe`).
2. Runs the Phase 2 GUI freezer (PyInstaller one-folder mode → `dist\gui\`).
3. Invokes `iscc.exe installer\LocalAIStack.iss` (Inno Setup compiler,
   bundled in `vendor\inno-setup\` so CI doesn't need a global install).

### 5.2 What the installer ships

**Bundled (~600 MB compressed):**
- `LocalAIStack.exe` (ps2exe-compiled launcher)
- `gui\` (PyInstaller-frozen PySide6 app, ~250 MB)
- `backend\` (Python source, run via the venv-backend interpreter we
  install at runtime — keeps installer small)
- `tools\`, `config\` (data files, ~5 MB)
- `vendor\python-3.12-embed\` (~30 MB; embeddable Python distro,
  used to bootstrap the venvs without requiring a system Python)
- `vendor\qdrant\qdrant.exe` (~40 MB)
- `vendor\llama-server\llama-server.exe` (~220 MB)
- `vendor\cloudflared\cloudflared.exe` (~30 MB)
- `assets\icon.ico`, license, README

**Downloaded post-install (during Setup wizard):**
- Ollama (winget; ~150 MB)
- Ollama models (24–72 GB)
- Vision GGUF (~25 GB, optional)
- `passlib`, `httpx`, `fastapi`, etc. into `venv-backend` (~250 MB)
- PySide6 deps not already frozen (none if PyInstaller mode is used)

This split keeps the installer at ~600 MB instead of ~80 GB.

### 5.3 Inno Setup script outline

```
[Setup]
AppId={{LOCAL-AI-STACK-GUID}}
AppName=Local AI Stack
AppVersion=#{Version}
DefaultDirName={pf}\LocalAIStack
DefaultGroupName=Local AI Stack
PrivilegesRequired=admin
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputBaseFilename=LocalAIStackInstaller-#{Version}

[Files]
Source: "dist\LocalAIStack.exe"; DestDir: "{app}"
Source: "dist\gui\*"; DestDir: "{app}\gui"; Flags: recursesubdirs
Source: "backend\*"; DestDir: "{app}\backend"; Flags: recursesubdirs
Source: "tools\*"; DestDir: "{app}\tools"; Flags: recursesubdirs
Source: "config\*"; DestDir: "{app}\config"; Flags: recursesubdirs
Source: "vendor\*"; DestDir: "{app}\vendor"; Flags: recursesubdirs
Source: "assets\icon.ico"; DestDir: "{app}\assets"

[Icons]
Name: "{group}\Local AI Stack"; Filename: "{app}\LocalAIStack.exe"; \
    IconFilename: "{app}\assets\icon.ico"
Name: "{group}\Stop Local AI Stack"; Filename: "{app}\LocalAIStack.exe"; \
    Parameters: "-Stop"
Name: "{commondesktop}\Local AI Stack"; Filename: "{app}\LocalAIStack.exe"; \
    Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
; Run -Setup -SkipModels at the end so prereq installs + venv creation
; happen as admin, then the wizard launches at first user logon.
Filename: "{app}\LocalAIStack.exe"; \
    Parameters: "-Setup -SkipModels"; \
    StatusMsg: "Installing prerequisites and creating Python environments..."; \
    Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "{app}\LocalAIStack.exe"; Parameters: "-Stop"; Flags: runhidden
Filename: "{app}\vendor\cloudflared\cloudflared.exe"; \
    Parameters: "service uninstall"; Flags: runhidden skipifdoesntexist

[Code]
function InitializeSetup(): Boolean;
begin
  // Refuse to run on non-x64 / non-Windows-10+
  Result := IsWin64 and (GetWindowsVersion >= $0A000000);
  if not Result then
    MsgBox('Local AI Stack requires 64-bit Windows 10 or newer.', mbError, MB_OK);
end;
```

### 5.4 Per-user vs. per-machine layout

Installer (per-machine, admin-required):

- `%PROGRAMFILES%\LocalAIStack\` — code, vendored binaries (read-only)

Runtime (per-user, no admin):

- `%LOCALAPPDATA%\LocalAIStack\` — `.env`, `data\lai.db`, `data\history\`,
  `data\qdrant\`, `data\models\`, `logs\`, `pids.json`

The launcher honors `$env:LAI_INSTALLED='1'` (set by an installer-written
registry key) to switch path resolution between dev (repo-root) and
installed (split) layouts. This is the change called out as **S5** in
Phase 1.

### 5.5 Code-signing (optional, do later)

Unsigned `.exe`s trigger SmartScreen on every download. Signing requires
either an EV cert (~$300/yr, no warm-up) or a standard cert (~$80/yr,
multi-month SmartScreen warm-up). Defer until the installer flow is
stable. Document the SmartScreen "More info → Run anyway" workaround
in the README.

### 5.6 Auto-update

Out of scope for the first installer release. Document a manual
"download new installer, run, it overwrites in place" path; Inno Setup
handles upgrade detection via `AppId`. Persistent data in
`%LOCALAPPDATA%` is untouched on upgrade.

---

### 2.4 Re-running the wizard

`LocalAIStack.ps1 -Setup` always re-runs the wizard, but each page reads
its current value from `.env` / SQLite and pre-fills. The user can step
through to change a single field (e.g. rotate the Cloudflare tunnel) and
the rest are no-ops. This is the supported "edit env" UX — there is no
file the user is expected to open in a text editor.

---
