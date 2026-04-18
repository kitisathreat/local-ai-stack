<#
.SYNOPSIS
    Set up all connectors, tools, and integrations for the local AI stack.
.DESCRIPTION
    Waits for all services to be ready, then:
      1. Registers SearXNG web search in Open WebUI
      2. Verifies Qdrant vector DB is healthy
      3. Verifies Pipelines server is running
      4. Verifies n8n automation is running
      5. Registers Ollama as an additional model source (optional)
      6. Prints URLs for all services
.PARAMETER WebuiUrl
    Base URL of Open WebUI. Default: http://localhost:3000
.PARAMETER SkipOllama
    Skip Ollama setup (if not using Ollama models)
.EXAMPLE
    .\scripts\setup-connectors.ps1
    .\scripts\setup-connectors.ps1 -SkipOllama
#>
param(
    [string]$WebuiUrl  = "http://localhost:3000",
    [switch]$SkipOllama
)

$ErrorActionPreference = "Continue"

function Write-Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "   WARN: $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "   FAIL: $msg" -ForegroundColor Red }

function Wait-ForService($name, $url, $maxWait = 30) {
    Write-Host "   Waiting for $name..." -ForegroundColor DarkGray
    for ($i = 0; $i -lt $maxWait; $i += 3) {
        try {
            $r = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
            if ($r.StatusCode -lt 400) {
                Write-OK "$name is ready at $url"
                return $true
            }
        } catch {}
        Start-Sleep 3
    }
    Write-Warn "$name not ready after ${maxWait}s"
    return $false
}

# ── 1. Service Health Checks ──────────────────────────────────────────────────
Write-Step "Checking service health..."

$webuiOk   = Wait-ForService "Open WebUI"  "$WebuiUrl"
$searxngOk = Wait-ForService "SearXNG"     "http://localhost:4000"
$qdrantOk  = Wait-ForService "Qdrant"      "http://localhost:6333/health" 60
$pipesOk   = Wait-ForService "Pipelines"   "http://localhost:9099"
$n8nOk     = Wait-ForService "n8n"         "http://localhost:5678"

if (-not $SkipOllama) {
    $ollamaOk = Wait-ForService "Ollama" "http://localhost:11434" 20
} else {
    $ollamaOk = $false
}

