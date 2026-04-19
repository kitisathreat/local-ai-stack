param([string]$RepoRoot)
. (Join-Path $PSScriptRoot "_common.ps1")

# Master's Cloudflare Tunnel is configured via CLOUDFLARE_TUNNEL_TOKEN in
# .env.local and started as a compose profile. If the token is absent we run
# local-only (no error — the user may just want localhost access).
$envLocal = Join-Path $RepoRoot ".env.local"
if (-not (Test-Path $envLocal)) {
    Emit-Result -Ok $true -Message "No .env.local — running local-only (no public tunnel)"
    exit 0
}

$hasToken = (Select-String -Path $envLocal -Pattern "^CLOUDFLARE_TUNNEL_TOKEN=.+$" -Quiet)
if (-not $hasToken) {
    Emit-Result -Ok $true -Message "CLOUDFLARE_TUNNEL_TOKEN unset — running local-only (no public tunnel)"
    exit 0
}

Emit-Result -Ok $true -Message "Cloudflare Tunnel token present"
