param([string]$RepoRoot)
. (Join-Path $PSScriptRoot "_common.ps1")

$dockerExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"

# Is the daemon already reachable?
$null = & docker ps 2>&1
if ($LASTEXITCODE -eq 0) {
    Emit-Result -Ok $true -Message "Docker daemon reachable"
    exit 0
}

if (-not (Test-Path $dockerExe)) {
    Emit-Result -Ok $false -NeedsUser $true `
        -Message "Docker Desktop is required but not installed. Click Install to download it, then re-run LocalAIStack." `
        -ActionLabel "Download" -ActionUrl "https://www.docker.com/products/docker-desktop/"
    exit 0
}

Start-Process -FilePath $dockerExe -WindowStyle Hidden

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 3
    $null = & docker ps 2>&1
    if ($LASTEXITCODE -eq 0) {
        Emit-Result -Ok $true -Message "Docker Desktop started ($($i*3 + 3)s)"
        exit 0
    }
}

Emit-Result -Ok $false `
    -Message "Docker Desktop did not become ready within 90 seconds. Check Docker Desktop manually, then retry."
