param([string]$RepoRoot)
. (Join-Path $PSScriptRoot "_common.ps1")

Push-Location $RepoRoot
try {
    $output = & docker compose up -d 2>&1
    $code   = $LASTEXITCODE
    $output | ForEach-Object { [Console]::Error.WriteLine($_) }
    if ($code -eq 0) {
        Emit-Result -Ok $true -Message "docker compose up -d completed"
    } else {
        Emit-Result -Ok $false `
            -Message "docker compose up failed (exit $code). See launcher.log for details."
    }
} finally {
    Pop-Location
}
