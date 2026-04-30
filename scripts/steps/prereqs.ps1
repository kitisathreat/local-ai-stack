# Prerequisites bootstrap — run as the first step of Invoke-Setup.
# Verifies Windows build, detects NVIDIA (never auto-installs drivers),
# and winget-installs the tools we require. Idempotent.

# Tools we expect on PATH after -Setup. Each entry is:
#   @{ Id = '<winget id>'; Exe = '<binary name>'; Label = '<pretty>' }
$Script:PrereqTools = @(
    @{ Id = 'Git.Git';                Exe = 'git';         Label = 'Git' }
    @{ Id = 'Python.Python.3.12';     Exe = 'python';      Label = 'Python 3.12' }
    @{ Id = 'Microsoft.PowerShell';   Exe = 'pwsh';        Label = 'PowerShell 7' }
    @{ Id = 'Cloudflare.cloudflared'; Exe = 'cloudflared'; Label = 'cloudflared' }
)

function Test-WingetAvailable {
    return [bool](Get-Command winget -ErrorAction SilentlyContinue)
}

function Refresh-SessionPath {
    # winget installs land in Machine PATH but the current session keeps a
    # cached copy. Rebuild $env:Path from Machine + User so Get-Command
    # picks up newly-installed binaries without a shell restart.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($machine -and $user) {
        $env:Path = "$machine;$user"
    } elseif ($machine) {
        $env:Path = $machine
    }
}

function Invoke-WingetInstall {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Id,
        [Parameter(Mandatory)][string]$Label
    )
    Write-Host "   .. installing $Label ($Id) via winget — UAC prompt incoming" -ForegroundColor Cyan
    $args = @(
        'install', '--id', $Id,
        '--silent',
        '--accept-source-agreements',
        '--accept-package-agreements',
        '--disable-interactivity'
    )
    & winget @args
    $code = $LASTEXITCODE
    if ($code -ne 0 -and $code -ne -1978335189) {
        # -1978335189 = APPINSTALLER_CLI_ERROR_UPDATE_NOT_APPLICABLE (already up-to-date)
        Write-Host "   !! winget install $Id returned exit code $code" -ForegroundColor Yellow
        return $false
    }
    Refresh-SessionPath
    return $true
}

function Test-WindowsVersion {
    $build = [int][System.Environment]::OSVersion.Version.Build
    if ($build -lt 19041) {
        Write-Host "   xx Windows build $build is too old (need 19041 / 2004+)" -ForegroundColor Red
        Write-Host "      Upgrade Windows 10 to 20H1 or newer, or run Windows 11." -ForegroundColor Red
        return $false
    }
    Write-Host "   ok Windows build $build" -ForegroundColor Green
    return $true
}

function Test-NvidiaDriver {
    # Detect-only: never auto-install NVIDIA drivers. Huge installer,
    # product-specific, and installing silently can wedge the graphics
    # stack. Print guidance if missing and let the user decide.
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $smi) {
        Write-Host "   !! nvidia-smi not found on PATH" -ForegroundColor Yellow
        Write-Host "      GPU tiers will not work without an NVIDIA driver >= 550." -ForegroundColor Yellow
        Write-Host "      Download: https://www.nvidia.com/Download/index.aspx" -ForegroundColor Yellow
        Write-Host "      CUDA 12 runtime is bundled with the driver." -ForegroundColor Yellow
        return $false
    }
    try {
        $raw = (& nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>&1 | Select-Object -First 1).Trim()
    } catch {
        Write-Host "   !! nvidia-smi errored: $($_.Exception.Message)" -ForegroundColor Yellow
        return $false
    }
    if (-not $raw) {
        Write-Host "   !! nvidia-smi returned no driver version" -ForegroundColor Yellow
        return $false
    }
    $major = 0
    if ($raw -match '^(\d+)') { $major = [int]$Matches[1] }
    if ($major -lt 550) {
        Write-Host "   xx NVIDIA driver $raw is too old (need >= 550 for CUDA 12 runtime)" -ForegroundColor Red
        Write-Host "      Update: https://www.nvidia.com/Download/index.aspx" -ForegroundColor Yellow
        return $false
    }
    Write-Host "   ok NVIDIA driver $raw" -ForegroundColor Green

    if (-not (Get-Command nvcc -ErrorAction SilentlyContinue)) {
        # Informational only — the CUDA runtime is bundled with the driver,
        # so `nvcc` missing just means the CUDA toolkit (compiler) isn't
        # installed. We don't need it.
        Write-Host "   .. nvcc not on PATH (fine — we only need the CUDA runtime)" -ForegroundColor DarkGray
    }
    return $true
}

