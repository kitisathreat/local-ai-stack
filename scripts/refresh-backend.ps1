#Requires -Version 7
<#
.SYNOPSIS
  Refresh the running Local AI Stack after a `git pull`.

.DESCRIPTION
  Diffs the working tree against the previous HEAD (or against the SHA
  passed by the .githooks/post-merge hook) and decides whether to:
    1. pip install backend/gui dependency changes
    2. restart the backend so the running process picks up new code
       and re-discovers the tool registry

  Designed to be safe to run by hand at any time:
    - exits cleanly when nothing relevant changed (e.g. docs-only pull)
    - exits cleanly when the backend isn't currently running
    - prints what it would do, then does it

  Triggered automatically by .githooks/post-merge after every `git pull`.

.PARAMETER PrevSha
  The commit SHA before the merge. The git hook passes ORIG_HEAD here. When
  invoked by hand without -PrevSha, falls back to the marker written on the
  previous run, or refuses to act unless -Force is given.

.PARAMETER Force
  Restart even if no backend-relevant files changed. Useful after editing
  files locally without committing.

.PARAMETER NoRestart
  Install dep changes but don't bounce the backend. The next `LocalAIStack.ps1
  -Stop; -Start` will pick up the code.

.PARAMETER NoDeps
  Skip pip install even if backend/requirements.txt changed.
#>

[CmdletBinding()]
param(
    [string]$PrevSha,
    [switch]$Force,
    [switch]$NoRestart,
    [switch]$NoDeps
)

$ErrorActionPreference = 'Stop'

# pwsh 7+ is required everywhere now — `#Requires -Version 7` already enforces this,
# but a friendly message helps users who type `powershell.exe -File ...` by mistake.
if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Host 'refresh-backend.ps1 requires PowerShell 7 or higher.' -ForegroundColor Red
    Write-Host 'Install:  winget install --id Microsoft.PowerShell --source winget' -ForegroundColor Yellow
    exit 1
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $RepoRoot

function Write-Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "   ok $m" -ForegroundColor Green }
function Write-Skip($m) { Write-Host "   -- $m" -ForegroundColor DarkGray }
function Write-Warn2($m){ Write-Host "   !! $m" -ForegroundColor Yellow }

$AppData    = Join-Path $env:APPDATA 'LocalAIStack'
$PidFile    = Join-Path $AppData 'pids.json'
$MarkerPath = Join-Path $AppData 'last-refresh-sha'
if (-not (Test-Path $AppData)) { New-Item -ItemType Directory -Path $AppData -Force | Out-Null }

# ── Resolve the SHA to diff against ─────────────────────────────────────────
$currentSha = (git rev-parse HEAD).Trim()
if (-not $PrevSha -and (Test-Path $MarkerPath)) {
    $PrevSha = (Get-Content $MarkerPath -Raw -Encoding utf8).Trim()
}

$changed = @()
if ($PrevSha -and ($PrevSha -ne $currentSha)) {
    $changed = git diff --name-only $PrevSha $currentSha 2>$null
} elseif ($PrevSha -eq $currentSha -and -not $Force) {
    Write-Skip "HEAD unchanged since previous refresh ($($currentSha.Substring(0,7))) — nothing to do."
    return
} elseif (-not $Force) {
    Write-Skip "No previous SHA to diff against (use -Force to restart anyway)."
    return
}

# ── Classify the change set ─────────────────────────────────────────────────
$backendRel  = $changed | Where-Object { $_ -match '^(backend|tools|config)/' }
$reqChanged  = $changed -contains 'backend/requirements.txt'
$guiReqChanged = $changed -contains 'gui/requirements.txt'
$launcherRel = $changed | Where-Object { $_ -match '^(LocalAIStack\.ps1|scripts/steps/)' }

if (-not $Force -and -not $backendRel -and -not $reqChanged -and -not $launcherRel) {
    Write-Skip ("No backend-relevant changes between {0} and {1}." -f $PrevSha.Substring(0,7), $currentSha.Substring(0,7))
    if ($changed) {
        Write-Skip ("Changed (irrelevant): {0} files" -f @($changed).Count)
    }
    Set-Content -Path $MarkerPath -Value $currentSha -Encoding utf8
    return
}

Write-Step ("Refreshing for {0} -> {1}" -f $PrevSha.Substring(0,7), $currentSha.Substring(0,7))
if ($backendRel)  { Write-Host ("   backend/tools/config: {0} files changed" -f @($backendRel).Count) -ForegroundColor DarkGray }
if ($launcherRel) { Write-Host ("   launcher/steps:       {0} files changed" -f @($launcherRel).Count) -ForegroundColor DarkGray }
if ($reqChanged)  { Write-Host '   backend/requirements.txt changed → pip install pending' -ForegroundColor DarkGray }
if ($guiReqChanged) { Write-Host '   gui/requirements.txt changed → pip install pending' -ForegroundColor DarkGray }

