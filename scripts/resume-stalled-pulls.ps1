#Requires -Version 7
<#
.SYNOPSIS
  Watchdog that monitors HF's XetHub CDN reachability and auto-restarts
  stalled tier pulls when it recovers.

.DESCRIPTION
  When the XetHub host (cas-bridge.xethub.hf.co) goes unreachable from
  the user's network — DNS timeout / temporary outage — every in-flight
  HF pull bails out after exhausting its retry budget. The .incomplete
  blobs are preserved in `data/models/.cache/huggingface/download/` so
  resuming on the next pull picks up where it left off, but somebody
  still has to *trigger* the resume.

  This script polls DNS every -PollSeconds (default 60). When the host
  resolves successfully, it walks the configured tier list, checks
  which ones still don't have a fully-resolved `data/models/<tier>.gguf`
  symlink, and kicks off a `model_resolver resolve --pull --tier <name>`
  for each, hidden + backgrounded so no console window flashes.

  Exits cleanly when every tier in -Tiers has a resolved symlink.

  Designed to run from PowerShell directly OR be wrapped in a Windows
  Scheduled Task with -WindowStyle Hidden so it sits silently in the
  background restoring downloads after transient HF outages.

.PARAMETER Tiers
  Tier names to watch. Default: reasoning_max, reasoning_xl, frontier
  (the three currently in flight).

.PARAMETER PollSeconds
  How often to test DNS. Default 60 s — short enough to catch
  recoveries promptly, long enough that the script costs essentially
  nothing while idle.

.PARAMETER MaxHours
  Hard cap on how long the watchdog runs. Default 12 h — covers the
  worst-case overnight pull. Exits non-zero if the cap is hit with
  any tier still pending.

.EXAMPLE
  pwsh .\scripts\resume-stalled-pulls.ps1
  pwsh .\scripts\resume-stalled-pulls.ps1 -Tiers reasoning_max
  pwsh .\scripts\resume-stalled-pulls.ps1 -PollSeconds 30 -MaxHours 24
#>

[CmdletBinding()]
param(
    [string[]]$Tiers = @('reasoning_max', 'reasoning_xl', 'frontier'),
    [int]$PollSeconds = 60,
    [int]$MaxHours = 12,
    # How long a tier's bytes-on-disk can stagnate before the watchdog
    # treats the resolver as "stuck" (TCP frozen, hf_hub_download in
    # backoff sleep, etc.) and kills it for respawn. 600 s = 10 min,
    # matches the user's "monitor every 10 min" instruction.
    [int]$ProgressStallSeconds = 600,
    # When DNS resolution fails, fall back to public DNS servers
    # (8.8.8.8 / 1.1.1.1) to confirm the upstream is actually up. If
    # public DNS works, the broken piece is a local resolver — most
    # commonly ProtonVPN's loopback proxy at 127.0.2.2/.3 going stale.
    # Pass -RestartProtonVPN to also try `Restart-Service ProtonVPN*`
    # in that case (best-effort; service restart needs the user's
    # implicit consent — bury this behind an opt-in flag).
    [switch]$RestartProtonVPN
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$Py = Join-Path $RepoRoot 'vendor\venv-backend\Scripts\python.exe'
$LogDir = Join-Path $env:APPDATA 'LocalAIStack'
$null = New-Item -ItemType Directory -Path $LogDir -Force -ErrorAction SilentlyContinue
$LogPath = Join-Path $LogDir 'resume-stalled-pulls.log'

function Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $LogPath -Value $line -Encoding utf8
    Write-Host $line
}

# Source HF_TOKEN from .env so the spawned resolver runs can authenticate.
$envPath = Join-Path $RepoRoot '.env'
if (Test-Path $envPath) {
    Get-Content $envPath | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { return }
        $i = $line.IndexOf('=')
        $name = $line.Substring(0, $i).Trim()
        $val = $line.Substring($i + 1).Trim().Trim('"').Trim("'")
        Set-Item "Env:$name" $val
    }
}
# Force the slow-but-resumable downloader path; hf_transfer doesn't auto-retry.
$env:HF_HUB_ENABLE_HF_TRANSFER = '0'
$env:HF_HUB_DISABLE_PROGRESS_BARS = '1'

# Tracks PIDs of resolver child processes we spawned, so we don't double-launch.
$activePids = @{}

