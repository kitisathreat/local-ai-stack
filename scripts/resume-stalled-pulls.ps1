#Requires -Version 7
<#
.SYNOPSIS
  Watchdog that monitors HF's XetHub CDN reachability and auto-restarts
  stalled tier pulls when it recovers.

.DESCRIPTION
  When the XetHub host (cas-bridge.xethub.hf.co) goes unreachable from
  the user's network — DNS timeout / temporary outage — every in-flight
  HF pull bails out after exhausting its retry budget. The .incomplete
  blobs are preserved in `data/models/.cache/huggingface/download/` so
  resuming on the next pull picks up where it left off, but somebody
  still has to *trigger* the resume.

  This script polls DNS every -PollSeconds (default 60). When the host
  resolves successfully, it walks the configured tier list, checks
  which ones still don't have a fully-resolved `data/models/<tier>.gguf`
  symlink, and kicks off a `model_resolver resolve --pull --tier <name>`
  for each, hidden + backgrounded so no console window flashes.

  Exits cleanly when every tier in -Tiers has a resolved symlink.

  Designed to run from PowerShell directly OR be wrapped in a Windows
  Scheduled Task with -WindowStyle Hidden so it sits silently in the
  background restoring downloads after transient HF outages.

.PARAMETER Tiers
  Tier names to watch. Default: reasoning_max, reasoning_xl, frontier
  (the three currently in flight).

.PARAMETER PollSeconds
  How often to test DNS. Default 60 s — short enough to catch
  recoveries promptly, long enough that the script costs essentially
  nothing while idle.

.PARAMETER MaxHours
  Hard cap on how long the watchdog runs. Default 12 h — covers the
  worst-case overnight pull. Exits non-zero if the cap is hit with
  any tier still pending.

.EXAMPLE
  pwsh .\scripts\resume-stalled-pulls.ps1
  pwsh .\scripts\resume-stalled-pulls.ps1 -Tiers reasoning_max
  pwsh .\scripts\resume-stalled-pulls.ps1 -PollSeconds 30 -MaxHours 24
#>

[CmdletBinding()]
param(
    [string[]]$Tiers = @('reasoning_max', 'reasoning_xl', 'frontier'),
    [int]$PollSeconds = 60,
    [int]$MaxHours = 12
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$Py = Join-Path $RepoRoot 'vendor\venv-backend\Scripts\python.exe'
$LogDir = Join-Path $env:APPDATA 'LocalAIStack'
$null = New-Item -ItemType Directory -Path $LogDir -Force -ErrorAction SilentlyContinue
$LogPath = Join-Path $LogDir 'resume-stalled-pulls.log'

function Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $LogPath -Value $line -Encoding utf8
    Write-Host $line
}

# Source HF_TOKEN from .env so the spawned resolver runs can authenticate.
$envPath = Join-Path $RepoRoot '.env'
if (Test-Path $envPath) {
    Get-Content $envPath | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { return }
        $i = $line.IndexOf('=')
        $name = $line.Substring(0, $i).Trim()
        $val = $line.Substring($i + 1).Trim().Trim('"').Trim("'")
        Set-Item "Env:$name" $val
    }
}
# Force the slow-but-resumable downloader path; hf_transfer doesn't auto-retry.
$env:HF_HUB_ENABLE_HF_TRANSFER = '0'
$env:HF_HUB_DISABLE_PROGRESS_BARS = '1'

# Tracks PIDs of resolver child processes we spawned, so we don't double-launch.
$activePids = @{}

function Has-CompletedTier([string]$tier) {
    $link = Join-Path $RepoRoot "data\models\$tier.gguf"
    if (-not (Test-Path $link)) { return $false }
    try {
        $resolved = (Get-Item $link).ResolveLinkTarget($true)
        return $resolved -and (Test-Path $resolved.FullName)
    } catch {
        return ((Get-Item $link).Length -gt 100MB)
    }
}

function Test-XethubDns {
    # `Resolve-DnsName` short-circuits if DNS is down. 5 s timeout
    # so the loop doesn't stall waiting for slow upstream resolvers.
    try {
        $r = Resolve-DnsName -Name 'cas-bridge.xethub.hf.co' -DnsOnly -QuickTimeout -Type A -ErrorAction Stop
        return [bool]$r
    } catch {
        return $false
    }
}

function Spawn-Resolver([string]$tier) {
    # Re-check we're not already running one for this tier.
    if ($activePids.ContainsKey($tier)) {
        try {
            $p = Get-Process -Id $activePids[$tier] -ErrorAction Stop
            if (-not $p.HasExited) {
                Log "  $tier : resolver already running (PID=$($p.Id)), skipping spawn"
                return
            }
        } catch { }
        $activePids.Remove($tier) | Out-Null
    }
    # Start-Process refuses the same path for both stdout + stderr, so
    # split them. The .err.log surfaces resolver bailouts (CDN drops,
    # 404s, etc.); .out.log is the resolver's normal progress.
    $logOut = Join-Path $RepoRoot "data\pull-$tier-watchdog.out.log"
    $logErr = Join-Path $RepoRoot "data\pull-$tier-watchdog.err.log"
    $argv = @(
        '-m', 'backend.model_resolver',
        'resolve', '--force', '--pull', '--tier', $tier
    )
    # Wrap in try/catch so a transient Start-Process failure (e.g. file
    # lock contention on the log path) doesn't kill the whole watchdog —
    # the next poll cycle will retry. ErrorAction explicitly Continue
    # because the script's top-level $ErrorActionPreference is Stop.
    try {
        $proc = Start-Process -FilePath $Py -ArgumentList $argv `
            -WorkingDirectory $RepoRoot `
            -RedirectStandardOutput $logOut -RedirectStandardError $logErr `
            -PassThru -WindowStyle Hidden -ErrorAction Stop
        $activePids[$tier] = $proc.Id
        Log "  $tier : spawned resolver (PID=$($proc.Id))"
    } catch {
        Log "  $tier : spawn FAILED ($($_.Exception.Message)) — will retry next poll"
    }
}

# ── Main loop ───────────────────────────────────────────────────────────────
$start = Get-Date
$deadline = $start.AddHours($MaxHours)
Log "=== watchdog start: tiers=$($Tiers -join ',') poll=${PollSeconds}s cap=${MaxHours}h ==="

while ((Get-Date) -lt $deadline) {
    # Wrap the body so one bad iteration doesn't kill the watchdog —
    # any uncaught error gets logged and we continue polling.
    try {
        $pending = @($Tiers | Where-Object { -not (Has-CompletedTier $_) })
        if ($pending.Count -eq 0) {
            Log "all tiers complete — exiting"
            exit 0
        }

        if (Test-XethubDns) {
            Log "xethub.hf.co reachable — checking $($pending.Count) pending tier(s): $($pending -join ', ')"
            foreach ($t in $pending) { Spawn-Resolver $t }
        } else {
            # Don't log every poll while DNS is down — would flood the
            # log. Only log every 10 minutes.
            $minutesSinceStart = ((Get-Date) - $start).TotalMinutes
            if ([int]$minutesSinceStart % 10 -eq 0) {
                Log "xethub.hf.co STILL unreachable (pending: $($pending -join ', '))"
            }
        }
    } catch {
        Log "iteration error: $($_.Exception.Message) — continuing"
    }

    Start-Sleep -Seconds $PollSeconds
}

Log "MAX HOURS reached with $((@($Tiers | Where-Object { -not (Has-CompletedTier $_) })).Count) tier(s) still pending — exiting non-zero"
exit 2
