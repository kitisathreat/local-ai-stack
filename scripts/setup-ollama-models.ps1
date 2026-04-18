<#
.SYNOPSIS
    Pull Ollama models from the registry and register them in Open WebUI.
.DESCRIPTION
    Reads config/ollama-models.yaml and pulls the specified model group.
    Ollama supplements LM Studio — it's useful for embeddings, vision models,
    and quick lightweight inference without loading into LM Studio.
.PARAMETER Group
    Model group to pull: minimal, standard, full, coding, vision.
    Default: minimal (llama3.2:3b + nomic-embed-text)
.PARAMETER Model
    Pull a specific model tag (e.g. "phi4-mini:latest")
.PARAMETER ListModels
    List all available Ollama models without pulling
.PARAMETER OllamaUrl
    Ollama API URL. Default: http://localhost:11434
.EXAMPLE
    .\scripts\setup-ollama-models.ps1
    .\scripts\setup-ollama-models.ps1 -Group standard
    .\scripts\setup-ollama-models.ps1 -Model phi4-mini:latest
    .\scripts\setup-ollama-models.ps1 -ListModels
#>
param(
    [string]$Group      = "minimal",
    [string]$Model      = "",
    [switch]$ListModels,
    [string]$OllamaUrl  = "http://localhost:11434"
)

$root = Split-Path $PSScriptRoot -Parent

function Write-Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "   WARN: $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "   FAIL: $msg" -ForegroundColor Red }

# ── Check Ollama is running ───────────────────────────────────────────────────
Write-Step "Checking Ollama..."
try {
    $status = Invoke-RestMethod "$OllamaUrl/" -TimeoutSec 5 -ErrorAction Stop
    Write-OK "Ollama is running at $OllamaUrl"
}
catch {
    Write-Fail "Ollama is not running at $OllamaUrl"
    Write-Host "   Start it with: docker compose up -d ollama" -ForegroundColor DarkGray
    exit 1
}

# ── List current models ───────────────────────────────────────────────────────
function Get-OllamaModels {
    try {
        $r = Invoke-RestMethod "$OllamaUrl/api/tags" -TimeoutSec 10
        return $r.models
    }
    catch { return @() }
}

if ($ListModels) {
    Write-Step "Models currently available in Ollama:"
    $models = Get-OllamaModels
    if ($models.Count -eq 0) {
        Write-Host "   No models pulled yet." -ForegroundColor Yellow
    }
    else {
        foreach ($m in $models) {
            $size = [math]::Round($m.size / 1GB, 1)
            Write-Host "   - $($m.name) (${size}GB)" -ForegroundColor White
        }
    }
    exit 0
}

# ── Parse groups from ollama-models.yaml (simple parser) ─────────────────────
function Get-GroupModels($groupName) {
    $yaml  = Get-Content "$root\config\ollama-models.yaml"
    $inGroups = $false
    $inGroup  = $false
    $models   = @()

    foreach ($line in $yaml) {
        if ($line -match "^groups:") { $inGroups = $true; continue }
        if ($inGroups) {
            if ($line -match "^  ${groupName}:") { $inGroup = $true; continue }
            elseif ($line -match "^  \w" -and $inGroup) { break }
            elseif ($inGroup -and $line -match "^\s+-\s+(.+)") {
                $models += $Matches[1].Trim()
            }
        }
    }
    return $models
}

# ── Determine models to pull ──────────────────────────────────────────────────
$modelsToPull = @()

if ($Model) {
    $modelsToPull = @($Model)
    Write-Step "Pulling single model: $Model"
}
else {
    $modelsToPull = Get-GroupModels $Group
    if ($modelsToPull.Count -eq 0) {
        Write-Warn "Group '$Group' not found or empty. Using minimal group."
        $modelsToPull = @("llama3.2:3b", "nomic-embed-text")
    }
    Write-Step "Pulling model group: $Group ($($modelsToPull.Count) models)"
}

# ── Pull models ───────────────────────────────────────────────────────────────
$existing = (Get-OllamaModels | ForEach-Object { $_.name })

foreach ($tag in $modelsToPull) {
    Write-Host "`n   Pulling: $tag" -ForegroundColor White

    if ($existing -contains $tag) {
        Write-OK "$tag already present — skipping"
        continue
    }

    $body = @{ name = $tag } | ConvertTo-Json
    try {
        # Stream the pull response
        $response = Invoke-RestMethod "$OllamaUrl/api/pull" `
            -Method Post `
            -ContentType "application/json" `
            -Body $body `
            -TimeoutSec 600

        if ($response -match "success" -or $LASTEXITCODE -eq 0) {
            Write-OK "Pulled: $tag"
        }
        else {
            # Use ollama CLI if available
            if (Get-Command ollama -ErrorAction SilentlyContinue) {
                ollama pull $tag
                if ($LASTEXITCODE -eq 0) { Write-OK "Pulled: $tag" }
                else { Write-Fail "Failed to pull: $tag" }
            }
            else {
                Write-Warn "Could not verify pull status for: $tag"
            }
        }
    }
    catch {
        Write-Fail "Error pulling ${tag}: $_"
    }
}

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Step "Current Ollama model library:"
$finalModels = Get-OllamaModels
if ($finalModels.Count -eq 0) {
    Write-Warn "No models found after pull. Check Ollama logs: docker logs ollama"
}
else {
    foreach ($m in $finalModels) {
        $size = [math]::Round($m.size / 1GB, 1)
        Write-OK "$($m.name) (${size}GB)"
    }
}

Write-Host ""
Write-Host "Ollama models are accessible in Open WebUI automatically." -ForegroundColor Green
Write-Host "Primary inference still routes through LM Studio at port 1234." -ForegroundColor DarkGray