function Has-CompletedTier([string]$tier) {
    # Two checks. Either alone isn't sufficient:
    #   (a) The symlink at data/models/<tier>.gguf must exist and
    #       resolve to a real file.
    #   (b) No `.incomplete` blobs may remain in the HF download cache
    #       for any companion shard. With sharded GGUFs (e.g. reasoning_xl
    #       has a tiny 11 MB manifest shard 1 that finishes instantly +
    #       three 40 GB bulk shards), a symlink-only check would
    #       incorrectly mark the tier "done" while shards 2..N are still
    #       streaming. Pre-PR-#185 resolvers also created the symlink
    #       eagerly; old half-finished pulls show up here.
    $link = Join-Path $RepoRoot "data\models\$tier.gguf"
    $linkOk = $false
    if (Test-Path $link) {
        try {
            $resolved = (Get-Item $link).ResolveLinkTarget($true)
            $linkOk = ($resolved -and (Test-Path $resolved.FullName))
        } catch {
            $linkOk = ((Get-Item $link).Length -gt 100MB)
        }
    }
    if (-not $linkOk) { return $false }

    # Walk the HF cache for any in-flight blob whose name suggests
    # it's a shard of this tier's GGUF. Different tiers store under
    # different subdirectories (UD-IQ2_M/, UD-IQ1_S/,
    # openai_gpt-oss-120b-Q4_K_M/, etc.) so we just walk recursively
    # under the cache and check basenames for the model identifier.
    $cacheRoot = Join-Path $RepoRoot 'data\models\.cache\huggingface\download'
    if (-not (Test-Path $cacheRoot)) { return $true }
    # Map tier → a basename fragment unique to that tier's GGUFs.
    # Anything more complex would re-implement the resolver's manifest
    # logic; this is good enough for the watchdog's purpose.
    $needle = switch ($tier) {
        'reasoning_max' { 'openai_gpt-oss-120b' }
        'reasoning_xl'  { 'Qwen3.5-397B-A17B' }
        'frontier'      { 'DeepSeek-V3.2' }
        'highest_quality' { 'Qwen3-Next-80B' }
        'coding_80b'    { 'Qwen3-Coder-Next' }
        default         { $tier }
    }
    $stillPending = @(Get-ChildItem $cacheRoot -Recurse -Filter '*.incomplete' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match [regex]::Escape($needle) })
    return ($stillPending.Count -eq 0)
}

function Test-XethubDns {
    # `Resolve-DnsName` short-circuits if DNS is down. 5 s timeout
    # so the loop doesn't stall waiting for slow upstream resolvers.
    try {
        $r = Resolve-DnsName -Name 'cas-bridge.xethub.hf.co' -DnsOnly -QuickTimeout -Type A -ErrorAction Stop
        return [bool]$r
    } catch {
        return $false
    }
}

function Test-XethubDnsViaPublic {
    # When system DNS fails, query 8.8.8.8 / 1.1.1.1 directly. If they
    # resolve, the upstream is fine and we just have a stuck local
    # resolver — actionable (e.g. ProtonVPN restart). If they fail too,
    # it's a real upstream / network outage — wait it out.
    foreach ($srv in '8.8.8.8','1.1.1.1') {
        try {
            $r = Resolve-DnsName -Name 'cas-bridge.xethub.hf.co' `
                -Server $srv -Type A -ErrorAction Stop -DnsOnly -QuickTimeout
            if ($r) { return $true }
        } catch {}
    }
    return $false
}

function Restart-ProtonVPN {
    # Restart-Service the ProtonVPN-named services. Returns $true if at
    # least one was restarted. Caller should re-test DNS afterward.
    $any = $false
    Get-Service | Where-Object { $_.Name -match 'Proton' } | ForEach-Object {
        try {
            Restart-Service -Name $_.Name -Force -ErrorAction Stop
            Log "    restarted $($_.Name)"
            $any = $true
        } catch {
            Log "    failed to restart $($_.Name): $($_.Exception.Message)"
        }
    }
    if ($any) {
        Clear-DnsClientCache -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 5
    }
    return $any
}

function Invoke-ProtonAutoRecover {
    # Defer to tools/proton_vpn.py::auto_recover_dns() via the backend
    # venv. That method walks the full Restart-Service → cycle-adapter
    # ladder and reports back. Falls back to Restart-ProtonVPN
    # (above) if Python invocation fails for any reason.
    #
    # Path-passing notes:
    #   - $RepoRoot travels as argv[1], NOT as f-string interpolation
    #     into the Python source, so a path containing a single quote
    #     (e.g. C:\repos\kit's stuff\local-ai-stack) can't break the
    #     `r'...'` raw string and inject code.
    #   - $LASTEXITCODE is checked; a non-zero exit from the Python
    #     side falls back to the simpler Restart-ProtonVPN path.
    $py = Join-Path $RepoRoot 'vendor\venv-backend\Scripts\python.exe'
    if (-not (Test-Path $py)) {
        Log "    proton_vpn tool unavailable (no backend venv) — falling back to service restart"
        return (Restart-ProtonVPN)
    }
    try {
        $pyScript = @'
import asyncio
import sys

sys.path.insert(0, sys.argv[1])
from tools.proton_vpn import Tools

print(asyncio.run(Tools().auto_recover_dns()))
'@
        $out = & $py -c $pyScript $RepoRoot 2>&1
        $exit = $LASTEXITCODE
        if ($exit -ne 0) {
            Log "    proton_vpn.auto_recover_dns exited $exit — falling back to service restart"
            $tail = ($out -split "`n" | Select-Object -Last 6) -join '; '
            Log "      stderr/stdout tail: $tail"
            return (Restart-ProtonVPN)
        }
        $tail = ($out -split "`n" | Select-Object -Last 6) -join '; '
        Log "    proton_vpn.auto_recover_dns: $tail"
        return $true
    } catch {
        Log "    proton_vpn.auto_recover_dns failed: $($_.Exception.Message) — falling back to service restart"
        return (Restart-ProtonVPN)
    }
}

