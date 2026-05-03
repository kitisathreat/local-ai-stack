#Requires -Version 7
#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Grow the Windows pagefile so the `frontier` tier (171 GB
  DeepSeek V3.2 sharded GGUF) can load.

.DESCRIPTION
  The frontier tier's UD-IQ1_S quant is 171 GB on disk. On a 125 GB
  RAM box with the default 8 GB system-managed pagefile, total
  commit charge is capped at 133 GB — so llama-server crashes
  during tensor load before any inference runs. Three llama.cpp
  configs (-ot, pure-CPU, --cpu-moe) all hit the same wall; the
  underlying problem is OS commit-charge, not model architecture.

  This script:
    1. Switches the pagefile from system-managed to manual sizing.
    2. Sets initial 8 GB / max <Size>GB on the chosen drive.
    3. Reports what happened and whether a reboot is needed.

  Default <Size> is 220 GB (171 GB model + ~50 GB headroom for
  attention scratch + KV + Windows commit reserves). Default drive
  is the one with the most free space (typically D: on this box).

  After running, REBOOT. Pagefile resizes only take effect after the
  next boot. Without the reboot the new max isn't usable.

  Reverting: re-run with -RevertToManaged. Restores the original
  Windows-managed pagefile behaviour (no manual reboot needed
  beyond the normal one).

.PARAMETER SizeGB
  Maximum pagefile size in GB. Default 220.

.PARAMETER InitialGB
  Initial pagefile size in GB. Default 8 (low so it doesn't pre-
  reserve disk; OS grows it on demand up to MaximumSize).

.PARAMETER Drive
  Drive letter to host the pagefile (e.g. 'C', 'D'). Default: the
  drive with the most free space among local fixed drives.

.PARAMETER RevertToManaged
  Switch back to the default Windows-managed pagefile and remove
  any manual entry on the chosen drive.

.EXAMPLE
  pwsh .\scripts\operator\grow-pagefile.ps1
  # 220 GB max on the largest free drive

.EXAMPLE
  pwsh .\scripts\operator\grow-pagefile.ps1 -SizeGB 300 -Drive D
  # 300 GB max on D:

.EXAMPLE
  pwsh .\scripts\operator\grow-pagefile.ps1 -RevertToManaged
  # back to defaults
#>

[CmdletBinding()]
param(
    [int]$SizeGB = 220,
    [int]$InitialGB = 8,
    [string]$Drive,
    [switch]$RevertToManaged
)

$ErrorActionPreference = 'Stop'

function Show-PagefileState {
    Write-Host '--- Current pagefile state ---' -ForegroundColor DarkGray
    $cs = Get-CimInstance Win32_ComputerSystem
    "  AutomaticManagedPagefile: {0}" -f $cs.AutomaticManagedPagefile
    Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue |
        ForEach-Object { "  Manual: {0}  Initial={1} MB  Maximum={2} MB" -f $_.Name, $_.InitialSize, $_.MaximumSize }
    Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue |
        ForEach-Object { "  Live:   {0}  Allocated={1} MB  CurrentUse={2} MB  Peak={3} MB" -f $_.Name, $_.AllocatedBaseSize, $_.CurrentUsage, $_.PeakUsage }
    $os = Get-CimInstance Win32_OperatingSystem
    "  Total commit limit: {0:N1} GB  ({1:N1} GB free)" -f ($os.TotalVirtualMemorySize/1MB), ($os.FreeVirtualMemory/1MB)
}

Show-PagefileState
Write-Host ''

# Always start by switching off auto-management — without this, manual
# entries are ignored.
$cs = Get-CimInstance Win32_ComputerSystem
if ($cs.AutomaticManagedPagefile) {
    Write-Host 'Disabling automatic pagefile management...' -ForegroundColor Cyan
    Set-CimInstance -InputObject $cs -Property @{ AutomaticManagedPagefile = $false }
}

if ($RevertToManaged) {
    Write-Host 'Removing manual pagefile entries...' -ForegroundColor Cyan
    Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue | Remove-CimInstance
    Write-Host 'Re-enabling automatic management...' -ForegroundColor Cyan
    Set-CimInstance -InputObject $cs -Property @{ AutomaticManagedPagefile = $true }
    Write-Host ''
    Show-PagefileState
    Write-Host ''
    Write-Host 'REBOOT for the change to take effect.' -ForegroundColor Yellow
    return
}

# Pick the drive with the most free space if not specified.
if (-not $Drive) {
    $best = Get-PSDrive -PSProvider FileSystem |
        Where-Object { $_.Free -ne $null -and $_.Used -ne $null -and $_.Name -match '^[A-Z]$' } |
        Sort-Object Free -Descending | Select-Object -First 1
    if (-not $best) {
        Write-Host 'Could not auto-detect a usable drive. Pass -Drive C explicitly.' -ForegroundColor Red
        exit 1
    }
    $Drive = $best.Name
    Write-Host ("Auto-selected drive {0}: ({1:N1} GB free)" -f $Drive, ($best.Free/1GB)) -ForegroundColor DarkGray
}

# Sanity: the chosen drive must have at least SizeGB+10 free so we
# don't fill it.
$selected = Get-PSDrive -Name $Drive -ErrorAction SilentlyContinue
if (-not $selected) { throw "Drive '$Drive' not found." }
$freeGB = [math]::Round($selected.Free/1GB, 1)
if ($freeGB -lt ($SizeGB + 10)) {
    Write-Host ("Drive {0}: only has {1:N1} GB free; need at least {2} GB." -f $Drive, $freeGB, ($SizeGB + 10)) -ForegroundColor Red
    exit 1
}

$pagefilePath = "${Drive}:\pagefile.sys"
$initialMB = $InitialGB * 1024
$maximumMB = $SizeGB * 1024

# Drop any existing manual entry on this drive (idempotent).
$existing = Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq $pagefilePath }
if ($existing) {
    Write-Host "Removing existing manual entry for $pagefilePath..." -ForegroundColor Cyan
    $existing | Remove-CimInstance
}

# Also drop any other-drive manual entries so we don't end up with
# two pagefiles fighting each other.
Get-CimInstance Win32_PageFileSetting -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -ne $pagefilePath } |
    ForEach-Object {
        Write-Host ("Removing stale entry on {0}..." -f $_.Name) -ForegroundColor Cyan
        $_ | Remove-CimInstance
    }

Write-Host ("Creating pagefile {0}  initial={1} MB  max={2} MB..." -f $pagefilePath, $initialMB, $maximumMB) -ForegroundColor Cyan
New-CimInstance -ClassName Win32_PageFileSetting -Property @{
    Name = $pagefilePath
    InitialSize = $initialMB
    MaximumSize = $maximumMB
} | Out-Null

Write-Host ''
Show-PagefileState
Write-Host ''
Write-Host '✓ Pagefile config updated.' -ForegroundColor Green
Write-Host '⚠ REBOOT NOW for the new max to take effect.' -ForegroundColor Yellow
Write-Host ''
Write-Host 'Verification post-reboot: re-run this script (it just shows state and exits if no changes are needed),'
Write-Host 'or run:  Get-CimInstance Win32_OperatingSystem | Select TotalVirtualMemorySize'
Write-Host ''
Write-Host 'After the reboot, frontier should spawn through the existing'
Write-Host '`tier.frontier` chat path. Cold load is ~30-60 s as the OS warm-pages'
Write-Host 'attention/embedding tensors; subsequent tokens stream at ~0.5-2 t/s.'
