<#
.SYNOPSIS
    LocalAIStack -- Setup, Repair, and Build (WSL/Docker-Engine edition)
.DESCRIPTION
    Installs all prerequisites on a fresh Windows system, configures the
    project environment, and compiles the LocalAIStack.exe / AirgapChat.exe
    launcher files.

    Uses Docker Engine inside a WSL2 Ubuntu distro rather than Docker Desktop.
    Docker Desktop is NOT required and (if installed) is left untouched.

    Safe to run multiple times -- skips steps that are already complete.
    Run as Administrator for fully automated prerequisite installation.

.PARAMETER Repair
    Force-reinstall or reconfigure every step even if it appears healthy.
.PARAMETER NoBuild
    Skip compiling the .exe files (prerequisites + config only).
.PARAMETER NoShortcuts
    Skip creating Desktop and Start Menu shortcuts.
.PARAMETER Distro
    WSL distro name. Default: Ubuntu.

.PARAMETER PullModels
    Pull Ollama models after setup (default group: minimal, ~7 GB).
    Implies starting the ollama container temporarily.
.PARAMETER DownloadVision
    Also download vision GGUF files from HuggingFace (~21 GB, resumable).
    Implies -PullModels.
.PARAMETER ModelGroup
    Which Ollama group to pull: minimal (default), standard, tiers.
    Only used when -PullModels or -Interactive is set.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup.ps1
    powershell -ExecutionPolicy Bypass -File setup.ps1 -Repair
    powershell -ExecutionPolicy Bypass -File setup.ps1 -NoShortcuts
    powershell -ExecutionPolicy Bypass -File setup.ps1 -PullModels
    powershell -ExecutionPolicy Bypass -File setup.ps1 -PullModels -ModelGroup tiers
    powershell -ExecutionPolicy Bypass -File setup.ps1 -DownloadVision
#>

param(
    [switch]$Repair,
    [switch]$NoBuild,
    [switch]$NoShortcuts,
    [switch]$Interactive,       # Prompt for optional credentials (Cloudflare, SMTP, n8n)
    [switch]$PullModels,        # Pull Ollama models + optionally download vision GGUFs
    [switch]$DownloadVision,    # Also download vision GGUF files (~21 GB)
    [string]$ModelGroup = "",   # minimal / standard / tiers (default: minimal)
    [string]$Distro = "Ubuntu"
)

# "Continue" so native command stderr (docker, wsl, dism) doesn't terminate
# the script -- we check $LASTEXITCODE ourselves after each call.
$ErrorActionPreference = "Continue"
$root            = $PSScriptRoot
$warnings        = [System.Collections.Generic.List[string]]::new()
$failures        = [System.Collections.Generic.List[string]]::new()
$script:GpuAvailable = $false   # set in Phase 4, used in Phase 6.5

