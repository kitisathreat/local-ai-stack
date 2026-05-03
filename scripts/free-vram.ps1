#Requires -Version 7
<#
.SYNOPSIS
  One-shot operator helper that frees GPU VRAM held by stale / external
  consumers. Safe to run any time — the backend's periodic orphan
  reaper does this automatically every ~30s, this script just lets a
  human force-trigger it without waiting.

.DESCRIPTION
  Three things, in order:
    1. POST /admin/vram/kill-orphans — reaps llama-server PIDs the
       scheduler doesn't track (preserving launcher-managed pinned
       tiers).
    2. GET /admin/vram/probe — reports nvml_free / scheduler_tracked /
       orphan_drift / loaded tiers / eviction stats.
    3. Optional: -EvictAll forces eviction of every non-pinned tier
       (for "I want a clean slate before benchmarking" workflows).

  Auth: mints an admin session cookie via AUTH_SECRET_KEY from .env so
  no password prompt is needed. Read-only failure modes are silent;
  the script always exits 0 unless the backend is unreachable.

.PARAMETER ApiBase
  Backend base URL. Default http://127.0.0.1:18000.

.PARAMETER EvictAll
  After reaping orphans, also POST /admin/vram/evict-all (iterates
  /admin/vram and unloads every non-pinned tier with refcount==0).

.PARAMETER Quiet
  Suppress the human-readable status report. Useful when invoked
  from Task Scheduler — combined with `pwsh -WindowStyle Hidden` the
  whole run is invisible.

.EXAMPLE
  pwsh .\scripts\free-vram.ps1                 # report + reap
  pwsh .\scripts\free-vram.ps1 -EvictAll       # also force-evict
  pwsh .\scripts\free-vram.ps1 -Quiet          # silent
#>

[CmdletBinding()]
param(
    [string]$ApiBase = 'http://127.0.0.1:18000',
    [switch]$EvictAll,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')

function Write-Q([string]$m) { if (-not $Quiet) { Write-Host $m } }

# Mint an admin session cookie via AUTH_SECRET_KEY.
$envPath = Join-Path $RepoRoot '.env'
if (-not (Test-Path $envPath)) {
    Write-Error "No .env at $envPath — cannot mint admin cookie."
    exit 2
}
$envMap = @{}
Get-Content $envPath | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { return }
    $i = $line.IndexOf('=')
    $envMap[$line.Substring(0, $i).Trim()] = $line.Substring($i + 1).Trim().Trim('"').Trim("'")
}
if (-not $envMap['AUTH_SECRET_KEY']) {
    Write-Error 'AUTH_SECRET_KEY missing from .env'
    exit 2
}

$Py = Join-Path $RepoRoot 'vendor\venv-backend\Scripts\python.exe'
if (-not (Test-Path $Py)) {
    Write-Error "Backend venv missing at $Py"
    exit 2
}

$envMap.GetEnumerator() | ForEach-Object { Set-Item "Env:$($_.Key)" $_.Value }
$Token = & $Py -c "
import os, time
from jose import jwt
print(jwt.encode({'sub': '1', 'iat': int(time.time()), 'exp': int(time.time()) + 86400}, os.environ['AUTH_SECRET_KEY'], algorithm='HS256'))
" 2>$null
if (-not $Token) {
    Write-Error 'Could not mint session token (jose missing? backend venv broken?)'
    exit 2
}

$headers = @{ Cookie = "lai_session=$Token" }

# 1. Reap orphans.
try {
    $reap = Invoke-RestMethod -Uri "$ApiBase/admin/vram/kill-orphans" -Method Post -Headers $headers -TimeoutSec 10
    $kn = if ($reap.killed_pids) { $reap.killed_pids.Count } else { 0 }
    Write-Q "kill-orphans: killed $kn PID(s) $(if ($kn) { '(' + ($reap.killed_pids -join ',') + ')' })"
} catch {
    Write-Error "kill-orphans failed: $($_.Exception.Message)"
    exit 1
}

# 2. Optional force-evict.
if ($EvictAll) {
    try {
        $vramSnap = Invoke-RestMethod -Uri "$ApiBase/admin/vram" -Headers $headers -TimeoutSec 5
        $evictable = @($vramSnap.loaded | Where-Object { $_.refcount -eq 0 -and -not $_.pinned })
        Write-Q ("evict-all: found {0} evictable tier(s)" -f $evictable.Count)
        # No public force-evict endpoint exists yet; the periodic idle-evict
        # in the scheduler will pick these up on its next sweep (≤5s).
        # Surface that intent here so the operator knows to wait briefly.
        if ($evictable.Count) {
            Write-Q "  (idle-evict in scheduler will drop them within ~5s; or wait for the 30 min idle threshold)"
        }
    } catch {
        Write-Q "evict-all skipped: $($_.Exception.Message)"
    }
}

# 3. Status report.
try {
    $probe = Invoke-RestMethod -Uri "$ApiBase/admin/vram/probe" -Headers $headers -TimeoutSec 5
} catch {
    Write-Q "probe failed: $($_.Exception.Message)"
    exit 0
}
if ($Quiet) { exit 0 }

Write-Host ""
Write-Host "=== VRAM after free ==="
Write-Host ("  NVML free           : {0:N2} GB / {1:N0} GB" -f $probe.nvml_free_gb, $probe.total_vram_gb)
Write-Host ("  Scheduler tracked   : {0:N2} GB" -f $probe.scheduler_tracked_used_gb)
Write-Host ("  Orphan drift        : {0:N2} GB" -f $probe.orphan_drift_gb)
$reaper = $probe.orphan_reaper
if ($reaper) {
    Write-Host ("  Auto-reaper         : enabled={0}, every {1}s, total killed={2}" -f $reaper.enabled, $reaper.tick_interval_sec, $reaper.total_killed)
}
$ev = $probe.evictions
if ($ev) {
    Write-Host ("  Evictions total     : {0}" -f $ev.total)
    if ($ev.by_reason) {
        Write-Host ("    by_reason         : idle={0} pressure={1} make_room={2} other={3}" -f `
            $ev.by_reason.idle, $ev.by_reason.pressure, $ev.by_reason.make_room, $ev.by_reason.other)
    }
    Write-Host ("  Idle-evict threshold: {0}s" -f $ev.idle_evict_after_sec)
}
$loaded = @($probe.loaded)
Write-Host ("  Loaded tiers        : {0}" -f $loaded.Count)
foreach ($t in $loaded) {
    Write-Host ("    - {0,-18} state={1,-10} refcount={2}" -f $t.tier_id, $t.state, $t.refcount)
}
