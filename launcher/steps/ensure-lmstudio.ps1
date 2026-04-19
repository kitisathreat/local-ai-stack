param([string]$RepoRoot)
. (Join-Path $PSScriptRoot "_common.ps1")

if (-not (Test-CommandExists "lms")) {
    Emit-Result -Ok $false -NeedsUser $true `
        -Message "LM Studio CLI (lms) is required. Click Download to install LM Studio, then run ``lms bootstrap`` once in its terminal." `
        -ActionLabel "Download" -ActionUrl "https://lmstudio.ai/"
    exit 0
}

# Already running?
$status = & lms server status 2>&1
if ($status -match "running") {
    Emit-Result -Ok $true -Message "LM Studio server already running on :1234"
    exit 0
}

# Start it silently
$null = Start-Process -FilePath "lms" -ArgumentList "server","start","--port","1234","--cors" `
    -WindowStyle Hidden -PassThru -RedirectStandardOutput "NUL" -RedirectStandardError "NUL"

# Poll until it responds
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-WebRequest "http://localhost:1234/v1/models" -TimeoutSec 1 -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -eq 200) {
            Emit-Result -Ok $true -Message "LM Studio server ready on :1234"
            exit 0
        }
    } catch {}
}

Emit-Result -Ok $false `
    -Message "LM Studio CLI is installed but the server did not respond on :1234. Open LM Studio once and keep it running, then retry."
