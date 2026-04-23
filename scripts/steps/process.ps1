# Process helpers — spawn background services with logs + tracked PIDs.
# Dot-sourced by LocalAIStack.ps1.

function Start-TrackedProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$Args = @(),
        [Parameter(Mandatory)][string]$LogDir,
        [string]$WorkDir,
        [hashtable]$Env
    )
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    $out = Join-Path $LogDir ("{0}.out.log" -f $Name)
    $err = Join-Path $LogDir ("{0}.err.log" -f $Name)

    $splat = @{
        FilePath               = $FilePath
        RedirectStandardOutput = $out
        RedirectStandardError  = $err
        PassThru               = $true
        WindowStyle            = 'Hidden'
    }
    if ($Args)    { $splat['ArgumentList'] = $Args }
    if ($WorkDir) { $splat['WorkingDirectory'] = $WorkDir }

    # Per-process env overrides scope — we can't rely on Start-Process passing
    # env cleanly, so we set/restore in the caller's scope.
    $restore = @{}
    if ($Env) {
        foreach ($k in $Env.Keys) {
            $restore[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
            Set-Item -Path "Env:$k" -Value $Env[$k]
        }
    }
    try {
        $p = Start-Process @splat
    } finally {
        foreach ($k in $restore.Keys) {
            if ($null -eq $restore[$k]) { Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue }
            else { Set-Item -Path "Env:$k" -Value $restore[$k] }
        }
    }
    return $p
}

function Wait-HealthOk {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string[]]$Urls,
        [int]$TimeoutSeconds = 120,
        [int]$IntervalSeconds = 2
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $pending = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($u in $Urls) { $null = $pending.Add($u) }

    while ($pending.Count -gt 0 -and (Get-Date) -lt $deadline) {
        foreach ($url in @($pending)) {
            try {
                $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 4 -ErrorAction Stop
                if ($r.StatusCode -lt 400) {
                    Write-Host "   ok $url" -ForegroundColor Green
                    $null = $pending.Remove($url)
                }
            } catch { }
        }
        if ($pending.Count -gt 0) { Start-Sleep -Seconds $IntervalSeconds }
    }
    foreach ($url in $pending) {
        Write-Host "   !! health timeout: $url" -ForegroundColor Yellow
    }
}
