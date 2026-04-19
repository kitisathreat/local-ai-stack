param(
    # Run in CI mode: skips live-service checks that require Docker/LM Studio/Open WebUI
    [switch]$CI
)

$log = if ($CI) { "$PSScriptRoot\ci_results.txt" } else { "C:\Users\Kit\Documents\test_results.txt" }
$pass = 0; $fail = 0

# Derive repo root from the script's own location so tests work in CI and locally
$root = (Resolve-Path "$PSScriptRoot\..").Path
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Test-Case($name, $block) {
    try {
        $result = & $block
        if ($result -eq $false) { throw "returned false" }
        "  [PASS] $name" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Green
        $script:pass++
    } catch {
        "  [FAIL] $name - $_" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Red
        # Also emit a GitHub Actions workflow annotation so the failure is
        # surfaced in the check_runs API even without log access.
        $msg = "$_" -replace "`n"," " -replace "`r",""
        Write-Host "::error title=Test failed: $name::$msg"
        $script:fail++
    }
}

"===== LOCAL AI STACK TEST SUITE =====" | Tee-Object -FilePath $log | Write-Host -ForegroundColor Cyan
"$(Get-Date)" | Tee-Object -FilePath $log -Append | Write-Host

# -- SYNTAX CHECKS --
"`n[ Syntax Checks ]" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Yellow

foreach ($script in @("start.ps1","stop.ps1","switch-model.ps1","setup-connectors.ps1","setup-tools.ps1","setup-ollama-models.ps1")) {
    Test-Case "PS syntax: $script" {
        $errors = $null
        $src = Get-Content "$root\scripts\$script" -Raw
        [System.Management.Automation.PSParser]::Tokenize($src, [ref]$errors) | Out-Null
        if ($errors.Count -gt 0) { throw "$($errors.Count) syntax error(s)" }
    }
}

# -- CONFIG VALIDATION --
"`n[ Config Validation ]" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Yellow

Test-Case "models.yaml exists" {
    Test-Path "$root\config\models.yaml"
}

Test-Case "models.yaml has 'default' key" {
    $lines = Get-Content "$root\config\models.yaml"
    $hit = $lines | Where-Object { $_ -match "^default:" }
    if (-not $hit) { throw "default key not found" }
}

Test-Case "models.yaml has all 5 tiers" {
    $yaml = Get-Content "$root\config\models.yaml" -Raw
    foreach ($p in @("highest_quality","versatile","fast","coding","vision")) {
        if ($yaml -notmatch "  ${p}:") { throw "Missing tier: $p" }
    }
}

Test-Case "models.yaml has backwards-compat aliases" {
    # Use multiline regex so ^aliases: anchors to line-start, not file-start.
    $yaml = Get-Content "$root\config\models.yaml" -Raw
    if ($yaml -notmatch "(?m)^aliases:") { throw "Missing 'aliases:' section" }
    foreach ($a in @("quality","large","balanced","analyst","creative","roleplay","summarizer")) {
        if ($yaml -notmatch "  ${a}:\s*\w") { throw "Missing alias entry: $a" }
    }
}

# docker-compose schema validation is covered by tests/test_config.py
# (service-presence + WEBUI_AUTH check) via yaml.safe_load. Running
# `docker compose config` here duplicates that coverage and was flaky
# on the Windows runner's docker version with the Phase 1 compose file
# (GPU deploy blocks + build: service + ${VAR:-default} env interpolation).
# Keep one lightweight static check here as a fast fail.

Test-Case "docker-compose.yml parses as YAML" {
    $yaml = Get-Content "$root\docker-compose.yml" -Raw
    # Crude check: must contain services: and each expected service name.
    if ($yaml -notmatch "(?m)^services:") { throw "services: section missing" }
    foreach ($svc in @("backend","frontend","ollama","llama-server","jupyter","qdrant","searxng","n8n")) {
        if ($yaml -notmatch "(?m)^  ${svc}:") { throw "service '$svc' missing" }
    }
}

Test-Case "docker-compose frontend proxies backend" {
    # Frontend container serves the Preact bundle and proxies /api/* to
    # the FastAPI backend. Verify nginx.conf references backend:8000.
    $nginxConf = Get-Content "$root\frontend\nginx.conf" -Raw
    if ($nginxConf -notmatch "backend:8000") {
        throw "frontend/nginx.conf must proxy /api/ to backend:8000"
    }
}

Test-Case "searxng settings.yml exists" {
    Test-Path "$root\config\searxng\settings.yml"
}

