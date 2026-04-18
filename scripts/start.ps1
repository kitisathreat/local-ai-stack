<#
.SYNOPSIS
    Start the local AI stack.
.DESCRIPTION
    Starts Docker Desktop if needed, brings up all services via docker compose,
    and loads the default model (or specified profile) in LM Studio.
.PARAMETER Profile
    Model profile to load. Defaults to the 'default' in config/models.yaml.
.EXAMPLE
    .\scripts\start.ps1
    .\scripts\start.ps1 -Profile coding
#>
param(
    [string]$Profile = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$modelsConfig = "$root\config\models.yaml"
$lms = "lms"

function Write-Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "   FAIL: $msg" -ForegroundColor Red }

# ── 1. Parse models.yaml (simple line-by-line, no module needed) ──────────────
function Get-ModelConfig($profileName) {
    $yaml = Get-Content $modelsConfig
    $inBlock = $false
    $config = @{}
    foreach ($line in $yaml) {
        if ($line -match "^  ${profileName}:") { $inBlock = $true; continue }
        if ($inBlock) {
            if ($line -match "^  \w") { break }  # next sibling
            if ($line -match "^\s+(\w+):\s+[`"']?(.+?)[`"']?\s*$") {
                $config[$Matches[1]] = $Matches[2]
            }
        }
    }
    return $config
}

function Get-DefaultProfile() {
    $yaml = Get-Content $modelsConfig
    foreach ($line in $yaml) {
        if ($line -match "^default:\s+(\w+)") { return $Matches[1] }
    }
    return "quality"
}

# ── 2. Ensure Docker is running ───────────────────────────────────────────────
Write-Step "Checking Docker..."
$dockerReady = $false
for ($i = 0; $i -lt 12; $i++) {
    $out = docker ps 2>&1
    if ($LASTEXITCODE -eq 0) { $dockerReady = $true; break }
    if ($i -eq 0) {
        Write-Host "   Docker not running — starting Docker Desktop..."
        Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    }
    Write-Host "   [$($i*5)s] Waiting for Docker..." -ForegroundColor DarkGray
    Start-Sleep 5
}
if (-not $dockerReady) { Write-Fail "Docker did not start in time."; exit 1 }
Write-OK "Docker is running"

# ── 3. Start services via docker compose ─────────────────────────────────────
Write-Step "Starting services (Open WebUI + Jupyter)..."
Set-Location $root
docker compose up -d 2>&1 | ForEach-Object { Write-Host "   $_" -ForegroundColor DarkGray }
if ($LASTEXITCODE -ne 0) { Write-Fail "docker compose up failed"; exit 1 }

# Wait for Open WebUI
Write-Host "   Waiting for Open WebUI to be ready..."
for ($i = 0; $i -lt 24; $i++) {
    try {
        $r = Invoke-WebRequest http://localhost:3000 -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        if ($r.StatusCode -eq 200) { break }
    } catch {}
    Start-Sleep 5
}
Write-OK "Open WebUI is up at http://localhost:3000"

# ── 4. Load model in LM Studio ───────────────────────────────────────────────
Write-Step "Loading model in LM Studio..."
if ($Profile -eq "") { $Profile = Get-DefaultProfile }
$model = Get-ModelConfig $Profile

if ($model.Count -eq 0) {
    Write-Fail "Profile '$Profile' not found in config/models.yaml"
    exit 1
}

Write-Host "   Profile : $Profile"
Write-Host "   Model   : $($model['id'])"
Write-Host "   GPU     : $($model['gpu'])"
Write-Host "   Context : $($model['context'])"

# Check LM Studio server is running
$serverUp = lms server status 2>&1
if ($serverUp -notlike "*running*") {
    Write-Host "   LM Studio server not running — starting..."
    lms server start --port 1234 --cors 2>&1 | Out-Null
    Start-Sleep 3
}

# Unload any current model
$running = lms ps 2>&1
if ($running -match "IDLE|LOADING") {
    Write-Host "   Unloading current model..."
    lms unload --all 2>&1 | Out-Null
    Start-Sleep 2
}

# Load the new model
lms load $model['id'] --gpu $model['gpu'] --context-length $model['context'] --parallel $model['parallel'] -y 2>&1
if ($LASTEXITCODE -ne 0) { Write-Fail "Failed to load model"; exit 1 }
Write-OK "Model loaded: $($model['id'])"

# ── 5. Done ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=========================================" -ForegroundColor Green
Write-Host "  Stack is ready!" -ForegroundColor Green
Write-Host "  Local:   http://localhost:3000" -ForegroundColor Green
Write-Host "  Remote:  http://100.65.252.37:3000" -ForegroundColor Green
Write-Host "  Model:   $($model['name'])" -ForegroundColor Green
Write-Host "=========================================" -ForegroundColor Green
