# Binary download helpers for -Setup.
# Pulls pinned GitHub releases for Qdrant and llama.cpp (CUDA server).
# Every download verifies a SHA256 against hashes pinned in LocalAIStack.ps1.

function Invoke-SafeDownload {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$OutFile,
        [string]$Sha256   # expected lower-case hex; empty = skip verification (dev only)
    )
    try {
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    } catch {
        Write-Host "   !! download failed: $($_.Exception.Message)" -ForegroundColor Yellow
        return $false
    }
    if ($Sha256) {
        $actual = (Get-FileHash -Algorithm SHA256 -Path $OutFile).Hash.ToLower()
        $expected = $Sha256.ToLower()
        if ($actual -ne $expected) {
            Write-Host "   !! SHA256 mismatch for $OutFile" -ForegroundColor Red
            Write-Host "      expected: $expected"
            Write-Host "      actual:   $actual"
            Remove-Item $OutFile -Force -ErrorAction SilentlyContinue
            return $false
        }
        Write-Host "   ok SHA256 verified ($actual)" -ForegroundColor Green
    } else {
        Write-Host "   !! SHA256 check skipped — set expected hash before shipping" -ForegroundColor Yellow
    }
    return $true
}

function Invoke-DownloadQdrant {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Version,
        [Parameter(Mandatory)][string]$Dest,
        [string]$Sha256
    )
    $exe = Join-Path $Dest 'qdrant.exe'
    if (Test-Path $exe) {
        Write-Host "   ok qdrant already present at $exe" -ForegroundColor Green
        return
    }
    if (-not (Test-Path $Dest)) { New-Item -ItemType Directory -Path $Dest -Force | Out-Null }
    $asset = "qdrant-x86_64-pc-windows-msvc.zip"
    $url = "https://github.com/qdrant/qdrant/releases/download/$Version/$asset"
    $zip = Join-Path $Dest 'qdrant.zip'
    Write-Host "==> Downloading Qdrant $Version from $url" -ForegroundColor Cyan
    if (-not (Invoke-SafeDownload -Url $url -OutFile $zip -Sha256 $Sha256)) {
        Write-Host "      Set LAI_QDRANT_VERSION to a valid release tag or drop qdrant.exe at $Dest manually." -ForegroundColor Yellow
        return
    }
    Expand-Archive -Path $zip -DestinationPath $Dest -Force
    Remove-Item $zip -Force
    if (-not (Test-Path $exe)) {
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
        [Parameter(Mandatory)][string]$Dest,
        [string]$Sha256
    )
    $exe = Join-Path $Dest 'llama-server.exe'
    if (Test-Path $exe) {
        Write-Host "   ok llama-server already present at $exe" -ForegroundColor Green
        return
    }
    if (-not (Test-Path $Dest)) { New-Item -ItemType Directory -Path $Dest -Force | Out-Null }
    $asset = "llama-$Version-bin-win-cuda-x64.zip"
    $url = "https://github.com/ggml-org/llama.cpp/releases/download/$Version/$asset"
    $zip = Join-Path $Dest 'llama.zip'
    Write-Host "==> Downloading llama.cpp $Version from $url" -ForegroundColor Cyan
    if (-not (Invoke-SafeDownload -Url $url -OutFile $zip -Sha256 $Sha256)) {
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
