#Requires -Version 7
<#
.SYNOPSIS
  Check which tier GGUFs have finished downloading, run the bench
  against the newly-available subset, append rows to the README PR,
  and exit. Designed to be re-run periodically by Task Scheduler until
  every tier has been benched.

.DESCRIPTION
  This is the worker invoked by `install-bench-task.ps1`. The original
  problem: highest_quality / coding_80b / reasoning_max GGUFs are
  ~50 GB each and HF CDN drops connections every ~100 MB during the
  pull. The model_resolver auto-resumes from `data/models/.cache/
  huggingface/download/*.incomplete` blobs, but the pull-then-bench
  cycle can take 12+ hours total. Rather than babysitting that, this
  script lets Task Scheduler poll: every N hours it checks what
  symlinks resolve to real files, benches anything new, posts the
  table to the PR, and self-disables once all tiers are accounted for.

  Idempotent: tiers already recorded in `data/eval/bench-task.state.json`
  are skipped on subsequent runs.

  Side-effects:
    - Writes data/eval/tier-bench-<ts>.json (raw bench output)
    - Updates data/eval/bench-task.state.json (covered tiers + run log)
    - Pushes a commit to docs/post-pr169-170-vram-cascade with the
      updated README "Tier benchmarks" table (when run with -PostPR)
    - Posts a comment on PR #172 summarising the new rows (when -PostPR)
    - Calls `install-bench-task.ps1 -Uninstall` when every tier is done

.PARAMETER PrNumber
  GitHub PR to post results into. Default: 172 (the docs PR).

.PARAMETER DocsBranch
  Branch to push README updates onto. Default: docs/post-pr169-170-vram-cascade.

.PARAMETER PostPR
  When set, commits the README diff and posts a PR comment. Without it,
  the bench runs and writes the JSON snapshot but doesn't touch git.

.PARAMETER ApiBase
  Local backend URL. Default: http://127.0.0.1:18000.
#>

[CmdletBinding()]
param(
    [int]$PrNumber = 172,
    [string]$DocsBranch = 'docs/post-pr169-170-vram-cascade',
    [switch]$PostPR,
    [string]$ApiBase = 'http://127.0.0.1:18000',
    [string]$TaskName = 'LocalAIStack-BenchWhenReady'
)

$ErrorActionPreference = 'Stop'
$RepoRoot   = Resolve-Path (Join-Path $PSScriptRoot '..')
$ModelsDir  = Join-Path $RepoRoot 'data\models'
$EvalDir    = Join-Path $RepoRoot 'data\eval'
$LogDir     = Join-Path $env:APPDATA 'LocalAIStack'
$null = New-Item -ItemType Directory -Path $EvalDir, $LogDir -Force -ErrorAction SilentlyContinue
$LogPath    = Join-Path $LogDir 'bench-when-tiers-ready.log'
$StatePath  = Join-Path $EvalDir 'bench-task.state.json'

function Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $LogPath -Value $line -Encoding utf8
    Write-Host $line
}

# ── Load + persist run state ────────────────────────────────────────────────

function Read-State {
    if (Test-Path $StatePath) {
        try { return Get-Content -Raw -Path $StatePath | ConvertFrom-Json -AsHashtable }
        catch { Log "state read failed: $($_.Exception.Message); resetting"; }
    }
    return @{
        covered_tiers = @()
        last_runs     = @()
    }
}

function Write-State($state) {
    $state | ConvertTo-Json -Depth 6 | Set-Content -Path $StatePath -Encoding utf8
}

# ── Tier discovery ──────────────────────────────────────────────────────────

# Tiers we care about for benchmarking. Vision and embedding excluded —
# they're not chat tiers and bench_tiers.py doesn't exercise them.
$AllChatTiers = @('versatile', 'fast', 'coding', 'coding_80b', 'highest_quality', 'reasoning_max')

function Get-AvailableTiers {
    $available = @()
    foreach ($t in $AllChatTiers) {
        $link = Join-Path $ModelsDir ($t + '.gguf')
        if (-not (Test-Path $link)) { continue }
        # Resolve symlink to its target. If the target file doesn't
        # exist (resolver wrote the link before the download finished),
        # treat the tier as still pending.
        try {
            $resolved = (Get-Item $link).ResolveLinkTarget($true)
            if ($resolved -and (Test-Path $resolved.FullName)) {
                $available += $t
            }
        } catch {
            if ((Get-Item $link).Length -gt 100MB) {
                # Real file (not a dangling symlink); accept.
                $available += $t
            }
        }
    }
    return $available
}

