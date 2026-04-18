$log = "C:\Users\Kit\Documents\test_results.txt"
$pass = 0; $fail = 0
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Test-Case($name, $block) {
    try {
        $result = & $block
        if ($result -eq $false) { throw "returned false" }
        "  [PASS] $name" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Green
        $script:pass++
    } catch {
        "  [FAIL] $name - $_" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Red
        $script:fail++
    }
}

$root = "C:\Users\Kit\Documents\claude code"
"===== LOCAL AI STACK TEST SUITE =====" | Tee-Object -FilePath $log | Write-Host -ForegroundColor Cyan
"$(Get-Date)" | Tee-Object -FilePath $log -Append | Write-Host

# -- SYNTAX CHECKS --
"`n[ Syntax Checks ]" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Yellow

foreach ($script in @("start.ps1","stop.ps1","switch-model.ps1")) {
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

Test-Case "models.yaml has all 4 profiles" {
    $yaml = Get-Content "$root\config\models.yaml" -Raw
    foreach ($p in @("fast","quality","coding","large")) {
        if ($yaml -notmatch "  ${p}:") { throw "Missing profile: $p" }
    }
}

Test-Case "docker-compose.yml valid" {
    $out = (docker compose -f "$root\docker-compose.yml" config 2>&1) -join "`n"
    if (-not ($out -match "services:")) { throw "config output looks invalid" }
}

Test-Case "docker-compose has open-webui service" {
    $out = (docker compose -f "$root\docker-compose.yml" config 2>&1) -join "`n"
    if ($out -notmatch "open-webui") { throw "open-webui service missing" }
}

Test-Case "docker-compose has jupyter service" {
    $out = (docker compose -f "$root\docker-compose.yml" config 2>&1) -join "`n"
    if ($out -notmatch "jupyter") { throw "jupyter service missing" }
}

Test-Case "docker-compose WEBUI_AUTH=False set" {
    $out = (docker compose -f "$root\docker-compose.yml" config 2>&1) -join "`n"
    if ($out -notmatch "WEBUI_AUTH.*False") { throw "WEBUI_AUTH not set to False" }
}

Test-Case "squarespace-embed.html exists" {
    Test-Path "$root\squarespace-embed.html"
}

Test-Case "embed HTML contains correct Tailscale hostname" {
    $html = Get-Content "$root\squarespace-embed.html" -Raw
    if ($html -notmatch "desktop-j4g42gi\.taila2838f\.ts\.net") {
        throw "Tailscale hostname missing or incorrect"
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

foreach ($profile in @("fast","quality","coding","large")) {
    Test-Case "Parser: '$profile' has required keys" {
        $m = Get-ModelConfig $profile "$root\config\models.yaml"
        foreach ($key in @("id","gpu","context","parallel","description")) {
            if (-not $m.ContainsKey($key)) { throw "Missing key: $key" }
        }
    }
}

Test-Case "Parser: 'quality' is default profile" {
    $yaml = Get-Content "$root\config\models.yaml"
    $defaultLine = ($yaml | Select-String "^default:").Line
    $default = $defaultLine -replace "default:\s*",""
    if ($default.Trim() -ne "quality") { throw "Default is '$($default.Trim())' not 'quality'" }
}

Test-Case "Parser: unknown profile returns empty" {
    $m = Get-ModelConfig "nonexistent" "$root\config\models.yaml"
    if ($m.Count -ne 0) { throw "Should return empty for unknown profile" }
}

# -- LIVE SERVICE CHECKS --
"`n[ Live Service Checks ]" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Yellow

Test-Case "Open WebUI responds HTTP 200" {
    $r = Invoke-WebRequest http://localhost:3000 -UseBasicParsing -TimeoutSec 5
    if ($r.StatusCode -ne 200) { throw "Got $($r.StatusCode)" }
}

Test-Case "Open WebUI auth is disabled" {
    $cfg = Invoke-RestMethod http://localhost:3000/api/config -TimeoutSec 5
    if ($cfg.features.auth -ne $false) { throw "Auth is not disabled" }
}

Test-Case "LM Studio server responding" {
    $r = Invoke-RestMethod http://localhost:1234/v1/models -TimeoutSec 5
    if ($r.data.Count -eq 0) { throw "No models returned" }
}

# Get auth token (WEBUI_AUTH=False allows empty-credential signin)
$script:token = $null
try {
    $authResp = Invoke-RestMethod http://localhost:3000/api/v1/auths/signin `
        -Method Post `
        -Body '{"email":"","password":""}' `
        -ContentType "application/json" `
        -TimeoutSec 5
    $script:token = $authResp.token
} catch {}

Test-Case "Open WebUI sees LM Studio models" {
    if (-not $script:token) { throw "Could not obtain auth token" }
    $headers = @{Authorization = "Bearer $script:token"}
    $r = Invoke-RestMethod http://localhost:3000/api/models -Headers $headers -TimeoutSec 5
    if ($r.data.Count -eq 0) { throw "No models visible in Open WebUI" }
}

Test-Case "Kit's Assistant model exists" {
    if (-not $script:token) { throw "Could not obtain auth token" }
    $headers = @{Authorization = "Bearer $script:token"}
    $r = Invoke-RestMethod http://localhost:3000/api/models -Headers $headers -TimeoutSec 5
    $kit = $r.data | Where-Object { $_.id -like "*kits-assistant*" -or $_.name -like "*Kit*" }
    if (-not $kit) {
        $names = ($r.data | ForEach-Object { $_.id }) -join ", "
        throw "Kit's Assistant not found. Models: $names"
    }
}

Test-Case "Docker container healthy" {
    $status = docker inspect open-webui --format "{{.State.Health.Status}}" 2>&1
    if ($status -ne "healthy") { throw "Container status: $status" }
}

# -- SUMMARY --
"`n=====================================" | Tee-Object -FilePath $log -Append | Write-Host
"  PASSED: $pass" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor Green
"  FAILED: $fail" | Tee-Object -FilePath $log -Append | Write-Host -ForegroundColor $(if ($fail -gt 0) { "Red" } else { "Green" })
"  TOTAL:  $($pass + $fail)" | Tee-Object -FilePath $log -Append | Write-Host
"=====================================" | Tee-Object -FilePath $log -Append | Write-Host
