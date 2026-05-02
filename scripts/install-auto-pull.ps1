#Requires -Version 7
<#
.SYNOPSIS
  Register (or remove) a Windows Scheduled Task that polls origin for
  new commits and pulls them.

.DESCRIPTION
  Creates a scheduled task that runs scripts/poll-origin.ps1 every
  N minutes. The task runs as the current user (Logon Type: S4U so it
  fires whether or not you're logged in), survives reboots, and silently
  no-ops when origin hasn't moved.

  When new commits exist, poll-origin.ps1 fast-forward pulls them, which
  triggers .githooks/post-merge → scripts/refresh-backend.ps1 → backend
  bounce. Net effect: a PR squash+merge on GitHub propagates to the
  running stack within IntervalMinutes (default 2).

  Registering scheduled tasks for the current user does NOT require
  administrator privileges in normal cases.

.PARAMETER IntervalMinutes
  Polling interval. Default 2 minutes — fast enough that a merged PR is
  live within ~2 min, slow enough that GitHub's API isn't hammered.

.PARAMETER TaskName
  Scheduled task name. Default: LocalAIStack-AutoPull.

.PARAMETER Uninstall
  Remove the scheduled task and exit.

.EXAMPLE
  pwsh .\scripts\install-auto-pull.ps1
  pwsh .\scripts\install-auto-pull.ps1 -IntervalMinutes 5
  pwsh .\scripts\install-auto-pull.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [int]$IntervalMinutes = 2,
    [string]$TaskName = 'LocalAIStack-AutoPull',
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

$RepoRoot   = Resolve-Path (Join-Path $PSScriptRoot '..')
$pollScript = Join-Path $RepoRoot 'scripts\poll-origin.ps1'
if (-not (Test-Path $pollScript)) {
    throw "poll-origin.ps1 not found at $pollScript"
}

$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
if (-not $pwsh) {
    Write-Host 'pwsh 7+ not on PATH.' -ForegroundColor Red
    Write-Host 'Install:  winget install --id Microsoft.PowerShell --source winget' -ForegroundColor Yellow
    exit 1
}

$action = New-ScheduledTaskAction `
    -Execute $pwsh.Source `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$pollScript`" -LogQuiet" `
    -WorkingDirectory $RepoRoot

# Repeat indefinitely starting 30s from now. Using -Once + -RepetitionInterval
# is the standard idiom for "every N minutes forever" in Task Scheduler.
$trigger = New-ScheduledTaskTrigger `
    -Once -At (Get-Date).AddSeconds(30) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
# PS 7 sometimes drops -RepetitionDuration; force it to MaxValue for "forever".
$trigger.Repetition.Duration = "P9999D"

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

# S4U = service-for-user: runs whether logged in or not, no stored password,
# no admin needed to register for self.
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType S4U `
    -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $TaskName -Force `
        -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
} catch {
    Write-Host "Register-ScheduledTask failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "If this is an access denied error, run pwsh as Administrator once and retry." -ForegroundColor Yellow
    exit 1
}

Write-Host "Registered scheduled task '$TaskName' (every $IntervalMinutes min)." -ForegroundColor Green
Write-Host ''
Write-Host 'Status:' -ForegroundColor DarkGray
Get-ScheduledTaskInfo -TaskName $TaskName |
    Format-List LastRunTime, NextRunTime, LastTaskResult, NumberOfMissedRuns
Write-Host ''
Write-Host 'Logs:' -ForegroundColor DarkGray
Write-Host "  $env:APPDATA\LocalAIStack\poll-origin.log"
Write-Host ''
Write-Host 'To stop:  pwsh .\scripts\install-auto-pull.ps1 -Uninstall' -ForegroundColor DarkGray