# Returns a hashtable: tier -> { downloaded_bytes, expected_bytes_or_null }
# from the .incomplete blobs in data/models/.cache/huggingface/download/.
function Get-PendingDownloads {
    $cacheDir = Join-Path $ModelsDir '.cache\huggingface\download'
    $result = @{}
    if (-not (Test-Path $cacheDir)) { return $result }
    $tierFiles = @{
        'highest_quality' = 'Qwen3-Next-80B-A3B-Thinking-UD-Q4_K_XL'
        'coding_80b'      = 'Qwen3-Coder-Next-UD-Q4_K_XL'
        'reasoning_max'   = 'openai_gpt-oss-120b-Q4_K_M'
    }
    foreach ($tier in $tierFiles.Keys) {
        $stem = $tierFiles[$tier]
        $partial = Get-ChildItem $cacheDir -Filter "$stem*.incomplete" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($partial) {
            $result[$tier] = $partial.Length
        }
    }
    return $result
}

# ── Backend health + diagnostics ────────────────────────────────────────────

function Wait-BackendUp {
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "$ApiBase/healthz" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
            if ($r.StatusCode -eq 200) { return $true }
        } catch { Start-Sleep -Seconds 2 }
    }
    return $false
}

# ── Bench invocation ────────────────────────────────────────────────────────

$VenvPy = Join-Path $RepoRoot 'vendor\venv-backend\Scripts\python.exe'

function Invoke-Bench([string[]]$tiers, [string]$evictTier = 'fast') {
    $tierArg = ($tiers -join ',')
    Log "Running bench against tiers: $tierArg (evict-tier=$evictTier)"
    $output = & $VenvPy (Join-Path $RepoRoot 'scripts\bench_tiers.py') `
        '--tiers' $tierArg '--evict-tier' $evictTier 2>&1
    $exit = $LASTEXITCODE
    $output | ForEach-Object { Log "bench: $_" }
    if ($exit -ne 0) { throw "bench_tiers.py exit=$exit" }
    # The script prints the saved JSON path on its last line.
    $savedLine = $output | Where-Object { $_ -match '^Saved:' } | Select-Object -Last 1
    if (-not $savedLine) { throw 'bench did not report a saved path' }
    $jsonPath = ($savedLine -replace '^Saved:\s*', '').Trim()
    if (-not [System.IO.Path]::IsPathRooted($jsonPath)) {
        $jsonPath = Join-Path $RepoRoot $jsonPath
    }
    return $jsonPath
}

# ── README mutation ─────────────────────────────────────────────────────────

# Pre-merge baselines for the regression check; matches the existing
# README's "Tier benchmarks" table commentary.
$BaselineTokps = @{
    'versatile' = 8.6
    'fast'      = 23.0
    'coding'    = 9.5
}

function Append-ReadmeRows([hashtable[]]$rows) {
    $readme = Join-Path $RepoRoot 'README.md'
    $text = Get-Content -Raw -Path $readme
    # Find the bench table; insert new rows before the trailing "Reproduce with:" block.
    $marker = '`coding`    | 11.05 | 1.03 |  9.7 |'
    if ($text -notmatch [regex]::Escape($marker)) {
        Log "README marker row not found; skipping append"
        return $false
    }
    $insertion = ''
    foreach ($r in $rows) {
        $insertion += "`n| ``$($r.tier)`` | $($r.cold) | $($r.warm_first) | **$($r.tokps)** | $($r.notes) |"
    }
    if (-not $insertion) { return $false }
    $updated = $text -replace [regex]::Escape($marker), ($marker + $insertion)
    if ($updated -eq $text) { return $false }
    Set-Content -Path $readme -Value $updated -Encoding utf8
    return $true
}

# ── Main ────────────────────────────────────────────────────────────────────

Log "=== bench-when-tiers-ready run start ==="

# 1. Refresh local repo so we see any newly-merged PRs.
try {
    & git -C $RepoRoot fetch origin master 2>&1 | Out-Null
    & git -C $RepoRoot pull --ff-only origin master 2>&1 | Out-Null
} catch { Log "git pull warn: $($_.Exception.Message)" }

# 2. Discover what's on disk.
$state = Read-State
$available = Get-AvailableTiers
$pending   = Get-PendingDownloads
$todo = $available | Where-Object { $state.covered_tiers -notcontains $_ }
Log "available=$($available -join ','), pending=$($pending.Keys -join ','), todo=$($todo -join ',')"

if (-not $todo) {
    Log "No new tiers to bench."
    if (-not $pending.Keys.Count) {
        Log "No pending downloads either — every tier accounted for. Self-uninstalling."
        try {
            & (Join-Path $PSScriptRoot 'install-bench-task.ps1') -Uninstall -TaskName $TaskName | Out-Null
        } catch { Log "self-uninstall warn: $($_.Exception.Message)" }
    }
    exit 0
}

# 3. Backend must be alive.
if (-not (Wait-BackendUp)) {
    Log "Backend not reachable at $ApiBase/healthz — aborting (run will retry on next schedule)."
    exit 0
}

# 4. Diagnostic snapshot before bench.
try {
    $probe = Invoke-RestMethod -Uri "$ApiBase/admin/vram/probe" -TimeoutSec 5 -ErrorAction Stop
    Log "vram/probe: nvml_free=$($probe.nvml_free_gb)GB tracked=$($probe.scheduler_tracked_used_gb)GB drift=$($probe.orphan_drift_gb)GB orphans=$($probe.orphan_llama_server_pids.Count)"
} catch {
    # /admin/vram/probe needs admin auth; without a session cookie we'll get
    # 401 here. That's fine — bench still runs.
    Log "vram/probe: $($_.Exception.Message)"
}

