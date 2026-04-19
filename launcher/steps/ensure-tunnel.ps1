param([string]$RepoRoot)
. (Join-Path $PSScriptRoot "_common.ps1")

$configFile     = Join-Path $RepoRoot "cloudflare\config.yml"
$credsDir       = Join-Path $env:USERPROFILE ".cloudflared"
$anyCreds       = $false
if (Test-Path $credsDir) {
    $anyCreds = [bool](Get-ChildItem -Path $credsDir -Filter "*.json" -ErrorAction SilentlyContinue)
}

if (-not (Test-Path $configFile)) {
    Emit-Result -Ok $false -NeedsUser $true `
        -Message "Cloudflare tunnel config not found at cloudflare/config.yml. See launcher/README.md for first-time setup (tunnel create + route dns)." `
        -ActionLabel "Open Docs" -ActionUrl "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/"
    exit 0
}

if (-not $anyCreds) {
    Emit-Result -Ok $false -NeedsUser $true `
        -Message "Cloudflare tunnel not authorized yet. Click Authorize to run ``cloudflared tunnel login`` (one-time, opens a browser)." `
        -ActionLabel "Authorize" -ActionUrl "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/"
    exit 0
}

# cloudflared runs as a docker-compose service; nothing to spawn here.
Emit-Result -Ok $true -Message "Cloudflare tunnel credentials present"
