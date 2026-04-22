param([string]$RepoRoot)
. (Join-Path $PSScriptRoot "_common.ps1")

# This launcher targets Docker Engine running inside a WSL2 Ubuntu distro
# (not Docker Desktop). Docker Desktop's AF_UNIX reparse-point sockets on
# Windows cause repeated startup crashes; Docker Engine in WSL doesn't.

$distro = "Ubuntu"

# -- WSL installed? ----------------------------------------------------------
if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
    Emit-Result -Ok $false -NeedsUser $true `
        -Message "WSL is not installed. Run setup.ps1 to install WSL + Docker Engine." `
        -ActionLabel "Open Setup Guide" `
        -ActionUrl "https://learn.microsoft.com/windows/wsl/install"
    exit 0
}

# -- Distro present? ---------------------------------------------------------
$distroList = (& wsl.exe -l -q 2>&1) -join "`n" -split "[\r\n]+" | ForEach-Object { $_.Trim() }
if (-not ($distroList -contains $distro)) {
    Emit-Result -Ok $false -NeedsUser $true `
        -Message "WSL distro '$distro' not found. Run setup.ps1 to install it."
    exit 0
}

# -- Docker reachable inside the distro? ------------------------------------
& wsl.exe -d $distro -- docker info 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Emit-Result -Ok $true -Message "Docker Engine reachable inside $distro"
    exit 0
}

# -- Try starting docker.service --------------------------------------------
& wsl.exe -d $distro -u root -- bash -c "systemctl start docker 2>/dev/null || service docker start" 2>&1 | Out-Null

for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep 2
    & wsl.exe -d $distro -- docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Emit-Result -Ok $true -Message "Docker Engine started ($($i*2 + 2)s)"
        exit 0
    }
}

Emit-Result -Ok $false `
    -Message "Docker Engine did not start inside $distro. Diagnostic: wsl -d $distro -- sudo journalctl -u docker -n 50"
