# Diagnostic preflight for `LocalAIStack.ps1 -Start`.
#
# Aggregates every "missing component" check into a single pass so the
# user gets ONE error dialog listing everything to fix, rather than
# bouncing off the first failure and replaying after each fix.
#
# Returns a hashtable:
#   @{
#     ok       = bool        # all required checks passed
#     errors   = @(...)      # required-component failures (block startup)
#     warnings = @(...)      # nice-to-haves (don't block startup)
#     suggestion = string    # next-step the dialog can show
#   }
#
# Used by Invoke-Start before any subprocess is spawned.

function Invoke-Preflight {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$VendorDir,
        [Parameter(Mandatory)][string]$DataDir,
        [Parameter(Mandatory)][string]$EnvFile
    )

    $errors   = New-Object System.Collections.Generic.List[string]
    $warnings = New-Object System.Collections.Generic.List[string]

    # ── 1. Backend Python venv ───────────────────────────────────────
    $py = Join-Path $VendorDir 'venv-backend\Scripts\python.exe'
    if (-not (Test-Path $py)) {
        $errors.Add("Backend Python environment missing: $py")
    }

    # ── 2. GUI Python venv ───────────────────────────────────────────
    $guiPy = Join-Path $VendorDir 'venv-gui\Scripts\pythonw.exe'
    if (-not (Test-Path $guiPy)) {
        $warnings.Add("GUI Python environment missing: $guiPy (admin window unavailable)")
    }

    # ── 3. llama-server binary ───────────────────────────────────────
    $llama = Join-Path $VendorDir 'llama-server\llama-server.exe'
    if (-not (Test-Path $llama)) {
        $errors.Add("llama-server binary missing: $llama")
    }

    # ── 4. CUDA 12 runtime DLLs ──────────────────────────────────────
    # llama-server is a CUDA build and exits 0xC0000135 silently if
    # cudart/cublas DLLs aren't on disk. Same probe Setup uses.
    if (Get-Command Test-CudaRuntimeAvailable -ErrorAction SilentlyContinue) {
        if (-not (Test-CudaRuntimeAvailable -VendorDir (Join-Path $VendorDir 'llama-server'))) {
            $errors.Add(
                "CUDA 12 runtime DLLs not found. Re-run the installer to fetch them, " +
                "or install the NVIDIA CUDA 12 Toolkit."
            )
        }
    }

    # ── 5. Qdrant binary ─────────────────────────────────────────────
    $qdrant = Join-Path $VendorDir 'qdrant\qdrant.exe'
    if (-not (Test-Path $qdrant)) {
        $warnings.Add("qdrant binary missing — RAG features will be unavailable.")
    }

    # ── 6. .env file with required keys ──────────────────────────────
    if (-not (Test-Path $EnvFile)) {
        $errors.Add(".env file missing at $EnvFile — first-time setup didn't finish.")
    } else {
        $envContent = Get-Content $EnvFile -Raw -ErrorAction SilentlyContinue
        foreach ($key in @('AUTH_SECRET_KEY','HISTORY_SECRET_KEY')) {
            if (-not ($envContent -match "(?m)^${key}=\S")) {
                $errors.Add("$key missing or empty in .env — re-run setup wizard.")
            }
        }
    }

    # ── 7. Admin user exists in the database ─────────────────────────
    if (Test-Path $py) {
        Push-Location $RepoRoot
        try {
            & $py -m backend.seed_admin --check-only 2>$null
            if ($LASTEXITCODE -ne 0) {
                $errors.Add("No admin user in database — re-run setup wizard.")
            }
        } catch {
            # Swallow; if the venv import fails, the (1) check above caught it.
        } finally {
            Pop-Location
        }
    }

    # ── 8. NVIDIA driver present (warn-only — CPU mode works) ────────
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        $smi = & nvidia-smi 2>$null
        if ($LASTEXITCODE -ne 0) {
            $warnings.Add("nvidia-smi failed — GPU acceleration unavailable.")
        }
    } else {
        $warnings.Add("nvidia-smi not on PATH — assuming CPU-only mode.")
    }

    # ── 9. Required ports free ───────────────────────────────────────
    foreach ($pair in @(
        @{Port=18000; Name='backend'},
        @{Port=6333;  Name='qdrant'},
        @{Port=8090;  Name='embedding tier'},
        @{Port=8001;  Name='vision tier'},
        @{Port=8010;  Name='highest_quality tier'},
        @{Port=8011;  Name='versatile tier'},
        @{Port=8012;  Name='fast tier'},
        @{Port=8013;  Name='coding tier'}
    )) {
        try {
            $listener = Get-NetTCPConnection -LocalPort $pair.Port -State Listen -ErrorAction SilentlyContinue
            if ($listener) {
                # Filter out our own services (the launcher might be re-running).
                $owners = ($listener | ForEach-Object { (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName } | Sort-Object -Unique) -join ', '
                if ($owners -and $owners -notmatch '^(python|pythonw|qdrant|llama-server|jupyter|jupyter-lab)$') {
                    $warnings.Add(
                        "Port $($pair.Port) ($($pair.Name)) is already in use by: $owners. " +
                        "The $($pair.Name) service may fail to start."
                    )
                }
            }
        } catch {
            # Get-NetTCPConnection isn't available on every Windows SKU;
            # silently skip if so.
        }
    }

    # ── 10. At least the embedding GGUF on disk ──────────────────────
    # Other tiers cold-spawn on demand; embedding is required for RAG/memory.
    $embedding = Join-Path $DataDir 'models\embedding.gguf'
    if (-not (Test-Path $embedding)) {
        $warnings.Add(
            "Embedding GGUF not yet on disk — RAG and memory disabled until model_resolver finishes pulling."
        )
    }

    $suggestion = ""
    if ($errors.Count -gt 0) {
        # The installer EXE is not bundled into the install dir anymore;
        # the user re-runs the original LocalAIStackInstaller-<ver>.exe
        # to repair. From inside the install we can also self-repair via
        # `LocalAIStack.exe -SetupGui` (no admin) for .env edits, or
        # `-Setup` (self-elevates) for full vendor re-fetch.
        $suggestion = "Re-run the Local AI Stack installer to repair, " +
                      "or run 'LocalAIStack.exe -Setup' from an elevated prompt."
    }

    return @{
        ok         = ($errors.Count -eq 0)
        errors     = $errors
        warnings   = $warnings
        suggestion = $suggestion
    }
}

