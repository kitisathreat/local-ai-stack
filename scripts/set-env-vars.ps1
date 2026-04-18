<#
.SYNOPSIS
    Load API keys and secrets from a .env file into Windows User environment
    variables (persistent) and the current PowerShell session.

.DESCRIPTION
    Reads a KEY=VALUE file (defaults to ../.env.local relative to this script)
    and stores each entry as a User-scope environment variable via
    [Environment]::SetEnvironmentVariable so the value survives reboots and is
    visible to GUI apps like Docker Desktop, LM Studio, and Open WebUI.

    The tool files under /tools read these variables at startup (see
    `.env.example` at the repo root for the full list), so after running this
    script the Valves in Open WebUI are pre-populated without pasting keys into
    the UI.

    Safety:
      * Blank values are skipped (they would otherwise clear an existing var).
      * Lines starting with `#` and blank lines are ignored.
      * Values may be optionally wrapped in single or double quotes.
      * The script never prints the values it loads; it prints the variable
        names only.

.PARAMETER EnvFile
    Path to the .env file to load. Defaults to `.env.local` next to this repo.

.PARAMETER Scope
    "User" (default) persists across sessions. "Process" only sets the vars
    for the current PowerShell session.

.PARAMETER Clear
    If specified, any variable listed in the env file with an EMPTY value will
    be removed from the target scope. Off by default (safer).

.EXAMPLE
    PS> .\set-env-vars.ps1
    Loads ..\.env.local into User-scope env vars.

.EXAMPLE
    PS> .\set-env-vars.ps1 -EnvFile C:\secrets\ai.env -Scope Process
    Loads only for the current shell (useful for one-off docker compose runs).
#>

[CmdletBinding()]
param(
    [string]$EnvFile,
    [ValidateSet('User', 'Process')]
    [string]$Scope = 'User',
    [switch]$Clear
)

$ErrorActionPreference = 'Stop'

if (-not $EnvFile) {
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $EnvFile  = Join-Path $repoRoot '.env.local'
}

if (-not (Test-Path -LiteralPath $EnvFile)) {
    Write-Error "Env file not found: $EnvFile`nCopy .env.example to .env.local, fill in values, then re-run."
    exit 1
}

Write-Host "Loading env vars from: $EnvFile  (scope: $Scope)" -ForegroundColor Cyan

$set     = @()
$cleared = @()
$skipped = @()

Get-Content -LiteralPath $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq '' -or $line.StartsWith('#')) { return }

    $eq = $line.IndexOf('=')
    if ($eq -lt 1) {
        Write-Warning "Skipping malformed line: $line"
        return
    }

    $name  = $line.Substring(0, $eq).Trim()
    $value = $line.Substring($eq + 1).Trim()

    # Strip one matching pair of surrounding quotes, if any.
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }

    if ($value -eq '') {
        if ($Clear) {
            [Environment]::SetEnvironmentVariable($name, $null, $Scope)
            Set-Item -Path "Env:$name" -Value $null -ErrorAction SilentlyContinue
            $cleared += $name
        } else {
            $skipped += $name
        }
        return
    }

    [Environment]::SetEnvironmentVariable($name, $value, $Scope)
    # Also mirror into the current process so subsequent commands in this
    # shell see the value immediately, regardless of -Scope.
    Set-Item -Path "Env:$name" -Value $value
    $set += $name
}

if ($set.Count -gt 0) {
    Write-Host "Set $($set.Count) variable(s):" -ForegroundColor Green
    $set | Sort-Object | ForEach-Object { Write-Host "  $_" }
}
if ($cleared.Count -gt 0) {
    Write-Host "Cleared $($cleared.Count) variable(s):" -ForegroundColor Yellow
    $cleared | Sort-Object | ForEach-Object { Write-Host "  $_" }
}
if ($skipped.Count -gt 0) {
    Write-Host "Skipped $($skipped.Count) blank entry/entries (pass -Clear to remove):" -ForegroundColor DarkGray
    $skipped | Sort-Object | ForEach-Object { Write-Host "  $_" }
}

if ($Scope -eq 'User') {
    Write-Host "`nNote: User-scope changes require new processes to pick them up." -ForegroundColor DarkGray
    Write-Host "Restart Docker Desktop / Open WebUI / pipelines to apply." -ForegroundColor DarkGray
}
