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

# ── CUDA runtime redist ─────────────────────────────────────────────────────
# llama-server.exe is built against CUDA 12 and dynamically loads
# cudart64_12.dll / cublas64_12.dll / cublasLt64_12.dll. A bare NVIDIA
# driver install does NOT ship these — only the CUDA Toolkit / runtime
# does. Without them the exe exits silently with STATUS_DLL_NOT_FOUND
# (0xC0000135) before printing anything, so we provision the redist
# ourselves alongside the binary.
#
# We only download when the DLLs aren't already discoverable, so users
# with a real CUDA Toolkit install don't pay the bandwidth.
function Test-CudaRuntimeAvailable {
    [CmdletBinding()]
    param(
        [string]$VendorDir,
        # llama.cpp's CUDA build is forward-compatible across CUDA 12.x
        # minor versions — any cudart64_12.dll on disk works.
        [string]$MajorVersion = '12'
    )
    $needed = @("cudart64_$MajorVersion.dll", "cublas64_$MajorVersion.dll", "cublasLt64_$MajorVersion.dll")
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($VendorDir -and (Test-Path $VendorDir)) { $candidates.Add($VendorDir) }
    foreach ($p in @($env:CUDA_PATH, $env:CUDA_HOME)) {
        if ($p) {
            $candidates.Add($p)
            $candidates.Add((Join-Path $p 'bin'))
        }
    }
    foreach ($entry in (($env:PATH -split ';') | Where-Object { $_ })) {
        $candidates.Add($entry)
    }
    foreach ($dll in $needed) {
        $found = $false
        foreach ($dir in ($candidates | Select-Object -Unique)) {
            $full = Join-Path $dir $dll
            if (Test-Path $full -PathType Leaf) { $found = $true; break }
        }
        if (-not $found) { return $false }
    }
    return $true
}

function Invoke-DownloadCudaRuntime {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$LlamaCppVersion,
        [Parameter(Mandatory)][string]$Dest,
        [string]$Sha256
    )
    if (Test-CudaRuntimeAvailable -VendorDir $Dest) {
        Write-Host "   ok CUDA 12 runtime DLLs already discoverable (system or vendor)" -ForegroundColor Green
        return
    }
    if (-not (Test-Path $Dest)) { New-Item -ItemType Directory -Path $Dest -Force | Out-Null }
    # ggml-org/llama.cpp ships the matching cudart redist on every release.
    $asset = "cudart-llama-bin-win-cu12.4-x64.zip"
    $url = "https://github.com/ggml-org/llama.cpp/releases/download/$LlamaCppVersion/$asset"
    $zip = Join-Path $Dest 'cudart.zip'
    Write-Host "==> Downloading CUDA runtime redist from $url" -ForegroundColor Cyan
    if (-not (Invoke-SafeDownload -Url $url -OutFile $zip -Sha256 $Sha256)) {
        Write-Host "      llama-server requires CUDA 12 runtime DLLs. Install the" -ForegroundColor Yellow
        Write-Host "      CUDA 12 Toolkit from NVIDIA, or drop cudart64_12.dll +" -ForegroundColor Yellow
        Write-Host "      cublas64_12.dll + cublasLt64_12.dll into $Dest manually." -ForegroundColor Yellow
        return
    }
    Expand-Archive -Path $zip -DestinationPath $Dest -Force
    Remove-Item $zip -Force
    if (Test-CudaRuntimeAvailable -VendorDir $Dest) {
        Write-Host "   ok CUDA runtime DLLs installed in $Dest" -ForegroundColor Green
    } else {
        Write-Host "   !! CUDA archive extracted but expected DLLs not found" -ForegroundColor Yellow
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
    $asset = "llama-$Version-bin-win-cuda-cu12.4-x64.zip"
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
