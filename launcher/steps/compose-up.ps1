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

# Build the bash script as a plain string (no special chars that could be
# mangled by PowerShell's native argument passing), then base64-encode it and
# decode inside WSL. This avoids every quoting/escaping hazard.
$bashLines = @(
    "#!/usr/bin/env bash",
    "set -euo pipefail",
    "cd '$wslRoot'",
    "set -a",
    "[ -f .env.local ] && . ./.env.local || true",
    "set +a",
    'CLOUDFLARE_TUNNEL_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-_disabled_}"',
    "export CLOUDFLARE_TUNNEL_TOKEN",
    "docker compose $profileFlag up -d"
)
$bashScript = $bashLines -join "`n"

# UTF-8 encode then base64 (no BOM)
$bytes = (New-Object System.Text.UTF8Encoding $false).GetBytes($bashScript)
$b64   = [Convert]::ToBase64String($bytes)

$output = & wsl.exe -d $distro -- bash -c "echo '$b64' | base64 -d | bash" 2>&1
$code   = $LASTEXITCODE
$output | ForEach-Object { [Console]::Error.WriteLine($_) }

if ($code -eq 0) {
    $note = if ($profileFlag) { " (public tunnel enabled)" } else { "" }
    Emit-Result -Ok $true -Message "docker compose up -d completed$note"
} else {
    Emit-Result -Ok $false `
        -Message "docker compose up failed (exit $code). See launcher.log for details."
}