# 5. Run the bench. Always include a known-good tier (fast) so eviction
#    works between runs.
$benchTiers = $todo
if ('fast' -notin $benchTiers -and ($available -contains 'fast')) {
    $benchTiers = ,'fast' + $benchTiers
}
$jsonPath = Invoke-Bench -tiers $benchTiers

# 6. Parse results, build new rows, regression check.
$bench = Get-Content -Raw -Path $jsonPath | ConvertFrom-Json
$newRows = @()
foreach ($r in $bench.results) {
    if ($r.tier -notin $todo) { continue }   # skip rows that were just used as eviction filler
    if ($r.PSObject.Properties.Name -contains 'error') {
        Log "tier=$($r.tier) FAILED: $($r.error)"
        continue
    }
    $row = @{
        tier       = $r.tier
        cold       = $r.cold_load_s
        warm_first = $r.warm_first_token_s
        tokps      = $r.tokens_per_sec
        notes      = "Auto-bench $((Get-Date).ToString('yyyy-MM-dd'))"
    }
    if ($BaselineTokps.ContainsKey($r.tier)) {
        $base = $BaselineTokps[$r.tier]
        $delta = [math]::Round((($r.tokens_per_sec - $base) / $base) * 100, 1)
        $tag = if ($delta -ge 0) { "+$delta" } else { "$delta" }
        $row.notes = "$tag% vs pre-merge baseline"
        if ([math]::Abs($delta) -ge 15) {
            Log "REGRESSION FLAG tier=$($r.tier) delta=${delta}%"
        }
    }
    $newRows += $row
}

if (-not $newRows) {
    Log "No new bench rows produced (all errored?). Will retry next schedule."
    exit 0
}

# 7. Update state so we don't re-bench the same tier next cycle.
$state.covered_tiers = @($state.covered_tiers + ($newRows | ForEach-Object { $_.tier }) | Select-Object -Unique)
$state.last_runs = @($state.last_runs + @{
    ts        = (Get-Date).ToString('o')
    tiers     = ($newRows | ForEach-Object { $_.tier })
    json_path = $jsonPath
} | Select-Object -Last 20)
Write-State $state

# 8. Optionally push to docs branch + comment on PR.
if (-not $PostPR) {
    Log "PostPR off — leaving git untouched. State written to $StatePath."
    exit 0
}

try {
    & git -C $RepoRoot fetch origin $DocsBranch 2>&1 | Out-Null
    & git -C $RepoRoot checkout $DocsBranch 2>&1 | Out-Null
    & git -C $RepoRoot pull --ff-only origin $DocsBranch 2>&1 | Out-Null

    $changed = Append-ReadmeRows $newRows
    if ($changed) {
        $msg = "README: auto-bench rows for $(($newRows | ForEach-Object { $_.tier }) -join ', ')"
        & git -C $RepoRoot add README.md
        & git -C $RepoRoot commit -m $msg | Out-Null
        & git -C $RepoRoot push origin $DocsBranch 2>&1 | Out-Null
        Log "Pushed README update to $DocsBranch"
    } else {
        Log "README marker missing — skipped commit"
    }

    # Post comment on the PR with the rendered table.
    $comment = "Auto-bench results for newly-available tier(s): $(($newRows | ForEach-Object { $_.tier }) -join ', ')`n`n"
    $comment += "| tier | cold-load (s) | warm-first (s) | tok/s | notes |`n"
    $comment += "|---|---:|---:|---:|---|`n"
    foreach ($r in $newRows) {
        $comment += "| ``$($r.tier)`` | $($r.cold) | $($r.warm_first) | **$($r.tokps)** | $($r.notes) |`n"
    }
    $comment += "`nProbe at run-time: nvml_free=$($probe.nvml_free_gb)GB, scheduler_tracked=$($probe.scheduler_tracked_used_gb)GB, drift=$($probe.orphan_drift_gb)GB."
    $tmp = New-TemporaryFile
    Set-Content -Path $tmp -Value $comment -Encoding utf8
    & gh pr comment $PrNumber --body-file $tmp.FullName | Out-Null
    Remove-Item $tmp -Force
    Log "Posted comment on PR #$PrNumber"
} catch {
    Log "PostPR step warn: $($_.Exception.Message)"
}

# 9. If nothing pending and everything covered, self-uninstall.
$stillMissing = $AllChatTiers | Where-Object { $state.covered_tiers -notcontains $_ }
if (-not $stillMissing) {
    Log "All chat tiers benched. Self-uninstalling."
    try {
        & (Join-Path $PSScriptRoot 'install-bench-task.ps1') -Uninstall -TaskName $TaskName | Out-Null
    } catch { Log "self-uninstall warn: $($_.Exception.Message)" }
}

Log "=== run end ==="
