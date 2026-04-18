<#
.SYNOPSIS
    Switch the loaded LM Studio model without restarting services.
.PARAMETER Profile
    Model profile name from config/models.yaml (e.g. fast, quality, coding, large).
.EXAMPLE
    .\scripts\switch-model.ps1 -Profile coding
    .\scripts\switch-model.ps1 -Profile fast
#>
param(
    [Parameter(Mandatory=$true)]
    [string]$Profile
)

$root = Split-Path $PSScriptRoot -Parent
$modelsConfig = "$root\config\models.yaml"

function Write-Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "   FAIL: $msg" -ForegroundColor Red; exit 1 }

function Get-ModelConfig($profileName) {
    $yaml = Get-Content $modelsConfig
    $inBlock = $false
    $config = @{}
    foreach ($line in $yaml) {
        if ($line -match "^  ${profileName}:") { $inBlock = $true; continue }
        if ($inBlock) {
            if ($line -match "^  \w") { break }
            if ($line -match "^\s+(\w+):\s+[`"']?(.+?)[`"']?\s*$") {
                $config[$Matches[1]] = $Matches[2]
            }
        }
    }
    return $config
}

$model = Get-ModelConfig $Profile
if ($model.Count -eq 0) { Write-Fail "Profile '$Profile' not found in config/models.yaml" }

Write-Step "Switching to profile: $Profile"
Write-Host "   Model   : $($model['id'])"
Write-Host "   GPU     : $($model['gpu'])"
Write-Host "   Context : $($model['context'])"
Write-Host "   Desc    : $($model['description'])"

Write-Step "Unloading current model..."
lms unload --all 2>&1 | Out-Null
Start-Sleep 2

Write-Step "Loading $($model['id'])..."
lms load $model['id'] --gpu $model['gpu'] --context-length $model['context'] --parallel $model['parallel'] -y 2>&1
if ($LASTEXITCODE -ne 0) { Write-Fail "Failed to load model" }

Write-OK "Now running: $($model['name'])"
Write-Host "   ($($model['description']))" -ForegroundColor DarkGray
