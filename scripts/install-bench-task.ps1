#Requires -Version 7
<#
.SYNOPSIS
  Register (or remove) a Windows Scheduled Task that runs
  bench-when-tiers-ready.ps1 every N hours, benching new tiers as
  their GGUFs finish downloading and posting results to PR #172.

.DESCRIPTION
  Companion to scripts/bench-when-tiers-ready.ps1. Same shape as
  scripts/install-auto-pull.ps1 — uses Task Scheduler so the bench
  cycle survives reboots and runs even when the operator isn't
  actively logged in (S4U logon if elevation is available, falls
  back to Interactive otherwise).

  The worker is idempotent + self-uninstalling: it persists which
  tiers it has already benched into data/eval/bench-task.state.json
  and removes its own scheduled task once every chat tier is
  accounted for.

  Default interval is 6 hours — enough to cover overnight HF pulls
  without spinning up llama-server too aggressively. Adjust with
  -IntervalHours for a faster/slower cycle.

.PARAMETER IntervalHours
  How often the bench check runs. Default: 6.

.PARAMETER PrNumber
  PR to post results into. Default: 172.

.PARAMETER DocsBranch
  Branch the README updates land on. Default: docs/post-pr169-170-vram-cascade.

.PARAMETER NoPostPR
  Run the bench locally only — don't push the README diff or post
  the PR comment. State is still updated, so the next run still
  skips already-benched tiers.

.PARAMETER TaskName
  Scheduled task name. Default: LocalAIStack-BenchWhenReady.

.PARAMETER Uninstall
  Remove the scheduled task and exit.

.EXAMPLE
  pwsh .\scripts\install-bench-task.ps1                  # default: every 6h, posts to PR #172
  pwsh .\scripts\install-bench-task.ps1 -IntervalHours 3
  pwsh .\scripts\install-bench-task.ps1 -NoPostPR        # local-only, no git/gh
  pwsh .\scripts\install-bench-task.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [int]$IntervalHours = 6,
    [int]$PrNumber = 172,
    [string]$DocsBranch = 'docs/post-pr169-170-vram-cascade',
    [switch]$NoPostPR,
    [string]$TaskName = 'LocalAIStack-BenchWhenReady',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
    } else {
        Write-Host "No scheduled task named '$TaskName' — nothing to remove." -ForegroundColor DarkGray
    }
    return
}

$RepoRoot    = Resolve-Path (Join-Path $PSScriptRoot '..')
$workerScript = Join-Path $RepoRoot 'scripts\bench-when-tiers-ready.ps1'
if (-not (Test-Path $workerScript)) {
    throw "bench-when-tiers-ready.ps1 not found at $workerScript"
}

$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
if (-not $pwsh) {
    Write-Host 'pwsh 7+ not on PATH.' -ForegroundColor Red
    Write-Host 'Install:  winget install --id Microsoft.PowerShell --source winget' -ForegroundColor Yellow
    exit 1
}

$workerArgs = @(
    "-NoProfile -ExecutionPolicy Bypass -File `"$workerScript`""
    "-PrNumber $PrNumber"
    "-DocsBranch `"$DocsBranch`" -TaskName `"$TaskName`""
)
if (-not $NoPostPR) { $workerArgs += '-PostPR' }
$argString = $workerArgs -join ' '

$action = New-ScheduledTaskAction `
    -Execute $pwsh.Source `
    -Argument $argString `
    -WorkingDirectory $RepoRoot

# First run 5 minutes from now (gives the operator time to ctrl-c if
# they didn't actually mean to register), then every $IntervalHours.
$trigger = New-ScheduledTaskTrigger `
    -Once -At (Get-Date).AddMinutes(5) `
    -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)
$trigger.Repetition.Duration = "P9999D"

# Bench can take 60–90s when all 6 tiers are present (cold-spawn each).
# Cap at 30 min to be safe — covers very-large-tier loads (highest_quality,
# coding_80b cold-spawn ~30–60s each on a hot OS page cache).
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$registered = $false
foreach ($logonType in @('S4U', 'Interactive')) {
    $principal = New-ScheduledTaskPrincipal `
        -UserId $currentUser -LogonType $logonType -RunLevel Limited
    # Register-ScheduledTask writes a non-terminating "Access denied"
    # error to the error stream WITHOUT throwing in some cases (e.g.
    # S4U registration without elevation), so we have to (a) force
    # ErrorAction Stop and (b) verify Get-ScheduledTask returns a
    # record before counting it as success. Same fix shape as the one
    # applied to install-watchdog-task.ps1.
    try {
        Register-ScheduledTask -TaskName $TaskName -Force `
            -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
            -ErrorAction Stop | Out-Null
        $verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $verify) { throw "Get-ScheduledTask returned nothing after Register" }
        Write-Host "Registered scheduled task '$TaskName' (every $IntervalHours hr, LogonType=$logonType)." -ForegroundColor Green
        if ($logonType -eq 'Interactive') {
            Write-Host "Note: Interactive logon means the task only runs while you're signed in." -ForegroundColor DarkYellow
            Write-Host "      For 24/7 polling without sign-in, re-run this script as Administrator." -ForegroundColor DarkYellow
        }
        $registered = $true
        break
    } catch [System.UnauthorizedAccessException] {
        continue
    } catch {
        if ($_.Exception.Message -match 'denied|0x80070005|Access') { continue }
        Write-Host "Register-ScheduledTask failed: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}
if (-not $registered) {
    Write-Host "Could not register scheduled task with any LogonType. Try running pwsh as Administrator." -ForegroundColor Red
    exit 1
}

Write-Host ''
Write-Host 'Status:' -ForegroundColor DarkGray
Get-ScheduledTaskInfo -TaskName $TaskName |
    Format-List LastRunTime, NextRunTime, LastTaskResult, NumberOfMissedRuns
Write-Host ''
Write-Host 'Logs:' -ForegroundColor DarkGray
Write-Host "  $env:APPDATA\LocalAIStack\bench-when-tiers-ready.log"
Write-Host ''
Write-Host 'State (which tiers have been benched, last run paths):' -ForegroundColor DarkGray
Write-Host "  $RepoRoot\data\eval\bench-task.state.json"
Write-Host ''
Write-Host 'To stop:  pwsh .\scripts\install-bench-task.ps1 -Uninstall' -ForegroundColor DarkGray
