param([string]$RepoRoot)
. (Join-Path $PSScriptRoot "_common.ps1")

# Pull in --profile public when CLOUDFLARE_TUNNEL_TOKEN is set, so the
# cloudflared service starts automatically. Otherwise, run local-only.
$envLocal = Join-Path $RepoRoot ".env.local"
$profileArgs = @()
if (Test-Path $envLocal) {
    if (Select-String -Path $envLocal -Pattern "^CLOUDFLARE_TUNNEL_TOKEN=.+$" -Quiet) {
        $profileArgs = @("--profile", "public")
    }
}

Push-Location $RepoRoot
try {
    $args = $profileArgs + @("up", "-d")
    $output = & docker compose @args 2>&1
    $code   = $LASTEXITCODE
    $output | ForEach-Object { [Console]::Error.WriteLine($_) }
    if ($code -eq 0) {
        $note = if ($profileArgs.Count -gt 0) { " (public tunnel enabled)" } else { "" }
        Emit-Result -Ok $true -Message "docker compose up -d completed$note"
    } else {
        Emit-Result -Ok $false `
            -Message "docker compose up failed (exit $code). See launcher.log for details."
    }
} finally {
    Pop-Location
}
