#Requires -Version 7
<#
.SYNOPSIS
  Poll origin for new commits and fast-forward pull when present.

.DESCRIPTION
  Designed to run on a Windows Task Scheduler interval (default: every
  2 minutes) so PR squash+merges on GitHub land in the running stack
  without manual intervention. The actual code refresh chains via:

    poll-origin.ps1
      → git pull --ff-only origin master
      → .githooks/post-merge fires (gated to origin pulls)
      → scripts/refresh-backend.ps1 bounces the backend

  Safety guards (refuses to pull when):
    - Working tree has uncommitted tracked changes (preserves WIP)
    - Local commits are ahead of origin (would need merge — operator's call)
    - Current branch is not the configured branch (defaults to master)
    - The fetch itself fails (transient network errors don't escalate)

  Logs to %APPDATA%\LocalAIStack\poll-origin.log. Silent when origin
  hasn't moved (keeps the log readable).

.PARAMETER Branch
  Branch to track. Default: master. Polling refuses to act on any other
  branch so feature-branch checkouts aren't auto-pulled.

.PARAMETER Remote
  Remote to track. Default: origin.

.PARAMETER Verbose
  Log even the "no change" no-op. Useful for confirming the scheduled
  task is actually firing.
#>

[CmdletBinding()]
param(
    [string]$Branch = 'master',
    [string]$Remote = 'origin',
    [switch]$LogQuiet
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $RepoRoot

$AppData = Join-Path $env:APPDATA 'LocalAIStack'
$LogFile = Join-Path $AppData 'poll-origin.log'
if (-not (Test-Path $AppData)) { New-Item -ItemType Directory -Path $AppData -Force | Out-Null }

function Log($msg) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "$ts $msg" | Add-Content -Path $LogFile -Encoding utf8
    if ($Host.UI.RawUI -and -not $LogQuiet) {
        Write-Host "$ts $msg"
    }
}

# ── Branch sanity ─────────────────────────────────────────────────────────
$current = ''
try {
    $current = (git symbolic-ref --short HEAD 2>$null).Trim()
} catch { }
if ($current -ne $Branch) {
    Log "skip: on branch '$current' (expected '$Branch')"
    return
}

# ── Working-tree sanity ───────────────────────────────────────────────────
$dirty = git status --porcelain 2>$null
if ($dirty) {
    Log "skip: working tree has uncommitted changes (preserve before pulling)"
    return
}

# ── Fetch (transient errors are OK — try again on next tick) ─────────────
try {
    & git fetch $Remote $Branch --quiet 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Log "skip: git fetch $Remote $Branch failed (exit $LASTEXITCODE) — will retry next tick"
        return
    }
} catch {
    Log "skip: git fetch threw ($($_.Exception.Message)) — will retry next tick"
    return
}

$head     = (git rev-parse HEAD).Trim()
$upstream = (git rev-parse "$Remote/$Branch").Trim()

if ($head -eq $upstream) {
    if (-not $LogQuiet) { Log "no change ($($head.Substring(0,7)))" }
    return
}

# ── Local-only commits → don't auto-pull (would create a merge commit) ───
$ahead = (git rev-list --count "$upstream..HEAD" 2>$null).Trim()
if ($ahead -ne '0') {
    Log "skip: local is $ahead commit(s) ahead of $Remote/$Branch — needs operator (push or rebase)"
    return
}

$behind = (git rev-list --count "HEAD..$upstream" 2>$null).Trim()
Log "pulling: $behind new commit(s) on $Remote/$Branch ($($head.Substring(0,7)) -> $($upstream.Substring(0,7)))"

# ── Pull (post-merge hook will fire and refresh the backend) ─────────────
try {
    $output = & git pull --ff-only $Remote $Branch 2>&1
    $output | Out-File -FilePath $LogFile -Append -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
        Log "pull FAILED (exit $LASTEXITCODE) — leaving local state untouched"
        exit 1
    }
    $newhead = (git rev-parse HEAD).Trim()
    Log "pulled: HEAD now $($newhead.Substring(0,7)) — post-merge hook handles the rest"
} catch {
    Log "pull threw: $($_.Exception.Message)"
    exit 1
}
