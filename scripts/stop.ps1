<#
.SYNOPSIS
    Stop the local AI stack (WSL/Docker-Engine edition).
.PARAMETER Distro
    WSL distro name. Default: Ubuntu.
#>
param([string]$Distro = "Ubuntu")

$ErrorActionPreference = "Continue"
$root = Split-Path $PSScriptRoot -Parent

function Write-Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "   OK: $msg" -ForegroundColor Green }

if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
    Write-Host "WSL not installed -- nothing to stop." -ForegroundColor Yellow
    exit 0
}

$wslRoot = (& wsl.exe -d $Distro -- wslpath -u "$root" 2>&1 | Select-Object -First 1).Trim()
Write-Step "docker compose down (inside $Distro)"
& wsl.exe -d $Distro -- bash -c "cd '$wslRoot' && bash scripts/stop.sh" 2>&1 |
    ForEach-Object { Write-Host "   $_" -ForegroundColor DarkGray }

Write-OK "Stack stopped"