# ---- Persistent log file ----------------------------------------------------
$script:LogDir = Join-Path $env:LOCALAPPDATA "LocalAIStack"
if (-not (Test-Path $script:LogDir)) { New-Item -ItemType Directory -Path $script:LogDir | Out-Null }
$script:LogFile = Join-Path $script:LogDir "setup.log"
function Write-LogFile {
    param([string]$Level, [string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
    "[$ts] [$Level] $Message" | Out-File -FilePath $script:LogFile -Append -Encoding utf8
}

# ---- Console helpers --------------------------------------------------------
function Write-Phase($msg) {
    $pad = "-" * [Math]::Max(2, 46 - $msg.Length)
    Write-Host ""
    Write-Host "  -- $msg $pad" -ForegroundColor Cyan
    Write-LogFile "PHASE" $msg
}
function Write-Step($msg) { Write-Host "     > $msg" -ForegroundColor DarkGray; Write-LogFile "STEP" $msg }
function Write-OK($msg)   { Write-Host "     + $msg" -ForegroundColor Green;    Write-LogFile "OK"   $msg }
function Write-Warn($msg) { Write-Host "     ! $msg" -ForegroundColor Yellow;   Write-LogFile "WARN" $msg; $warnings.Add($msg) }
function Write-Fail($msg) { Write-Host "     X $msg" -ForegroundColor Red;      Write-LogFile "FAIL" $msg; $failures.Add($msg) }
function Write-Info($msg) { Write-Host "       $msg" -ForegroundColor DarkGray; Write-LogFile "INFO" $msg }

# ---- Crypto-safe secret generator (URL-safe Base64, no padding) -------------
function New-Secret {
    param([int]$Bytes = 48)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $buf = New-Object byte[] $Bytes
    $rng.GetBytes($buf)
    return ([Convert]::ToBase64String($buf)) -replace '\+', '-' -replace '/', '_' -replace '=', ''
}

# ---- .env.local editors -----------------------------------------------------
function Get-EnvValue {
    param([string]$EnvFile, [string]$Key)
    if (-not (Test-Path $EnvFile)) { return $null }
    foreach ($line in [System.IO.File]::ReadAllLines($EnvFile)) {
        if ($line -match "^\s*$Key\s*=\s*(.*)$") { return $Matches[1] }
    }
    return $null
}
function Set-EnvValue {
    param([string]$EnvFile, [string]$Key, [string]$Value)
    $text = if (Test-Path $EnvFile) { [System.IO.File]::ReadAllText($EnvFile) } else { "" }
    $pattern = "(?m)^\s*$([Regex]::Escape($Key))\s*=.*$"
    if ($text -match $pattern) {
        $text = [System.Text.RegularExpressions.Regex]::Replace($text, $pattern, "$Key=$Value")
    } else {
        if ($text -and -not $text.EndsWith("`n")) { $text += "`n" }
        $text += "$Key=$Value`n"
    }
    [System.IO.File]::WriteAllText($EnvFile, ($text -replace "`r`n", "`n"),
        (New-Object System.Text.UTF8Encoding $false))
}
function Read-UserInput {
    param([string]$Prompt, [string]$Default = "", [switch]$Mask)
    $shown = if ($Default) { " [$Default]" } else { "" }
    if ($Mask) {
        $sec = Read-Host "     $Prompt$shown" -AsSecureString
        $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
        if (-not $plain) { return $Default }
        return $plain
    } else {
        $v = Read-Host "     $Prompt$shown"
        if (-not $v) { return $Default }
        return $v
    }
}
function Read-YesNo {
    param([string]$Prompt, [bool]$Default = $false)
    $suffix = if ($Default) { "(Y/n)" } else { "(y/N)" }
    $v = Read-Host "     $Prompt $suffix"
    if (-not $v) { return $Default }
    return $v -match '^(y|yes)$'
}

# ---- winget wrapper --------------------------------------------------------
function Install-WithWinget {
    param([string]$Id, [string]$Name)
    Write-Step "Installing $Name via winget..."
    winget install --id $Id -e --accept-source-agreements --accept-package-agreements --silent 2>&1 | Out-Null
    return $LASTEXITCODE -eq 0
}

# ---- Check whether a named WSL distro exists -------------------------------
function Test-WslDistro {
    param([string]$Name)
    # wsl -l -q output is UTF-16 LE -- must decode properly
    $raw = & wsl.exe -l -q 2>&1
    if ($LASTEXITCODE -ne 0) { return $false }
    $lines = ($raw -join "`n") -split "[\r\n]+" | ForEach-Object { $_.Trim() }
    return $lines -contains $Name
}

# ---- Run a command inside the WSL distro, streaming output -----------------
function Invoke-Wsl {
    param(
        [string]$DistroName,
        [string]$Command,
        [switch]$AsRoot
    )
    $args = @("-d", $DistroName)
    if ($AsRoot) { $args += @("-u", "root") }
    $args += @("--", "bash", "-c", $Command)
    & wsl.exe @args
    return $LASTEXITCODE
}

# ---- Convert a Windows path to a WSL path ----------------------------------
function ConvertTo-WslPath {
    param([string]$DistroName, [string]$WindowsPath)
    $out = & wsl.exe -d $DistroName -- wslpath -u "$WindowsPath" 2>&1
    return ($out | Select-Object -First 1).Trim()
}

# =============================================================================
Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host "  |   LocalAIStack  --  Setup / Build      |" -ForegroundColor Cyan
Write-Host "  |   Docker Engine in WSL2                |" -ForegroundColor Cyan
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-LogFile "START" ("setup.ps1 invoked; Repair={0} NoBuild={1} NoShortcuts={2} Distro={3}" `
    -f $Repair, $NoBuild, $NoShortcuts, $Distro)
Write-Host "  Log: $script:LogFile" -ForegroundColor DarkGray

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host ""
    Write-Host "  ! Not running as Administrator." -ForegroundColor Yellow
    Write-Host "    WSL and PowerShell 7 installs will be skipped." -ForegroundColor Yellow
    Write-Host "    Re-run from an elevated terminal for a fully automated setup." -ForegroundColor DarkGray
}
if ($Repair) { Write-Host "  Mode: REPAIR" -ForegroundColor Yellow }

$hasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)

# =============================================================================
# PHASE 1 -- Windows version
# =============================================================================
Write-Phase "System Requirements"

$osBuild = [System.Environment]::OSVersion.Version.Build
if ($osBuild -lt 19041) {
    Write-Fail "Windows 10 build 19041 (version 2004) or later required. Current: $osBuild"
    exit 1
}
Write-OK "Windows build $osBuild"

if (-not $hasWinget) {
    Write-Warn "winget not found -- install 'App Installer' from the Microsoft Store."
}

# =============================================================================
# PHASE 2 -- WSL 2 + Ubuntu distro
# =============================================================================
Write-Phase "WSL 2 + $Distro Distro"

$wslExe = Get-Command wsl -ErrorAction SilentlyContinue
if (-not $wslExe) {
    if ($isAdmin) {
        Write-Step "Installing WSL (this may take a few minutes)..."
        wsl --install --no-distribution 2>&1 | Out-Null
        Write-OK "WSL installed -- a reboot may be required before continuing."
    } else {
        Write-Fail "WSL not installed and not running as Administrator. Re-run elevated."
        exit 1
    }
} else {
    # Check that a WSL2 kernel is usable
    & wsl.exe -e true 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0 -and $isAdmin) {
        Write-Step "WSL not usable yet -- running 'wsl --update'..."
        wsl --update 2>&1 | Out-Null
    }
    Write-OK "WSL 2 available"
}

# Ensure the target distro exists
$distroPresent = Test-WslDistro -Name $Distro
if (-not $distroPresent) {
    Write-Step "Installing $Distro WSL distro..."
    # --no-launch skips the interactive first-run user setup; we operate as
    # root and create a user inside the installer script if needed.
    wsl --install -d $Distro --no-launch 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to install $Distro distro. Try manually: wsl --install -d $Distro"
        exit 1
    }
    # Wait for distro to become listable
    for ($i = 0; $i -lt 12; $i++) {
        Start-Sleep 2
        if (Test-WslDistro -Name $Distro) { break }
    }
    Write-OK "$Distro installed"
} else {
    Write-OK "$Distro distro present"
}

# Verify we can run commands inside it
& wsl.exe -d $Distro -- true 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Cannot exec inside $Distro. Run: wsl -d $Distro manually to diagnose."
    exit 1
}
Write-OK "$Distro reachable"

# =============================================================================
# PHASE 3 -- Docker Engine inside $Distro
# =============================================================================
Write-Phase "Docker Engine (inside $Distro)"

$installerWin = Join-Path $root "scripts\install-docker-wsl.sh"
if (-not (Test-Path $installerWin)) {
    Write-Fail "Installer not found: $installerWin"
    exit 1
}
$installerWsl = ConvertTo-WslPath -DistroName $Distro -WindowsPath $installerWin

# Check if docker is already installed (fast-path)
$dockerCheck = & wsl.exe -d $Distro -- bash -c "command -v docker && docker info >/dev/null 2>&1 && echo READY" 2>&1
$dockerReady = ($dockerCheck -join " ") -match "READY"

if ($dockerReady -and -not $Repair) {
    Write-OK "Docker Engine already running inside $Distro"
} else {
    Write-Step "Running install-docker-wsl.sh inside $Distro (may take 2-5 min)..."
    & wsl.exe -d $Distro -u root -- bash "$installerWsl"
    $rc = $LASTEXITCODE
    if ($rc -ne 0) {
        Write-Fail "Docker install inside $Distro failed (exit $rc). See log for details."
    } else {
        Write-OK "Docker install script completed"
    }

    # Re-verify. If systemd was just enabled, we need 'wsl --shutdown' to activate it.
    & wsl.exe -d $Distro -- docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Step "Restarting WSL so systemd/docker picks up the new config..."
        wsl --shutdown 2>&1 | Out-Null
        Start-Sleep 3
        # Pre-start the distro and wait up to 30s for docker to come up
        & wsl.exe -d $Distro -- true 2>&1 | Out-Null
        for ($i = 0; $i -lt 15; $i++) {
            & wsl.exe -d $Distro -- docker info 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { break }
            Start-Sleep 2
        }
    }

    & wsl.exe -d $Distro -- docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $dockerVer = (& wsl.exe -d $Distro -- docker --version 2>&1 | Out-String).Trim()
        Write-OK "Docker reachable: $dockerVer"
    } else {
        Write-Fail "Docker is still not reachable inside $Distro. Run setup.ps1 -Repair or check: wsl -d $Distro"
    }
}

# =============================================================================
# PHASE 4 -- NVIDIA GPU (advisory)
# =============================================================================
Write-Phase "NVIDIA GPU"

# Check GPU visibility inside WSL (WSL CUDA driver passes through from host)
$gpuInWsl = & wsl.exe -d $Distro -- bash -c "command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1" 2>&1
$gpuLine = ($gpuInWsl | Out-String).Trim()
if ($gpuLine -and $gpuLine -notmatch "command not found|failed") {
    Write-OK "GPU in WSL: $gpuLine"
    # Test nvidia-container-toolkit by running a quick CUDA container
    Write-Step "Testing GPU passthrough to Docker..."
    & wsl.exe -d $Distro -- bash -c "docker run --rm --gpus all nvidia/cuda:12.3.0-base-ubuntu22.04 nvidia-smi -L" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-OK "nvidia-container-toolkit working -- models will load on GPU"
        $script:GpuAvailable = $true
    } else {
        Write-Warn "GPU visible in WSL but nvidia-container-toolkit test failed."
        Write-Info "Fix: wsl -d $Distro -- sudo nvidia-ctk runtime configure --runtime=docker"
        Write-Info "     wsl -d $Distro -- sudo systemctl restart docker"
        Write-Info "Models will run on CPU until this is resolved."
    }
} else {
    Write-Warn "No GPU visible inside WSL -- stack will run CPU-only."
    Write-Info ""
    Write-Info "WHY YOUR CPU IS MAXED OUT:"
    Write-Info "  Ollama and llama-server run all inference on CPU when no GPU is found."
    Write-Info "  A 7B model saturates every core; a 35B model does so for minutes per reply."
    Write-Info ""
    Write-Info "TO FIX -- two steps, both on Windows:"
    Write-Info "  1. Install NVIDIA Game Ready/Studio driver >= 550 from nvidia.com/drivers"
    Write-Info "  2. Install the WSL CUDA driver: https://developer.nvidia.com/cuda/wsl"
    Write-Info "  After installing, reboot Windows, then re-run setup.ps1."
    Write-Info "  See docs/manual-setup.md section 1 for the full walkthrough."
}

# =============================================================================
# PHASE 5 -- PowerShell 7
# =============================================================================
Write-Phase "PowerShell 7"

$pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
if ($pwshCmd -and -not $Repair) {
    Write-OK "PowerShell 7: $($pwshCmd.Source)"
} elseif ($hasWinget) {
    $ok = Install-WithWinget -Id "Microsoft.PowerShell" -Name "PowerShell 7"
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    $pwshCmd = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($pwshCmd) { Write-OK "PowerShell 7: $($pwshCmd.Source)" }
    else          { Write-Warn "PowerShell 7 install may take effect after reopening your terminal." }
} else {
    Write-Warn "PowerShell 7 not found. Download: https://aka.ms/powershell-release?tag=stable"
}

# =============================================================================
# PHASE 6 -- Project configuration
# =============================================================================
Write-Phase "Project Configuration"

$envLocal   = Join-Path $root ".env.local"
$envExample = Join-Path $root ".env.example"

if (-not (Test-Path $envLocal)) {
    Write-Step "Creating .env.local from .env.example..."
    Copy-Item $envExample $envLocal
    Write-OK ".env.local created"
}

# Auto-generate all required random secrets that are currently empty. These
# are never placeholders the user needs to know about -- strong random values.
foreach ($secretKey in @("AUTH_SECRET_KEY", "HISTORY_SECRET_KEY", "JUPYTER_TOKEN")) {
    $cur = Get-EnvValue -EnvFile $envLocal -Key $secretKey
    if (-not $cur) {
        Set-EnvValue -EnvFile $envLocal -Key $secretKey -Value (New-Secret -Bytes 48)
        Write-OK "$secretKey generated"
    } else {
        Write-OK "$secretKey already set"
    }
}

# ---- Interactive credential setup (optional, -Interactive flag) -------------
# Prompts for credentials the stack needs but can't auto-generate: admin
# email (magic-link recipient), Cloudflare tunnel, SMTP, n8n basic-auth.
# Any value left blank stays unset and the corresponding feature is disabled.
if ($Interactive) {
    Write-Host ""
    Write-Host "     --- Interactive credential setup ---" -ForegroundColor Cyan
    Write-Host "     Press Enter to skip any prompt and keep the current value." -ForegroundColor DarkGray

    # Admin email -- receives magic links and gets admin dashboard access
    $curAdmin = Get-EnvValue -EnvFile $envLocal -Key "ADMIN_EMAILS"
    Write-Host ""
    Write-Host "     ADMIN_EMAILS: email(s) allowed into the admin dashboard." -ForegroundColor Yellow
    Write-Host "     Comma-separated. Your login uses this email for magic links." -ForegroundColor DarkGray
    $adminEmail = Read-UserInput -Prompt "Admin email(s)" -Default $curAdmin
    if ($adminEmail) {
        Set-EnvValue -EnvFile $envLocal -Key "ADMIN_EMAILS" -Value $adminEmail
        Write-OK "ADMIN_EMAILS saved"
    }

    # Cloudflare Tunnel
    Write-Host ""
    Write-Host "     CLOUDFLARE_TUNNEL_TOKEN: public HTTPS access (optional)." -ForegroundColor Yellow
    Write-Host "     Find it at: https://one.dash.cloudflare.com -> Networks" -ForegroundColor DarkGray
    Write-Host "     -> Tunnels -> Create tunnel -> Copy the install token." -ForegroundColor DarkGray
    if (Read-YesNo "Configure Cloudflare Tunnel now?" $false) {
        $cfToken = Read-UserInput -Prompt "Tunnel token" -Mask
        $cfHost  = Read-UserInput -Prompt "Public hostname (e.g. chat.example.com)"
        if ($cfToken) { Set-EnvValue $envLocal "CLOUDFLARE_TUNNEL_TOKEN" $cfToken; Write-OK "CLOUDFLARE_TUNNEL_TOKEN saved" }
        if ($cfHost)  { Set-EnvValue $envLocal "CLOUDFLARE_HOSTNAME" $cfHost;      Write-OK "CLOUDFLARE_HOSTNAME saved" }
        if ($cfHost)  { Set-EnvValue $envLocal "PUBLIC_BASE_URL" "https://$cfHost"; Write-OK "PUBLIC_BASE_URL saved" }
    }

    # SMTP
    Write-Host ""
    Write-Host "     SMTP: email magic-links instead of logging them to stdout (optional)." -ForegroundColor Yellow
    Write-Host "     Gmail: use an App Password (https://myaccount.google.com/apppasswords)." -ForegroundColor DarkGray
    Write-Host "     Host examples: smtp.gmail.com, smtp.office365.com, smtp-mail.outlook.com" -ForegroundColor DarkGray
    if (Read-YesNo "Configure SMTP now?" $false) {
        $h = Read-UserInput -Prompt "SMTP host"
        $p = Read-UserInput -Prompt "SMTP port (usually 587)" -Default "587"
        $u = Read-UserInput -Prompt "SMTP username"
        $pw = Read-UserInput -Prompt "SMTP password" -Mask
        $from = Read-UserInput -Prompt "From address" -Default $u
        if ($h) { Set-EnvValue $envLocal "SMTP_HOST" $h }
        if ($p) { Set-EnvValue $envLocal "SMTP_PORT" $p }
        if ($u) { Set-EnvValue $envLocal "SMTP_USER" $u }
        if ($pw) { Set-EnvValue $envLocal "SMTP_PASS" $pw }
        if ($from) { Set-EnvValue $envLocal "AUTH_EMAIL_FROM" $from }
        Write-OK "SMTP credentials saved"
    }

    # n8n basic auth
    Write-Host ""
    Write-Host "     n8n workflow editor (port 5678) has no auth by default." -ForegroundColor Yellow
    Write-Host "     Recommended if you expose n8n beyond localhost." -ForegroundColor DarkGray
    if (Read-YesNo "Enable n8n basic auth now?" $true) {
        $nu = Read-UserInput -Prompt "n8n admin username" -Default "admin"
        $np = Read-UserInput -Prompt "n8n admin password (blank = auto-generate)" -Mask
        if (-not $np) { $np = New-Secret -Bytes 18; Write-Info "Generated n8n password: $np" }
        Set-EnvValue $envLocal "N8N_BASIC_AUTH_ACTIVE" "true"
        Set-EnvValue $envLocal "N8N_ADMIN_USER" $nu
        Set-EnvValue $envLocal "N8N_ADMIN_PASSWORD" $np
        Write-OK "n8n admin auth saved"
    }
    Write-Host ""
}

foreach ($dir in @("data", "models")) {
    $dirPath = Join-Path $root $dir
    if (-not (Test-Path $dirPath)) {
        New-Item -ItemType Directory -Path $dirPath | Out-Null
        Write-OK "Created $dir/"
    } else {
        Write-OK "$dir/ present"
    }
}

# =============================================================================
# PHASE 6.5 -- Model Downloads
# =============================================================================
# Triggered by -PullModels, -DownloadVision, or -Interactive (asks the user).
# Starts the ollama container, pulls the chosen tier group, then optionally
# downloads the vision GGUF files (~21 GB) to ./models/.

$doPull = $PullModels -or $DownloadVision
if (-not $doPull -and $Interactive) {
    Write-Host ""
    Write-Host "  -- Model Downloads ---------------------------------" -ForegroundColor Cyan
    Write-Host ""

    if (-not $script:GpuAvailable) {
        Write-Host "     ! GPU not detected. Models will run on CPU until NVIDIA drivers" -ForegroundColor Yellow
        Write-Host "       and the WSL CUDA driver are installed (see Phase 4 output above)." -ForegroundColor Yellow
        Write-Host "       Pulling large models without GPU means very high CPU load." -ForegroundColor Yellow
        Write-Host ""
    }

    $doPull = Read-YesNo "Download AI models now? (can also be done later by re-running with -PullModels)" $false
}

if ($doPull) {
    Write-Phase "Model Downloads"

    if (-not $script:GpuAvailable) {
        Write-Warn "GPU not available -- models will run on CPU (slow, high CPU load)."
        Write-Info "Set up NVIDIA drivers first for best performance (docs/manual-setup.md s.1)."
    } else {
        Write-OK "GPU available -- models will load on GPU after stack starts"
    }

    # Choose which Ollama model group to pull
    $chosenGroup = $ModelGroup
    if (-not $chosenGroup -and $Interactive) {
        Write-Host ""
        Write-Host "     Which model tier?" -ForegroundColor Yellow
        Write-Host "     minimal  : qwen3.5:9b + nomic-embed-text (~7 GB)  -- fast, good quality" -ForegroundColor DarkGray
        Write-Host "     standard : adds llava:7b, phi4-mini (~20 GB total)" -ForegroundColor DarkGray
        Write-Host "     tiers    : all backend tiers -- qwen3 72B, 35B, 9B, coder, embed (~80 GB)" -ForegroundColor DarkGray
        $chosenGroup = Read-UserInput -Prompt "Group (minimal/standard/tiers)" -Default "minimal"
    }
    if (-not $chosenGroup) { $chosenGroup = "minimal" }

    $wslRoot = ConvertTo-WslPath -DistroName $Distro -WindowsPath $root
    $modelsScriptWin = Join-Path $root "scripts\setup-models.sh"
    $modelsScriptWsl = ConvertTo-WslPath -DistroName $Distro -WindowsPath $modelsScriptWin

    # Start ollama service so we can pull
    Write-Step "Starting ollama service..."
    $envCmd  = "set -a; [ -f '$wslRoot/.env.local' ] && . '$wslRoot/.env.local'; set +a"
    $cfxCmd  = "export CLOUDFLARE_TUNNEL_TOKEN=`${CLOUDFLARE_TUNNEL_TOKEN:-_disabled_}"
    $upCmd   = "cd '$wslRoot' && $envCmd && $cfxCmd && docker compose up -d ollama 2>&1"
    & wsl.exe -d $Distro -- bash -c $upCmd 2>&1 | ForEach-Object { Write-Info $_ }

    # Wait up to 60 s for Ollama API
    $ollamaUp = $false
    for ($i = 0; $i -lt 20; $i++) {
        $chk = & wsl.exe -d $Distro -- bash -c "curl -fsS --max-time 3 http://localhost:11434/api/tags >/dev/null 2>&1 && echo UP" 2>&1
        if (($chk | Out-String) -match "UP") { $ollamaUp = $true; break }
        Start-Sleep 3
    }

    if (-not $ollamaUp) {
        Write-Warn "Ollama did not respond within 60 s. Pull skipped."
        Write-Info "Manually pull later: wsl -d $Distro -- bash scripts/setup-models.sh $chosenGroup --skip-vision"
    } else {
        Write-OK "Ollama ready"
        Write-Step "Pulling '$chosenGroup' models (downloads may take a while)..."
        & wsl.exe -d $Distro -- bash -c "bash '$modelsScriptWsl' $chosenGroup --skip-vision"
        if ($LASTEXITCODE -eq 0) {
            Write-OK "Ollama '$chosenGroup' models pulled"
        } else {
            Write-Warn "Model pull reported warnings. Check: wsl -d $Distro -- ollama list"
        }
    }

    # Vision GGUF download
    $doVision = $DownloadVision
    if (-not $doVision -and $Interactive) {
        Write-Host ""
        Write-Host "     Vision tier files (~21 GB):" -ForegroundColor Yellow
        Write-Host "     qwen3.6-35b-a3b-Q4_K_M.gguf (~20 GB) + mmproj (~1 GB)" -ForegroundColor DarkGray
        Write-Host "     Required only for image understanding -- text tiers work without it." -ForegroundColor DarkGray
        $doVision = Read-YesNo "Download vision model files now? (requires wget + ~21 GB free)" $false
    }

    if ($doVision) {
        Write-Step "Downloading vision GGUF files via wget (resumes if interrupted)..."
        & wsl.exe -d $Distro -- bash -c "bash '$modelsScriptWsl' --download-vision '$wslRoot'"
        if ($LASTEXITCODE -eq 0) {
            Write-OK "Vision files downloaded to ./models/"
            Write-Info "Restart llama-server to load them: docker compose restart llama-server (in WSL)"
        } else {
            Write-Warn "Vision download incomplete. Re-run: setup.ps1 -DownloadVision"
        }
    } else {
        Write-Info "Vision files skipped. Download later: setup.ps1 -DownloadVision"
    }
}

# =============================================================================
# PHASE 7 -- Build EXEs
# =============================================================================
if (-not $NoBuild) {
    Write-Phase "Build Application EXEs"

    $pwshExe = if ($pwshCmd) { $pwshCmd.Source } else { "powershell.exe" }

    Write-Step "Ensuring ps2exe module is installed..."
    & $pwshExe -NoProfile -NonInteractive -Command `
        "if (-not (Get-Module -ListAvailable -Name ps2exe)) { Install-Module ps2exe -Scope CurrentUser -Force -AllowClobber }" `
        2>&1 | ForEach-Object { Write-Info $_ }

    $buildScript = Join-Path $root "launcher\build.ps1"
    Write-Step "Compiling LocalAIStack.exe, AirgapChat.exe..."
    & $pwshExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $buildScript `
        2>&1 | ForEach-Object { Write-Info $_ }

    $distDir = Join-Path $root "launcher\dist"
    foreach ($exe in @("LocalAIStack.exe", "AirgapChat.exe")) {
        $exePath = Join-Path $distDir $exe
        if (Test-Path $exePath) {
            Write-OK "launcher\dist\$exe"
        } else {
            Write-Fail "launcher\dist\$exe -- build failed"
        }
    }
}

# =============================================================================
# PHASE 8 -- Shortcuts
# =============================================================================
if (-not $NoShortcuts) {
    Write-Phase "Shortcuts"

    $launcherExe = Join-Path $root "launcher\dist\LocalAIStack.exe"
    if (Test-Path $launcherExe) {
        $wsh = New-Object -ComObject WScript.Shell

        $desktopLnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "LocalAIStack.lnk"
        if (-not (Test-Path $desktopLnk) -or $Repair) {
            $sc = $wsh.CreateShortcut($desktopLnk)
            $sc.TargetPath       = $launcherExe
            $sc.WorkingDirectory = $root
            $sc.Description      = "Start LocalAIStack"
            $iconFile = Join-Path $root "launcher\assets\icon.ico"
            if (Test-Path $iconFile) { $sc.IconLocation = $iconFile }
            $sc.Save()
            Write-OK "Desktop shortcut created"
        } else {
            Write-OK "Desktop shortcut already exists"
        }

        $startDir = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\LocalAIStack"
        if (-not (Test-Path $startDir)) { New-Item -ItemType Directory $startDir | Out-Null }
        $sc = $wsh.CreateShortcut((Join-Path $startDir "LocalAIStack.lnk"))
        $sc.TargetPath       = $launcherExe
        $sc.WorkingDirectory = $root
        $sc.Description      = "Start LocalAIStack"
        $sc.Save()
        Write-OK "Start Menu shortcut created"
    } else {
        Write-Info "Skipping shortcuts -- EXE not present (run without -NoBuild to compile first)."
    }
}

# =============================================================================
# Summary
# =============================================================================
Write-Host ""
Write-Host "  ============================================" -ForegroundColor DarkGray
if ($failures.Count -gt 0) {
    Write-Host "  Setup finished with errors:" -ForegroundColor Red
    foreach ($f in $failures) { Write-Host "    X $f" -ForegroundColor Red }
}
if ($warnings.Count -gt 0) {
    Write-Host "  Warnings (non-fatal):" -ForegroundColor Yellow
    foreach ($w in $warnings) { Write-Host "    ! $w" -ForegroundColor Yellow }
}
if ($failures.Count -eq 0) {
    Write-Host "  Setup complete!" -ForegroundColor Green
    $launcherExe = Join-Path $root "launcher\dist\LocalAIStack.exe"
    if (Test-Path $launcherExe) {
        Write-Host "  Launch: launcher\dist\LocalAIStack.exe" -ForegroundColor Green
    } else {
        Write-Host "  Next: re-run setup.ps1 without -NoBuild to compile the launcher." -ForegroundColor Cyan
    }
    Write-Host "  Stack runs inside WSL distro '$Distro'." -ForegroundColor Green
}
Write-Host "  ============================================" -ForegroundColor DarkGray
Write-Host ""

exit $failures.Count
