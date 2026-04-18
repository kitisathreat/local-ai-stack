<#
.SYNOPSIS
    Register model presets in Open WebUI and write LM Studio preset files.
.DESCRIPTION
    Reads config/models.yaml and:
      1. Creates/updates named model presets in Open WebUI (visible in chat dropdown).
      2. Writes LM Studio preset JSON files to ~/.lmstudio/user/presets/.
    Safe to run multiple times — updates existing presets rather than duplicating.
.PARAMETER WebuiUrl
    Base URL of the Open WebUI instance. Default: http://localhost:3000
.PARAMETER SkipWebui
    Skip Open WebUI registration (e.g. if container is not running).
.PARAMETER SkipLmStudio
    Skip writing LM Studio preset files.
.EXAMPLE
    .\scripts\setup-webui-models.ps1
    .\scripts\setup-webui-models.ps1 -SkipLmStudio
#>
param(
    [string]$WebuiUrl   = "http://localhost:3000",
    [switch]$SkipWebui,
    [switch]$SkipLmStudio
)

$root        = Split-Path $PSScriptRoot -Parent
$modelsConfig = "$root\config\models.yaml"

function Write-Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "   WARN: $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "   FAIL: $msg" -ForegroundColor Red }

# ── Parse all model profiles ──────────────────────────────────────────────────
function Get-AllModels {
    $yaml    = Get-Content $modelsConfig
    $models  = [ordered]@{}
    $current = $null
    $cfg     = @{}

    foreach ($line in $yaml) {
        if ($line -match "^  (\w+):\s*$") {
            if ($current -and $cfg.Count -gt 0) { $models[$current] = $cfg }
            $current = $Matches[1]
            $cfg     = @{}
        }
        elseif ($current -and $line -match "^\s+(\w+):\s+[`"']?(.+?)[`"']?\s*$") {
            $cfg[$Matches[1]] = $Matches[2]
        }
    }
    if ($current -and $cfg.Count -gt 0) { $models[$current] = $cfg }
    return $models
}

$allModels = Get-AllModels
Write-Host "Loaded $($allModels.Count) model profiles from config/models.yaml"

# ── 1. Open WebUI model presets ───────────────────────────────────────────────
if (-not $SkipWebui) {
    Write-Step "Registering presets in Open WebUI ($WebuiUrl)..."

    # Verify WebUI is reachable
    try {
        Invoke-WebRequest "$WebuiUrl" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop | Out-Null
    }
    catch {
        Write-Warn "Open WebUI not reachable at $WebuiUrl — skipping. Start the stack first."
        $SkipWebui = $true
    }
}

if (-not $SkipWebui) {
    # Fetch existing custom models so we know whether to create or update
    $existingIds = @()
    try {
        $existing    = Invoke-RestMethod "$WebuiUrl/api/v1/models" -Method Get -ErrorAction Stop
        $existingIds = $existing | ForEach-Object { $_.id }
    }
    catch { <# first run — list may be empty or endpoint may differ #> }

    foreach ($profileName in $allModels.Keys) {
        $m = $allModels[$profileName]

        $payload = @{
            id            = $profileName
            name          = $m['name']
            base_model_id = $m['id']
            params        = @{
                temperature      = [double]$m['temperature']
                top_p            = [double]$m['top_p']
                top_k            = [int]   $m['top_k']
                frequency_penalty = [double]($m['repeat_penalty']) - 1.0  # OpenAI scale: 0=off
                max_tokens       = [int]   $m['max_tokens']
            }
            meta = @{
                description = $m['description']
                tags        = @()
            }
        } | ConvertTo-Json -Depth 5

        $method   = if ($existingIds -contains $profileName) { "update" } else { "create" }
        $endpoint = "$WebuiUrl/api/v1/models/$method"

        try {
            Invoke-RestMethod $endpoint -Method Post -ContentType "application/json" `
                -Body $payload -ErrorAction Stop | Out-Null
            Write-OK "$method → $($m['name'])"
        }
        catch {
            # Fallback: try the other verb
            $fallback = if ($method -eq "create") { "update" } else { "create" }
            try {
                Invoke-RestMethod "$WebuiUrl/api/v1/models/$fallback" -Method Post `
                    -ContentType "application/json" -Body $payload -ErrorAction Stop | Out-Null
                Write-OK "$fallback → $($m['name'])"
            }
            catch {
                Write-Fail "$($m['name']): $_"
            }
        }
    }
}

# ── 2. LM Studio preset files ─────────────────────────────────────────────────
if (-not $SkipLmStudio) {
    Write-Step "Writing LM Studio preset files..."

    $lmsPresetDir = "$env:USERPROFILE\.lmstudio\user\presets"
    if (-not (Test-Path $lmsPresetDir)) {
        # Try alternate path used by some LM Studio versions
        $lmsPresetDir = "$env:APPDATA\LM-Studio\presets"
    }

    if (Test-Path $lmsPresetDir) {
        foreach ($profileName in $allModels.Keys) {
            $m = $allModels[$profileName]

            $preset = @{
                name = $m['name']
                llmPredictionConfigOverride = @{
                    temperature       = [double]$m['temperature']
                    topP              = [double]$m['top_p']
                    topK              = [int]   $m['top_k']
                    repeatPenalty     = [double]$m['repeat_penalty']
                    maxPredictedTokens = [int]  $m['max_tokens']
                    stopStrings       = @()
                }
            } | ConvertTo-Json -Depth 5

            $outPath = "$lmsPresetDir\$profileName.preset.json"
            Set-Content -Path $outPath -Value $preset -Encoding UTF8
            Write-OK "Wrote $outPath"
        }
    }
    else {
        Write-Warn "LM Studio preset directory not found. Checked:"
        Write-Host  "   $env:USERPROFILE\.lmstudio\user\presets" -ForegroundColor DarkGray
        Write-Host  "   $env:APPDATA\LM-Studio\presets" -ForegroundColor DarkGray
        Write-Host  "   Restart LM Studio once to create this directory, then re-run." -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