function Test-ExecutionPolicy {
    $pol = Get-ExecutionPolicy -Scope CurrentUser
    if ($pol -in @('Restricted','Undefined')) {
        Write-Host "   .. setting CurrentUser execution policy to RemoteSigned" -ForegroundColor Cyan
        Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force
    } else {
        Write-Host "   ok execution policy: $pol" -ForegroundColor Green
    }
}

function Invoke-EnsurePrereqs {
    [CmdletBinding()]
    param([switch]$FailOnMissingGpu)

    Write-Host "==> Verifying prerequisites" -ForegroundColor Cyan

    $results = [ordered]@{}

    # ── Windows version (hard gate) ────────────────────────────────────
    if (-not (Test-WindowsVersion)) { throw "Windows version check failed." }
    $results['Windows'] = 'ok'

    # ── Execution policy ──────────────────────────────────────────────
    Test-ExecutionPolicy
    $results['ExecutionPolicy'] = 'ok'

    # ── NVIDIA driver (soft gate unless -FailOnMissingGpu) ───────────
    if (Test-NvidiaDriver) {
        $results['NVIDIA'] = 'ok'
    } else {
        $results['NVIDIA'] = 'warn'
        if ($FailOnMissingGpu) {
            throw "NVIDIA driver check failed."
        }
    }

    # ── winget tools ───────────────────────────────────────────────────
    if (-not (Test-WingetAvailable)) {
        Write-Host "   xx winget is not installed. Update Windows via the Store (App Installer)." -ForegroundColor Red
        throw "winget missing."
    }

    foreach ($tool in $PrereqTools) {
        if (Get-Command $tool.Exe -ErrorAction SilentlyContinue) {
            Write-Host "   ok $($tool.Label) already installed" -ForegroundColor Green
            $results[$tool.Label] = 'present'
            continue
        }
        if (Invoke-WingetInstall -Id $tool.Id -Label $tool.Label) {
            if (Get-Command $tool.Exe -ErrorAction SilentlyContinue) {
                Write-Host "   ok $($tool.Label) installed" -ForegroundColor Green
                $results[$tool.Label] = 'installed'
            } else {
                Write-Host "   !! $($tool.Label) install reported success but binary not on PATH" -ForegroundColor Yellow
                Write-Host "      Open a new PowerShell window and re-run -Setup." -ForegroundColor Yellow
                $results[$tool.Label] = 'path-missing'
            }
        } else {
            $results[$tool.Label] = 'failed'
        }
    }

    # ── Report ────────────────────────────────────────────────────────
    Write-Host ""
    Write-Host "Prerequisites summary:" -ForegroundColor Cyan
    foreach ($k in $results.Keys) {
        $v = $results[$k]
        $color = switch ($v) {
            'ok'          { 'Green' }
            'present'     { 'Green' }
            'installed'   { 'Green' }
            'warn'        { 'Yellow' }
            'path-missing'{ 'Yellow' }
            'failed'      { 'Red' }
            default       { 'White' }
        }
        Write-Host ("   {0,-20} {1}" -f $k, $v) -ForegroundColor $color
    }
    Write-Host ""

    $hardFails = @($results.Values | Where-Object { $_ -in @('failed') })
    if ($hardFails.Count -gt 0) {
        throw "Prerequisite install failed for $($hardFails.Count) package(s); fix and re-run -Setup."
    }
}
