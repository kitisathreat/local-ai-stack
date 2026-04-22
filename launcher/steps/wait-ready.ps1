param([string]$RepoRoot)
. (Join-Path $PSScriptRoot "_common.ps1")

# Master's architecture:
#   - backend (FastAPI) on :8000 with /healthz
#   - frontend (nginx-served Preact) on :3000
$checks = @(
    @{ Name = "backend";  Url = "http://localhost:18000/healthz" },
    @{ Name = "frontend"; Url = "http://localhost:3000/" }
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