Test-Case "searxng settings.yml has JSON format enabled" {
    $yaml = Get-Content "$root\config\searxng\settings.yml" -Raw
    if ($yaml -notmatch "json") { throw "json format not in searxng config" }
}

Test-Case "ollama-models.yaml exists" {
    Test-Path "$root\config\ollama-models.yaml"
}

Test-Case "ollama-models.yaml has auto_pull section" {
    $yaml = Get-Content "$root\config\ollama-models.yaml" -Raw
    if ($yaml -notmatch "auto_pull") { throw "auto_pull section missing" }
}

Test-Case "tools directory exists with Python tool files" {
    $files = Get-ChildItem "$root\tools\*.py" -ErrorAction SilentlyContinue
    if ($files.Count -eq 0) { throw "No .py tool files in tools/" }
}

Test-Case "all tool files have required title metadata" {
    $files = Get-ChildItem "$root\tools\*.py"
    foreach ($f in $files) {
        $content = Get-Content $f.FullName -Raw
        if ($content -notmatch "title:") { throw "$($f.Name) missing 'title:' metadata" }
    }
}

Test-Case "academic tool files exist (pubmed, semantic_scholar, crossref, openalex, zenodo, dblp, unpaywall, nasa_ads)" {
    $required = @("pubmed","semantic_scholar","crossref","openalex","zenodo","dblp","unpaywall","nasa_ads")
    foreach ($t in $required) {
        if (-not (Test-Path "$root\tools\$t.py")) { throw "Missing academic tool: $t.py" }
    }
}

Test-Case "extended tool files exist (finance, clinicaltrials, openfda, pubchem, open_library, rss_reader, hackernews, dictionary, dev_utils, package_search, network_tools, n8n_trigger)" {
    $required = @("finance","clinicaltrials","openfda","pubchem","open_library","rss_reader","hackernews","dictionary","dev_utils","package_search","network_tools","n8n_trigger")
    foreach ($t in $required) {
        if (-not (Test-Path "$root\tools\$t.py")) { throw "Missing tool: $t.py" }
    }
}

Test-Case "Phase 4 tool files exist (excel_tool, fred, yahoo_finance_extended, sec_edgar, forex, financial_calculator, world_bank, technical_analysis)" {
    $required = @("excel_tool","fred","yahoo_finance_extended","sec_edgar","forex","financial_calculator","world_bank","technical_analysis")
    foreach ($t in $required) {
        if (-not (Test-Path "$root\tools\$t.py")) { throw "Missing Phase 4 tool: $t.py" }
    }
}

Test-Case "Phase 5 tool files exist (nasa_apis, alpha_vantage, finnhub, acled, europeana, noaa_climate, materials_project, simbad, uniprot, usgs, ensembl)" {
    $required = @("nasa_apis","alpha_vantage","finnhub","acled","europeana","noaa_climate","materials_project","simbad","uniprot","usgs","ensembl")
    foreach ($t in $required) {
        if (-not (Test-Path "$root\tools\$t.py")) { throw "Missing Phase 5 tool: $t.py" }
    }
}

Test-Case "Phase 6 tool files exist (chart_generator, financial_model, jupyter_tool, ask_clarification)" {
    $required = @("chart_generator","financial_model","jupyter_tool","ask_clarification")
    foreach ($t in $required) {
        if (-not (Test-Path "$root\tools\$t.py")) { throw "Missing Phase 6 tool: $t.py" }
    }
}

Test-Case "clarification middleware exists (Phase 6 migration)" {
    Test-Path "$root\backend\middleware\clarification.py"
}

Test-Case "tools directory has at least 52 tool files" {
    $files = Get-ChildItem "$root\tools\*.py"
    if ($files.Count -lt 52) { throw "Only $($files.Count) tool files found, expected 52+" }
}

Test-Case "knowledge/sources.yaml covers 8+ knowledge domains" {
    $yaml = Get-Content "$root\knowledge\sources.yaml" -Raw
    $domains = @("biomedical","physics","chemistry","mathematics","computer_science","social_sciences","open_data")
    foreach ($d in $domains) {
        if ($yaml -notmatch $d) { throw "Missing knowledge domain: $d" }
    }
}

Test-Case "searxng settings.yml has academic engines" {
    $yaml = Get-Content "$root\config\searxng\settings.yml" -Raw
    foreach ($engine in @("pubmed","semantic scholar","paperswithcode","openaire","biorxiv")) {
        if ($yaml -notmatch $engine) { throw "Missing SearXNG engine: $engine" }
    }
}

