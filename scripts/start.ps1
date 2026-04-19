<#
.SYNOPSIS
    Start the local AI stack on Windows.
.DESCRIPTION
    Thin PowerShell wrapper around scripts/start.sh (the canonical
    launcher). Ensures Docker Desktop is running, then invokes the bash
    script via WSL (or falls back to a native docker-compose sequence).

    Since Phase 1, the stack is Docker-native and the LM Studio / lms
    CLI is no longer a dependency; since Phase 6, Tailscale is no
    longer in the launch path (run scripts/setup-cloudflared.sh for
    public access via Cloudflare Tunnel).
.EXAMPLE
    .\scripts\start.ps1
#>
param()

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

function Write-Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "   FAIL: $msg" -ForegroundColor Red }

# ── Docker Desktop ────────────────────────────────────────────────────────
Write-Step "Checking Docker..."
$dockerReady = $false
for ($i = 0; $i -lt 12; $i++) {
    $out = docker ps 2>&1
    if ($LASTEXITCODE -eq 0) { $dockerReady = $true; break }
    if ($i -eq 0) {
        Write-Host "   Docker not running - starting Docker Desktop..."
        Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    }
    Start-Sleep 5
}
if (-not $dockerReady) { Write-Fail "Docker did not start in time."; exit 1 }
Write-OK "Docker is running"

# ── Compose up ────────────────────────────────────────────────────────────
Write-Step "docker compose up -d"
Set-Location $root
docker compose up -d 2>&1 | ForEach-Object { Write-Host "   $_" -ForegroundColor DarkGray }
if ($LASTEXITCODE -ne 0) { Write-Fail "docker compose up failed"; exit 1 }

# ── Wait for backend ──────────────────────────────────────────────────────
Write-Step "Waiting for backend /healthz..."
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest http://localhost:8000/healthz -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        if ($r.StatusCode -eq 200) { Write-OK "Backend is ready"; break }
    } catch {}
    Start-Sleep 2
}

# ── Warm-pull tier models in the background ───────────────────────────────
Write-Step "Pulling tier models (background)"
Start-Process -NoNewWindow -FilePath "bash" -ArgumentList "$root/scripts/setup-models.sh","--skip-vision" -ErrorAction SilentlyContinue
Write-OK "Launched setup-models.sh"

# ── Done ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=========================================" -ForegroundColor Green
Write-Host "  Stack is ready!" -ForegroundColor Green
Write-Host "  Frontend:  http://localhost:3000" -ForegroundColor Green
Write-Host "  Backend:   http://localhost:8000/healthz" -ForegroundColor Green
Write-Host "  VRAM:      http://localhost:8000/api/vram" -ForegroundColor Green
Write-Host "=========================================" -ForegroundColor Green