# ── pip install if requirements changed ─────────────────────────────────────
$venvBackend = Join-Path $RepoRoot 'vendor\venv-backend\Scripts\python.exe'
$venvGui     = Join-Path $RepoRoot 'vendor\venv-gui\Scripts\python.exe'

if (-not $NoDeps -and $reqChanged -and (Test-Path $venvBackend)) {
    Write-Step "Installing backend deps (requirements.txt changed)…"
    & $venvBackend -m pip install --quiet --upgrade -r 'backend/requirements.txt'
    if ($LASTEXITCODE -ne 0) { throw "pip install failed for backend/requirements.txt (exit $LASTEXITCODE)" }
    Write-Ok 'venv-backend updated'
}
if (-not $NoDeps -and $guiReqChanged -and (Test-Path $venvGui)) {
    Write-Step "Installing GUI deps (gui/requirements.txt changed)…"
    & $venvGui -m pip install --quiet --upgrade -r 'gui/requirements.txt'
    if ($LASTEXITCODE -ne 0) { throw "pip install failed for gui/requirements.txt (exit $LASTEXITCODE)" }
    Write-Ok 'venv-gui updated'
}

if ($NoRestart) {
    Write-Skip 'Restart skipped (-NoRestart). Bounce by hand: .\LocalAIStack.ps1 -Stop; .\LocalAIStack.ps1 -Start'
    Set-Content -Path $MarkerPath -Value $currentSha -Encoding utf8
    return
}

# ── Only restart if the backend is actually running ─────────────────────────
$wasRunning = $false
$backendPid = $null
if (Test-Path $PidFile) {
    try {
        $pids = Get-Content $PidFile -Raw -Encoding utf8 | ConvertFrom-Json
        if ($pids.backend -and $pids.backend.pid) {
            $backendPid = [int]$pids.backend.pid
            $p = Get-Process -Id $backendPid -ErrorAction SilentlyContinue
            if ($p) { $wasRunning = $true }
        }
    } catch {
        Write-Warn2 ("Could not read $PidFile : {0}" -f $_.Exception.Message)
    }
}

if (-not $wasRunning) {
    Write-Skip ("Backend isn't running (pid {0} stale) — skipping restart. Start manually: .\LocalAIStack.ps1" -f $backendPid)
    Set-Content -Path $MarkerPath -Value $currentSha -Encoding utf8
    return
}

# ── Restart via the launcher (re-runs model resolution + diagnostics) ──────
Write-Step 'Stopping current stack…'
& (Join-Path $RepoRoot 'LocalAIStack.ps1') -Stop

Write-Step 'Starting fresh stack with updated code…'
# -NoGui keeps automated refreshes server-only. The user's chat clients
# (web + desktop) reconnect to the new backend on the same port.
& (Join-Path $RepoRoot 'LocalAIStack.ps1') -Start -NoGui

# ── Verify ─────────────────────────────────────────────────────────────────
Write-Step 'Waiting for /healthz…'
$healthy = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $r = Invoke-RestMethod -Uri 'http://127.0.0.1:18000/healthz' -TimeoutSec 2 -ErrorAction Stop
        if ($r.ok) { $healthy = $true; break }
    } catch { Start-Sleep -Milliseconds 500 }
}

if (-not $healthy) {
    Write-Warn2 'Backend did not become healthy in 30s — check data\backend.err.log'
    exit 2
}
Write-Ok 'Backend healthy on :18000'

# Sanity-check the tool registry. The whole reason tools.yaml + 152 modules
# matter is they end up here. If this is 0, something silenced the registry
# (the most common cause was the LAI_TOOLS_DIR env-propagation bug — fixed,
# but worth catching regressions).
try {
    $tools = Invoke-RestMethod -Uri 'http://127.0.0.1:18000/tools' -TimeoutSec 5
    $count = @($tools.data).Count
    if ($count -eq 0) {
        Write-Warn2 'Tool registry is EMPTY after restart. Inspect backend log for "Tools directory not found".'
    } else {
        Write-Ok ("Tool registry: {0} tools, {1} group nodes" -f $count, @($tools.groups).Count)
    }
} catch {
    Write-Warn2 ("Could not read /tools: {0}" -f $_.Exception.Message)
}

Set-Content -Path $MarkerPath -Value $currentSha -Encoding utf8
Write-Ok ("Refresh complete ({0})" -f $currentSha.Substring(0,7))
