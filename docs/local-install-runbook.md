# Local Install Runbook (for a Claude Code agent)

**Audience:** a Claude Code agent running on the user's Windows machine, about
to drive `LocalAIStack.ps1 -Setup -Start` for the first time.

**Status:** procedure validated end-to-end against a fresh `windows-latest`
GitHub-hosted runner — see [Cloud validation evidence](#cloud-validation-evidence)
below. If any step on the local box behaves *materially differently* from the
expected signal, stop and surface it to the user rather than papering over it.

> **Tone reminder:** narrate one sentence before each step you take; do not
> retry destructive commands silently; route every failure through the
> diagnostics block at the bottom.

---

## Cloud validation evidence

The same procedure below was executed on a clean `windows-latest` cloud VM
(GitHub Actions, run 25188693239) on 2026-04-30. It completed in 3m22s end
to end. Per-step timings and proof of work:

| Step              | Cloud time | Proof of work                                                |
|-------------------|------------|--------------------------------------------------------------|
| `-InitEnv`        | ~5s        | `.env` written with all expected keys                        |
| `-Setup`          | **107s**   | 3 venvs + qdrant.exe (71 MB) + llama-server.exe (2.3 MB)     |
| Ollama smoke      | 15s        | `qwen2.5:0.5b` pulled and `/api/generate` returned non-empty |
| Backend `-Start`  | 12s        | `/healthz` 200; `/v1/models` returned ≥ 1 tier               |
| GUI offscreen     | ~3-5s      | `ChatWindow + tray` constructed under `QT_QPA_PLATFORM=offscreen` |
| `cloudflared`     | ~1s        | `--version` and `tunnel --help` exited 0                     |

Local timings will be slower (no aggressive runner cache; user network may
be slower than the runner's ~1 Gbps). A 2-3× wall-clock multiplier on each
step is normal. **Do not** treat slowness as a failure signal — only assertion
mismatches.

---

## 0. Preflight (run before `-Setup`)

Run these checks first. Each one must pass; if any fail, fix before proceeding.

```powershell
# Working directory: repo root (must contain LocalAIStack.ps1)
Test-Path .\LocalAIStack.ps1

# Windows + PowerShell version
$PSVersionTable.PSVersion          # expect 5.1+ or 7.x
[System.Environment]::OSVersion    # expect Microsoft Windows NT 10.x

# winget present (used by -Setup -SkipPrereqs:$false to install Ollama, Python, NVIDIA driver checks)
Get-Command winget -ErrorAction SilentlyContinue

# python on PATH (any 3.11+; -Setup creates its own venvs but bootstrap needs a system python)
python --version
```

If `winget` is missing, ask the user to install **App Installer** from the
Microsoft Store before continuing. If `python` is missing, ask them to
install Python 3.12 (winget: `Python.Python.3.12`) and re-open the terminal.

---

## 1. `-InitEnv` — write `.env`

```powershell
.\LocalAIStack.ps1 -InitEnv
```

**Expected:** ~5s, exits 0. File `.env` exists in repo root with these keys
present (values can be defaults; only keys matter at this stage):

```
AUTH_SECRET_KEY
HISTORY_SECRET_KEY
CHAT_HOSTNAME
WEB_SEARCH_PROVIDER
MODEL_UPDATE_POLICY
```

**Assert before moving on:**
```powershell
$env_content = Get-Content .env -Raw
foreach ($k in 'AUTH_SECRET_KEY','HISTORY_SECRET_KEY','CHAT_HOSTNAME','WEB_SEARCH_PROVIDER','MODEL_UPDATE_POLICY') {
  if ($env_content -notmatch "(?m)^$k=") { throw "Missing $k in .env" }
}
```

`-InitEnv` writes placeholder secrets. If the user wants real production
secrets, edit `.env` now or use `-InitEnv -Force` after.

---

## 2. `-Setup` — venvs + vendor binaries (+ optional model pulls)

```powershell
.\LocalAIStack.ps1 -Setup
```

**Expected on a cold machine:** 3-15 minutes depending on whether models are
pulled (default behavior pulls all configured tier models — this can be
**tens of GB** and very slow). For a "is the install path even working"
smoke, run:

```powershell
.\LocalAIStack.ps1 -Setup -SkipModels
```

That's the variant we validated in cloud (107s with -SkipModels and
-SkipPrereqs). Locally, expect 3-8 minutes for the venv + vendor-binary
work.

**Hard assertions after `-Setup` returns:**

```powershell
$expected = @(
  'vendor\venv-backend\Scripts\python.exe',
  'vendor\venv-gui\Scripts\python.exe',
  'vendor\venv-jupyter\Scripts\python.exe',
  'vendor\qdrant\qdrant.exe',
  'vendor\llama-server\llama-server.exe',
  'data\resolved-models.json'
)
foreach ($p in $expected) {
  if (-not (Test-Path $p)) { throw "Setup did not produce $p" }
  $size = (Get-Item $p).Length
  Write-Host "  OK  $p ($size bytes)"
}
```

**Cloud-validated sizes** (use as sanity floor, not exact match — vendor
binary versions move):

- `vendor\qdrant\qdrant.exe` ~71 MB
- `vendor\llama-server\llama-server.exe` ~2.3 MB
- each `python.exe` ~270 KB (these are venv launcher stubs, not full Python)
- `data\resolved-models.json` ~1.5 KB and contains a `tiers` key

If `Setup` exits non-zero, check `%APPDATA%\LocalAIStack\logs` for
`setup-*.log` — see [Diagnostics](#diagnostics).

---

## 3. Optional: pull a small model to verify Ollama works

You can skip this if `-Setup` already pulled the configured tier models. For
a fast "Ollama works at all" check:

```powershell
ollama serve              # leave running in a separate terminal/job
Start-Sleep -Seconds 5
ollama pull qwen2.5:0.5b  # ~400 MB; cloud took ~10s, local will be slower
ollama list               # must show qwen2.5:0.5b
```

```powershell
$body = @{ model = 'qwen2.5:0.5b'; prompt = 'Say OK.'; stream = $false } | ConvertTo-Json
$r = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/generate' -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 120
if (-not $r.response) { throw 'Ollama returned empty response' }
Write-Host "Ollama response: $($r.response)"
```

---

## 4. `-Start` — backend + (optional) GUI

For a headless verification first:

```powershell
.\LocalAIStack.ps1 -Start -NoGui
```

**Expected:** ~10-30s to reach `/healthz` 200. The launcher itself polls
`/healthz` for 120s before returning.

**Assertions:**

```powershell
$ok = $false
foreach ($i in 1..30) {
  try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:18000/healthz' -UseBasicParsing -TimeoutSec 4
    if ($r.StatusCode -eq 200) { $ok = $true; break }
  } catch { }
  Start-Sleep -Seconds 3
}
if (-not $ok) { throw 'Backend /healthz never responded — see %APPDATA%\LocalAIStack\logs' }

$r = Invoke-RestMethod -Uri 'http://127.0.0.1:18000/v1/models' -TimeoutSec 10
if (@($r.data).Count -lt 1) { throw '/v1/models returned no tiers' }
Write-Host "Backend up; tiers: $($r.data.id -join ', ')"
```

Once that's green, start with the GUI:

```powershell
.\LocalAIStack.ps1 -Stop      # stop the headless run first
.\LocalAIStack.ps1 -Start     # default: chat window + tray
```

The GUI is **PySide6 + qasync**; it expects a desktop session. If the user is
on Windows Server / Core / RDP without a graphics stack, fall back to
`-NoGui` and tell them.

---

## 5. Healthcheck (`-Test`)

```powershell
.\LocalAIStack.ps1 -Test
```

This runs the local health-check suite under `tests/health_areas/`. Each
area exercises a different subsystem (auth, RAG, memory, tier loading, etc.).
On a fresh setup expect all green; if anything is red, surface the area name
and let the user decide whether to `-Test -Fix -Area <name>`.

---

## Diagnostics

If any step fails:

1. **Show stop status** so the launcher's tracked subprocesses stop:
   ```powershell
   .\LocalAIStack.ps1 -Stop
   ```

2. **Collect logs** from the launcher's log directory:
   ```powershell
   $logs = Join-Path $env:APPDATA 'LocalAIStack\logs'
   Get-ChildItem $logs -File | Sort-Object LastWriteTime -Descending | Select-Object -First 5
   # Then read the most recent one(s) with Get-Content -Tail 100
   ```

3. **Check `pids.json`**:
   ```powershell
   $pidf = Join-Path $env:APPDATA 'LocalAIStack\pids.json'
   if (Test-Path $pidf) { Get-Content $pidf -Raw }
   ```

4. **Port collisions** (backend = 18000, ollama = 11434, llama-server = 8001,
   qdrant = 6333, jupyter = 8888):
   ```powershell
   foreach ($p in 18000, 11434, 8001, 6333, 8888) {
     $hits = netstat -ano | Select-String ":$p\s"
     if ($hits) { Write-Host "Port $p in use:`n$hits" }
   }
   ```

5. **NVIDIA driver** (only matters if a GPU tier is supposed to be loaded):
   ```powershell
   nvidia-smi   # must show a GPU and driver version 550+
   ```

Surface findings to the user before proposing a fix; never `git stash`,
`Remove-Item -Force`, or otherwise discard local state to "make the error
go away." If a venv looks broken, prefer `.\LocalAIStack.ps1 -Setup` (which
re-creates idempotently) over deleting `vendor\` manually.

---

## What this runbook does NOT cover

- **Cloudflare Tunnel provisioning.** The setup wizard handles the OAuth
  flow interactively; do not attempt to script it. If the user wants public
  ingress, run `.\LocalAIStack.ps1 -Setup` interactively (no `-SkipModels`
  needed for tunnel) and let them click through Cloudflare's browser flow.
- **Real production secrets.** `-InitEnv` writes placeholders. If the user
  asks you to "set a real auth key," edit `.env` directly with a generated
  base64 secret rather than using `-InitEnv -Force` (which would clobber
  any other manual edits).
- **Model selection.** `config/models.yaml` defines five tiers (highest
  quality / versatile / fast / coding / vision). If the user has limited
  VRAM, point them at the `versatile` (Qwen3.6 35B-A3B MoE, ~21 GB) tier
  and don't try to "fix" models.yaml programmatically — that's a user
  decision.
- **Air-gapped installs.** `OFFLINE=1` in `.env` plus `-Offline` on
  `-Start` skips network calls but requires all models + binaries to
  already be present locally. The cloud run validated `-Setup -SkipModels
  -SkipPrereqs OFFLINE=1`; full air-gap is out of scope here.

---

## Quick reference

```powershell
.\LocalAIStack.ps1 -InitEnv           # write .env
.\LocalAIStack.ps1 -Setup             # full install (slow; pulls models)
.\LocalAIStack.ps1 -Setup -SkipModels # fast install path (cloud-validated)
.\LocalAIStack.ps1 -Start             # backend + GUI
.\LocalAIStack.ps1 -Start -NoGui      # backend only (headless)
.\LocalAIStack.ps1 -Stop              # stop tracked subprocesses
.\LocalAIStack.ps1 -Test              # local health-check suite
.\LocalAIStack.ps1 -Help              # full operator manual
```
