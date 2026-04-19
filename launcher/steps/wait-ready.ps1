param([string]$RepoRoot)
. (Join-Path $PSScriptRoot "_common.ps1")

$checks = @(
    @{ Name = "api";  Url = "http://localhost:8787/health" },
    @{ Name = "web";  Url = "http://localhost:3001/" }
)

$deadline = (Get-Date).AddSeconds(120)
foreach ($check in $checks) {
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest $check.Url -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) { $ready = $true; break }
        } catch {}
        Start-Sleep -Seconds 2
    }
    if (-not $ready) {
        Emit-Result -Ok $false -Message "Service '$($check.Name)' did not become ready at $($check.Url) within 120s."
        exit 0
    }
}

Emit-Result -Ok $true -Message "All services healthy"