# Per-tier byte-progress tracker so we can detect TCP stalls (DNS up,
# resolver alive, but no bytes flowing). Map: tier -> @{ bytes, ts }.
$progressMarks = @{}

function Get-TierBytesNow([string]$tier) {
    $cacheRoot = Join-Path $RepoRoot 'data\models\.cache\huggingface\download'
    if (-not (Test-Path $cacheRoot)) { return 0 }
    $needle = switch ($tier) {
        'reasoning_max'  { 'openai_gpt-oss-120b' }
        'reasoning_xl'   { 'Qwen3.5-397B-A17B' }
        'frontier'       { 'DeepSeek-V3.2' }
        'highest_quality' { 'Qwen3-Next-80B' }
        'coding_80b'     { 'Qwen3-Coder-Next' }
        default          { $tier }
    }
    $sum = 0L
    Get-ChildItem $cacheRoot -Recurse -Filter '*.incomplete' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match [regex]::Escape($needle) } |
        ForEach-Object { $sum += $_.Length }
    return $sum
}

function Test-TierProgress([string]$tier) {
    # Compare current incomplete-blob byte total against the last mark.
    # Returns 'growing' | 'stalled' | 'untracked'.
    $now = Get-Date
    $bytes = Get-TierBytesNow $tier
    $prev = $progressMarks[$tier]
    if (-not $prev) {
        $progressMarks[$tier] = @{ bytes = $bytes; ts = $now }
        return 'untracked'
    }
    if ($bytes -gt $prev.bytes) {
        # Growth — refresh the mark.
        $progressMarks[$tier] = @{ bytes = $bytes; ts = $now }
        return 'growing'
    }
    # No growth. Has the stall window elapsed?
    $stalledFor = ($now - $prev.ts).TotalSeconds
    if ($stalledFor -ge $ProgressStallSeconds) {
        return 'stalled'
    }
    return 'growing'   # not yet at threshold; still hopeful
}

function Kill-TierResolver([string]$tier) {
    if (-not $activePids.ContainsKey($tier)) { return $false }
    $pid = $activePids[$tier]
    try {
        Stop-Process -Id $pid -Force -ErrorAction Stop
        Log "    killed stalled resolver for $tier (PID=$pid)"
        $activePids.Remove($tier) | Out-Null
        # Reset the progress mark so the next observation establishes a
        # fresh baseline (the new resolver may take a few seconds to
        # start writing).
        $progressMarks.Remove($tier) | Out-Null
        return $true
    } catch {
        Log "    couldn't kill PID=$pid for $tier : $($_.Exception.Message)"
        return $false
    }
}

