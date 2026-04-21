param([string]$RepoRoot)
. (Join-Path $PSScriptRoot "_common.ps1")

$distro = "Ubuntu"

# Opt into the public Cloudflare tunnel profile when the token is configured
$profileFlag = ""
$envLocal = Join-Path $RepoRoot ".env.local"
if (Test-Path $envLocal) {
    if (Select-String -Path $envLocal -Pattern "^CLOUDFLARE_TUNNEL_TOKEN=.+$" -Quiet) {
        $profileFlag = "--profile public"
    }
}

# Translate the Windows repo path to /mnt/c/... for WSL
$wslRoot = (& wsl.exe -d $distro -- wslpath -u "$RepoRoot" 2>&1 | Select-Object -First 1).Trim()
if (-not $wslRoot) {
    Emit-Result -Ok $false -Message "Failed to translate '$RepoRoot' to a WSL path."
    exit 0
}

# Source .env.local so AUTH_SECRET_KEY and friends are exported before compose
# interpolates them. Also set CLOUDFLARE_TUNNEL_TOKEN to a placeholder when
# unset: the service is gated behind --profile public, but compose validates
# the ${VAR:?...} syntax even for inactive services.
$cmd = "cd '$wslRoot' && " +
       "set -a; [ -f .env.local ] && . ./.env.local; set +a; " +
       "export CLOUDFLARE_TUNNEL_TOKEN=`"`${CLOUDFLARE_TUNNEL_TOKEN:-_disabled_}`"; " +
       "docker compose $profileFlag up -d"

$output = & wsl.exe -d $distro -- bash -c $cmd 2>&1
$code = $LASTEXITCODE
$output | ForEach-Object { [Console]::Error.WriteLine($_) }

if ($code -eq 0) {
    $note = if ($profileFlag) { " (public tunnel enabled)" } else { "" }
    Emit-Result -Ok $true -Message "docker compose up -d completed$note"
} else {
    Emit-Result -Ok $false `
        -Message "docker compose up failed (exit $code). See launcher.log for details."
}
