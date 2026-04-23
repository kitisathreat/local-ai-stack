# Binary download helpers for -Setup.
# Pulls pinned GitHub releases for Qdrant and llama.cpp (CUDA server).

function Invoke-DownloadQdrant {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Version,
        [Parameter(Mandatory)][string]$Dest
    )
    $exe = Join-Path $Dest 'qdrant.exe'
    if (Test-Path $exe) {
        Write-Host "   ok qdrant already present at $exe" -ForegroundColor Green
        return
    }
    if (-not (Test-Path $Dest)) { New-Item -ItemType Directory -Path $Dest -Force | Out-Null }
    # Qdrant Windows release asset name varies; try the current (v1.12+) layout first.
    $asset = "qdrant-x86_64-pc-windows-msvc.zip"
    $url = "https://github.com/qdrant/qdrant/releases/download/$Version/$asset"
    $zip = Join-Path $Dest 'qdrant.zip'
    Write-Host "==> Downloading Qdrant $Version from $url" -ForegroundColor Cyan
    try {
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    } catch {
        Write-Host "   !! Qdrant download failed: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "      Set LAI_QDRANT_VERSION to a valid release tag or drop qdrant.exe at $Dest manually." -ForegroundColor Yellow
        return
    }
    Expand-Archive -Path $zip -DestinationPath $Dest -Force
    Remove-Item $zip -Force
    if (-not (Test-Path $exe)) {
        # Some asset layouts unpack into a subfolder — flatten.
        $found = Get-ChildItem -Path $Dest -Filter qdrant.exe -Recurse | Select-Object -First 1
        if ($found) { Move-Item $found.FullName $exe }
    }
    if (Test-Path $exe) {
        Write-Host "   ok Qdrant installed at $exe" -ForegroundColor Green
    } else {
        Write-Host "   !! Qdrant archive did not contain qdrant.exe" -ForegroundColor Yellow
    }
}

function Invoke-DownloadLlamaServer {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Version,
        [Parameter(Mandatory)][string]$Dest
    )
    $exe = Join-Path $Dest 'llama-server.exe'
    if (Test-Path $exe) {
        Write-Host "   ok llama-server already present at $exe" -ForegroundColor Green
        return
    }
    if (-not (Test-Path $Dest)) { New-Item -ItemType Directory -Path $Dest -Force | Out-Null }
    # llama.cpp publishes CUDA wheels as `llama-<tag>-bin-win-cuda-x64.zip`.
    $asset = "llama-$Version-bin-win-cuda-x64.zip"
    $url = "https://github.com/ggml-org/llama.cpp/releases/download/$Version/$asset"
    $zip = Join-Path $Dest 'llama.zip'
    Write-Host "==> Downloading llama.cpp $Version from $url" -ForegroundColor Cyan
    try {
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    } catch {
        Write-Host "   !! llama.cpp download failed: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "      Set LAI_LLAMACPP_VERSION to a valid release tag." -ForegroundColor Yellow
        return
    }
    Expand-Archive -Path $zip -DestinationPath $Dest -Force
    Remove-Item $zip -Force
    if (-not (Test-Path $exe)) {
        $found = Get-ChildItem -Path $Dest -Filter llama-server.exe -Recurse | Select-Object -First 1
        if ($found) { Copy-Item $found.FullName $exe }
    }
    if (Test-Path $exe) {
        Write-Host "   ok llama-server installed at $exe" -ForegroundColor Green
    } else {
        Write-Host "   !! llama.cpp archive did not contain llama-server.exe" -ForegroundColor Yellow
    }
}