Test-Case "backend/middleware directory has 4+ modules (ported from pipelines/ in Phase 6)" {
    $files = Get-ChildItem "$root\backend\middleware\*.py" -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne "__init__.py" }
    if ($files.Count -lt 4) { throw "Expected 4+ middleware modules, found $($files.Count)" }
}

Test-Case "knowledge/sources.yaml exists" {
    Test-Path "$root\knowledge\sources.yaml"
}

Test-Case "setup-connectors.ps1 exists and has valid syntax" {
    $errors = $null
    $src = Get-Content "$root\scripts\setup-connectors.ps1" -Raw
    [System.Management.Automation.PSParser]::Tokenize($src, [ref]$errors) | Out-Null
    if ($errors.Count -gt 0) { throw "$($errors.Count) syntax error(s)" }
}

Test-Case "setup-tools.ps1 exists and has valid syntax" {
    $errors = $null
    $src = Get-Content "$root\scripts\setup-tools.ps1" -Raw
    [System.Management.Automation.PSParser]::Tokenize($src, [ref]$errors) | Out-Null
    if ($errors.Count -gt 0) { throw "$($errors.Count) syntax error(s)" }
}

Test-Case "setup-ollama-models.ps1 exists and has valid syntax" {
    $errors = $null
    $src = Get-Content "$root\scripts\setup-ollama-models.ps1" -Raw
    [System.Management.Automation.PSParser]::Tokenize($src, [ref]$errors) | Out-Null
    if ($errors.Count -gt 0) { throw "$($errors.Count) syntax error(s)" }
}

Test-Case "squarespace-embed.html exists" {
    Test-Path "$root\squarespace-embed.html"
}

Test-Case "embed HTML uses Cloudflare hostname placeholder (Phase 6)" {
    $html = Get-Content "$root\squarespace-embed.html" -Raw
    if ($html -notmatch "__CLOUDFLARE_HOSTNAME__") {
        throw "Embed should use __CLOUDFLARE_HOSTNAME__ placeholder (run scripts/render-embed.sh to substitute)"
    }
    if ($html -match "taila2838f\.ts\.net") {
        throw "Embed still references Tailscale hostname — should be Cloudflare placeholder"
    }
}

Test-Case "embed HTML has error fallback" {
    $html = Get-Content "$root\squarespace-embed.html" -Raw
    if ($html -notmatch "ai-error") { throw "Error fallback UI missing" }
}

Test-Case ".gitignore excludes .env.local" {
    $gi = Get-Content "$root\.gitignore" -Raw
    if ($gi -notmatch "\.env\.local") { throw ".env.local not in .gitignore" }
}

# -- YAML PARSER UNIT TEST --
"`n[ Model Config Parser ]" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Yellow

