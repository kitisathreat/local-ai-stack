#Requires -Version 5.1
<#
.SYNOPSIS
  Local AI Stack — single-file setup, launcher, and build tool for
  non-Docker native Windows mode.

.DESCRIPTION
  One script to rule them all. Replaces setup.ps1, launcher/*.ps1,
  scripts/start.ps1, scripts/stop.ps1, and launcher/build.ps1 with a
  consolidated command surface:

      .\LocalAIStack.ps1              # default: -Start
      .\LocalAIStack.ps1 -Setup       # install prereqs + vendor binaries + venvs + models
      .\LocalAIStack.ps1 -Start       # resolve models, spawn services, launch GUI, tray
      .\LocalAIStack.ps1 -Stop        # terminate all tracked processes
      .\LocalAIStack.ps1 -Build       # ps2exe into LocalAIStack.exe
      .\LocalAIStack.ps1 -InitEnv     # write a default .env if missing
      .\LocalAIStack.ps1 -CheckUpdates# re-poll HuggingFace for model updates
      .\LocalAIStack.ps1 -Help        # full operator guide (run for deep docs)
      .\LocalAIStack.ps1 -Start -NoUpdateCheck  # skip polling, use pinned
#>

[CmdletBinding(DefaultParameterSetName = 'Start')]
param(
    [Parameter(ParameterSetName = 'Setup')]           [switch]$Setup,
    [Parameter(ParameterSetName = 'SetupGui')]        [switch]$SetupGui,
    [Parameter(ParameterSetName = 'Start')]           [switch]$Start,
    [Parameter(ParameterSetName = 'Stop')]            [switch]$Stop,
    [Parameter(ParameterSetName = 'Build')]           [switch]$Build,
    [Parameter(ParameterSetName = 'BuildInstaller')]  [switch]$BuildInstaller,
    [Parameter(ParameterSetName = 'InitEnv')]         [switch]$InitEnv,
    [Parameter(ParameterSetName = 'CheckUpdates')]    [switch]$CheckUpdates,
    [Parameter(ParameterSetName = 'Admin')]           [switch]$Admin,
    [Parameter(ParameterSetName = 'Test')]            [switch]$Test,
    [Parameter(ParameterSetName = 'Help')]            [switch]$Help,

    # Modifier flags (may combine with -Start)
    [Parameter(ParameterSetName = 'Start')] [switch]$NoUpdateCheck,
    [Parameter(ParameterSetName = 'Start')] [switch]$Offline,
    [Parameter(ParameterSetName = 'Start')] [switch]$NoGui,

    # Modifier flag for -Setup
    [Parameter(ParameterSetName = 'Setup')]  [switch]$SkipModels,
    [Parameter(ParameterSetName = 'Setup')]  [switch]$SkipPrereqs,

    # Modifier flags for -Test
    [Parameter(ParameterSetName = 'Test')] [switch]$Fix,
    [Parameter(ParameterSetName = 'Test')] [string]$Area,

    # -Force: overwrite .env even when it already exists (-InitEnv)
    [Parameter(ParameterSetName = 'InitEnv')] [switch]$Force
)

$ErrorActionPreference = 'Stop'

# ── Paths ────────────────────────────────────────────────────────────────────
$Script:RepoRoot  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script:StepsDir  = Join-Path $RepoRoot 'scripts\steps'
$Script:VendorDir = Join-Path $RepoRoot 'vendor'
$Script:ConfigDir = Join-Path $RepoRoot 'config'
$Script:DataDir   = Join-Path $RepoRoot 'data'
$Script:AssetsDir = Join-Path $RepoRoot 'assets'
$Script:EnvFile   = Join-Path $RepoRoot '.env'
$Script:AppData   = Join-Path $env:APPDATA 'LocalAIStack'
$Script:LogsDir   = Join-Path $AppData 'logs'
$Script:PidFile   = Join-Path $AppData 'pids.json'

# ── Pinned third-party release tags (override with env vars) ────────────────
# SHA256 hashes are for the Windows CUDA release assets; leave blank to skip
# verification (dev only). Ship updated hashes when bumping versions.
$Script:QdrantVersion    = if ($env:LAI_QDRANT_VERSION)    { $env:LAI_QDRANT_VERSION }    else { 'v1.12.4' }
$Script:QdrantSha256     = if ($env:LAI_QDRANT_SHA256)     { $env:LAI_QDRANT_SHA256 }     else { '' }
$Script:LlamaCppVersion  = if ($env:LAI_LLAMACPP_VERSION)  { $env:LAI_LLAMACPP_VERSION }  else { 'b4404' }
$Script:LlamaCppSha256   = if ($env:LAI_LLAMACPP_SHA256)   { $env:LAI_LLAMACPP_SHA256 }   else { '' }

# ── Tiny helpers (dedupe pattern from setup.ps1/launcher) ────────────────────
function Write-Step($msg)    { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)      { Write-Host "   ok $msg" -ForegroundColor Green }
function Write-Warn2($msg)   { Write-Host "   !! $msg" -ForegroundColor Yellow }
function Write-Err($msg)     { Write-Host "   xx $msg" -ForegroundColor Red }

function Test-Command($name) {
    try { return [bool](Get-Command $name -ErrorAction Stop) } catch { return $false }
}

function Ensure-Dir($path) {
    if (-not (Test-Path $path)) { New-Item -ItemType Directory -Path $path -Force | Out-Null }
}

# NOTE: step files must be dot-sourced at script scope (see bottom of
# this file). Dot-sourcing inside a function or ForEach-Object block
# puts the imported functions in that inner scope and they disappear
# when the block returns — so Get-Command Invoke-DownloadQdrant / …
# inside Invoke-Setup silently fails and Setup does nothing.

# ── Env file support ─────────────────────────────────────────────────────────
$Script:EnvTemplate = @'
# Local AI Stack — native mode environment.
# Created by `LocalAIStack.ps1 -InitEnv`. Loaded by -Start into the backend
# and GUI child processes. Treat secrets as sensitive.

# ── Auth ──────────────────────────────────────────────────────────────
AUTH_SECRET_KEY=
HISTORY_SECRET_KEY=

# ── Chat subdomain gating ──────────────────────────────────────────────
# Chat (POST /v1/chat/completions, /api/chats, /api/rag, /api/memory, /)
# is only reachable via this hostname unless airgap mode is on, in which
# case only loopback works.
CHAT_HOSTNAME=chat.mylensandi.com
ADMIN_API_ALLOWED_HOSTS=127.0.0.1,localhost
PUBLIC_BASE_URL=https://chat.mylensandi.com

# ── Web search ─────────────────────────────────────────────────────────
# Provider: brave | ddg | none   (default: brave if BRAVE_API_KEY set, else ddg)
WEB_SEARCH_PROVIDER=ddg
BRAVE_API_KEY=

# ── Model update behaviour ─────────────────────────────────────────────
MODEL_UPDATE_POLICY=prompt        # auto | prompt | skip
HF_TOKEN=                         # optional, for gated/private HF repos
OFFLINE=                          # 1 = never poll upstream registries

# ── Service URLs (leave blank to use native localhost defaults) ────────
QDRANT_URL=
JUPYTER_URL=

# ── Single-worker native mode: Redis unused ────────────────────────────
REDIS_URL=
'@

function Invoke-InitEnv {
    if (Test-Path $EnvFile) {
        Write-Warn2 ".env already exists at $EnvFile — leaving it alone"
        return
    }
    $EnvTemplate | Out-File -FilePath $EnvFile -Encoding utf8NoBOM
    Write-Ok "Wrote default .env to $EnvFile"
}

function Read-EnvFile {
    if (-not (Test-Path $EnvFile)) { return @{} }
    $result = [ordered]@{}
    foreach ($line in Get-Content $EnvFile) {
        if ($line -match '^\s*#' -or -not $line.Trim()) { continue }
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $key = $Matches[1]; $val = $Matches[2].Trim('"').Trim("'")
            $result[$key] = $val
        }
    }
    return $result
}

function Apply-Env($dict) {
    foreach ($k in $dict.Keys) {
        if ($dict[$k] -ne '') { Set-Item -Path "Env:$k" -Value $dict[$k] }
    }
    # Native-mode defaults the launcher owns regardless of .env.
    $Env:LAI_CONFIG_DIR  = $ConfigDir
    $Env:LAI_DATA_DIR    = $DataDir
    $Env:LAI_DB_PATH     = Join-Path $DataDir 'lai.db'
    $Env:LAI_HISTORY_DIR = Join-Path $DataDir 'history'
    $Env:LAI_AIRGAP_STATE= Join-Path $DataDir 'airgap.state'
    $Env:LAI_TOOLS_DIR   = Join-Path $RepoRoot 'tools'
    if (-not $Env:QDRANT_URL)   { $Env:QDRANT_URL   = 'http://127.0.0.1:6333' }
    if (-not $Env:JUPYTER_URL)  { $Env:JUPYTER_URL  = 'http://127.0.0.1:8888' }
}

# ── Help ─────────────────────────────────────────────────────────────────────
function Invoke-Help {
@'
Local AI Stack — native Windows mode (no Docker, no browser)
============================================================

First run
---------
  1. .\LocalAIStack.ps1 -InitEnv        Write a default .env at the repo root.
  2. Edit .env: set AUTH_SECRET_KEY, HISTORY_SECRET_KEY, optionally BRAVE_API_KEY / HF_TOKEN.
  3. .\LocalAIStack.ps1 -Setup          Install prereqs, download binaries,
                                        create Python venvs, pull GGUFs from
                                        Hugging Face.
  4. .\LocalAIStack.ps1 -Start          Launch. A native Qt window opens; no browser.

What -Setup installs
--------------------
  Verified prerequisites (one-time UAC prompt per missing package):
    Git.Git                      (for self-updates)
    Python.Python.3.12           (backend + GUI + Jupyter venvs)
    Microsoft.PowerShell         (PS 7 — recommended)
    Cloudflare.cloudflared       (HTTPS tunnel for chat.<your-domain>)

  Detected (never auto-installed):
    NVIDIA driver >= 550 (CUDA 12 runtime is bundled).
    If missing, download: https://www.nvidia.com/Download/index.aspx

  Downloaded + SHA256-verified:
    vendor\qdrant\qdrant.exe             (pinned via LAI_QDRANT_VERSION)
    vendor\llama-server\llama-server.exe (pinned via LAI_LLAMACPP_VERSION)

  Python venvs created under vendor\:
    venv-backend  ~250 MB
    venv-gui      ~180 MB
    venv-jupyter  ~400 MB

Daily use
---------
  .\LocalAIStack.ps1                    = -Start
  .\LocalAIStack.ps1 -Admin             Open the admin Qt window (login prompt).
  .\LocalAIStack.ps1 -Start -NoUpdateCheck
                                        Skip HF polling.
  .\LocalAIStack.ps1 -CheckUpdates      Re-poll, list pending updates.
  .\LocalAIStack.ps1 -Stop              Terminate all tracked child processes.
  .\LocalAIStack.ps1 -Build             Compile this script to LocalAIStack.exe.

Cloudflared
-----------
Native mode never starts cloudflared. Point your existing native cloudflared
tunnel at the backend. Chat is host-gated, so the subdomain in your ingress
MUST match the CHAT_HOSTNAME env var (default: chat.mylensandi.com).

IMPORTANT: list the chat hostname BEFORE any wildcard catch-all or
`http_status:404` fallback — cloudflared evaluates rules top-to-bottom.

    ingress:
      - hostname: chat.mylensandi.com
        service: http://localhost:18000
      - service: http_status:404

Logs
----
  %APPDATA%\LocalAIStack\logs\<service>.log      Per-service stdout/stderr
  %APPDATA%\LocalAIStack\pids.json               Tracked PIDs (used by -Stop)

Services started by -Start
--------------------------
  qdrant              127.0.0.1:6333
  llama-server        127.0.0.1:8001    (vision tier GGUF)
  llama-server        127.0.0.1:8090    (embedding tier — nomic-embed-text)
  jupyter-lab         127.0.0.1:8888    (code-interpreter sandbox; never opens a browser)
  backend (FastAPI)   127.0.0.1:18000   (uvicorn, single worker)
  gui (PySide6)       no listening port — talks to the backend over httpx

  The 4 chat tiers (highest_quality, versatile, fast, coding) cold-spawn
  on first request via the backend's VRAMScheduler — no pre-spawn at boot.

Model version resolution
------------------------
On -Start the script runs `python -m backend.model_resolver resolve`, which
polls Hugging Face for each tier declared in config\model-sources.yaml. On
any network failure or when OFFLINE=1 is set in .env, the resolver falls
back to the pinned version recorded in that file.

Set MODEL_UPDATE_POLICY in .env to control behaviour when an update is
detected:
  auto    download immediately
  prompt  GUI dialog asks the user (default)
  skip    note it, do not download until next -Setup

Uninstall / reset
-----------------
  .\LocalAIStack.ps1 -Stop
  Remove-Item -Recurse $env:APPDATA\LocalAIStack
  Remove-Item -Recurse .\vendor .\data
  # .\data\lai.db holds users, chats, memories, and RAG metadata.
  # Deleting it resets the install; re-run -Setup to re-seed an admin.
  #
  # The Phase 3 schema migration (v2 -> v3, magic-link -> password
  # auth) is one-way. A manual downgrade would require
  # `ALTER TABLE users DROP COLUMN username; ...` + recreating the
  # magic_links table. In practice: blow away data/lai.db.

Web-search providers
--------------------
  WEB_SEARCH_PROVIDER=ddg (default)     DuckDuckGo via ddgs pip package.
                                        Rate-limits silently after a few
                                        hundred queries/day; fine for
                                        interactive use but switch to
                                        Brave once you're past that.
  WEB_SEARCH_PROVIDER=brave             Brave Search API. Free tier is
                                        2000 queries/month — sign up at
                                        https://api.search.brave.com/app/keys
                                        and set BRAVE_API_KEY in .env.
  WEB_SEARCH_PROVIDER=none              Disabled. Tools that call the
                                        middleware return empty results.

Disk and memory
---------------
  vendor\venv-backend   ~250 MB
  vendor\venv-gui       ~180 MB  (PySide6 + QtCharts)
                                 Dropped to ~120 MB if you swap to
                                 PySide6-Essentials (no multimedia, no
                                 WebEngine).
  vendor\venv-jupyter   ~400 MB
  vendor\qdrant         ~40 MB
  vendor\llama-server   ~220 MB  (CUDA build)
  GGUF models           24 - 72 GB depending on tier group
  Vision GGUF           ~25 GB  (optional; download offered during -Setup)
  Embedding GGUF        ~150 MB (nomic-embed-text-v1.5.Q8_0)

Windows Developer Mode
----------------------
  Creating the data\models\vision.gguf symlink to the Hugging Face
  download requires symlink privileges. On Windows 10/11 non-admin
  accounts, enable Developer Mode (Settings -> Privacy & security ->
  For developers). If disabled, model_resolver falls back to a full
  file copy, wasting ~25 GB but still working.

Full documentation of internals lives under docs\ (architecture, API,
troubleshooting). Re-run .\LocalAIStack.ps1 -Help for this summary.
'@ | Write-Host
}

# ── Setup ────────────────────────────────────────────────────────────────────
function Invoke-Setup {
    # Consolidated prerequisite check (Windows build, NVIDIA driver,
    # winget-installed tools). Idempotent — safe to call on every -Setup.
    if ($SkipPrereqs) {
        Write-Warn2 'Skipping prereq check (-SkipPrereqs).'
    } elseif (Get-Command Invoke-EnsurePrereqs -ErrorAction SilentlyContinue) {
        Invoke-EnsurePrereqs
    } else {
        throw "scripts\steps\prereqs.ps1 missing — re-clone the repo."
    }

    Ensure-Dir $VendorDir
    Ensure-Dir $DataDir
    Ensure-Dir $AssetsDir
    Ensure-Dir $AppData
    Ensure-Dir $LogsDir

    if (Get-Command Invoke-DownloadQdrant -ErrorAction SilentlyContinue) {
        Invoke-DownloadQdrant -Version $QdrantVersion -Dest (Join-Path $VendorDir 'qdrant') -Sha256 $QdrantSha256
    }
    if (Get-Command Invoke-DownloadLlamaServer -ErrorAction SilentlyContinue) {
        Invoke-DownloadLlamaServer -Version $LlamaCppVersion -Dest (Join-Path $VendorDir 'llama-server') -Sha256 $LlamaCppSha256
    }
    if (Get-Command Invoke-DownloadCudaRuntime -ErrorAction SilentlyContinue) {
        # llama-server is a CUDA 12 build and silently dies (0xC0000135)
        # when the cudart/cublas DLLs are missing. Provision them next to
        # the exe unless they're already discoverable on PATH / CUDA_PATH.
        Invoke-DownloadCudaRuntime -LlamaCppVersion $LlamaCppVersion -Dest (Join-Path $VendorDir 'llama-server')
    }
    if (Get-Command Invoke-CreateVenvs -ErrorAction SilentlyContinue) {
        Invoke-CreateVenvs -Root $VendorDir -RepoRoot $RepoRoot
    }

    Apply-Env (Read-EnvFile)
    $py = Join-Path $VendorDir 'venv-backend\Scripts\python.exe'

    if ($SkipModels) {
        Write-Step 'Resolving tiers + dry-run pull (no downloads)'
        if (Test-Path $py) {
            & $py -m backend.model_resolver resolve --force --pull --dry-run --offline
            if ($LASTEXITCODE -ne 0) {
                throw "model_resolver dry-run exited with code $LASTEXITCODE"
            }
        } else {
            Write-Warn2 "Backend venv missing at $py — skipping dry-run"
        }
        Write-Warn2 'Skipping actual model pulls and admin seed (-SkipModels).'
    } else {
        Write-Step 'Pulling GGUF model tiers from Hugging Face'
        if (Test-Path $py) {
            # Pulls the small tiers (fast + embedding) by default; the
            # vision GGUF (~25 GB) is gated behind a confirmation below
            # because it's the biggest single file.
            & $py -m backend.model_resolver resolve --force --pull

            $visionFile = Join-Path $DataDir 'models\vision.gguf'
            if (-not (Test-Path $visionFile)) {
                Write-Host ''
                Write-Host 'Vision tier GGUF (~25 GB) is not on disk.' -ForegroundColor Cyan
                $answer = Read-Host 'Download now from Hugging Face? [y/N]'
                if ($answer -match '^(y|yes)$') {
                    & $py -m backend.model_resolver resolve --force --pull
                } else {
                    Write-Warn2 'Vision tier skipped — run `-CheckUpdates` or admin panel later.'
                }
            }
        } else {
            Write-Warn2 "Backend venv missing at $py — skipping model pull"
        }

        # First-run admin bootstrap: prompt if no admin user exists yet.
        if (Test-Path $py) {
            Write-Step 'Checking for an admin user'
            & $py -m backend.seed_admin --if-no-admins
        }
    }

    Write-Ok 'Setup complete.'
}

# ── Start ────────────────────────────────────────────────────────────────────
function Invoke-Start {
    Ensure-Dir $AppData
    Ensure-Dir $LogsDir

    # Diagnostic preflight: aggregate every "missing component" check
    # so the user gets ONE actionable message rather than bouncing off
    # the first failure. Skip the dialog when running with -NoGui (CI)
    # since CI parses stdout, not Win32 message boxes.
    if (Get-Command Invoke-Preflight -ErrorAction SilentlyContinue) {
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

    if (-not (Test-Path $EnvFile)) {
        Write-Warn2 ".env missing — creating default. Edit it and re-run."
        Invoke-InitEnv
    }
    Apply-Env (Read-EnvFile)

    # ── Resolve + auto-pull missing GGUFs ─────────────────────────────
    # On first -Start (or any time a tier's GGUF is missing on disk),
    # we run `resolve --pull` so the user doesn't need a separate
    # download step. The pull is idempotent: already-downloaded files
    # are skipped, so re-running -Start is cheap.
    $modelsDir = Join-Path $DataDir 'models'
    $expectedTiers = @('highest_quality','versatile','fast','coding','vision','embedding')
    $missing = @($expectedTiers | Where-Object {
        -not (Test-Path (Join-Path $modelsDir "$_.gguf"))
    })

    if ($NoUpdateCheck -or $Offline -or $Env:OFFLINE -eq '1') {
        Write-Warn2 'Skipping upstream model poll (offline or -NoUpdateCheck).'
        $Env:OFFLINE = '1'
        if ($missing.Count -gt 0) {
            Write-Warn2 "Missing tier GGUFs: $($missing -join ', ') — those tiers will be unavailable until you re-run -Start online."
        }
    } else {
        $py = Join-Path $VendorDir 'venv-backend\Scripts\python.exe'
        if (-not (Test-Path $py)) {
            Write-Warn2 "Backend venv missing — skipping resolution (run -Setup first)"
        } elseif ($missing.Count -gt 0) {
            Write-Step "Resolving + pulling $($missing.Count) missing GGUF tier(s) from HuggingFace: $($missing -join ', ')"
            Write-Host "   .. this may take a while on first run; subsequent -Starts skip already-downloaded files." -ForegroundColor DarkGray
            & $py -m backend.model_resolver resolve --pull
            if ($LASTEXITCODE -ne 0) {
                Write-Warn2 "model_resolver returned exit $LASTEXITCODE — some tiers may still be missing"
            }
        } else {
            Write-Step 'Resolving model versions (HuggingFace) — all GGUFs already on disk'
            & $py -m backend.model_resolver resolve
        }
    }

    # ── Spawn services ───────────────────────────────────────────────
    $pids = [ordered]@{}
    $start = (Get-Command Start-TrackedProcess -ErrorAction SilentlyContinue)
    if (-not $start) {
        throw "scripts\steps\process.ps1 missing — re-clone or run -Setup."
    }

    Write-Step 'Starting qdrant'
    $qdrantBin = Join-Path $VendorDir 'qdrant\qdrant.exe'
    if (Test-Path $qdrantBin) {
        $qdrantStorage = Join-Path $DataDir 'qdrant'
        Ensure-Dir $qdrantStorage
        $pids['qdrant'] = Record-PidEntry (Start-TrackedProcess -Name 'qdrant' -FilePath $qdrantBin `
            -Args @() -LogDir $LogsDir -WorkDir (Split-Path $qdrantBin) `
            -Env @{ QDRANT__STORAGE__STORAGE_PATH = $qdrantStorage })
    } else {
        Write-Warn2 "Qdrant binary missing at $qdrantBin — RAG features disabled"
    }

    # Vision + embedding tiers are pinned and pre-spawned. The four chat
    # tiers (highest_quality, versatile, fast, coding) are subprocess-managed
    # by the backend's VRAMScheduler — they cold-spawn on first request.
    $llamaBin = Join-Path $VendorDir 'llama-server\llama-server.exe'

    Write-Step 'Starting llama-server (vision tier, port 8001)'
    $visionGguf = Join-Path $DataDir 'models\vision.gguf'
    $visionMmproj = Join-Path $DataDir 'models\vision.mmproj.gguf'
    if ((Test-Path $llamaBin) -and (Test-Path $visionGguf)) {
        $visionArgs = @('--host', '127.0.0.1', '--port', '8001', '-m', $visionGguf,
                        '--ctx-size', '16384', '--parallel', '2', '-ngl', '-1', '-fa',
                        '--cache-type-k', 'q8_0', '--cache-type-v', 'q8_0', '--jinja')
        if (Test-Path $visionMmproj) { $visionArgs += @('--mmproj', $visionMmproj) }
        $pids['llama-server'] = Record-PidEntry (Start-TrackedProcess -Name 'llama-server' -FilePath $llamaBin `
            -Args $visionArgs -LogDir $LogsDir)
    } else {
        Write-Warn2 "llama-server or vision GGUF missing — vision tier disabled"
    }

    Write-Step 'Starting llama-server (embedding tier, port 8090)'
    $embedGguf = Join-Path $DataDir 'models\embedding.gguf'
    if ((Test-Path $llamaBin) -and (Test-Path $embedGguf)) {
        $embedArgs = @('--host', '127.0.0.1', '--port', '8090', '-m', $embedGguf,
                       '--ctx-size', '8192', '--parallel', '4', '-ngl', '-1',
                       '--embedding', '--pooling', 'mean')
        $pids['embedding'] = Record-PidEntry (Start-TrackedProcess -Name 'embedding' -FilePath $llamaBin `
            -Args $embedArgs -LogDir $LogsDir)
    } else {
        Write-Warn2 "llama-server or embedding GGUF missing — RAG and memory distillation disabled"
    }

    Write-Step 'Starting jupyter-lab (code interpreter)'
    $jupyter = Join-Path $VendorDir 'venv-jupyter\Scripts\jupyter-lab.exe'
    if (Test-Path $jupyter) {
        $token = [Guid]::NewGuid().ToString('N')
        $Env:JUPYTER_TOKEN = $token
        $pids['jupyter'] = Record-PidEntry (Start-TrackedProcess -Name 'jupyter' -FilePath $jupyter `
            -Args @('--no-browser','--port','8888',"--ServerApp.token=$token") -LogDir $LogsDir)
    } else {
        Write-Warn2 "Jupyter venv missing — code interpreter disabled"
    }

    Write-Step 'Starting backend (FastAPI)'
    $backendPy = Join-Path $VendorDir 'venv-backend\Scripts\python.exe'
    if (-not (Test-Path $backendPy)) {
        throw "Backend venv missing at $backendPy — run -Setup."
    }
    $pids['backend'] = Record-PidEntry (Start-TrackedProcess -Name 'backend' -FilePath $backendPy `
        -Args @('-m','uvicorn','backend.main:app','--host','127.0.0.1','--port','18000') `
        -LogDir $LogsDir -WorkDir $RepoRoot)

    if (Get-Command Wait-HealthOk -ErrorAction SilentlyContinue) {
        Wait-HealthOk -Urls @(
            'http://127.0.0.1:6333/healthz',
            'http://127.0.0.1:18000/healthz'
        ) -TimeoutSeconds 120
    }

    if ($NoGui) {
        Write-Warn2 'Skipping GUI spawn (-NoGui).'
    } else {
        Write-Step 'Launching native GUI'
        $guiPy = Join-Path $VendorDir 'venv-gui\Scripts\pythonw.exe'
        if (Test-Path $guiPy) {
            $pids['gui'] = Record-PidEntry (Start-TrackedProcess -Name 'gui' -FilePath $guiPy `
                -Args @((Join-Path $RepoRoot 'gui\main.py'), '--api', 'http://127.0.0.1:18000') `
                -LogDir $LogsDir -WorkDir $RepoRoot)
        } else {
            Write-Warn2 "GUI venv missing — run -Setup or launch manually"
        }
    }

    ($pids | ConvertTo-Json) | Out-File -FilePath $PidFile -Encoding utf8NoBOM
    Write-Ok "Running. PIDs tracked in $PidFile"
    Write-Host ''
    Write-Host 'Leave this window open (or close it — services stay alive).' -ForegroundColor DarkGray
    Write-Host 'Stop everything:  .\LocalAIStack.ps1 -Stop' -ForegroundColor DarkGray
}

# ── Stop ─────────────────────────────────────────────────────────────────────
function Record-PidEntry([System.Diagnostics.Process]$Process) {
    # Capture the process name at start time so -Stop can verify the PID
    # hasn't been reused between runs (e.g. after a reboot).
    return [pscustomobject]@{
        pid         = $Process.Id
        processName = $Process.ProcessName
    }
}

function Invoke-Stop {
    if (-not (Test-Path $PidFile)) {
        Write-Warn2 "No pids.json at $PidFile — nothing to stop."
        return
    }
    $pids = Get-Content $PidFile -Raw | ConvertFrom-Json
    foreach ($name in $pids.PSObject.Properties.Name) {
        $entry = $pids.$name
        # Back-compat: older pids.json had `{name: pid}`; newer has `{name: {pid, processName}}`.
        if ($entry -is [int] -or $entry -is [long]) {
            $procId = [int]$entry
            $expectedName = $null
        } else {
            $procId = [int]$entry.pid
            $expectedName = $entry.processName
        }
        try {
            $proc = Get-Process -Id $procId -ErrorAction Stop
            if ($expectedName -and $proc.ProcessName -ne $expectedName) {
                Write-Warn2 "skipping $name (pid $procId): expected process '$expectedName' but PID owned by '$($proc.ProcessName)' — likely reused"
                continue
            }
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Ok "stopped $name (pid $procId)"
        } catch [Microsoft.PowerShell.Commands.ProcessCommandException] {
            Write-Warn2 "$name (pid $procId) already gone"
        } catch {
            Write-Warn2 "could not stop $name (pid $procId): $($_.Exception.Message)"
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# ── Test (health-check suite) ────────────────────────────────────────────────
function Invoke-Test {
    Apply-Env (Read-EnvFile)
    $py = Join-Path $VendorDir 'venv-backend\Scripts\python.exe'
    if (-not (Test-Path $py)) {
        Write-Warn2 'venv-backend not found; falling back to system python.'
        $py = 'python'
    }
    $args = @('-m', 'tests.local_health')
    if ($Area)  { $args += @('--area', $Area) }
    if ($Fix)   { $args += '--fix' }
    Write-Step 'Running health-check suite…'
    & $py @args
}

# ── BuildInstaller (Inno Setup) ──────────────────────────────────────────────
function Invoke-BuildInstaller {
    Write-Step 'Building LocalAIStack.exe + LocalAIStackInstaller.exe (ps2exe)…'
    Invoke-Build

    Write-Step 'Freezing GUI with PyInstaller…'
    $guiPy = Join-Path $VendorDir 'venv-gui\Scripts\python.exe'
    if (-not (Test-Path $guiPy)) { throw 'GUI venv not found — run -Setup first.' }
    & $guiPy -m PyInstaller --noconfirm --onedir --windowed `
        --name gui `
        --specpath (Join-Path $RepoRoot 'installer') `
        (Join-Path $RepoRoot 'gui\main.py')

    Write-Step 'Running Inno Setup compiler…'
    $iscc = Join-Path $VendorDir 'inno-setup\ISCC.exe'
    if (-not (Test-Path $iscc)) {
        Write-Warn2 'ISCC.exe not found in vendor\inno-setup\; trying system PATH…'
        $iscc = 'ISCC.exe'
    }
    $iss = Join-Path $RepoRoot 'installer\LocalAIStack.iss'
    & $iscc $iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed (exit $LASTEXITCODE)" }
    Write-Ok 'Installer built: dist\LocalAIStackInstaller-*.exe'
}

# ── Build (ps2exe) ───────────────────────────────────────────────────────────
function Invoke-Build {
    # Compiles BOTH binaries in one pass:
    #   LocalAIStack.exe          ← runtime (this file)
    #   LocalAIStackInstaller.exe ← installer/Installer.ps1
    # The two are bundled together by Inno Setup but only LocalAIStack.exe
    # gets the user-visible Start-menu / desktop shortcuts. The installer
    # is reachable via Apps & Features → Modify and a "Reconfigure"
    # Start-menu entry.
    if (-not (Get-Module -ListAvailable -Name ps2exe)) {
        Write-Warn2 'ps2exe module missing — installing…'
        Install-Module -Name ps2exe -Scope CurrentUser -Force -AllowClobber
    }
    Import-Module ps2exe
    $icon = Join-Path $AssetsDir 'icon.ico'

    # Runtime EXE
    $runtimeOut = Join-Path $RepoRoot 'LocalAIStack.exe'
    $runtimeSplat = @{
        InputFile  = $MyInvocation.MyCommand.Path
        OutputFile = $runtimeOut
        NoConsole  = $true
        Title      = 'Local AI Stack'
        Company    = 'Local AI Stack'
    }
    if (Test-Path $icon) { $runtimeSplat['IconFile'] = $icon }
    Invoke-ps2exe @runtimeSplat
    Write-Ok "Built $runtimeOut"

    # Installer EXE
    $installerSrc = Join-Path $RepoRoot 'installer\Installer.ps1'
    if (Test-Path $installerSrc) {
        $installerOut = Join-Path $RepoRoot 'LocalAIStackInstaller.exe'
        $installerSplat = @{
            InputFile  = $installerSrc
            OutputFile = $installerOut
            NoConsole  = $true
            Title      = 'Local AI Stack Installer'
            Company    = 'Local AI Stack'
            requireAdmin = $true   # prereqs / vendor downloads need admin
        }
        if (Test-Path $icon) { $installerSplat['IconFile'] = $icon }
        Invoke-ps2exe @installerSplat
        Write-Ok "Built $installerOut"
    } else {
        Write-Warn2 "installer\Installer.ps1 missing — skipped LocalAIStackInstaller.exe build"
    }
}

# ── SetupGui ─────────────────────────────────────────────────────────────────
# Runs ONLY the setup wizard (no prereq check, no vendor downloads). Used
# by the installer's -Reconfigure path when the user wants to change
# .env values without re-running the whole setup pipeline.
function Invoke-SetupGui {
    Apply-Env (Read-EnvFile)
    $guiPy = Join-Path $VendorDir 'venv-gui\Scripts\pythonw.exe'
    if (-not (Test-Path $guiPy)) {
        throw "GUI venv missing at $guiPy — run -Setup (full) first."
    }
    Write-Step 'Launching setup wizard for reconfiguration'
    & $guiPy (Join-Path $RepoRoot 'gui\main.py') '--mode' 'wizard'
}

# ── Admin ────────────────────────────────────────────────────────────────────
function Invoke-Admin {
    # Spawn the GUI in admin-only mode. Requires the backend to already
    # be running; fails fast if not.
    Apply-Env (Read-EnvFile)
    $guiPy = Join-Path $VendorDir 'venv-gui\Scripts\pythonw.exe'
    if (-not (Test-Path $guiPy)) {
        throw "GUI venv missing at $guiPy — run -Setup first."
    }
    Write-Step 'Launching admin window'
    & $guiPy (Join-Path $RepoRoot 'gui\main.py') '--mode' 'admin' '--api' 'http://127.0.0.1:18000'
}


# ── CheckUpdates ─────────────────────────────────────────────────────────────
function Invoke-CheckUpdates {
    Apply-Env (Read-EnvFile)
    $py = Join-Path $VendorDir 'venv-backend\Scripts\python.exe'
    if (-not (Test-Path $py)) { throw 'Run -Setup first.' }
    & $py -m backend.model_resolver resolve --force
}

# ── Dispatch ─────────────────────────────────────────────────────────────────
if (Test-Path $StepsDir) {
    # `foreach` (language keyword) runs at script scope; ForEach-Object
    # creates a per-iteration scope that would swallow the dot-sourced
    # function definitions.
    foreach ($f in Get-ChildItem -Path $StepsDir -Filter *.ps1 -File) {
        . $f.FullName
    }
}

if ($Help)             { Invoke-Help;             return }
if ($InitEnv)          { Invoke-InitEnv;          return }
if ($Setup)            { Invoke-Setup;             return }
if ($SetupGui)         { Invoke-SetupGui;          return }
if ($Stop)             { Invoke-Stop;              return }
if ($Build)            { Invoke-Build;             return }
if ($BuildInstaller)   { Invoke-BuildInstaller;    return }
if ($CheckUpdates)     { Invoke-CheckUpdates;      return }
if ($Admin)            { Invoke-Admin;             return }
if ($Test)             { Invoke-Test;              return }

# Default is -Start
Invoke-Start
