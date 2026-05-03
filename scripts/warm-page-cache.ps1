#Requires -Version 7
<#
.SYNOPSIS
  Read every chat-tier .gguf into the OS file-page cache so subsequent
  llama-server mmap loads come from RAM, not disk.

.DESCRIPTION
  Cold-spawning a 6-20 GB GGUF the first time after boot pays full disk
  read latency (~1 GB/s on NVMe → 6-20 s just for I/O). Once a file's
  pages have been read once, Windows keeps them in the standby list as
  long as memory pressure allows. The next mmap-load skips disk
  entirely and the spawn is bounded by GPU upload + scheduler fixup,
  not disk.

  This is the lightweight version of "load all models into RAM": no
  Python runtime in the loop, no llama-server spin-up, no GPU touch.
  Just pump bytes through the page cache.

  Skips files larger than (system RAM - 2 GB) so we never push the
  cache into pressure that would evict things llama-server actually
  needs (KV cache pages, vision mmproj, etc.).

  Idempotent — safe to run on every -Start. Re-reads are cache-fast
  if the pages are already resident.

.PARAMETER ModelsDir
  Directory holding the .gguf files. Defaults to <repo>/data/models.

.PARAMETER ChunkBytes
  Read chunk size. 64 MB is the sweet spot — large enough to amortize
  syscall overhead, small enough to keep working set bounded.
#>

[CmdletBinding()]
param(
    [string]$ModelsDir = '',
    [int]$ChunkBytes = 67108864     # 64 MB
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
if (-not $ModelsDir) { $ModelsDir = Join-Path $RepoRoot 'data\models' }
if (-not (Test-Path $ModelsDir)) {
    Write-Host "warm-page-cache: $ModelsDir not found" -ForegroundColor Yellow
    return
}

# Skip pre-warming files individually larger than (RAM - 2 GB) — those
# can't fit cleanly in the page cache without evicting other things.
$totalRamGB = [int]([System.GC]::GetGCMemoryInfo().TotalAvailableMemoryBytes / 1GB)
if ($totalRamGB -le 0) {
    # Fallback to WMI when GC's view is unreliable on older runtimes.
    $totalRamGB = [int]((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
}
$maxFileBytes = [long]([Math]::Max(2, $totalRamGB - 2)) * 1GB

# .gguf files only — drop drafts and mmprojs are tiny and worth warming
# alongside the chat tiers.
$files = Get-ChildItem -Path $ModelsDir -Filter '*.gguf' -ErrorAction SilentlyContinue |
         Where-Object { -not $_.PSIsContainer } |
         Sort-Object Length

if (-not $files) {
    Write-Host 'warm-page-cache: no .gguf files in models dir' -ForegroundColor DarkGray
    return
}

$total = 0L
$skipped = @()
$start = Get-Date
foreach ($f in $files) {
    if ($f.Length -gt $maxFileBytes) {
        $skipped += "$($f.Name) ($([int]($f.Length/1GB)) GB > limit)"
        continue
    }
    try {
        $stream = [System.IO.FileStream]::new(
            $f.FullName,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite,
            $ChunkBytes,
            [System.IO.FileOptions]::SequentialScan
        )
        try {
            $buf = [byte[]]::new($ChunkBytes)
            while ($stream.Read($buf, 0, $ChunkBytes) -gt 0) { }
        } finally {
            $stream.Dispose()
        }
        $total += $f.Length
    } catch {
        Write-Host ("   warm-page-cache: skipped {0} ({1})" -f $f.Name, $_.Exception.Message) -ForegroundColor DarkYellow
    }
}

$secs = [int]((Get-Date) - $start).TotalSeconds
$mb = [int]($total / 1MB)
Write-Host ("   warmed {0} MB across {1} GGUFs in {2}s" -f $mb, $files.Count, $secs) -ForegroundColor DarkGray
foreach ($s in $skipped) {
    Write-Host "   skipped: $s (won't fit in page cache)" -ForegroundColor DarkYellow
}
