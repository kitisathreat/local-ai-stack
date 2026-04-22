<#
.SYNOPSIS
    Start the local AI stack on Windows (Docker Engine in WSL2).
.DESCRIPTION
    Thin wrapper around scripts/start.sh. Ensures the WSL2 distro is up and
    Docker Engine is reachable inside it, then runs the canonical start.sh.

    Docker Desktop is NOT used -- this stack runs on Docker Engine inside a
    WSL2 Ubuntu distro to avoid Docker Desktop's AF_UNIX-reparse-point crash
    bugs on Windows. Run setup.ps1 first to install Docker in WSL.
.PARAMETER Distro
    WSL distro name. Default: Ubuntu.
.EXAMPLE
    .\scripts\start.ps1
#>
param([string]$Distro = "Ubuntu")

$ErrorActionPreference = "Continue"
$root = Split-Path $PSScriptRoot -Parent

function Write-Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "   FAIL: $msg" -ForegroundColor Red }

# -- WSL present? ------------------------------------------------------------
if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
    Write-Fail "WSL is not installed. Run setup.ps1 first."
    exit 1
}

# -- Distro present? ---------------------------------------------------------
Write-Step "Checking WSL distro '$Distro'..."
$distros = (& wsl.exe -l -q 2>&1) -join "`n" -split "[\r\n]+" | ForEach-Object { $_.Trim() }
if (-not ($distros -contains $Distro)) {
    Write-Fail "Distro '$Distro' not found. Run setup.ps1 to install."
    exit 1
}
Write-OK "$Distro available"

# -- Docker reachable inside WSL? -------------------------------------------
Write-Step "Checking Docker inside $Distro..."
& wsl.exe -d $Distro -- docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Step "Docker not running -- starting docker.service..."
    & wsl.exe -d $Distro -u root -- bash -c "systemctl start docker 2>/dev/null || service docker start" 2>&1 | Out-Null
    for ($i = 0; $i -lt 15; $i++) {
        & wsl.exe -d $Distro -- docker info 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep 2
    }
}
& wsl.exe -d $Distro -- docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Docker did not come up inside $Distro. Check: wsl -d $Distro -- sudo journalctl -u docker -n 50"
    exit 1
}
Write-OK "Docker Engine reachable"

# -- Translate repo path to /mnt/c/... and run start.sh ---------------------
$wslRoot = (& wsl.exe -d $Distro -- wslpath -u "$root" 2>&1 | Select-Object -First 1).Trim()
Write-Step "Running start.sh inside $Distro (repo: $wslRoot)..."
& wsl.exe -d $Distro -- bash -c "cd '$wslRoot' && bash scripts/start.sh"
$rc = $LASTEXITCODE

if ($rc -eq 0) {
    Write-Host ""
    Write-Host "=========================================" -ForegroundColor Green
    Write-Host "  Stack is ready!" -ForegroundColor Green
    Write-Host "  Frontend:  http://localhost:3000" -ForegroundColor Green
    Write-Host "  Backend:   http://localhost:18000/healthz" -ForegroundColor Green
    Write-Host "  VRAM:      http://localhost:18000/api/vram" -ForegroundColor Green
    Write-Host "=========================================" -ForegroundColor Green
} else {
    Write-Fail "start.sh exited with code $rc"
    exit $rc
}
