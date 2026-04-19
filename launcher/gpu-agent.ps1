<#
.SYNOPSIS
    GPU telemetry agent for LocalAIStack.
.DESCRIPTION
    HTTP listener on 127.0.0.1:8788 that returns JSON GPU metrics from nvidia-smi.
    Started by the launcher; runs hidden. Exposed inside containers via
    host.docker.internal:8788.
#>

$ErrorActionPreference = "Stop"
$port = 8788
$listener = [System.Net.HttpListener]::new()
$listener.Prefixes.Add("http://127.0.0.1:$port/")
$listener.Start()

function Get-Metrics {
    if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
        return @{ gpu = "unknown"; vram_used_mb = 0; vram_total_mb = 0; vram_pct = 0; gpu_util_pct = 0; temp_c = 0; overspill = "unknown" }
    }
    $csv = & nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv,noheader,nounits 2>$null
    if (-not $csv) {
        return @{ gpu = "error"; vram_used_mb = 0; vram_total_mb = 0; vram_pct = 0; gpu_util_pct = 0; temp_c = 0; overspill = "unknown" }
    }
    $parts = $csv -split ","
    $used  = [int]$parts[1].Trim()
    $total = [int]$parts[2].Trim()
    $pct   = if ($total -gt 0) { [math]::Round(100.0 * $used / $total, 1) } else { 0 }

    # Overspill band. Ollama manages CPU offload internally and the container
    # doesn't expose a simple CLI probe for layer placement, so the band here
    # is driven by VRAM % alone. When ~100% VRAM is in use but GPU utilization
    # is low, Ollama is likely paging through CPU — we flag that as red too.
    $util = [int]$parts[3].Trim()
    $overspill = "green"
    if ($pct -gt 95 -and $util -lt 40)     { $overspill = "red"   }
    elseif ($pct -gt 90)                   { $overspill = "red"   }
    elseif ($pct -gt 70)                   { $overspill = "amber" }

    return @{
        gpu           = $parts[0].Trim()
        vram_used_mb  = $used
        vram_total_mb = $total
        vram_pct      = $pct
        gpu_util_pct  = $util
        temp_c        = [int]$parts[4].Trim()
        overspill     = $overspill
    }
}

while ($listener.IsListening) {
    $ctx = $listener.GetContext()
    try {
        $metrics = Get-Metrics
        $json    = $metrics | ConvertTo-Json -Compress
        $bytes   = [System.Text.Encoding]::UTF8.GetBytes($json)
        $ctx.Response.ContentType = "application/json"
        $ctx.Response.AddHeader("Access-Control-Allow-Origin", "*")
        $ctx.Response.ContentLength64 = $bytes.Length
        $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
    } catch {} finally {
        $ctx.Response.Close()
    }
}
