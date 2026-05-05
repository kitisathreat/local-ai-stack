<#
.SYNOPSIS
  Provision the Cloudflare Access policy that gates stream.mylensandi.com to a single admin email.

.DESCRIPTION
  Creates a self-hosted Access application for stream.mylensandi.com plus an
  Allow policy whose Include rule is a one-email allowlist. Idempotent —
  if an application with the same domain already exists, the script reuses
  it and updates the policy instead of duplicating.

.PARAMETER Token
  Cloudflare API token. Falls back to $env:CF_API_TOKEN if omitted.
  Required scopes:
    Account -> Access: Apps and Policies -> Edit
  Create at https://dash.cloudflare.com/profile/api-tokens

.PARAMETER Email
  Email allowed by the policy. Default: kitisathreat@gmail.com.

.PARAMETER Domain
  Hostname to gate. Default: stream.mylensandi.com.

.PARAMETER SessionDuration
  Access session length. Default: 720h (30 days).

.EXAMPLE
  $env:CF_API_TOKEN = '<paste-token>'
  pwsh .\scripts\setup-stream-cf-access.ps1
#>
[CmdletBinding()]
param(
    [string]$Token = $env:CF_API_TOKEN,
    [string]$Email = 'kitisathreat@gmail.com',
    [string]$Domain = 'stream.mylensandi.com',
    [string]$SessionDuration = '720h',
    [string]$AppName = 'Jellyfin (stream)',
    [string]$PolicyName = 'kit only'
)

$ErrorActionPreference = 'Stop'

if (-not $Token) {
    throw "No API token. Set `$env:CF_API_TOKEN or pass -Token. Create one at https://dash.cloudflare.com/profile/api-tokens with 'Account -> Access: Apps and Policies -> Edit'."
}

# Hard-coded account: Kitisathreat@gmail.com's Account (only account on the user).
# Discovered via cloudflare MCP accounts_list; refresh via:
#   curl -H "Authorization: Bearer $env:CF_API_TOKEN" https://api.cloudflare.com/client/v4/accounts
$AccountId = '922210bc695af8c05e864dfb2491386f'

$Base = "https://api.cloudflare.com/client/v4/accounts/$AccountId/access"
$Headers = @{
    Authorization  = "Bearer $Token"
    'Content-Type' = 'application/json'
}

function Invoke-CF {
    param([string]$Method, [string]$Url, $Body)
    $args = @{ Method = $Method; Uri = $Url; Headers = $Headers }
    if ($Body) { $args.Body = ($Body | ConvertTo-Json -Depth 10 -Compress) }
    $resp = Invoke-RestMethod @args
    if (-not $resp.success) {
        $errs = ($resp.errors | ForEach-Object { "$($_.code): $($_.message)" }) -join '; '
        throw "Cloudflare API error: $errs"
    }
    return $resp.result
}

Write-Host "[1/3] Looking for existing Access app for $Domain ..." -ForegroundColor Cyan
$apps = Invoke-CF GET "$Base/apps"
$app = $apps | Where-Object { $_.domain -eq $Domain } | Select-Object -First 1

if ($app) {
    Write-Host "      Found existing app id=$($app.id). Updating settings." -ForegroundColor Yellow
    $body = @{
        name             = $AppName
        domain           = $Domain
        type             = 'self_hosted'
        session_duration = $SessionDuration
    }
    $app = Invoke-CF PUT "$Base/apps/$($app.id)" $body
} else {
    Write-Host "      None found. Creating." -ForegroundColor Cyan
    $body = @{
        name             = $AppName
        domain           = $Domain
        type             = 'self_hosted'
        session_duration = $SessionDuration
    }
    $app = Invoke-CF POST "$Base/apps" $body
}
Write-Host "      App id: $($app.id)" -ForegroundColor Green
Write-Host "      Domain: $($app.domain)" -ForegroundColor Green

Write-Host "[2/3] Configuring Allow policy '$PolicyName' for $Email ..." -ForegroundColor Cyan
$policies = Invoke-CF GET "$Base/apps/$($app.id)/policies"
$policy = $policies | Where-Object { $_.name -eq $PolicyName } | Select-Object -First 1

$policyBody = @{
    name      = $PolicyName
    decision  = 'allow'
    include   = @(@{ email = @{ email = $Email } })
    precedence = 1
}

if ($policy) {
    Write-Host "      Found existing policy id=$($policy.id). Updating." -ForegroundColor Yellow
    $policy = Invoke-CF PUT "$Base/apps/$($app.id)/policies/$($policy.id)" $policyBody
} else {
    Write-Host "      None found. Creating." -ForegroundColor Cyan
    $policy = Invoke-CF POST "$Base/apps/$($app.id)/policies" $policyBody
}
Write-Host "      Policy id: $($policy.id)" -ForegroundColor Green
Write-Host "      Allowed:   $Email" -ForegroundColor Green

Write-Host "[3/3] Verifying — anonymous request to https://$Domain should now hit the CF Access login ..." -ForegroundColor Cyan
try {
    $r = Invoke-WebRequest -Uri "https://$Domain/System/Info/Public" -UseBasicParsing -TimeoutSec 15 -MaximumRedirection 0 -ErrorAction Stop
    Write-Warning "  Anonymous request returned $($r.StatusCode). Either CF Access is still propagating (give it a minute) or the policy didn't apply."
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code -eq 302) {
        $loc = $_.Exception.Response.Headers['Location']
        if ($loc -match 'cloudflareaccess\.com') {
            Write-Host "      OK — CF Access is gating the origin (302 -> $loc)" -ForegroundColor Green
        } else {
            Write-Warning "      302, but redirect target is unexpected: $loc"
        }
    } elseif ($code -eq 401 -or $code -eq 403) {
        Write-Host "      OK — origin is gated (got $code without an Access JWT)" -ForegroundColor Green
    } else {
        Write-Warning "      Unexpected status $code. Verify in dashboard."
    }
}

Write-Host ""
Write-Host "Done. Open https://$Domain in a fresh browser; sign in as $Email when prompted." -ForegroundColor Cyan
