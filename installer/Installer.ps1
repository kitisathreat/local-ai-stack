# LocalAIStackInstaller.exe — first-time setup + reconfiguration EXE.
#
# Compiled separately from LocalAIStack.exe so:
#   1. The Start-menu / desktop shortcuts only point at the day-to-day
#      runtime EXE (LocalAIStack.exe). The installer is reachable from
#      Apps & Features ("Modify"), the Start menu Reconfigure entry,
#      and the un-installed launcher itself if the user re-runs setup.
#   2. We can ship the installer signed differently (or with a different
#      manifest level) without re-signing the runtime.
#
# Behaviour:
#   - With no flags, runs the full setup wizard end-to-end:
#       prereq check → vendor venvs/binaries → CUDA redist → setup wizard
#       (admin user, .env, Cloudflare) → background model pull
#   - With `-Reconfigure`, skips prereq + venv steps and just re-launches
#       the GUI wizard so the user can change `.env` values.
#   - With `-RepairOnly`, runs only the prereq + binary download steps
#       (skips the wizard) — useful when an EXE / DLL is missing but the
#       user's .env is still valid.
#
# All real work is delegated to LocalAIStack.ps1's Invoke-Setup / wizard.
# This file is only the dispatcher.

[CmdletBinding()]
param(
    [switch]$Reconfigure,
    [switch]$RepairOnly,
    [switch]$SkipModels,
    [switch]$SkipPrereqs,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
# Walk up if Installer.ps1 sits inside installer/ (dev layout)
$parentName = Split-Path -Leaf $RepoRoot
if ($parentName -eq 'installer') {
    $RepoRoot = Split-Path -Parent $RepoRoot
}
$LauncherPs1 = Join-Path $RepoRoot 'LocalAIStack.ps1'
if (-not (Test-Path $LauncherPs1)) {
    $msg = "Installer launched from an unexpected location: $RepoRoot"
    if (Get-Command Show-PreflightDialog -ErrorAction SilentlyContinue) {
        [System.Windows.MessageBox]::Show($msg, 'Local AI Stack Installer', 'OK', 'Error')
    }
    throw $msg
}

if ($Help) {
    Write-Host @"
LocalAIStackInstaller — first-time setup and reconfiguration

Usage:
  LocalAIStackInstaller.exe                   # full first-time install
  LocalAIStackInstaller.exe -Reconfigure      # re-run setup wizard only
  LocalAIStackInstaller.exe -RepairOnly       # re-fetch binaries only
  LocalAIStackInstaller.exe -SkipModels       # skip model GGUF pull
  LocalAIStackInstaller.exe -SkipPrereqs      # skip prereq check
"@
    return
}

if ($Reconfigure) {
    # GUI wizard only — the venvs and binaries are assumed already in
    # place from the original install.
    & $LauncherPs1 -SetupGui
    return
}
if ($RepairOnly) {
    # Re-runs prereq + vendor downloads, no wizard. Don't touch .env.
    & $LauncherPs1 -Setup -SkipModels
    return
}

# Default path: full setup. The launcher's Invoke-Setup runs the prereq
# check, downloads vendor binaries (qdrant, llama-server, CUDA redist),
# creates the three venvs, runs the GUI wizard for credentials/Cloudflare,
# then kicks off the model pull in the background.
$args = @()
if ($SkipModels)  { $args += '-SkipModels' }
if ($SkipPrereqs) { $args += '-SkipPrereqs' }
& $LauncherPs1 -Setup @args
