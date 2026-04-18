<#
.SYNOPSIS
    Stop the local AI stack.
.PARAMETER UnloadModel
    Also unload the model from LM Studio (default: true).
#>
param(
    [bool]$UnloadModel = $true
)

$root = Split-Path $PSScriptRoot -Parent

function Write-Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "   OK: $msg" -ForegroundColor Green }

Write-Step "Stopping services..."
Set-Location $root
docker compose down 2>&1 | ForEach-Object { Write-Host "   $_" -ForegroundColor DarkGray }
Write-OK "Docker services stopped"

if ($UnloadModel) {
    Write-Step "Unloading model from LM Studio..."
    lms unload --all 2>&1 | Out-Null
    Write-OK "Model unloaded"
}

Write-Host ""
Write-Host "Stack stopped." -ForegroundColor Yellow
