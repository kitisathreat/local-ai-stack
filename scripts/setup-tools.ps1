<#
.SYNOPSIS
    Deploy Open WebUI tools to the running Open WebUI instance.
.DESCRIPTION
    Uploads Python tool files from the tools/ directory to Open WebUI via API.
    Tools extend what models can DO — they're functions the model calls mid-conversation.
.PARAMETER WebuiUrl
    Base URL of Open WebUI. Default: http://localhost:3000
.PARAMETER ToolName
    Upload a specific tool by filename (without .py). Default: upload all.
.PARAMETER ListTools
    List tools currently installed in Open WebUI.
.EXAMPLE
    .\scripts\setup-tools.ps1
    .\scripts\setup-tools.ps1 -ToolName web_search
    .\scripts\setup-tools.ps1 -ListTools
#>
param(
    [string]$WebuiUrl  = "http://localhost:3000",
    [string]$ToolName  = "",
    [switch]$ListTools
)

$root = Split-Path $PSScriptRoot -Parent
$toolsDir = "$root\tools"

function Write-Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "   OK: $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "   WARN: $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "   FAIL: $msg" -ForegroundColor Red }

# ── Verify Open WebUI is up ───────────────────────────────────────────────────
Write-Step "Connecting to Open WebUI ($WebuiUrl)..."
try {
    Invoke-WebRequest "$WebuiUrl" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop | Out-Null
}
catch {
    Write-Fail "Open WebUI not reachable. Start the stack first: .\scripts\start.ps1"
    exit 1
}

# Get auth token
$token = $null
try {
    $authResp = Invoke-RestMethod "$WebuiUrl/api/v1/auths/signin" `
        -Method Post `
        -Body '{"email":"","password":""}' `
        -ContentType "application/json" `
        -TimeoutSec 5
    $token = $authResp.token
}
catch {}

if (-not $token) {
    Write-Warn "Could not get auth token — trying unauthenticated"
    $headers = @{}
} else {
    Write-OK "Authenticated"
    $headers = @{ Authorization = "Bearer $token" }
}

# ── List tools ────────────────────────────────────────────────────────────────
if ($ListTools) {
    Write-Step "Tools currently in Open WebUI:"
    try {
        $tools = Invoke-RestMethod "$WebuiUrl/api/v1/tools" `
            -Headers $headers -TimeoutSec 10
        if ($tools.Count -eq 0) {
            Write-Host "   No tools installed yet." -ForegroundColor Yellow
        }
        else {
            foreach ($t in $tools) {
                Write-Host "   - $($t.id): $($t.name)" -ForegroundColor White
            }
        }
    }
    catch {
        Write-Fail "Could not list tools: $_"
    }
    exit 0
}

# ── Get tool files to upload ──────────────────────────────────────────────────
if ($ToolName) {
    $toolFiles = Get-ChildItem "$toolsDir\${ToolName}.py" -ErrorAction SilentlyContinue
} else {
    $toolFiles = Get-ChildItem "$toolsDir\*.py"
}

if (-not $toolFiles) {
    Write-Fail "No tool files found in $toolsDir"
    exit 1
}

Write-Step "Uploading $($toolFiles.Count) tool(s) to Open WebUI..."

# ── Upload each tool ──────────────────────────────────────────────────────────
# Get existing tools to know create vs update
$existingTools = @()
try {
    $existing = Invoke-RestMethod "$WebuiUrl/api/v1/tools" -Headers $headers -TimeoutSec 10
    $existingTools = $existing | ForEach-Object { $_.id }
}
catch {}

foreach ($file in $toolFiles) {
    $toolId = $file.BaseName
    $content = Get-Content $file.FullName -Raw -Encoding UTF8

    # Extract title from docstring
    $titleMatch = $content | Select-String 'title:\s+(.+)'
    $title = if ($titleMatch) { $titleMatch.Matches[0].Groups[1].Value.Trim() } else { $toolId }

    $payload = @{
        id      = $toolId
        name    = $title
        content = $content
        meta    = @{ description = "Uploaded by setup-tools.ps1" }
    } | ConvertTo-Json -Depth 5

    $method = if ($existingTools -contains $toolId) { "update" } else { "create" }

    try {
        Invoke-RestMethod "$WebuiUrl/api/v1/tools/$method" `
            -Method Post -Headers $headers `
            -ContentType "application/json" `
            -Body $payload -ErrorAction Stop | Out-Null
        Write-OK "$method → $title ($toolId)"
    }
    catch {
        # Try alternate verb
        $alt = if ($method -eq "create") { "update" } else { "create" }
        try {
            Invoke-RestMethod "$WebuiUrl/api/v1/tools/$alt" `
                -Method Post -Headers $headers `
                -ContentType "application/json" `
                -Body $payload -ErrorAction Stop | Out-Null
            Write-OK "$alt → $title ($toolId)"
        }
        catch {
            Write-Fail "${toolId}: $_"
            Write-Host "   Manual: Open WebUI > Admin > Tools > Upload $($file.Name)" -ForegroundColor DarkGray
        }
    }
}

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Done. Enable tools per-model in:" -ForegroundColor Green
Write-Host "  Open WebUI > Workspace > Models > (select model) > Tools" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Or enable in a conversation with the Tools toggle (🔧 icon)." -ForegroundColor DarkGray