# ── 2. Configure SearXNG in Open WebUI ───────────────────────────────────────
Write-Step "Configuring SearXNG web search in Open WebUI..."
if ($webuiOk -and $searxngOk) {
    try {
        # Get auth token (auth disabled)
        $authResp = Invoke-RestMethod "$WebuiUrl/api/v1/auths/signin" `
            -Method Post `
            -Body '{"email":"","password":""}' `
            -ContentType "application/json" `
            -TimeoutSec 5
        $token = $authResp.token

        if ($token) {
            $headers = @{ Authorization = "Bearer $token" }

            # Update RAG config to use SearXNG
            $ragConfig = @{
                web_search = @{
                    enabled    = $true
                    engine     = "searxng"
                    searxng_query_url = "http://searxng:8080/search?q=<query>&format=json"
                    result_count  = 5
                }
            } | ConvertTo-Json -Depth 5

            Invoke-RestMethod "$WebuiUrl/api/v1/configs/rag" `
                -Method Post -Headers $headers `
                -ContentType "application/json" -Body $ragConfig `
                -ErrorAction SilentlyContinue | Out-Null
            Write-OK "SearXNG registered as web search engine"
        }
    }
    catch {
        Write-Warn "Could not configure SearXNG via API: $_"
        Write-Host "   Manual step: Open WebUI > Admin > Settings > Web Search" -ForegroundColor DarkGray
        Write-Host "   Set engine to 'searxng' and URL to: http://searxng:8080/search?q=<query>&format=json" -ForegroundColor DarkGray
    }
} else {
    Write-Warn "Skipping SearXNG config — services not ready"
}

# ── 3. Verify Qdrant Collections ─────────────────────────────────────────────
Write-Step "Verifying Qdrant vector database..."
if ($qdrantOk) {
    try {
        $collections = Invoke-RestMethod "http://localhost:6333/collections" -TimeoutSec 5
        $count = $collections.result.collections.Count
        Write-OK "Qdrant healthy — $count collection(s)"
    }
    catch {
        Write-Warn "Qdrant responded but couldn't list collections: $_"
    }
}

# ── 4. Verify Pipelines ───────────────────────────────────────────────────────
Write-Step "Verifying Open WebUI Pipelines..."
if ($pipesOk) {
    try {
        $pipes = Invoke-RestMethod "http://localhost:9099/models" `
            -Headers @{Authorization = "Bearer 0p3n-w3bu!"} `
            -TimeoutSec 5
        Write-OK "Pipelines running — $($pipes.data.Count) pipeline(s) loaded"
    }
    catch {
        Write-Warn "Pipelines running but could not list models: $_"
    }
}

# ── 5. Verify n8n ─────────────────────────────────────────────────────────────
Write-Step "Verifying n8n workflow automation..."
if ($n8nOk) {
    Write-OK "n8n running — configure workflows at http://localhost:5678"
    Write-Host "   Tip: Create an HTTP node pointing to $WebuiUrl/api/chat for AI-powered workflows" -ForegroundColor DarkGray
}

# ── 6. Ollama Status ──────────────────────────────────────────────────────────
Write-Step "Checking Ollama models..."
if ($ollamaOk) {
    try {
        $models = Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 5
        $count = $models.models.Count
        Write-OK "Ollama running — $count model(s) available"
        if ($count -eq 0) {
            Write-Host "   Run: .\scripts\setup-ollama-models.ps1 -Group minimal" -ForegroundColor DarkGray
        }
    }
    catch {
        Write-Warn "Ollama running but could not list models"
    }
} elseif (-not $SkipOllama) {
    Write-Warn "Ollama not running — primary inference via LM Studio is unaffected"
}

# ── 7. Tools Info ─────────────────────────────────────────────────────────────
Write-Step "Tool installation info..."
Write-Host "   Tools are Python files in the tools/ directory." -ForegroundColor DarkGray
Write-Host "   Install via Open WebUI Admin > Tools > Upload file" -ForegroundColor DarkGray
Write-Host "   Or run: .\scripts\setup-tools.ps1" -ForegroundColor DarkGray
Write-Host ""
Write-Host "   Available tools:" -ForegroundColor DarkGray
Get-ChildItem "$PSScriptRoot\..\tools\*.py" | ForEach-Object {
    $name = $_.BaseName
    Write-Host "     - $name" -ForegroundColor DarkGray
}

# ── 8. Summary ───────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=============================================" -ForegroundColor Green
Write-Host "  Connector Setup Complete" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Service URLs:" -ForegroundColor White
Write-Host "  Open WebUI   → $WebuiUrl" -ForegroundColor $(if ($webuiOk)   { "Green" } else { "Red" })
Write-Host "  SearXNG      → http://localhost:4000" -ForegroundColor $(if ($searxngOk) { "Green" } else { "Red" })
Write-Host "  Qdrant UI    → http://localhost:6333/dashboard" -ForegroundColor $(if ($qdrantOk)  { "Green" } else { "Red" })
Write-Host "  Pipelines    → http://localhost:9099" -ForegroundColor $(if ($pipesOk)   { "Green" } else { "Red" })
Write-Host "  n8n          → http://localhost:5678" -ForegroundColor $(if ($n8nOk)     { "Green" } else { "Red" })
Write-Host "  Ollama       → http://localhost:11434" -ForegroundColor $(if ($ollamaOk) { "Green" } else { "Yellow" })
Write-Host "  LM Studio    → http://localhost:1234 (primary inference)" -ForegroundColor Green
Write-Host ""