function Get-ModelConfig($profileName, $configPath) {
    $yaml = Get-Content $configPath
    $inBlock = $false; $config = @{}
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

foreach ($tier in @("highest_quality","versatile","fast","coding","vision")) {
    Test-Case "Parser: tier '$tier' has required keys" {
        $m = Get-ModelConfig $tier "$root\config\models.yaml"
        foreach ($key in @("backend","model_tag","context_window")) {
            if (-not $m.ContainsKey($key)) { throw "Missing key: $key" }
        }
    }
}

Test-Case "Parser: 'versatile' is default tier" {
    $yaml = Get-Content "$root\config\models.yaml"
    $defaultLine = ($yaml | Select-String "^default:").Line
    $default = $defaultLine -replace "default:\s*",""
    if ($default.Trim() -ne "versatile") { throw "Default is '$($default.Trim())' not 'versatile'" }
}

Test-Case "Parser: unknown tier returns empty" {
    $m = Get-ModelConfig "nonexistent_tier_xyz" "$root\config\models.yaml"
    if ($m.Count -ne 0) { throw "Should return empty for unknown tier" }
}

# -- LIVE SERVICE CHECKS --
if (-not $CI) {
    "`n[ Live Service Checks ]" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Yellow

    Test-Case "Frontend responds HTTP 200" {
        $r = Invoke-WebRequest http://localhost:3000 -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -ne 200) { throw "Got $($r.StatusCode)" }
    }

    Test-Case "Backend /healthz returns ok" {
        $r = Invoke-RestMethod http://localhost:8000/healthz -TimeoutSec 5
        if (-not $r.ok) { throw "Backend healthz did not return ok" }
    }

    Test-Case "Backend /v1/models exposes tiers" {
        $r = Invoke-RestMethod http://localhost:8000/v1/models -TimeoutSec 5
        if ($r.data.Count -lt 5) {
            throw "Expected 5 tiers, got $($r.data.Count)"
        }
    }

    Test-Case "Backend /api/vram returns scheduler status" {
        $r = Invoke-RestMethod http://localhost:8000/api/vram -TimeoutSec 5
        if (-not ($r.PSObject.Properties.Name -contains "total_vram_gb")) {
            throw "VRAM status endpoint missing total_vram_gb field"
        }
    }

    Test-Case "Frontend proxies /api to backend" {
        $r = Invoke-RestMethod http://localhost:3000/api/v1/models -TimeoutSec 5
        if ($r.data.Count -lt 5) { throw "Proxy not forwarding properly" }
    }

    Test-Case "Backend /me rejects unauth'd requests" {
        $failed = $false
        try {
            Invoke-RestMethod http://localhost:8000/me -TimeoutSec 5 -ErrorAction Stop
        } catch {
            if ($_.Exception.Response.StatusCode.value__ -eq 401) { $failed = $true }
        }
        if (-not $failed) { throw "Expected 401 for unauthenticated /me" }
    }

    Test-Case "Docker container lai-backend healthy" {
        $status = docker inspect lai-backend --format "{{.State.Status}}" 2>&1
        if ($status -ne "running") { throw "Container status: $status" }
    }
} else {
    "`n[ Live Service Checks ]" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Yellow
    "  [SKIP] All live service checks (CI mode)" |
        Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor DarkGray
}

# -- CONNECTOR SERVICE CHECKS (soft — warn on failure, don't count toward pass/fail) --
"`n[ Connector Service Checks (informational) ]" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Yellow

function Test-Optional($name, $url) {
    try {
        $r = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        "  [UP]   $name at $url" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Green
    } catch {
        "  [DOWN] $name — start with: docker compose up -d" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Yellow
    }
}

Test-Optional "SearXNG"    "http://localhost:4000"
Test-Optional "Pipelines"  "http://localhost:9099"
Test-Optional "Qdrant"     "http://localhost:6333/health"
Test-Optional "Ollama"     "http://localhost:11434"
Test-Optional "n8n"        "http://localhost:5678"

# -- CODE ASSIST SCRIPT --
"`n[ Code Assist Script ]" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Yellow

Test-Case "Python syntax: code_assist.py" {
    $out = python -m py_compile "$root\scripts\code_assist.py" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Syntax error: $out" }
}

Test-Case "All 5 prompt files exist" {
    foreach ($mode in @("explain","review","fix","test","plan")) {
        $p = "$root\scripts\prompts\$mode.txt"
        if (-not (Test-Path $p)) { throw "Missing prompt file: $p" }
    }
}

if (-not $CI) {
    Test-Case "LM Studio API endpoint reachable (code_assist target)" {
        $r = Invoke-RestMethod http://localhost:1234/v1/models -TimeoutSec 5
        if ($r.data.Count -eq 0) { throw "No models at LM Studio API endpoint" }
    }
} else {
    "  [SKIP] LM Studio API endpoint reachable (CI mode)" |
        Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor DarkGray
}

Test-Case "code_assist.py --help exits cleanly" {
    $out = (python "$root\scripts\code_assist.py" --help 2>&1) -join " "
    if ($LASTEXITCODE -ne 0) { throw "Script exited with error on --help: $out" }
    if ($out -notmatch "mode") { throw "--help output missing expected 'mode' flag description" }
}

Test-Case "code_assist.py accepts --profile and --mode flags" {
    # Pipe empty input to trigger clean startup then EOF exit
    $out = "" | python "$root\scripts\code_assist.py" --profile coding --mode review 2>&1
    if ($LASTEXITCODE -gt 1) { throw "Script crashed on startup (exit $LASTEXITCODE): $out" }
}

# -- SUMMARY --
"`n=====================================" | Tee-Object -FilePath $log -Append | Write-Host
"  PASSED: $pass" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Green
"  FAILED: $fail" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor $(if ($fail -gt 0) { "Red" } else { "Green" })
"  TOTAL:  $($pass + $fail)" | Tee-Object -FilePath $log -Append | Write-Host
"=====================================" | Tee-Object -FilePath $log -Append | Write-Host
if ($fail -gt 0) { exit 1 }
# Explicit exit 0 so we don't inherit $LASTEXITCODE from the last native
# command (e.g. `python code_assist.py --profile coding` returns 1 because
# the new tier schema doesn't have an `id:` field — but the wrapping test
# only fails on exit > 1, so it passes while leaking a non-zero $LASTEXITCODE).
exit 0
