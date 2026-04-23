# Native Windows + Domain-Gated Chat + Username/Password Auth

## Context

PR #96 (`non-docker-dependent` → `master`, currently open, CI green) already removed Docker, deleted the Preact frontend, and scaffolded a PySide6 Qt GUI with a consolidated `LocalAIStack.ps1` launcher. A code review of that PR surfaced seven implementation gaps; the user also wants three substantive additions on top of the baseline:

1. **First-run prerequisites bootstrap** — `-Setup` must install Git, Python 3.12, PowerShell 7, Ollama, and cloudflared via winget, detect the NVIDIA driver + CUDA runtime (warn + link, never auto-install drivers), and verify Windows build ≥ 19041 before anything else.
2. **Replace magic-link auth with username + password**, stored as bcrypt hashes in the existing SQLite `users` table. Admin accounts are seeded at first-run by `-Setup`. Chat users are created from the Qt admin window.
3. **Chat is reachable only via `https://chat.mylensandi.com`** (the user's existing native `cloudflared` fronts it). In normal mode the backend rejects chat requests whose `Host` header is anything else. In airgap mode the hostname check is lifted and the local Qt chat window becomes usable by any logged-in user.
4. **Admin GUI is a separate Qt window** with its own username/password login dialog, launched via tray → *Open Admin* or `LocalAIStack.ps1 -Admin`.
5. **Minimal vanilla HTML/JS chat page** served by the backend at `/` and fetched over the cloudflared tunnel by browsers on any device. No build step, no framework — one HTML file + inline `<script>` using `fetch` + SSE.

The seven PR #96 gaps (stale `/app/data` default, no GUI auth, vision GGUF never downloaded, `$args` shadow in `Invoke-Build`, quadratic markdown re-render, dead `setAttribute` in tray, no SHA pinning on downloads) are all fixed in this plan.

Branch for this work: continue on `non-docker-dependent`. Each phase below is a separate commit; CI should stay green at every step.

## Final repo shape (post-plan)

```
LocalAIStack.ps1                    # One launcher: -Setup/-Start/-Stop/-Admin/-Build/-InitEnv/-Help
LocalAIStack.exe                    # ps2exe output of the above
.env                                # Single env file
.gitignore, LICENSE, README.md      # keep
assets/icon.ico                     # NEW — committed placeholder (currently only .gitkeep)

backend/
  main.py                           # + Host-gating middleware, serves static chat page at /
  auth.py                           # replaced with username/password + bcrypt
  admin.py                          # + POST /admin/users (create) + POST /admin/users/{id}/password
  db.py                             # + password_hash column migration + is_admin flag
  middleware/host_gate.py           # NEW — enforces CHAT_HOSTNAME unless airgap on
  static/chat.html                  # NEW — single-file vanilla chat UI served at /
  model_resolver.py                 # + Hugging Face file download (closes vision-GGUF gap)
  middleware/web_search.py          # unchanged from PR #96
gui/
  main.py                           # dispatches to ChatWindow or AdminWindow based on --mode
  api_client.py                     # + login(), register(), change_password(); token in memory
  widgets/
    login_dialog.py                 # NEW — username/password modal
    markdown_view.py                # + debounced append (fix quadratic re-render)
    tray.py                         # - dead setAttribute; + launches admin as separate process
  windows/
    chat.py                         # + airgap-aware gate; "chat is hosted at chat.mylensandi.com" card
    admin.py                        # Users tab with CRUD, Airgap toggle, Config view
    metrics.py                      # unchanged
scripts/steps/
  process.ps1                       # rename $Args -> $ArgumentList
  download.ps1                      # + SHA256 verification from pinned hashes
  venvs.ps1                         # unchanged
  prereqs.ps1                       # NEW — Windows version + winget + NVIDIA detection
config/
  auth.yaml                         # drop magic_link block, add password_policy block
  model-sources.yaml                # unchanged from PR #96
tests/
  test_auth.py                      # rewritten for username/password
  test_db.py                        # drop magic_link tests, add password_hash tests
  test_host_gate.py                 # NEW
  test_model_resolver.py            # NEW
```

Deleted (on top of PR #96's deletions):
- `backend/auth.py` magic-link helpers (`send_magic_email`, `check_rate_limits`)
- `backend/db.py` magic-link table + functions (`create_magic_link`, `consume_magic_link`, `count_recent_magic_links_for_email`)
- `/auth/request` and `/auth/verify` route handlers in `main.py`
- `config/auth.yaml` `magic_link:` block and `rate_limits.requests_per_hour_per_email`
- `aiosmtplib` dependency from `backend/requirements.txt`
- `SMTP_*` / `AUTH_EMAIL_FROM` from the `.env` template

## Phase 0 — Baseline (already shipped in PR #96)

Docker removed, Preact frontend deleted, consolidated `LocalAIStack.ps1` at root, PySide6 Qt GUI scaffolded, `backend/model_resolver.py` + `config/model-sources.yaml` added, `web_search` middleware rewritten around Brave/DDG/None providers. CI passes on five jobs. This phase is the starting point; everything below builds on it.

## Phase 1 — Fix the seven PR #96 gaps

Small, targeted patches on the current branch. One commit.

1. **`backend/main.py` `/resolved-models` default path.** Replace the hardcoded `/app/data` fallback with a repo-relative resolution:
   ```python
   data_dir = Path(os.getenv("LAI_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
   ```
   Add a test in `tests/test_main.py` (new file) that hits the endpoint with `LAI_DATA_DIR` unset and asserts it finds `data/resolved-models.json`.

2. **`gui/widgets/tray.py:40` dead `setAttribute`.** Delete the line `w.setAttribute(w.windowFlags() | 0, True)` — it passes a `QFlags[Qt.WindowType]` where a `Qt.WidgetAttribute` is expected.

3. **`LocalAIStack.ps1 Invoke-Build` `$args` shadow.** Rename the hashtable from `$args` to `$splat` and splat with `@splat` into `Invoke-ps2exe`. Confirm the cmdlet name matches what `Import-Module ps2exe` exports.

4. **`gui/widgets/markdown_view.py` quadratic re-render.** Add a 60 ms coalesce timer: `append_markdown` pushes the delta into a pending buffer and arms a `QTimer.singleShot(60, self._flush)`; `_flush` parses and renders once per interval. Keep the append cursor at end.

5. **`gui/windows/chat.py` Send-button race.** Disable `Send` + `Ctrl+Return` shortcut while `_stream_reply` is running; re-enable in a `finally` block.

6. **`scripts/steps/download.ps1` SHA verification.** Add a `-Sha256` parameter to `Invoke-DownloadQdrant` / `Invoke-DownloadLlamaServer`. Hardcode expected hashes for the pinned versions. After download, compute the SHA256 and reject the archive if it doesn't match (delete + error). Ship updated hashes when bumping pinned versions.

7. **`LocalAIStack.ps1 Invoke-Stop` PID reuse.** At `-Start` time, capture `(Get-Process -Id <pid>).ProcessName` alongside each PID in `pids.json`. In `-Stop`, re-check the process name before `Stop-Process` — skip with a warning if it doesn't match.

Verification: `pytest tests/test_main.py`, manual `-Build` run, manual send-spam in the Qt chat window. Commit: `fix(gui,launcher): address PR #96 review gaps`.

## Phase 2 — Prerequisites bootstrap on first `-Setup`

A new first step in `Invoke-Setup` that gates on prerequisites before touching venvs or binaries. One commit.

New file `scripts/steps/prereqs.ps1`, dot-sourced by `LocalAIStack.ps1`. Exports `Invoke-EnsurePrereqs`.

**Checks in order, each with a deterministic pass/fail/install branch:**

1. **Windows version.** `[System.Environment]::OSVersion.Version.Build -ge 19041`. If not, print a hard error and exit; no workaround.

2. **NVIDIA driver + CUDA runtime** (detect-only, never auto-install).
   - Run `nvidia-smi --query-gpu=driver_version --format=csv,noheader`. Parse major version.
   - Fail with guidance if driver < 550 or `nvidia-smi` missing: print the exact URL `https://www.nvidia.com/Download/index.aspx` and explain CUDA 12 runtime comes bundled.
   - Continue with a yellow warning if driver ≥ 550 but `nvcc --version` isn't on PATH (the runtime is bundled with the driver now, so this is informational only).

3. **winget-installed tools.** For each `(id, exe, args)` tuple below, check `Get-Command <exe>`; if missing, run `winget install --id <id> --silent --accept-source-agreements --accept-package-agreements`. Each triggers one UAC prompt:
   ```
   Git.Git               git       --version
   Python.Python.3.12    python    --version   (expect 3.12.x)
   Microsoft.PowerShell  pwsh      --version   (PS 7)
   Ollama.Ollama         ollama    --version
   Cloudflare.cloudflared cloudflared --version
   ```
   After each install, re-probe `Get-Command` — on fresh PATH-miss, source `$env:Path` from the registry (`[Environment]::GetEnvironmentVariable('Path','Machine')`) so the current session sees newly-installed binaries without a shell restart.

4. **Execution policy.** If `Get-ExecutionPolicy -Scope CurrentUser` is `Restricted` or `Undefined`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force`.

5. **Report.** Print a table of checked/installed/skipped items. `Invoke-Setup` continues only if all hard checks pass.

**Idempotency.** Re-runs do nothing when everything is present. Safe to call on every `-Setup`.

**User-facing help update.** `-Help` gains a "What -Setup installs" section listing the five winget packages + the NVIDIA expectation.

Verification: clean Windows 11 VM, run `.\LocalAIStack.ps1 -Setup`, confirm exactly one UAC prompt per missing tool and zero on re-run.

## Phase 3 — Replace magic-link auth with username + password

Wholesale swap. One commit that adds the password path and deletes the magic-link code atomically; there is no transition period since there are no production users yet.

**Schema migration** (`backend/db.py`):
- Add columns to `users`: `username TEXT UNIQUE NOT NULL DEFAULT ''`, `password_hash TEXT NOT NULL DEFAULT ''`, `is_admin INTEGER NOT NULL DEFAULT 0`.
- Drop table `magic_links` and its index.
- New helpers: `create_user(username, email, password_hash, is_admin=False)`, `get_user_by_username(username)`, `verify_user_password(username, password)` (constant-time compare via `bcrypt.checkpw`), `set_user_password(user_id, new_hash)`, `set_user_admin(user_id, is_admin)`.
- Bump `SCHEMA_VERSION`; new `migrate_v2_to_v3(conn)` adds the columns and drops the magic-links table. Safe to run on empty DB.

**New password utility** (`backend/passwords.py`):
- `hash_password(plain) -> str` using bcrypt (cost 12).
- `verify_password(plain, hash) -> bool`.
- `bcrypt==4.2.0` added to `backend/requirements.txt`.

**Endpoint rewrite** (`backend/main.py`):
- Delete `/auth/request` and `/auth/verify`.
- Add `POST /auth/login` — body `{username, password}`. On success, set `lai_session` cookie (reuses the existing JWT machinery) and return `{ok: true, is_admin}`. On failure, 401 with a constant-time delay. Rate-limit by IP (5/min, 20/hour).
- Add `POST /auth/logout` — clear the cookie. Unchanged from existing.
- Add `POST /auth/change-password` — requires current user, body `{current_password, new_password}`. Verifies the current password, re-hashes.
- Add `POST /admin/users` — admin only, body `{username, email, password, is_admin}`. Creates a new user.
- `PATCH /admin/users/{id}` — admin only, body `{username?, email?, password?, is_admin?}`.
- Existing `DELETE /admin/users/{id}` stays (still prevents self-delete).

**Admin gating refactor** (`backend/admin.py`):
- Delete `_admin_emails()` and `is_admin_email()`.
- `require_admin` now checks `user.is_admin` (column) instead of env allowlist.
- `ADMIN_EMAILS` env var retired; `/admin/me` returns `{username, email, is_admin, admin_configured}` where `admin_configured` means "at least one admin user exists in the DB."

**Config cleanup** (`config/auth.yaml`):
- Delete the entire `magic_link:` block.
- Delete `rate_limits.requests_per_hour_per_email`. Keep `rate_limits.requests_per_hour_per_ip` (now enforced on `/auth/login`), per-minute/per-day user limits (still enforced on `/v1/chat/completions`).
- Session block unchanged.

**`.env` cleanup** (`LocalAIStack.ps1` template + `-Help` text): drop `SMTP_*` and `AUTH_EMAIL_FROM` entries; drop `ADMIN_EMAILS`. Keep `AUTH_SECRET_KEY` (JWT signing) and `HISTORY_SECRET_KEY` (history encryption).

**`auth.py` module cleanup**: delete `send_magic_email`, `check_rate_limits` (magic-link variant), `aiosmtplib` import, `email.message` import. Keep `issue_session_token`, `decode_session_token`, `current_user`, `optional_user`, `_secret_key`. `aiosmtplib` removed from `backend/requirements.txt`.

**First-run admin bootstrap** — handled in Phase 4 (Qt admin window). At the end of `-Setup`, the launcher checks the `users` table via `python -m backend.seed_admin --if-no-admins` (new CLI) — if zero admin rows exist, it prompts in the PowerShell terminal:
```
No admin user exists. Create one now?
  Username: _______
  Email:    _______
  Password: _______ (bcrypt-hashed, never logged)
```
Writes the row via `create_user(..., is_admin=True)`.

**Tests to update/delete**:
- `tests/test_auth.py` — delete `test_send_magic_email_no_smtp_env_logs_and_returns`, `test_rate_limit_triggers`, `test_rate_limit_allows_below_threshold`. Add `test_password_hash_roundtrip`, `test_login_wrong_password_is_401`, `test_login_rate_limited_after_5`, `test_change_password_requires_current`, `test_session_cookie_issued_on_login`.
- `tests/test_db.py` — delete all 5 `test_magic_link_*`. Add `test_create_user_duplicate_username_rejected`, `test_verify_user_password_wrong_is_false`, `test_migrate_v2_to_v3_adds_columns`.
- `tests/test_middleware.py` — no changes expected (middleware pipeline untouched).

Commit: `feat(auth): replace magic-link with bcrypt username/password`.

## Phase 4 — Admin GUI as a separate Qt window with its own login

The Qt chat window and the Qt admin window become independent `QMainWindow` instances. The admin window requires username + password every time it opens; closing it invalidates nothing (chat session continues separately).

**New launcher subcommand**: `.\LocalAIStack.ps1 -Admin` → spawns `vendor/venv-gui/Scripts/pythonw.exe gui/main.py --mode admin --api http://127.0.0.1:18000`. The tray menu gets an *Open Admin* entry that does the same.

**New Qt module `gui/windows/login.py`**:
- `LoginDialog(client, *, require_admin)` — modal `QDialog` with username + password fields, "Remember me" checkbox (stores JWT in `QSettings` under `LocalAIStack/session_token`), "Sign in" button.
- On submit, calls `client.login(username, password)` which POSTs `/auth/login`, stores the session cookie, and returns `{is_admin}`.
- If `require_admin=True` and the returned user isn't admin, shows "This account is not an admin" and stays open.
- Constant-time error reporting ("Invalid username or password" for both 401 paths).

**`gui/api_client.py` additions**:
- `async def login(username, password) -> dict` — posts credentials, persists cookie via `httpx.CookieJar` on the client, stores token via `QSettings`.
- `async def logout()` — POST `/auth/logout`, wipe cookie jar + QSettings.
- `async def me() -> dict` — returns `{username, email, is_admin}` from `/admin/me`.
- Autoload: on `BackendClient.__init__`, if a token is stored in QSettings and not expired, attach it to the cookie jar so subsequent requests are authenticated.

**`gui/windows/admin.py` rewrite** to match the restored backend admin surface:
- Tabs: *Users*, *Models*, *Tools*, *Airgap*, *Config*, *Metrics*.
- **Users** tab: `QTableWidget` listing users (username, email, is_admin, created_at). Buttons: *Add user*, *Edit*, *Change password*, *Delete*, *Toggle admin*. All wired to `POST/PATCH/DELETE /admin/users/...`.
- **Models** tab: reads `/resolved-models` and `/v1/models`, shows tier status (source, identifier, origin, update_available). *Pull update* button triggers `/admin/models/pull` (new endpoint — see below).
- **Tools** tab: `/admin/tools` GET list with checkboxes for enabled/disabled; PATCH on toggle.
- **Airgap** tab: single toggle bound to `PATCH /admin/airgap`. Large banner warning explains "chat subdomain will become inaccessible; local Qt chat becomes available to all logged-in users."
- **Config** tab: read-only YAML viewer for `config/{models,router,vram,auth,runtime}.yaml`. Editing is optional follow-up.
- **Metrics** tab: `QChart` with VRAM-per-tier line series polled from `/admin/vram` + `/admin/overview`.

**New backend endpoint** `POST /admin/models/pull` (body `{tier}`): triggers `backend.model_resolver.pull_missing_ollama_tags()` or `hf_hub_download` for HF tiers; streams progress as SSE so the admin UI can show a progress bar. Admin-only.

**`gui/main.py` dispatch**:
- `--mode chat` (default) — opens `ChatWindow`, sets up tray with *Open Admin* / *View Logs* / *Quit*.
- `--mode admin` — skips the chat window, opens `LoginDialog(require_admin=True)` → on success opens `AdminWindow`. No tray.
- Sharing a single app: if the user opens admin from the tray while chat is running, we reuse the same Qt app instance and just spawn `AdminWindow`.

**Qt session persistence**: `QSettings("LocalAIStack", "LocalAIStack")` stores `{session_token, token_expires_at, last_username}`. Token auto-loads on startup; expired tokens clear silently and force a login prompt.

**Tests**:
- `tests/test_admin_endpoints.py` — new. `POST /auth/login` + `POST /admin/users` + `PATCH /admin/users/{id}` + `DELETE /admin/users/{id}` with an admin session cookie.
- Qt dialogs aren't unit-tested (PySide6 testing is painful); covered by manual smoke.

Commit: `feat(gui,admin): separate Qt admin window with username/password login`.

## Phase 5 — Chat host-gating + minimal web chat page

The backend enforces hostname on chat endpoints; anything that reaches it with a `Host` header other than the configured chat subdomain (while airgap is off) gets a 403 redirect. A single self-contained HTML page is served at `/` for the subdomain.

**New middleware** `backend/middleware/host_gate.py`:
- Reads `CHAT_HOSTNAME` env (default `chat.mylensandi.com`) and `ADMIN_API_ALLOWED_HOSTS` (default `127.0.0.1,localhost`).
- Applies to these path prefixes: `/`, `/chat`, `/v1/chat/completions`, `/api/chats`, `/api/rag`, `/api/memory`. Explicitly does **not** gate `/healthz`, `/v1/models`, `/admin/*`, `/auth/*`, `/api/airgap` — those are needed by the local Qt windows and by cloudflared health probes.
- Logic per request:
  ```
  host = request.headers.get("host", "").split(":")[0].lower()
  if airgap.is_enabled():
      if host not in ADMIN_API_ALLOWED_HOSTS: 403
  else:
      if host == CHAT_HOSTNAME: allow
      elif host in ADMIN_API_ALLOWED_HOSTS: allow **only** for non-chat paths
      else: 403
  ```
- Registered in `backend/main.py` **before** CORSMiddleware (so rejections short-circuit before preflight).

**New static chat page** `backend/static/chat.html`:
- Single HTML file, vanilla JS, no build step. Inline `<style>` + `<script>`. Mobile-first CSS (viewport meta, flex column, max-width 720px on desktop).
- States: `sign-in` (username + password form → POST `/auth/login`, on 200 flip to `chat`), `chat` (message list + composer + model picker + logout button).
- Uses `EventSource`-incompatible SSE via `fetch` + `ReadableStream.getReader()` (mirrors the Qt client).
- Markdown rendering via the `marked` single-file dependency fetched from a CDN pin (SHA-pinned integrity attribute).
- Features: tier picker (populated from `/v1/models`), think toggle, "New chat" button, conversation list sidebar (collapsible on mobile), token/sec readout.
- No admin features; this is chat-only.

**FastAPI mount** in `backend/main.py`:
```python
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="backend/static"), name="static")

@app.get("/", include_in_schema=False)
async def chat_index(request: Request):
    return FileResponse("backend/static/chat.html")
```
Serves only `chat.html`; no other static files needed initially.

**`.env` additions** (and in the `-InitEnv` template):
```
CHAT_HOSTNAME=chat.mylensandi.com
ADMIN_API_ALLOWED_HOSTS=127.0.0.1,localhost
PUBLIC_BASE_URL=https://chat.mylensandi.com
```

**`-Help` new section — Cloudflared ingress**:
```yaml
ingress:
  - hostname: chat.mylensandi.com
    service: http://localhost:18000
  - service: http_status:404
```
Note the user runs cloudflared natively; the launcher never spawns it.

**Tests** (`tests/test_host_gate.py`):
- `chat_path_from_correct_host_allowed`
- `chat_path_from_wrong_host_403`
- `chat_path_from_localhost_in_airgap_allowed`
- `chat_path_from_chat_hostname_in_airgap_403` (airgap closes the subdomain)
- `admin_path_from_localhost_allowed`
- `admin_path_from_chat_hostname_403`
- `healthz_always_allowed`

Commit: `feat(backend): host-gated chat + minimal vanilla HTML chat page`.

## Phase 6 — Airgap-aware Qt chat window

The Qt chat window becomes useful only when airgap is on. In normal mode it shows a guidance screen pointing users to the subdomain. The tray icon reflects airgap state with a badge.

**`gui/windows/chat.py` additions**:
- On show, `ChatWindow` polls `/api/airgap` (existing endpoint).
- If `airgap.enabled == false`:
  - Show a centered card: "Chat is at **https://chat.mylensandi.com** while airgap is off. Sign in there to talk to the models. To chat from this window, enable airgap mode in the admin panel." Plus a *Open Admin* button that opens `AdminWindow` through the tray dispatch.
  - Disable composer, model picker, Send.
- If `airgap.enabled == true`:
  - Show the login dialog (`LoginDialog(client, require_admin=False)`) — any user account works.
  - After login, enable the full chat flow. No host-gating applies to localhost anyway (see Phase 5 middleware).

**Live airgap state refresh**:
- `ChatWindow` starts a `QTimer` polling `/api/airgap` every 5 s.
- When the state flips:
  - on → switch to chat UI, show login if not already authenticated
  - off → immediately freeze composer, clear any in-flight stream, switch to the guidance card (existing chat history stays visible as read-only)

**Tray icon state**:
- `gui/widgets/tray.py` subscribes to the same airgap poll. Icon swaps between the normal icon and an `icon-airgap.ico` (red border) when airgap is on. Tooltip reads `"Local AI Stack — airgap ON"` / `"airgap OFF — chat at chat.mylensandi.com"`.
- *Open Chat* tray entry is always visible; in normal mode it still opens the (disabled) window so the user sees the explanation.

**Backend concurrency note**:
- `backend/airgap.py` already persists to `data/airgap.state` and holds an in-memory `_current`. Single-worker native mode makes cross-worker notification unnecessary. Polling is cheap and correct.

**`.env` update** (after Phase 3 cleanup this becomes the canonical template; `-InitEnv` writes it):
```
# Auth
AUTH_SECRET_KEY=
HISTORY_SECRET_KEY=

# Chat subdomain gating
CHAT_HOSTNAME=chat.mylensandi.com
ADMIN_API_ALLOWED_HOSTS=127.0.0.1,localhost
PUBLIC_BASE_URL=https://chat.mylensandi.com

# Web search
WEB_SEARCH_PROVIDER=ddg            # brave | ddg | none
BRAVE_API_KEY=

# Model update behaviour
MODEL_UPDATE_POLICY=prompt         # auto | prompt | skip
HF_TOKEN=
OFFLINE=

# Service URLs (blank = localhost defaults)
OLLAMA_URL=
LLAMACPP_URL=
QDRANT_URL=
JUPYTER_URL=

# Single-worker native mode: Redis unused
REDIS_URL=
```

Commit: `feat(gui): airgap-aware chat window with live state polling + tray badge`.

## Phase 7 — Wire `model_resolver` to actually download HF GGUFs

Closes the remaining PR #96 gap: currently the resolver picks a version but never pulls the file. `llama-server` fails silently because `data/models/vision.gguf` doesn't exist.

**`backend/model_resolver.py` additions**:
- New function `pull_missing_hf_files(result: ResolveResult, *, model_dir: Path) -> list[str]`. For each HF-sourced tier:
  - If a local file already matches the resolved `(identifier, revision)`, skip.
  - Else call `huggingface_hub.hf_hub_download(repo_id=..., filename=..., revision=..., local_dir=model_dir, local_dir_use_symlinks=False, token=HF_TOKEN)`.
  - For the `vision` tier, symlink (or copy on Windows without junctions) the downloaded file to `data/models/vision.gguf` so `llama-server` can find it at the hardcoded launcher path.
  - Show a progress bar via `tqdm` in the CLI; surface via SSE in the admin `/admin/models/pull` endpoint.
- CLI flag `--pull-hf` added to the existing `resolve` subcommand.

**`LocalAIStack.ps1 Invoke-Setup`** change: on first setup, call `python -m backend.model_resolver resolve --force --pull --pull-hf`. Add a confirmation prompt first ("Vision tier GGUF is ~25 GB — download now? [Y/n]"). User can skip; subsequent `-CheckUpdates` or admin panel *Pull update* handles it later.

**`LocalAIStack.ps1 Invoke-Start`** change: before spawning `llama-server.exe`, check that `data/models/vision.gguf` exists. If missing, skip vision startup and write a clear warning to the log (existing behaviour, just with better wording now that the cause is documented).

**`MODEL_UPDATE_POLICY=prompt` Qt dialog**:
- New `gui/windows/update_prompt.py` — `QMessageBox.question` with tier name, current revision, new revision, and approximate download size. Fired by the GUI's main loop on startup after reading `/resolved-models`.
- On "Yes", POSTs `/admin/models/pull` (requires admin session). On "No", marks the update as skipped-this-session (stored in `data/model-cache.json` with a TTL).
- Non-admin users see a read-only banner ("Update available — ask an admin to install").

**Tests** (`tests/test_model_resolver.py`):
- `test_pull_missing_skips_when_local_matches` (mock `hf_hub_download`, assert not called)
- `test_pull_missing_downloads_when_revision_differs`
- `test_pull_vision_creates_symlink_to_data_models_vision_gguf` (on Linux CI; Windows copy path covered by integration test)
- `test_pull_failure_leaves_pinned_fallback_in_place`

Commit: `feat(models): resolver now pulls HF GGUFs, creates vision.gguf symlink`.

## Critical files

**Touched by phase:**

| Phase | Files |
|---|---|
| 1 (gap fixes) | `backend/main.py` (`/resolved-models`), `gui/widgets/{tray,markdown_view}.py`, `gui/windows/chat.py`, `LocalAIStack.ps1` (Invoke-Build, Invoke-Stop), `scripts/steps/download.ps1` |
| 2 (bootstrap) | `scripts/steps/prereqs.ps1` (new), `LocalAIStack.ps1` (Invoke-Setup first step), `-Help` text |
| 3 (auth) | `backend/auth.py`, `backend/passwords.py` (new), `backend/db.py` (migration v3), `backend/admin.py`, `backend/main.py` (endpoints), `config/auth.yaml`, `backend/requirements.txt` (+ bcrypt, − aiosmtplib), `backend/seed_admin.py` (new CLI), `tests/test_auth.py`, `tests/test_db.py` |
| 4 (admin Qt) | `gui/windows/{login,admin}.py`, `gui/api_client.py`, `gui/main.py` (dispatch), `LocalAIStack.ps1` (-Admin switch), `backend/main.py` (`/admin/models/pull`), `tests/test_admin_endpoints.py` (new) |
| 5 (host gate) | `backend/middleware/host_gate.py` (new), `backend/main.py` (middleware order + `/` mount), `backend/static/chat.html` (new), `.env` template, `tests/test_host_gate.py` (new) |
| 6 (airgap chat) | `gui/windows/chat.py` (dual-mode + polling), `gui/widgets/tray.py` (badge), `.env` template final shape |
| 7 (vision pull) | `backend/model_resolver.py` (new `pull_missing_hf_files`), `gui/windows/update_prompt.py` (new), `LocalAIStack.ps1` (Invoke-Setup confirmation), `tests/test_model_resolver.py` (new) |

**Reused, not modified:** `backend/orchestrator.py`, `backend/router.py`, `backend/vram_scheduler.py`, `backend/kv_cache_manager.py`, `backend/model_residency.py`, `backend/memory.py`, `backend/history_store.py`, `backend/rag.py`, `backend/airgap.py`, `backend/schemas.py`, `backend/backends/{ollama,llama_cpp}.py`, `backend/middleware/{clarification,context,rate_limit,response_mode}.py`, `backend/tools/{executor,registry}.py`, every file in `tools/`, `config/{models,router,vram,runtime}.yaml`.

## Verification

**Per-phase** (run at the tip of each phase commit before moving on):

1. **Phase 1** — `pytest tests/test_main.py::test_resolved_models_default_path`; manual `-Build` produces `LocalAIStack.exe`; spam Send 10× in Qt chat, only one stream runs; long assistant reply renders without visible lag; `-Stop` after killing Ollama manually doesn't Stop-Process an unrelated PID (unit-testable by stubbing `Get-Process`).
2. **Phase 2** — clean Win 11 VM, observe exactly five UAC prompts on fresh `-Setup`, zero on re-run; test with `nvidia-smi` PATH-hidden to confirm the warn-and-continue path.
3. **Phase 3** — `pytest tests/test_auth.py tests/test_db.py` all green. Manual: `python -m backend.seed_admin --if-no-admins` creates first admin; `POST /auth/login` returns a cookie; `POST /auth/login` with wrong password returns 401 in constant time; `/admin/users` 403 without admin session.
4. **Phase 4** — `pytest tests/test_admin_endpoints.py`. Manual: `LocalAIStack.ps1 -Admin` shows login dialog; admin CRUD tab creates a non-admin user; that user logs in at subdomain and can chat but can't reach `/admin/*`.
5. **Phase 5** — `pytest tests/test_host_gate.py`. Manual: `curl -H 'Host: chat.mylensandi.com' http://localhost:18000/` returns the chat HTML; `curl -H 'Host: evil.example.com' http://localhost:18000/v1/chat/completions` returns 403; `curl http://127.0.0.1:18000/admin/me` with admin cookie returns 200; phone browser on `https://chat.mylensandi.com` (via cloudflared) shows the page, logs in, streams tokens.
6. **Phase 6** — Enable airgap via admin panel, confirm chat.mylensandi.com 403s and local Qt chat enables within 5 s; disable, confirm reverse. Tray icon swaps.
7. **Phase 7** — `pytest tests/test_model_resolver.py`. Manual: `-Setup --pull-hf` downloads vision GGUF; `llama-server` starts on next `-Start`; prompt-mode dialog appears when `MODEL_UPDATE_POLICY=prompt` and a new revision is published.

**End-to-end smoke on the merged branch:**

1. Clean Windows 11 VM, no tools installed. Clone repo, `.\LocalAIStack.ps1 -Setup`. Walk through UAC prompts. Answer "yes" to vision pull. Wait for model pulls (~2 h). Create first admin when prompted.
2. `.\LocalAIStack.ps1` → Qt chat window opens in "airgap off" mode with guidance card. Tray visible.
3. Start native cloudflared with the ingress snippet. Visit `https://chat.mylensandi.com` from phone → HTML chat loads, login with admin credentials, send "what time is it" → tool-use path fires, web search returns, SSE streams the answer.
4. Tray → *Open Admin* → login → Users tab → create `bob` with password, non-admin. Log out as admin, log in as bob at subdomain, chat works, `/admin/users` 403s.
5. Admin → Airgap tab → toggle ON. Chat subdomain starts returning 403. Local Qt window flips to login; log in as bob; chat streams locally with no outbound calls.
6. `.\LocalAIStack.ps1 -Stop` → every tracked child process dies; `pids.json` removed; cloudflared tunnel stays up (not our process). Re-start, confirm state persists.
7. `.\LocalAIStack.ps1 -Build` → `LocalAIStack.exe` at root; running the exe performs steps 2+ without PowerShell visible.

## Risks

- **ddgs rate-limits** after a few hundred queries/day. Document that Brave is the non-default recommendation once a key is in hand. Already staged in the `.env` template.
- **PySide6 venv is ~180 MB** — acceptable, noted in `-Help` under disk requirements.
- **CDN-fetched `marked` in the chat HTML** — SHA-pinned; fallback is a backend-served static copy if CDN is reachability-sensitive under airgap (but airgap disables the subdomain entirely, so this is a non-issue).
- **Cloudflared Access not used** — authentication is fully at the application layer. If the user later wants zero-trust SSO, add an optional `TRUST_CF_ACCESS_HEADER=Cf-Access-Authenticated-User-Email` mode to `host_gate.py` that swaps login for header-trust; out of scope for this plan.
- **Airgap state polling at 5 s** — 5 s latency between toggle and UI flip. Acceptable for an admin-only toggle; avoids the complexity of SSE or WebSockets for a single bit.
- **Windows symlink permissions** for the `vision.gguf` copy — falls back to a full file copy if `mklink` fails (non-developer accounts can't make symlinks without Developer Mode enabled).
- **bcrypt round count** — 12 is standard; benchmarks at ~250 ms per verify on a desktop CPU. Acceptable for the login-rate-limit window; revisit if login endpoint becomes a hot path.
- **Schema migration v3** is one-way. Rollback requires manual SQL. For pre-prod users (nobody yet), acceptable; document in `-Help` uninstall section.
- **cloudflared ingress order matters** — subdomains listed after a wildcard match won't route. Ingress snippet in `-Help` puts `chat.mylensandi.com` before the `http_status:404` catch-all.