function Spawn-Resolver([string]$tier) {
    # Re-check we're not already running one for this tier.
    if ($activePids.ContainsKey($tier)) {
        try {
            $p = Get-Process -Id $activePids[$tier] -ErrorAction Stop
            if (-not $p.HasExited) {
                Log "  $tier : resolver already running (PID=$($p.Id)), skipping spawn"
                return
            }
        } catch { }
        $activePids.Remove($tier) | Out-Null
    }
    # Start-Process refuses the same path for both stdout + stderr, so
    # split them. The .err.log surfaces resolver bailouts (CDN drops,
    # 404s, etc.); .out.log is the resolver's normal progress.
    $logOut = Join-Path $RepoRoot "data\pull-$tier-watchdog.out.log"
    $logErr = Join-Path $RepoRoot "data\pull-$tier-watchdog.err.log"
    $argv = @(
        '-m', 'backend.model_resolver',
        'resolve', '--force', '--pull', '--tier', $tier
    )
    # Wrap in try/catch so a transient Start-Process failure (e.g. file
    # lock contention on the log path) doesn't kill the whole watchdog —
    # the next poll cycle will retry. ErrorAction explicitly Continue
    # because the script's top-level $ErrorActionPreference is Stop.
    try {
        $proc = Start-Process -FilePath $Py -ArgumentList $argv `
            -WorkingDirectory $RepoRoot `
            -RedirectStandardOutput $logOut -RedirectStandardError $logErr `
            -PassThru -WindowStyle Hidden -ErrorAction Stop
        $activePids[$tier] = $proc.Id
        Log "  $tier : spawned resolver (PID=$($proc.Id))"
    } catch {
        Log "  $tier : spawn FAILED ($($_.Exception.Message)) — will retry next poll"
    }
}

# ── Main loop ───────────────────────────────────────────────────────────────
$start = Get-Date
$deadline = $start.AddHours($MaxHours)
Log "=== watchdog start: tiers=$($Tiers -join ',') poll=${PollSeconds}s cap=${MaxHours}h ==="

$lastProgressLogAt = Get-Date 0   # forces first iteration to log
while ((Get-Date) -lt $deadline) {
    try {
        $pending = @($Tiers | Where-Object { -not (Has-CompletedTier $_) })
        if ($pending.Count -eq 0) {
            Log "all tiers complete — exiting"
            exit 0
        }

        # ── DNS health, with public-DNS fallback diagnosis ────────────────
        $dnsOk = Test-XethubDns
        if (-not $dnsOk) {
            # System DNS failed. Probe public servers to see if it's a
            # local-resolver issue or a real upstream outage.
            $upstreamOk = Test-XethubDnsViaPublic
            if ($upstreamOk) {
                Log "system DNS broken but cas-bridge.xethub.hf.co resolves via 8.8.8.8 — local resolver stuck"
                if ($RestartProtonVPN) {
                    Log "  invoking proton_vpn.auto_recover_dns (Restart-Service → cycle_adapter ladder)..."
                    Invoke-ProtonAutoRecover | Out-Null
                    $dnsOk = Test-XethubDns
                    Log "  post-recover DNS check: $(if ($dnsOk) { 'RECOVERED' } else { 'STILL BROKEN' })"
                } else {
                    # Throttle the manual-fix nag to once per 10 min
                    if (((Get-Date) - $lastProgressLogAt).TotalMinutes -ge 10) {
                        Log "  manual fix: restart ProtonVPN (or rerun watchdog with -RestartProtonVPN)"
                        $lastProgressLogAt = Get-Date
                    }
                }
            } else {
                # Throttle full-outage logs to once per 10 min
                if (((Get-Date) - $lastProgressLogAt).TotalMinutes -ge 10) {
                    Log "xethub.hf.co unreachable from local + public DNS (pending: $($pending -join ', '))"
                    $lastProgressLogAt = Get-Date
                }
            }
        }

        # ── Per-tier work, when DNS is up ─────────────────────────────────
        if ($dnsOk) {
            foreach ($t in $pending) {
                $verdict = Test-TierProgress $t
                $bytes = Get-TierBytesNow $t
                $bytesGB = [math]::Round($bytes / 1GB, 2)
                $resolverAlive = $false
                if ($activePids.ContainsKey($t)) {
                    try {
                        $p = Get-Process -Id $activePids[$t] -ErrorAction Stop
                        $resolverAlive = -not $p.HasExited
                    } catch { $resolverAlive = $false }
                }

                if ($verdict -eq 'stalled') {
                    Log "  $t : STALLED at ${bytesGB} GB for >${ProgressStallSeconds}s — killing + respawning"
                    Kill-TierResolver $t | Out-Null
                    Spawn-Resolver $t
                } elseif (-not $resolverAlive) {
                    Log "  $t : resolver not running ($bytesGB GB on disk) — spawning"
                    Spawn-Resolver $t
                }
                # else: resolver alive + bytes growing, nothing to do
            }
        }

        # ── Periodic 10-min progress summary (always logs at intervals) ──
        if (((Get-Date) - $lastProgressLogAt).TotalMinutes -ge 10 -or $lastProgressLogAt -eq (Get-Date 0)) {
            $summary = ($Tiers | ForEach-Object {
                $b = Get-TierBytesNow $_
                $gb = [math]::Round($b / 1GB, 1)
                "$_=${gb}GB"
            }) -join ', '
            Log "progress: $summary  (DNS=$(if ($dnsOk) {'OK'} else {'down'}), pending=$($pending.Count))"
            $lastProgressLogAt = Get-Date
        }
    } catch {
        Log "iteration error: $($_.Exception.Message) — continuing"
    }

    Start-Sleep -Seconds $PollSeconds
}

Log "MAX HOURS reached with $((@($Tiers | Where-Object { -not (Has-CompletedTier $_) })).Count) tier(s) still pending — exiting non-zero"
exit 2
