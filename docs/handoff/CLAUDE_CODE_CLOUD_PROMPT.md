# Local AI Stack — Native-Mode Fix Pack (full)

Self-contained instruction set for a fresh Claude Code Cloud session targeting the unmodified `kitisathreat/local-ai-stack` repo at `main` (the version before any of my work). Apply ALL of the changes below in a single feature branch named `fix/native-mode-bringup`. Open one PR titled `Native Windows mode: wizard, CUDA, llama.cpp b8992, chat UX, installer split, conversation resumption`. Each numbered area is its own commit for reviewability.

The repo is a Windows-native multi-tier llama.cpp stack with a PySide6 setup wizard, FastAPI backend, vanilla-JS chat UI served from `backend/static/chat.html`, a PowerShell launcher (`LocalAIStack.ps1`), and an Inno Setup-driven installer. Verify each section's preconditions before editing — read each file first.

Pinned third-party versions referenced below:
- `LAI_LLAMACPP_VERSION`: `b8992` (was `b4404`)
- `LAI_QDRANT_VERSION`: `v1.12.4` (unchanged)

---

## 1. Cloudflare wizard fails silently under pythonw.exe

**File:** `gui/cloudflare_setup.py`

- Add module-level `_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0`. Pass `creationflags=_NO_WINDOW` to every `subprocess.run` and `subprocess.Popen`.
- New `find_remote_tunnel_by_name(name, cloudflared)`: runs `cloudflared tunnel list --output json --name <name>`, parses, returns the existing UUID or None. Treat `0001-01-01T00:00:00Z` `deleted_at` as "live" (cloudflared's zero-value timestamp, not deleted).
- Make `create_tunnel(name)` idempotent: call `find_remote_tunnel_by_name` first; only create if missing.
- `route_dns` takes `overwrite: bool = True` and passes `-f` so a stale CNAME from a previous tunnel is replaced (Error 1003 fix).
- `_run_cloudflared` returns `stdout + "\n" + stderr` combined so callers can grep either stream for the UUID. Raise `CloudflareSetupError(message, stderr=stderr)` including last 500 chars of stderr.
- Add `cloudflared_login_via_terminal` using `CREATE_NEW_CONSOLE` for a visible OAuth banner.

**File:** `gui/windows/setup_wizard.py`

- Add `_ProvisionTunnelWorker(QThread)` running create/route/write-config off the UI thread. Emits `progress(str)` and `finished(bool, str)`. Connect button starts the worker and disables itself.
- Fix the `QFileSystemWatcher` ordering: install the watcher BEFORE the OAuth subprocess starts so the cert.pem-creation event isn't missed.

---

## 2. Wizard state persistence

**File:** `gui/windows/setup_wizard.py`

- Define `_PERSISTED_FIELDS` tuple naming every registered field except auto-generated secrets.
- New `_save_wizard_state(wizard)` and `_load_wizard_state()` serialise/deserialise to `<repo>/data/wizard-state.json` (or `LOCALAPPDATA/LocalAIStack/wizard-state.json` when `LAI_INSTALLED=1`).
- New `_clear_wizard_state()` — call from `_FinishPage._write_config` on success.
- `SetupWizard.__init__`: load state, call `setField` per persisted name. Mirror restored password into the Admin page's `_confirm` widget (UI-only, not registered) so validation passes without retype.
- Hook `currentIdChanged` → `_save_wizard_state(self)`.

---

## 3. Drop wizard's Models page; auto-pull on `-Start`

**File:** `gui/windows/setup_wizard.py`

- Delete `_ModelsPage` entirely. Don't add it to the wizard. Renumber subsequent pages.

**File:** `LocalAIStack.ps1`

- In `Invoke-Start`: if any `data/models/<tier>.gguf` is missing, call `& $py -m backend.model_resolver resolve --pull` automatically. Skip when `-Offline`, `-NoUpdateCheck`, or `OFFLINE=1`.

---

## 4. Resolver `--tier` flag + cache-hit filter fix

**File:** `backend/model_resolver.py`

- Add repeatable `--tier <name>` CLI arg. Tolerate `embed` as alias for `embedding`.
- Hoist the tier allow-list out of the fresh-resolve branch — the cache-hit branch must apply the same filter:

```python
wanted: set[str] | None = None
if tiers:
    wanted = {t.strip().lower() for t in tiers if t}
    if "embed" in wanted:
        wanted.discard("embed"); wanted.add("embedding")
    sources = {k: v for k, v in sources.items() if k.lower() in wanted}
# In cache branch:
if wanted is not None:
    cached_tiers = {k: v for k, v in cached_tiers.items() if k.lower() in wanted}
```

---

## 5. CORS auto-disable credentials when wildcard origin

**File:** `backend/main.py` (CORS middleware setup)

- Compute `_cors_allow_credentials = (_allowed_origins != ["*"])` regardless of the env var when origins is wildcard. Log WARN: `"CORS origin is wildcard ('*'). Disabling allow_credentials so the config is browser-valid. Set ALLOWED_ORIGINS to your Cloudflare hostname (e.g. 'https://chat.example.com') to re-enable cookies."`

**File:** `backend/diagnostics.py`

- `check_cors_config` mirrors the runtime auto-disable. Hard FAIL only when origins=`["*"]` AND `CORS_ALLOW_CREDENTIALS=true` is explicitly set. Add an OK branch: `"CORS wildcard origin with credentials disabled — browser-valid"`.

---

## 6. Remove stale n8n_auth diagnostic

**File:** `backend/diagnostics.py`

- Delete `check_env_n8n_auth()` entirely. Remove its call site in `run_startup_diagnostics`.

**File:** `tests/test_diagnostics.py`

- Remove the `check_env_n8n_auth` import and the `TestEnvN8nAuth` class.

---

## 7. CUDA 12 runtime DLL provisioning

**File:** `scripts/steps/download.ps1`

Add two functions BEFORE `Invoke-DownloadLlamaServer`:

- `Test-CudaRuntimeAvailable -VendorDir <path> [-MajorVersion '12']`: returns `$true` only when `cudart64_<MajorVersion>.dll`, `cublas64_<MajorVersion>.dll`, `cublasLt64_<MajorVersion>.dll` are all findable in `$VendorDir`, `$env:CUDA_PATH`, `$env:CUDA_HOME`, or any directory on `$env:PATH`. Any CUDA 12.x minor version satisfies the cu12.4 build.
- `Invoke-DownloadCudaRuntime -LlamaCppVersion <ver> -Dest <dir> [-Sha256 <hash>]`: short-circuits when the probe returns true. Otherwise tries `cudart-llama-bin-win-cuda-12.4-x64.zip` first, then falls back to the older `cudart-llama-bin-win-cu12.4-x64.zip` for users pinning pre-mid-2025 release tags. URL: `https://github.com/ggml-org/llama.cpp/releases/download/<LlamaCppVersion>/<asset>`. Verify hash if provided. On failure, print install instructions.

`Invoke-DownloadLlamaServer` should also try `llama-<ver>-bin-win-cuda-12.4-x64.zip` first then fall back to `llama-<ver>-bin-win-cuda-cu12.4-x64.zip` (asset naming changed mid-2025).

**File:** `LocalAIStack.ps1`

- In `Invoke-Setup`, after `Invoke-DownloadLlamaServer`, call:
  ```powershell
  Invoke-DownloadCudaRuntime -LlamaCppVersion $LlamaCppVersion -Dest (Join-Path $VendorDir 'llama-server')
  ```

---

## 8. Bump `LAI_LLAMACPP_VERSION` to b8992; flag compatibility

**File:** `LocalAIStack.ps1`

- Default `LlamaCppVersion = 'b8992'` (was `'b4404'`). b4404 doesn't recognise the `qwen35` model architecture used by Qwen3.5/3.6 GGUFs.

**File:** `backend/backends/llama_cpp.py`

Two breaking flag changes between b4404 and b8992:

- **`-fa`** changed from a bare flag to one that requires a value (`on`/`off`/`auto`). In `build_argv`, replace `argv.append("-fa")` with `argv += ["-fa", "on"]` (both old and new builds accept the explicit form).
- **`--jinja`** was added in b4500-ish. Probe `llama-server --help` once at runtime and only add the flag when supported. Add module-level cache:
  ```python
  _jinja_supported_cache: bool | None = None

  def _llama_supports_jinja() -> bool:
      global _jinja_supported_cache
      if _jinja_supported_cache is not None:
          return _jinja_supported_cache
      try:
          out = subprocess.run(
              [llama_server_binary(), "--help"],
              capture_output=True, text=True, timeout=10,
              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
          )
          haystack = (out.stdout or "") + (out.stderr or "")
          _jinja_supported_cache = bool(re.search(r"(?m)^\s*--jinja\b", haystack))
      except (OSError, subprocess.TimeoutExpired):
          _jinja_supported_cache = False
      return _jinja_supported_cache
  ```
  In `build_argv`, replace `argv.append("--jinja")` with `if _llama_supports_jinja(): argv.append("--jinja")`. Add `import re` at top.

**File:** `LocalAIStack.ps1` `Invoke-Start` vision pre-spawn args block

- Use `-fa on` (not bare `-fa`).
- Probe `& $llamaBin --help` for `--jinja`; only append when present.

---

## 9. DB path default + .env path in dev mode

**File:** `backend/db.py`

- Replace `DB_PATH = Path(os.getenv("LAI_DB_PATH", "/app/data/lai.db"))` with:
  ```python
  def _default_db_path() -> Path:
      env = os.getenv("LAI_DB_PATH")
      if env: return Path(env)
      docker_path = Path("/app/data")
      if docker_path.is_dir():
          return docker_path / "lai.db"
      return Path(__file__).resolve().parents[1] / "data" / "lai.db"

  DB_PATH = _default_db_path()
  ```

**File:** `gui/windows/setup_wizard.py` `_FinishPage._write_config`

- Set `data_root = _REPO` unless `os.environ.get("LAI_INSTALLED") == "1"` (then `LOCALAPPDATA / "LocalAIStack"`).

---

## 10. Username vs email login clarity

**File:** `gui/windows/setup_wizard.py` `_FinishPage` post-seed log

- Replace `"✓ Admin user created: {email}"` with two lines explicitly stating: log in with username (= `email.split("@", 1)[0]`), NOT email.

---

## 11. Repoint `config/model-sources.yaml` at the correct Qwen3.x lineup

**File:** `config/model-sources.yaml`

Replace the `tiers:` block with:

```yaml
tiers:
  highest_quality:
    source: huggingface
    repo: "mradermacher/Qwen3-72B-Instruct-GGUF"
    file: "*Q4_K_M.gguf"
    tracking: latest
    pinned:
      revision: "main"
      file: "Qwen3-72B-Instruct.Q4_K_M.gguf"

  versatile:
    source: huggingface
    repo: "lmstudio-community/Qwen3.6-35B-A3B-GGUF"
    file: "*Q4_K_M.gguf"
    tracking: latest
    pinned:
      revision: "main"
      file: "Qwen3.6-35B-A3B-Q4_K_M.gguf"

  fast:
    source: huggingface
    repo: "lmstudio-community/Qwen3.5-9B-GGUF"
    file: "*Q4_K_M.gguf"
    tracking: latest
    pinned:
      revision: "main"
      file: "Qwen3.5-9B-Q4_K_M.gguf"

  coding:
    source: huggingface
    repo: "unsloth/Qwen3-Coder-Next-GGUF"
    file: "*Q4_K_M.gguf"
    tracking: latest
    pinned:
      revision: "main"
      file: "Qwen3-Coder-Next-Q4_K_M.gguf"

  vision:
    source: huggingface
    repo: "lmstudio-community/Qwen3.6-35B-A3B-GGUF"
    file: "*Q4_K_M.gguf"
    mmproj: "mmproj-*BF16.gguf"
    tracking: latest
    pinned:
      revision: "main"
      file: "Qwen3.6-35B-A3B-Q4_K_M.gguf"
      mmproj: "mmproj-Qwen3.6-35B-A3B-BF16.gguf"

  embedding:
    source: huggingface
    repo: "nomic-ai/nomic-embed-text-v1.5-GGUF"
    file: "*Q8_0.gguf"
    tracking: latest
    pinned:
      revision: "main"
      file: "nomic-embed-text-v1.5.Q8_0.gguf"
```

**Verify each repo with `gated=False`** via `https://huggingface.co/api/models/<repo>` BEFORE committing. Notes:
- `versatile` and `vision` share the same base repo. The resolver pulls the GGUF once; vision additionally pulls the mmproj.
- `coding` uses `unsloth/Qwen3-Coder-Next-GGUF` (single-file Q4_K_M). Don't use the official `Qwen/Qwen3-Coder-Next-GGUF` — it ships sharded (00001-of-00004 etc.) and the resolver doesn't yet stitch shards.
- Verify `config/models.yaml` ports + ctx-sizes already match: highest_quality=8010/32768, versatile=8011/65536, fast=8012/65536, coding=8013/131072, vision=8001/16384, embedding=8090/8192.

---

## 12. Unpin vision tier (was hogging 21 GB on a 24 GB card)

**File:** `config/models.yaml`

- Change `vision:` `pinned: true` → `pinned: false`. Comment with rationale: 21 GB pinned leaves only ~2 GB free for any other tier on a 24 GB GPU, blocking versatile / highest_quality / coding from ever loading. Vision now cold-spawns on demand (auto-routed when an image is in the message) and joins the LRU eviction pool. Embedding stays pinned (1 GB, used continuously by RAG/memory).

---

## 13. HF download retry budget (500 attempts) + transfer fallback

**File:** `backend/model_resolver.py`

- Inside `pull_to_disk()`, replace direct `hf_hub_download(...)` calls with a `_download_with_retry(**kwargs)` wrapper:
  - Up to 500 attempts per file.
  - Backoff: `min(10, 2 + (attempt % 4))` seconds (capped short — HF CDN drops are transient).
  - First failure containing `"hf_transfer"` or `"HF_HUB_ENABLE_HF_TRANSFER"` → set `os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"` process-wide, retry immediately. The Rust parallel downloader has no resume; pure-Python does.
  - Log every retry up to attempt 10, then every 10th, to avoid log floods.
  - Re-raise after exhausting attempts.
- Each retry resumes from the partial blob in `~/.cache/huggingface/hub/.../blobs/<sha>` so progress accumulates across attempts.

---

## 14. HF token field in wizard

**File:** `gui/windows/setup_wizard.py`

- New constant `FIELD_HF_TOKEN = "hf_token"`. Add to `_PERSISTED_FIELDS`.
- `_AdminAccountPage`: add a `QLineEdit(echoMode=Password)` for the token plus a "Get one from HF" `QPushButton`. The button calls `webbrowser.open("https://huggingface.co/settings/tokens")`.
- Override `initializePage` to auto-open the HF tokens page **only when the field is empty**:
  ```python
  def initializePage(self) -> None:
      super().initializePage()
      if not self._hf_token.text().strip():
          QTimer.singleShot(400, self._open_hf_token_page)
  ```
- `_FinishPage._write_config`: read the field, write `f"HF_TOKEN={hf_token}"` into `.env`. Pass `HF_TOKEN` through to the model-pull subprocess env.

---

## 15. Bake all-tier pull into wizard; backend self-heal

**File:** `gui/windows/setup_wizard.py` `_FinishPage._write_config`

- After `seed_admin` succeeds, spawn the resolver detached:
  ```python
  log_path = data_root / "logs" / "model-pull.log"
  log_path.parent.mkdir(parents=True, exist_ok=True)
  log_fh = open(log_path, "ab", buffering=0)
  pull_env = {**os.environ}
  if hf_token:
      pull_env["HF_TOKEN"] = hf_token
  subprocess.Popen(
      [python, "-m", "backend.model_resolver", "resolve", "--pull"],
      stdout=log_fh, stderr=subprocess.STDOUT,
      cwd=str(_REPO), env=pull_env,
      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
  )
  ```
  Log: `"✓ Model pull started in background (~96 GB total).\n   Progress log: {log_path}\n   Tiers come online progressively as each GGUF lands."`

**File:** `backend/main.py`

- Add `import sys` at top.
- Add new function `_auto_pull_missing_tiers()`:
  ```python
  async def _auto_pull_missing_tiers() -> None:
      models_dir = Path(os.getenv("LAI_DATA_DIR") or
                        Path(__file__).resolve().parent.parent / "data") / "models"
      configured = list((state.config.models.tiers or {}).keys())
      missing = [t for t in configured if not (models_dir / f"{t}.gguf").exists()]
      if not missing:
          return
      logger.info("Auto-pull starting for missing tiers: %s", ", ".join(missing))
      cmd = [sys.executable, "-m", "backend.model_resolver", "resolve", "--pull"]
      for t in missing:
          cmd += ["--tier", t]
      proc = await asyncio.create_subprocess_exec(
          *cmd,
          stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
          cwd=str(Path(__file__).resolve().parent.parent),
      )
      rc = await proc.wait()
      logger.info("Auto-pull finished (exit %d)", rc)
  ```
- In the lifespan after `logger.info("Ready. Tiers: ...")`, conditionally start the task:
  ```python
  if not state.airgap.enabled and os.getenv("OFFLINE", "").strip() not in ("1", "true", "yes"):
      asyncio.create_task(_auto_pull_missing_tiers())
  ```

---

## 16. Annotate `/resolved-models` with `available: bool`

**File:** `backend/main.py`

- In `GET /resolved-models`, after parsing the manifest, walk each tier and set `info["available"] = bool(gguf and Path(gguf).exists())`. The manifest is written eagerly when the resolver picks a file (before pull finishes), so `gguf_path` being set does NOT mean the file is ready.

---

## 17. New `POST /api/chat/upload` for per-message attachments

**File:** `backend/main.py`

- Add an endpoint:
  ```python
  import secrets as _secrets

  _ATTACH_MAX_BYTES = 20 * 1024 * 1024

  def _attachments_dir(user_id: int) -> Path:
      base = Path(os.getenv("LAI_DATA_DIR") or
                  Path(__file__).resolve().parent.parent / "data")
      d = base / "uploads" / str(user_id)
      d.mkdir(parents=True, exist_ok=True)
      return d

  @app.post("/api/chat/upload")
  async def chat_upload(
      file: UploadFile = File(...),
      user: dict = Depends(auth.current_user),
  ):
      content = await file.read()
      if not content:
          raise HTTPException(400, "Empty file")
      if len(content) > _ATTACH_MAX_BYTES:
          raise HTTPException(413, f"File too large (>{_ATTACH_MAX_BYTES // (1024*1024)} MB)")
      aid = _secrets.token_urlsafe(12)
      fname = (file.filename or "upload").lower()
      ext = Path(fname).suffix[:8] or ""
      if ext and not re.fullmatch(r"\.[a-z0-9]+", ext):
          ext = ""
      dest = _attachments_dir(user["id"]) / f"{aid}{ext}"
      dest.write_bytes(content)
      return {
          "id": aid,
          "name": file.filename or "upload",
          "size": len(content),
          "content_type": file.content_type or "application/octet-stream",
      }
  ```
- Add `import re` if not already imported.

---

## 18. `enabled_tools` per-request whitelist; default to NO tools

**File:** `backend/schemas.py` `ChatRequest`

- Add fields:
  ```python
  enabled_tools: list[str] | None = None
  attachment_ids: list[str] | None = None
  ```

**File:** `backend/tools/registry.py` `all_schemas`

- Add `names: list[str] | set[str] | None = None` parameter. When provided, ignore `only_enabled` (user explicitly opted in) and filter to those exact names. Keep airgap filtering — a remote-service tool toggled on in airgap mode is silently dropped.

**File:** `backend/main.py` `chat_completions`

- Replace the existing tool-schema selection with:
  ```python
  tool_schemas: list[dict] | None = None
  if req.tools is None:
      # Default: NO tools. The 🔧 popover is the explicit opt-in surface.
      # Including 200+ default-enabled tool schemas blows past the per-slot
      # context window (e.g. 65536 / 4 parallel = 16384 tokens, vs. 28k for
      # the schemas alone).
      if req.enabled_tools:
          enabled = state.tools.all_schemas(
              airgap=airgap.is_enabled(),
              names=req.enabled_tools,
          )
          if enabled:
              tool_schemas = enabled
  else:
      tool_schemas = req.tools
  ```

---

## 19. Extend `AgentEvent.type` Literal for new SSE types

**File:** `backend/schemas.py`

- Add `"tier.loading"`, `"vram.making_room"`, `"queue"` to the `AgentEvent.type` Literal. (Pydantic raises ValidationError on unknown literals, which would abort chat completions.)

---

## 20. Surface VRAM scheduler events to the SSE stream

**File:** `backend/vram_scheduler.py`

- Inside `acquire()`, just before the loader is invoked (the `if load_needed:` block), emit a `tier.loading` event so the chat UI can show a load-progress label:
  ```python
  if on_event:
      try:
          await on_event({
              "type": "tier.loading",
              "tier_id": tier_id,
              "model_tag": tier.model_tag,
          })
      except Exception:
          logger.debug("on_event tier.loading raised; continuing")
  ```
- Just before `_make_room_for(tier)` is called, emit a `vram.making_room` event:
  ```python
  if on_event:
      try:
          await on_event({
              "type": "vram.making_room",
              "tier_id": tier_id,
              "needs_gb": tier.vram_estimate_gb,
          })
      except Exception:
          logger.debug("on_event vram.making_room raised; continuing")
  ```

**File:** `backend/main.py` `_reserve_with_sse`

- Forward events with their original `type` field instead of hardcoding `"queue"`:
  ```python
  def _ev_type(ev: dict) -> str:
      return str(ev.get("type") or "queue")

  yield _agent_event_sse(AgentEvent(type=_ev_type(ev), data=ev), model_id)
  ```

---

## 21. Admin GUI — fix Models tab (was rendering 0 rows + raw float for last login)

**File:** `gui/api_client.py` `BackendClient`

- Two methods named `resolved_models` exist. Python keeps the second; rename it to `admin_overview` (it calls `/admin/overview`, a separate concern). Keep the first one calling `/resolved-models`.
- Add `model_pull_status()` calling `GET /admin/model-pull-status` (defined below in §23).

**File:** `gui/windows/admin.py` `_refresh_models`

- Compose identifier from real fields:
  ```python
  repo = info.get("repo") or info.get("model_id") or ""
  filename = info.get("filename") or ""
  if repo and filename:
      identifier = f"{repo}/{filename}"
  else:
      identifier = repo or filename or info.get("path") or ""
  ```

**File:** `gui/windows/admin.py` `_refresh_users`

- Normalise `last_login_at`:
  ```python
  last = u.get("last_login_at")
  if isinstance(last, (int, float)):
      from datetime import datetime
      last_str = datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S")
  elif isinstance(last, str) and last:
      last_str = last[:19]
  else:
      last_str = ""
  self._users_table.setItem(row, 5, QTableWidgetItem(last_str))
  ```

---

## 22. Admin GUI sign-in deadlock fix

**File:** `gui/api_client.py`

- Add `import httpx` if not present.
- New `BackendClient.login_sync(username, password) -> dict` using `httpx.Client` (sync). Distinct exceptions:
  - `ValueError("Invalid username or password")` on 401
  - `ConnectionError("Could not reach backend at <url>. Is it running?")` on `httpx.ConnectError`
  - `ConnectionError("Login timed out talking to <url>.")` on `httpx.TimeoutException`
  - `ConnectionError(f"Backend returned {code}: {text[:200]}")` on other 4xx/5xx
- New `BackendClient.logout_sync()` companion (used by the admin-only privilege rejection path).

**File:** `gui/windows/login.py`

- Remove `import asyncio`.
- Add `from PySide6.QtCore import QThread, Signal` (if not already imported).
- New `_LoginWorker(QThread)`:
  ```python
  class _LoginWorker(QThread):
      success = Signal(dict)
      failure = Signal(str)
      def __init__(self, client, username, password):
          super().__init__()
          self._client, self._username, self._password = client, username, password
      def run(self):
          try:
              info = self._client.login_sync(self._username, self._password)
              self.success.emit(info or {})
          except ValueError:
              self.failure.emit("Invalid username or password.")
          except ConnectionError as exc:
              self.failure.emit(str(exc))
          except Exception as exc:
              self.failure.emit(f"Login failed: {type(exc).__name__}: {exc}")
  ```
- Replace `_submit()` body to spawn the worker, connect signals, block double-submission. New `_on_login_ok` and `_on_login_failed` handlers (the latter re-focuses + selects the password field for one-keystroke retry; uses `client.logout_sync()` not `await client.logout()` for the non-admin rejection path).

The bug: `dlg.exec()` runs Qt's modal event loop synchronously, suspending qasync's outer asyncio scheduler. Any `await` inside the dialog hangs forever. The QThread approach bypasses asyncio entirely.

---

## 23. New `GET /admin/model-pull-status` + GUI progress bars

**File:** `backend/admin.py`

- New endpoint `GET /admin/model-pull-status` (admin-only). Returns per tier:
  - `downloaded_bytes`: max of on-disk `<tier>.gguf` size (resolved through symlinks) and the largest blob in `~/.cache/huggingface/hub/models--<org>--<repo>/blobs/` for the configured repo.
  - `expected_bytes`: looked up via `huggingface_hub.HfApi().model_info(repo, files_metadata=True)` for the matching `siblings.rfilename`. Cache `(repo, filename) → size` in a module-level dict so polls don't hammer HF.
  - `percent`: `(downloaded / expected) * 100`, clamped to 100, or `None` if expected unknown.
  - `complete`: `bool(expected) and downloaded >= expected`.
  - `in_progress`: `(not complete) and downloaded > 0`.
  - `repo`, `filename`: from the resolved-models manifest.

**File:** `gui/windows/admin.py` Models tab

- Widen table to 6 columns: `["Tier", "Source", "Identifier", "Origin", "Update?", "Progress"]`.
- Add `QProgressBar` import to `from PySide6.QtWidgets import (...)`.
- `self._progress_bars: dict[str, QProgressBar]` keyed by tier name; reuse instances across refreshes.
- Helper `_make_progress_bar(tier)` creates-or-returns. `_fmt_bytes(n)` formats human-readable (B/KB/MB/GB/TB).
- `_update_bar(tier, status)`: when `complete=True` show `"✓ done"` at 100. When `percent` known, show `"{pct:.1f}% — {downloaded} / {expected}"`. When `in_progress` but no expected size yet, set range 0-0 (indeterminate animation) and show bytes. When idle, `"queued"` at 0.
- Add a `QTimer(self)` polling `/admin/model-pull-status` every 5 sec via `_refresh_pull_progress`. Self-stops once every tier reports `complete=True`.
- Seed each row's bar from `info.available` in `_refresh_models` so it isn't blank during the first round-trip.

---

## 24. Chat UI — composer redesign (Tools popover + Attach + tier labels)

**File:** `backend/static/chat.html`

Replace the bare textarea footer with a wrapped composer:

```html
<footer>
  <div id="composer-wrap">
    <textarea id="composer" placeholder="Type a message. Enter to send, Shift+Enter for newline." rows="1"></textarea>
    <div id="attached-list"></div>
    <div class="composer-tools">
      <button class="icon-btn" id="tools-btn" title="Enable tools">🔧 Tools <span id="tools-count" style="color:var(--muted);"></span></button>
      <button class="icon-btn" id="attach-btn" title="Attach files">📎 Attach</button>
      <input type="file" id="attach-input" multiple>
      <div id="tools-popover" class="hidden">
        <input type="text" class="tool-search" id="tool-search" placeholder="Filter tools…">
        <div id="tools-list"></div>
      </div>
    </div>
  </div>
  <button id="send">Send</button>
</footer>
```

Add CSS for `#composer-wrap` flex column with transparent background; `.composer-tools` strip; iOS-style `.toggle` (30×16, blue when checked, white circle slides on `:checked`); `.chip` for attachments; `.icon-btn`; `#tools-popover` positioned `bottom:100% left:0 z-index:10`.

JS:

- New helpers `tierLabel(id)` (capitalize first char, replace `_` with space) and `specificModel(info)` (parse repo basename / filename, strip trailing `-GGUF`, `-Q[0-9]+_K_M`, `.gguf`).
- `loadModels()` fetches `/v1/models` AND `/resolved-models`. Builds option text as `"{cat}: {model}"`. Marks `info.available === false` options with `"…  (downloading…)"` and `option.disabled = true`. Captures `previousValue = modelEl.value` before clearing innerHTML; restores it post-population if still available; falls back to `firstAvailableValue` only when previous selection is gone. Sets `lastSelectedModel = modelEl.value` after each refresh.
- 30-sec auto-refresh: `setInterval(() => { if (!app.classList.contains('hidden')) loadModels(); }, 30000);`
- `modelEl` `change` listener: when `history.length > 0 && lastSelectedModel && lastSelectedModel !== newVal`, prompt with `window.confirm("Switching models mid-conversation will load a different model into VRAM. The next reply may take 5–30 seconds for small tiers, longer for 72B / 80B-A3B (and unloads whatever was previously resident).\n\nContinue with the switch?")`. Cancel reverts; OK proceeds.
- `loadTools()` fetches `/tools`, populates `toolState` Map with **every value `false`** (defaults all toggles OFF). `renderTools()` filters by `toolSearch.value`, sorts alphabetically, renders rows with iOS toggle and per-tool checkbox listener. Update `toolsCount` badge to `(N)` when N>0.
- Tools popover open/close: `toolsBtn` click toggles `.hidden`; document-level click outside closes.
- Attach: `attachBtn` triggers hidden `<input type="file" multiple>`; on change push `{name, size, file}` onto `attached[]` and re-render chips with `×` to splice.

---

## 25. Chat UI — send: lazy create conversation, status placeholders, attachments upload

**File:** `backend/static/chat.html` `sendMessage()`

- Render initial assistant bubble with a "Thinking…" placeholder using a `.thinking` span and pulsing dots animation.
- Track `firstTokenSeen = false`. While streaming, on each SSE `event: agent` frame BEFORE the first real token, set `targetBody.innerHTML` to a friendly status line:
  - `route.decision` where `chunk.data.tier === userPickedTier` → `"Connecting to model…"`
  - `route.decision` with `specialist_reason` → `"Auto-routed to <Tier> ({reason})…"` where reason text is mapped: `image_in_message → "image attached"`, `code_block_present → "code block detected"`.
  - `tier.loading` / `model.spawn` → `"Loading <model_tag> into VRAM (first request takes 5–30s)…"`.
  - `vram.making_room` → `"Unloading idle models to make room for <tier>…"`.
  - `queue` / `queue.update` → `"Queued (<n> requests ahead)…"` with proper plural.
  - `tool.call` → `"Calling tool: <name>…"`.
  - `error` → render in red, set `firstTokenSeen = true` to suppress further status writes.
- On the first real token, clear the bubble and start streaming markdown.
- Before posting, upload each attached file via `POST /api/chat/upload` (FormData), collect ids:
  ```js
  const attachmentIds = [];
  const uploadList = attached.splice(0, attached.length);
  renderAttached();
  for (const a of uploadList) {
      try {
          const fd = new FormData();
          fd.append('file', a.file, a.name);
          const ur = await fetch('/api/chat/upload', { method: 'POST', credentials: 'include', body: fd });
          if (ur.ok) {
              const j = await ur.json();
              if (j.id) attachmentIds.push(j.id);
          }
      } catch (_) {}
  }
  ```
- Lazy-create the conversation row on the very first send (when `currentConversationId === null`):
  ```js
  if (currentConversationId === null) {
      try {
          const cr = await api('/chats', {
              method: 'POST',
              body: JSON.stringify({
                  title: text.slice(0, 60) || 'New chat',
                  tier: modelEl.value.replace(/^tier\./, ''),
              }),
          });
          if (cr.ok) {
              const conv = await cr.json();
              currentConversationId = conv.id;
              loadConversations();
          }
      } catch (_) {}
  }
  ```
- Build the request body with new fields:
  ```js
  const body = {
      model: modelEl.value, stream: true, messages: history,
      think: thinkEl.checked || undefined,
      conversation_id: currentConversationId || undefined,
  };
  if (toolState.size > 0) {
      const toolNames = [];
      toolState.forEach((on, name) => { if (on) toolNames.push(name); });
      body.enabled_tools = toolNames;
  }
  if (attachmentIds.length) body.attachment_ids = attachmentIds;
  ```

---

## 26. Chat UI — conversation sidebar

**File:** `backend/static/chat.html`

- Restructure `#chatapp` from `flex-direction:column` to `flex-direction:row`. New `<aside id="sidebar">` (240px) on the left containing a `+ New chat` button and a scrollable `#conv-list`. Wrap the existing header / log / footer in an inner `<div style="display:flex; flex-direction:column; flex:1; min-width:0;">`.
- CSS for `.conv-item` (8px padding, 6px radius, `:hover` and `.active` states with accent left-border), `#new-conv-btn`, mobile breakpoint hides the sidebar.
- Move `#newchat` button from the header into the sidebar as `#new-conv-btn`.
- JS:
  - `let currentConversationId = null;`
  - `loadConversations()`: `GET /chats`, render newest first with title + relative timestamp + tier. Click → `loadConversation(id)`. Empty-state placeholder.
  - `loadConversation(convId)`: `GET /chats/{id}`, reset history + log, replay user/assistant messages into `history[]` and DOM. Sync model dropdown to the conversation's tier (only if that tier is still in the dropdown and not disabled). Update `lastSelectedModel`. Refresh sidebar to mark active item.
  - `newConversation()`: clear `currentConversationId`, `history`, `logEl`, deactivate sidebar items.
  - `$('new-conv-btn').addEventListener('click', newConversation);`
- Call `await loadConversations()` after `await loadTools()` in both `bootstrap()` (auto-restored session) and the post-login success path.

---

## 27. CI smoke-test compatibility — preflight skip

**File:** `LocalAIStack.ps1` `Invoke-Start`

- Diagnostic preflight (defined in §28) auto-skips when ALL of `-NoGui -Offline -NoUpdateCheck` are set together. Real users never combine that triple; CI does for `-SkipModels` smoke runs.

---

## 28. Diagnostic preflight before service spawn

**File:** `scripts/steps/preflight.ps1` (new)

Implement `Invoke-Preflight -RepoRoot -VendorDir -DataDir -EnvFile` returning a hashtable `@{ ok; errors[]; warnings[]; suggestion }`. Ten checks:

1. `vendor\venv-backend\Scripts\python.exe` exists → ERROR if missing.
2. `vendor\venv-gui\Scripts\pythonw.exe` exists → WARN.
3. `vendor\llama-server\llama-server.exe` exists → ERROR.
4. `Test-CudaRuntimeAvailable` returns true → ERROR with "Re-run installer or install CUDA 12 Toolkit".
5. `vendor\qdrant\qdrant.exe` exists → WARN.
6. `.env` exists AND contains non-empty `AUTH_SECRET_KEY` and `HISTORY_SECRET_KEY` → ERROR each.
7. `& $py -m backend.seed_admin --check-only` exits 0 → ERROR if not (no admin user).
8. `nvidia-smi` present and exits 0 → WARN (CPU-only fallback works).
9. Required ports (`18000, 6333, 8090, 8001, 8010, 8011, 8012, 8013`) free of foreign listeners → WARN per port (filter own services: `python|pythonw|qdrant|llama-server|jupyter|jupyter-lab`).
10. `data\models\embedding.gguf` exists → WARN (RAG/memory disabled until pulled).

`Show-PreflightDialog -Result <hashtable>`: render a single Win32 MessageBox with all errors and warnings as bullets. Falls back to `Write-Host` on Server Core (no PresentationFramework). Silent when ok+no warnings.

**File:** `LocalAIStack.ps1` `Invoke-Start`

- BEFORE any subprocess spawn, run preflight (skipping in CI smoke pattern):
  ```powershell
  $isCiSmoke = $NoGui -and $Offline -and $NoUpdateCheck
  if ($isCiSmoke) {
      Write-Warn2 'Preflight skipped (-NoGui -Offline -NoUpdateCheck = CI smoke-test).'
  } elseif (Get-Command Invoke-Preflight -ErrorAction SilentlyContinue) {
      $pre = Invoke-Preflight -RepoRoot $RepoRoot -VendorDir $VendorDir `
                              -DataDir $DataDir -EnvFile $EnvFile
      foreach ($e in $pre.errors)   { Write-Err  $e }
      foreach ($w in $pre.warnings) { Write-Warn2 $w }
      if (-not $pre.ok) {
          if (-not $NoGui -and (Get-Command Show-PreflightDialog -ErrorAction SilentlyContinue)) {
              Show-PreflightDialog -Result $pre
          }
          throw "Startup blocked by preflight. " + $pre.suggestion
      }
  }
  ```

---

## 29. Installer / runtime EXE split with shortcuts

**File:** `installer/Installer.ps1` (new)

- Thin dispatcher compiled to `LocalAIStackInstaller.exe`. Handles three modes:
  - default (no flags) → invokes `LocalAIStack.ps1 -Setup`
  - `-Reconfigure` → invokes `LocalAIStack.ps1 -SetupGui`
  - `-RepairOnly` → invokes `LocalAIStack.ps1 -Setup -SkipModels`
- Walk up from `$PSScriptRoot` if it sits inside `installer/` (dev layout).

**File:** `LocalAIStack.ps1`

- New `[switch]$SetupGui` parameter in the param block (own ParameterSetName). Wire `if ($SetupGui) { Invoke-SetupGui; return }` into the dispatcher.
- New `Invoke-SetupGui` function — runs the GUI wizard only via `& $guiPy gui\main.py --mode wizard`. No prereq check, no vendor downloads.
- `Invoke-Build` rewritten to compile BOTH binaries:
  - `LocalAIStack.exe` from `$MyInvocation.MyCommand.Path` (existing).
  - `LocalAIStackInstaller.exe` from `installer\Installer.ps1` with `requireAdmin = $true`.

**File:** `installer/LocalAIStack.iss`

- `[Files]` section bundles both `..\LocalAIStack.exe` and `..\LocalAIStackInstaller.exe` (`skipifsourcedoesntexist` for the latter).
- `[Tasks]` section: both `desktopicon` and `startmenuicon` use plain `Flags: unchecked` (deliberate opt-in every install).
- `[Icons]` Start Menu entries (only `LocalAIStack.exe` for primary launches; the installer EXE only appears as a "Reconfigure" entry):
  ```
  Name: "{group}\{#AppName}";        Filename: "{app}\{#ExeName}";  Tasks: startmenuicon
  Name: "{group}\Admin Console";     Filename: "{app}\{#ExeName}";  Parameters: "-Admin"; Tasks: startmenuicon
  Name: "{group}\Health Check";      Filename: "{app}\{#ExeName}";  Parameters: "-Test";  Tasks: startmenuicon
  Name: "{group}\Reconfigure {#AppName}"; Filename: "{app}\LocalAIStackInstaller.exe"; Parameters: "-Reconfigure"; Tasks: startmenuicon
  Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
  Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#ExeName}";  Tasks: desktopicon
  ```
- `[Run]` three phases:
  - Phase 1 (always, hidden, blocking): `LocalAIStackInstaller.exe -RepairOnly`.
  - Phase 2 (post-install, default checked): `LocalAIStackInstaller.exe` for the wizard.
  - Phase 3 (post-install, default `unchecked`): `LocalAIStack.exe` to start the runtime — wizard must finish first.

---

## 30. `.gitignore`

**File:** `.gitignore`

- Ensure these are present (add any missing):
  ```
  vendor/
  __pycache__/
  *.pyc
  *.pyo
  .pytest_cache/
  launcher/dist/
  *.log
  ```
  `vendor/` covers `qdrant.exe`, `llama-server.exe`, the CUDA redist DLLs, and the three Python venvs (~1 GB).

---

## Verification before merging

1. `pytest tests/test_diagnostics.py` passes with no `n8n_auth` references.
2. Static-check the wizard imports — `webbrowser`, `QTimer`, `QHBoxLayout`, `QWidget`, `QPushButton`, `QThread`, `Signal`, `QProgressBar`, `subprocess.CREATE_NO_WINDOW`.
3. On a clean Windows machine with NVIDIA driver but no CUDA Toolkit: `LocalAIStack.ps1 -Setup -SkipModels` leaves `vendor/llama-server/cudart64_12.dll` on disk.
4. After `-Setup` (no `-SkipModels`): all six `data/models/<tier>.gguf` files present (or pulling) within minutes.
5. `LocalAIStack.ps1 -Start` → `curl http://127.0.0.1:18000/healthz` returns `{"ok":true,"status":"ok",...}` once the embedding tier finishes loading.
6. Browser at `http://127.0.0.1:18000`:
   - Login with `username` (NOT email) + password works; bad credentials return distinct error vs. backend down.
   - Tier dropdown reads `"Highest quality: Qwen3-72B-Instruct"` etc.; mid-download tiers show `"…  (downloading…)"` and are disabled.
   - Sidebar shows past conversations; clicking one resumes with full history.
   - `"test"` to fast tier streams a response.
   - Switching tiers mid-chat shows `window.confirm`.
   - 🔧 Tools popover defaults all toggles off.
   - 📎 Attach uploads via `/api/chat/upload`.
7. Admin GUI (`gui/main.py --mode admin`):
   - Sign-in dialog responds within ~1s; bad credentials don't deadlock.
   - Models tab populates with one row per resolved tier; Progress column shows live `QProgressBar` per tier.
   - Users tab Last login formatted as `YYYY-MM-DD HH:MM:SS`.

When verifying interactively, the active admin's password must be the one set during the wizard run on that machine. The original repo (no edits) has no admin user; the wizard seeds it.

Open one PR titled `Native Windows mode: wizard, CUDA, llama.cpp b8992, chat UX, installer split, conversation resumption`. Reference this prompt in the PR body.
