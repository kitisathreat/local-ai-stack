#Requires -Version 7
<#
.SYNOPSIS
  Register (or remove) a Windows Scheduled Task that runs the
  resume-stalled-pulls watchdog on user logon, hidden + survives reboot.

.DESCRIPTION
  Wraps scripts/resume-stalled-pulls.ps1 so it auto-runs after every
  reboot / logon and quietly resumes any HF tier pull whose download
  stalled because of a transient XetHub CDN outage. Uses the same
  Task Scheduler pattern as install-auto-pull.ps1 + install-bench-task.ps1
  (S4U preferred, Interactive fallback, -WindowStyle Hidden, no
  visible console).

  The watchdog itself loops up to its -MaxHours cap (default 12 h)
  and exits cleanly when every tier has a resolved symlink. With
  -MultipleInstances IgnoreNew on the task, a second logon while the
  watchdog is still running won't double-spawn.

  Registering scheduled tasks for the current user does NOT require
  administrator privileges in normal cases.

.PARAMETER Tiers
  Tier names the watchdog should monitor. Default:
  reasoning_max, reasoning_xl, frontier.

.PARAMETER PollSeconds
  Forwarded to the watchdog's -PollSeconds. Default 60.

.PARAMETER MaxHours
  Forwarded to the watchdog's -MaxHours. Default 12 — exits cleanly
  after 12 h even if pulls didn't finish, so the task doesn't hang
  indefinitely on a fundamentally-stuck pull.

.PARAMETER TaskName
  Scheduled task name. Default: LocalAIStack-ResumeStalledPulls.

.PARAMETER Uninstall
  Remove the scheduled task and exit.

.EXAMPLE
  pwsh .\scripts\install-watchdog-task.ps1                          # default
  pwsh .\scripts\install-watchdog-task.ps1 -Tiers reasoning_max
  pwsh .\scripts\install-watchdog-task.ps1 -PollSeconds 30 -MaxHours 24
  pwsh .\scripts\install-watchdog-task.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [string[]]$Tiers = @('reasoning_max', 'reasoning_xl', 'frontier'),
    [int]$PollSeconds = 60,
    [int]$MaxHours = 12,
    # Forwarded to the watchdog. 600 s = 10 min, matches the
    # "monitor every 10 min" requirement from PR #195.
    [int]$ProgressStallSeconds = 600,
    # Forwarded to the watchdog. When the watchdog detects DNS is broken
    # locally but cas-bridge.xethub.hf.co resolves via 8.8.8.8/1.1.1.1
    # (i.e. the local resolver is stuck — usually ProtonVPN's loopback
    # proxy at 127.0.2.2/.3 going stale), it tries
    # `Restart-Service ProtonVPN*`. Off by default since service restart
    # disrupts the user's VPN session — opt in via this flag.
    [switch]$RestartProtonVPN,
    [string]$TaskName = 'LocalAIStack-ResumeStalledPulls',
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

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$watchScript = Join-Path $RepoRoot 'scripts\resume-stalled-pulls.ps1'
if (-not (Test-Path $watchScript)) {
    throw "resume-stalled-pulls.ps1 not found at $watchScript"
}

$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
if (-not $pwsh) {
    Write-Host 'pwsh 7+ not on PATH.' -ForegroundColor Red
    Write-Host 'Install:  winget install --id Microsoft.PowerShell --source winget' -ForegroundColor Yellow
    exit 1
}

# Build the argument string. -WindowStyle Hidden suppresses the conhost
# flash; the script's own background subprocesses already use -WindowStyle
# Hidden too, so the chain is fully invisible.
$argv = @(
    "-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File `"$watchScript`""
    "-PollSeconds $PollSeconds"
    "-MaxHours $MaxHours"
    "-ProgressStallSeconds $ProgressStallSeconds"
    "-Tiers $($Tiers -join ',')"
)
if ($RestartProtonVPN) { $argv += '-RestartProtonVPN' }
$argString = $argv -join ' '

$action = New-ScheduledTaskAction `
    -Execute $pwsh.Source `
    -Argument $argString `
    -WorkingDirectory $RepoRoot

# Trigger: at logon. Downloads only matter when the user is signed in
# anyway, and AtStartup requires admin elevation. MultipleInstances=
# IgnoreNew prevents a second copy from launching if the first is still
# running. Repetition every 12 h covers long-lived sessions where a
# previous watchdog hit -MaxHours and exited.
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn `
    -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$logonTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 12)).Repetition
$triggers = @($logonTrigger)

# ExecutionTimeLimit just barely longer than -MaxHours so the task
# doesn't get killed mid-loop. Add a 30-min slack.
$execLimit = New-TimeSpan -Hours ($MaxHours + 1) -Minutes 30

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit $execLimit `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$registered = $false
foreach ($logonType in @('S4U', 'Interactive')) {
    $principal = New-ScheduledTaskPrincipal `
        -UserId $currentUser -LogonType $logonType -RunLevel Limited
    # Register-ScheduledTask sometimes writes a non-terminating "Access
    # denied" error to the error stream WITHOUT throwing (e.g. when
    # registering S4U without elevation), so we have to (a) force
    # ErrorAction Stop and (b) verify Get-ScheduledTask actually returns
    # a record before counting it as success.
    try {
        Register-ScheduledTask -TaskName $TaskName -Force `
            -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
            -ErrorAction Stop | Out-Null
        $verify = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $verify) { throw "Get-ScheduledTask returned nothing after Register" }
        Write-Host "Registered '$TaskName' (LogonType=$logonType, watching $($Tiers.Count) tier(s) every ${PollSeconds}s, cap ${MaxHours}h)." -ForegroundColor Green
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
Write-Host "  Watchdog : $env:APPDATA\LocalAIStack\resume-stalled-pulls.log"
Write-Host "  Resolver : $RepoRoot\data\pull-<tier>-watchdog.{out,err}.log"

Write-Host ''
Write-Host 'To stop:  pwsh .\scripts\install-watchdog-task.ps1 -Uninstall' -ForegroundColor DarkGray
Write-Host 'To run now (instead of waiting for next logon):' -ForegroundColor DarkGray
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor DarkGray