function Show-PreflightDialog {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][hashtable]$Result
    )
    Add-Type -AssemblyName PresentationFramework -ErrorAction SilentlyContinue

    $title = if ($Result.ok) { 'Local AI Stack — startup' } else { 'Local AI Stack — startup blocked' }
    $lines = New-Object System.Collections.Generic.List[string]
    if ($Result.errors.Count -gt 0) {
        $lines.Add('The following components are missing or misconfigured:'); $lines.Add('')
        foreach ($e in $Result.errors) { $lines.Add("  • $e") }
        $lines.Add('')
    }
    if ($Result.warnings.Count -gt 0) {
        $lines.Add('Warnings (the stack will still start):'); $lines.Add('')
        foreach ($w in $Result.warnings) { $lines.Add("  • $w") }
        $lines.Add('')
    }
    if ($Result.suggestion) { $lines.Add($Result.suggestion) }

    $body = ($lines -join "`n")
    if ($Result.ok -and $Result.warnings.Count -eq 0) {
        return   # Silent success — most common path on a healthy install.
    }
    try {
        $icon = if ($Result.ok) { [System.Windows.MessageBoxImage]::Information } else { [System.Windows.MessageBoxImage]::Error }
        [System.Windows.MessageBox]::Show($body, $title, [System.Windows.MessageBoxButton]::OK, $icon) | Out-Null
    } catch {
        # No WPF assemblies (rare on Windows Server / Core) — fall back
        # to console output. The launcher already prints to stdout for
        # console-mode invocations.
        Write-Host $title -ForegroundColor Red
        Write-Host $body
    }
}
