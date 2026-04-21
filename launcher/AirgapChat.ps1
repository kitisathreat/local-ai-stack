<#
.SYNOPSIS
    Local desktop chat GUI for airgap mode.
.DESCRIPTION
    A standalone WinForms chat window that talks to the FastAPI backend on
    localhost. Intended for use when airgap mode is ON — the user gets a
    native app instead of a browser tab, and external services stay out of
    the picture. Streams token-by-token via SSE from /v1/chat/completions.

    Runs independent of the admin UI and the main launcher progress window.
    Can be launched from the LocalAIStack tray menu or directly.
.NOTES
    Compiled to .exe via launcher/build.ps1 (uses ps2exe).
    UI work must happen on the UI thread — background network work
    marshals back via $form.Invoke().
#>

param(
    [string]$BackendUrl = ""
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Net.Http

# ── Resolve backend URL ───────────────────────────────────────────────────────
# Priority: explicit param > env var > .env.local PUBLIC_BASE_URL backend > localhost:18000.
$repoRoot   = Split-Path $PSScriptRoot -Parent
$appDataDir = Join-Path $env:APPDATA "LocalAIStack"
$logPath    = Join-Path $appDataDir "airgap-chat.log"
$iconPath   = Join-Path $PSScriptRoot "assets\icon.ico"
if (-not (Test-Path $appDataDir)) { New-Item -ItemType Directory -Path $appDataDir | Out-Null }

function Write-Log {
    param([string]$Level, [string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
    try { "[$ts] [$Level] $Message" | Out-File -FilePath $logPath -Append -Encoding utf8 } catch { }
}

function Resolve-BackendUrl {
    if ($BackendUrl) { return $BackendUrl.TrimEnd('/') }
    if ($env:LAI_BACKEND_URL) { return $env:LAI_BACKEND_URL.TrimEnd('/') }
    # The backend is published on host :18000 (container :8000); PUBLIC_BASE_URL
    # points at the frontend (nginx :3000 or a tunnel hostname) and proxies
    # /api/*. For a desktop client we talk to the backend directly.
    return "http://localhost:18000"
}
$script:BackendBase = Resolve-BackendUrl
Write-Log "INFO" "Backend URL: $script:BackendBase"

# ── Colors / theme (matches LocalAIStack launcher) ────────────────────────────
$Theme = @{
    Bg        = [System.Drawing.Color]::FromArgb(15, 15, 15)
    Panel     = [System.Drawing.Color]::FromArgb(24, 24, 26)
    Border    = [System.Drawing.Color]::FromArgb(50, 50, 55)
    Fg        = [System.Drawing.Color]::White
    Dim       = [System.Drawing.Color]::FromArgb(160, 160, 160)
    Faint     = [System.Drawing.Color]::FromArgb(100, 100, 100)
    Accent    = [System.Drawing.Color]::FromArgb(120, 140, 220)
    UserRole  = [System.Drawing.Color]::FromArgb(120, 200, 255)
    AsstRole  = [System.Drawing.Color]::FromArgb(180, 230, 170)
    SysRole   = [System.Drawing.Color]::FromArgb(220, 180, 120)
    AirOn     = [System.Drawing.Color]::FromArgb(90, 190, 110)
    AirOff    = [System.Drawing.Color]::FromArgb(220, 120, 100)
}

# ── Conversation state (UI thread) ────────────────────────────────────────────
$script:Messages   = New-Object System.Collections.ArrayList   # list of @{role,content}
$script:CurrentTier = "tier.versatile"
$script:Streaming  = $false
$script:CancelSrc  = $null
$script:AsstStart  = 0   # char offset of the in-progress assistant reply

# ── Form ──────────────────────────────────────────────────────────────────────
$form = New-Object System.Windows.Forms.Form
$form.Text = "LocalAIStack Chat"
$form.StartPosition = "CenterScreen"
$form.ClientSize = New-Object System.Drawing.Size(760, 620)
$form.MinimumSize = New-Object System.Drawing.Size(520, 420)
$form.BackColor = $Theme.Bg
$form.ForeColor = $Theme.Fg
if (Test-Path $iconPath) { $form.Icon = New-Object System.Drawing.Icon($iconPath) }

# ── Top bar: airgap status + tier picker + new-chat ───────────────────────────
$topBar = New-Object System.Windows.Forms.Panel
$topBar.Dock = "Top"
$topBar.Height = 44
$topBar.BackColor = $Theme.Panel
$form.Controls.Add($topBar)

$statusDot = New-Object System.Windows.Forms.Label
$statusDot.Text = "●"
$statusDot.Font = New-Object System.Drawing.Font("Segoe UI", 14, [System.Drawing.FontStyle]::Bold)
$statusDot.ForeColor = $Theme.Dim
$statusDot.AutoSize = $true
$statusDot.Location = New-Object System.Drawing.Point(12, 10)
$topBar.Controls.Add($statusDot)

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Text = "checking…"
$statusLabel.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$statusLabel.ForeColor = $Theme.Dim
$statusLabel.AutoSize = $true
$statusLabel.Location = New-Object System.Drawing.Point(32, 14)
$topBar.Controls.Add($statusLabel)

$tierLabel = New-Object System.Windows.Forms.Label
$tierLabel.Text = "Tier:"
$tierLabel.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$tierLabel.ForeColor = $Theme.Dim
$tierLabel.AutoSize = $true
$tierLabel.Location = New-Object System.Drawing.Point(300, 14)
$topBar.Controls.Add($tierLabel)

$tierPicker = New-Object System.Windows.Forms.ComboBox
$tierPicker.DropDownStyle = "DropDownList"
$tierPicker.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$tierPicker.FlatStyle = "Flat"
$tierPicker.BackColor = $Theme.Panel
$tierPicker.ForeColor = $Theme.Fg
$tierPicker.Location = New-Object System.Drawing.Point(336, 10)
$tierPicker.Width = 220
$topBar.Controls.Add($tierPicker)

$newChatBtn = New-Object System.Windows.Forms.Button
$newChatBtn.Text = "New chat"
$newChatBtn.FlatStyle = "Flat"
$newChatBtn.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$newChatBtn.BackColor = $Theme.Panel
$newChatBtn.ForeColor = $Theme.Fg
$newChatBtn.FlatAppearance.BorderColor = $Theme.Border
$newChatBtn.Size = New-Object System.Drawing.Size(92, 26)
$newChatBtn.Anchor = "Top, Right"
$newChatBtn.Location = New-Object System.Drawing.Point(($topBar.Width - 104), 9)
$topBar.Controls.Add($newChatBtn)

# ── Transcript (RichTextBox, read-only) ───────────────────────────────────────
$transcript = New-Object System.Windows.Forms.RichTextBox
$transcript.Dock = "Fill"
$transcript.ReadOnly = $true
$transcript.BackColor = $Theme.Bg
$transcript.ForeColor = $Theme.Fg
$transcript.BorderStyle = "None"
$transcript.Font = New-Object System.Drawing.Font("Segoe UI", 10)
$transcript.DetectUrls = $true
$transcript.WordWrap = $true
$transcript.ScrollBars = "Vertical"

# ── Bottom bar: input + send ──────────────────────────────────────────────────
$bottom = New-Object System.Windows.Forms.Panel
$bottom.Dock = "Bottom"
$bottom.Height = 110
$bottom.BackColor = $Theme.Panel
$form.Controls.Add($bottom)

$input = New-Object System.Windows.Forms.TextBox
$input.Multiline = $true
$input.AcceptsReturn = $true
$input.BackColor = $Theme.Bg
$input.ForeColor = $Theme.Fg
$input.BorderStyle = "FixedSingle"
$input.Font = New-Object System.Drawing.Font("Segoe UI", 10)
$input.ScrollBars = "Vertical"
$input.Anchor = "Top, Left, Right, Bottom"
$input.Location = New-Object System.Drawing.Point(12, 12)
$input.Size = New-Object System.Drawing.Size(($bottom.Width - 130), 86)
$bottom.Controls.Add($input)

$sendBtn = New-Object System.Windows.Forms.Button
$sendBtn.Text = "Send"
$sendBtn.FlatStyle = "Flat"
$sendBtn.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 10)
$sendBtn.BackColor = $Theme.Accent
$sendBtn.ForeColor = $Theme.Fg
$sendBtn.FlatAppearance.BorderSize = 0
$sendBtn.Size = New-Object System.Drawing.Size(100, 40)
$sendBtn.Anchor = "Top, Right"
$sendBtn.Location = New-Object System.Drawing.Point(($bottom.Width - 112), 12)
$bottom.Controls.Add($sendBtn)

$hintLabel = New-Object System.Windows.Forms.Label
$hintLabel.Text = "Ctrl+Enter to send"
$hintLabel.Font = New-Object System.Drawing.Font("Segoe UI", 8)
$hintLabel.ForeColor = $Theme.Faint
$hintLabel.AutoSize = $true
$hintLabel.Anchor = "Top, Right"
$hintLabel.Location = New-Object System.Drawing.Point(($bottom.Width - 112), 58)
$bottom.Controls.Add($hintLabel)

# Add transcript LAST so Fill sits between Top and Bottom correctly
$form.Controls.Add($transcript)
$transcript.BringToFront()
$topBar.BringToFront()
$bottom.BringToFront()

# ── Helpers to write into the transcript ──────────────────────────────────────
function Append-Text {
    param([string]$Text, [System.Drawing.Color]$Color, [bool]$Bold = $false)
    $transcript.SelectionStart = $transcript.TextLength
    $transcript.SelectionLength = 0
    $transcript.SelectionColor = $Color
    $style = if ($Bold) { [System.Drawing.FontStyle]::Bold } else { [System.Drawing.FontStyle]::Regular }
    $transcript.SelectionFont = New-Object System.Drawing.Font($transcript.Font, $style)
    $transcript.AppendText($Text)
    $transcript.SelectionStart = $transcript.TextLength
    $transcript.ScrollToCaret()
}

function Append-Role {
    param([string]$Role)
    $color = switch ($Role) {
        "user"      { $Theme.UserRole }
        "assistant" { $Theme.AsstRole }
        "system"    { $Theme.SysRole }
        default     { $Theme.Dim }
    }
    if ($transcript.TextLength -gt 0) { Append-Text -Text "`r`n" -Color $Theme.Fg }
    Append-Text -Text ("{0}`r`n" -f $Role.ToUpper()) -Color $color -Bold $true
}

function Render-Conversation {
    $transcript.Clear()
    foreach ($m in $script:Messages) {
        Append-Role -Role $m.role
        Append-Text -Text ($m.content + "`r`n") -Color $Theme.Fg
    }
}

function Set-Busy {
    param([bool]$Busy)
    $script:Streaming = $Busy
    if ($Busy) {
        $sendBtn.Text = "Stop"
        $sendBtn.BackColor = [System.Drawing.Color]::FromArgb(160, 80, 80)
    } else {
        $sendBtn.Text = "Send"
        $sendBtn.BackColor = $Theme.Accent
    }
}

# ── Status / airgap indicator ─────────────────────────────────────────────────
function Update-AirgapStatus {
    param([bool]$Enabled, [string]$Note = "")
    if ($Enabled) {
        $statusDot.ForeColor = $Theme.AirOn
        $statusLabel.Text = "Airgap: ON — local only"
        $statusLabel.ForeColor = $Theme.AirOn
    } else {
        $statusDot.ForeColor = $Theme.AirOff
        $statusLabel.Text = if ($Note) { "Airgap: OFF — $Note" } else { "Airgap: OFF" }
        $statusLabel.ForeColor = $Theme.AirOff
    }
}

# ── Shared HttpClient (no cookie jar — backend endpoints used are anon-ok) ────
$script:Handler = New-Object System.Net.Http.HttpClientHandler
$script:Handler.UseCookies = $false
$script:Http = New-Object System.Net.Http.HttpClient($script:Handler)
$script:Http.Timeout = [System.TimeSpan]::FromMinutes(30)

function Fetch-Json {
    param([string]$Path, [int]$TimeoutMs = 4000)
    $url = $script:BackendBase + $Path
    $cts = New-Object System.Threading.CancellationTokenSource
    $cts.CancelAfter($TimeoutMs)
    try {
        $resp = $script:Http.GetAsync($url, $cts.Token).GetAwaiter().GetResult()
        if (-not $resp.IsSuccessStatusCode) { throw "HTTP $($resp.StatusCode)" }
        $body = $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        return ($body | ConvertFrom-Json)
    } finally { $cts.Dispose() }
}

function Refresh-Airgap {
    try {
        $s = Fetch-Json -Path "/airgap"
        Update-AirgapStatus -Enabled ([bool]$s.enabled)
        Write-Log "INFO" "Airgap status: enabled=$($s.enabled)"
    } catch {
        Update-AirgapStatus -Enabled $false -Note "backend unreachable"
        Write-Log "WARN" "Airgap probe failed: $($_.Exception.Message)"
    }
}

function Refresh-Tiers {
    try {
        $r = Fetch-Json -Path "/v1/models"
        $tierPicker.Items.Clear()
        foreach ($t in $r.data) {
            [void]$tierPicker.Items.Add($t.id)
        }
        # Select versatile by default, else first.
        $idx = [array]::IndexOf(($tierPicker.Items | ForEach-Object { $_ }), "tier.versatile")
        if ($idx -ge 0) { $tierPicker.SelectedIndex = $idx }
        elseif ($tierPicker.Items.Count -gt 0) { $tierPicker.SelectedIndex = 0 }
        Write-Log "INFO" "Loaded $($tierPicker.Items.Count) tiers"
    } catch {
        if ($tierPicker.Items.Count -eq 0) {
            [void]$tierPicker.Items.Add("tier.versatile")
            $tierPicker.SelectedIndex = 0
        }
        Write-Log "WARN" "Tier load failed: $($_.Exception.Message)"
    }
}

# ── SSE stream in a background Runspace ───────────────────────────────────────
# The runspace is handed: url, body-json, and a callback that marshals tokens
# onto the UI thread via $form.Invoke(). We can't share complex types across
# runspaces cleanly, so we use a synchronized queue the UI polls on a Timer.
$script:EventQueue = [System.Collections.Queue]::Synchronized((New-Object System.Collections.Queue))
$script:StreamRunspace = $null
$script:StreamPowerShell = $null
$script:StreamHandle = $null

$streamScript = {
    param($Url, $BodyJson, [System.Collections.Queue]$Queue, [System.Threading.CancellationToken]$Token)

    try {
        Add-Type -AssemblyName System.Net.Http
        $handler = New-Object System.Net.Http.HttpClientHandler
        $handler.UseCookies = $false
        $client = New-Object System.Net.Http.HttpClient($handler)
        $client.Timeout = [System.TimeSpan]::FromMinutes(30)

        $req = New-Object System.Net.Http.HttpRequestMessage("POST", $Url)
        $req.Content = New-Object System.Net.Http.StringContent($BodyJson, [System.Text.Encoding]::UTF8, "application/json")
        $req.Headers.Accept.Add((New-Object System.Net.Http.Headers.MediaTypeWithQualityHeaderValue("text/event-stream")))

        $resp = $client.SendAsync($req, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead, $Token).GetAwaiter().GetResult()
        if (-not $resp.IsSuccessStatusCode) {
            $err = "HTTP $([int]$resp.StatusCode) $($resp.ReasonPhrase)"
            try { $err += ": " + $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult() } catch { }
            $Queue.Enqueue(@{ kind = "error"; text = $err })
            return
        }

        $stream = $resp.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $reader = New-Object System.IO.StreamReader($stream)

        $eventName = $null
        $dataLines = New-Object System.Collections.Generic.List[string]

        while (-not $reader.EndOfStream -and -not $Token.IsCancellationRequested) {
            $line = $reader.ReadLine()
            if ($null -eq $line) { break }
            if ($line.Length -eq 0) {
                # Event boundary — flush.
                if ($dataLines.Count -gt 0) {
                    $data = [string]::Join("`n", $dataLines)
                    if ($data -eq "[DONE]") {
                        $Queue.Enqueue(@{ kind = "done" })
                    } elseif ($eventName -eq "agent") {
                        $Queue.Enqueue(@{ kind = "agent"; text = $data })
                    } else {
                        # OpenAI-style chunk — pull delta.content.
                        try {
                            $parsed = $data | ConvertFrom-Json
                            $delta = $parsed.choices[0].delta.content
                            if ($delta) { $Queue.Enqueue(@{ kind = "token"; text = $delta }) }
                        } catch { }
                    }
                }
                $eventName = $null
                $dataLines.Clear()
                continue
            }
            if ($line.StartsWith("event:")) { $eventName = $line.Substring(6).Trim() }
            elseif ($line.StartsWith("data:")) { [void]$dataLines.Add($line.Substring(5).TrimStart()) }
            # ignore comments / other fields
        }
        $Queue.Enqueue(@{ kind = "done" })
    } catch [System.OperationCanceledException] {
        $Queue.Enqueue(@{ kind = "cancelled" })
    } catch {
        $Queue.Enqueue(@{ kind = "error"; text = $_.Exception.Message })
    }
}

function Start-Stream {
    param([string]$Url, [string]$BodyJson)
    $script:CancelSrc = New-Object System.Threading.CancellationTokenSource
    $rs = [runspacefactory]::CreateRunspace()
    $rs.ApartmentState = "STA"
    $rs.Open()
    $ps = [powershell]::Create()
    $ps.Runspace = $rs
    [void]$ps.AddScript($streamScript).AddArgument($Url).AddArgument($BodyJson).AddArgument($script:EventQueue).AddArgument($script:CancelSrc.Token)
    $script:StreamRunspace = $rs
    $script:StreamPowerShell = $ps
    $script:StreamHandle = $ps.BeginInvoke()
}

function Dispose-Stream {
    if ($script:StreamPowerShell) {
        try { $script:StreamPowerShell.Dispose() } catch { }
        $script:StreamPowerShell = $null
    }
    if ($script:StreamRunspace) {
        try { $script:StreamRunspace.Close(); $script:StreamRunspace.Dispose() } catch { }
        $script:StreamRunspace = $null
    }
    $script:StreamHandle = $null
    if ($script:CancelSrc) {
        try { $script:CancelSrc.Dispose() } catch { }
        $script:CancelSrc = $null
    }
}

# ── Poll timer: drain queued SSE events onto the UI thread ────────────────────
$pollTimer = New-Object System.Windows.Forms.Timer
$pollTimer.Interval = 40
$pollTimer.Add_Tick({
    while ($script:EventQueue.Count -gt 0) {
        $evt = $script:EventQueue.Dequeue()
        switch ($evt.kind) {
            "token" {
                Append-Text -Text $evt.text -Color $Theme.Fg
                # Track into message buffer.
                $last = $script:Messages[$script:Messages.Count - 1]
                if ($last.role -eq "assistant") { $last.content += $evt.text }
            }
            "agent" {
                # Route/queue/meta info rendered faintly so it doesn't clutter.
                try {
                    $a = $evt.text | ConvertFrom-Json
                    $line = switch ($a.type) {
                        "route.decision" { "  · route: tier=$($a.data.tier) think=$($a.data.think)" }
                        "queue"          { "  · queue: position=$($a.data.position)" }
                        default          { "  · $($a.type)" }
                    }
                    Append-Text -Text ("{0}`r`n" -f $line) -Color $Theme.Faint
                } catch { }
            }
            "error" {
                Append-Text -Text ("`r`n[error] {0}`r`n" -f $evt.text) -Color $Theme.AirOff
                Dispose-Stream
                Set-Busy -Busy $false
            }
            "cancelled" {
                Append-Text -Text "`r`n[stopped]`r`n" -Color $Theme.Dim
                Dispose-Stream
                Set-Busy -Busy $false
            }
            "done" {
                Dispose-Stream
                Set-Busy -Busy $false
                Append-Text -Text "`r`n" -Color $Theme.Fg
            }
        }
    }
})
$pollTimer.Start()

# ── Actions ───────────────────────────────────────────────────────────────────
function Send-Message {
    if ($script:Streaming) {
        # Button acts as Stop when streaming.
        if ($script:CancelSrc) { try { $script:CancelSrc.Cancel() } catch { } }
        return
    }
    $text = $input.Text.Trim()
    if (-not $text) { return }

    # Record user + placeholder assistant in message list and UI.
    [void]$script:Messages.Add(@{ role = "user";      content = $text })
    [void]$script:Messages.Add(@{ role = "assistant"; content = "" })
    Append-Role -Role "user"
    Append-Text -Text ("{0}`r`n" -f $text) -Color $Theme.Fg
    Append-Role -Role "assistant"

    $input.Clear()
    Set-Busy -Busy $true

    # Build OpenAI-compatible request. Exclude the trailing empty assistant
    # placeholder before sending.
    $sendable = @()
    for ($i = 0; $i -lt $script:Messages.Count - 1; $i++) {
        $m = $script:Messages[$i]
        $sendable += @{ role = $m.role; content = $m.content }
    }
    $tier = if ($tierPicker.SelectedItem) { [string]$tierPicker.SelectedItem } else { $script:CurrentTier }
    $body = @{
        model    = $tier
        messages = $sendable
        stream   = $true
    } | ConvertTo-Json -Depth 6 -Compress

    $url = $script:BackendBase + "/v1/chat/completions"
    Write-Log "INFO" "POST $url tier=$tier msgs=$($sendable.Count)"
    Start-Stream -Url $url -BodyJson $body
}

function Reset-Chat {
    if ($script:Streaming) {
        if ($script:CancelSrc) { try { $script:CancelSrc.Cancel() } catch { } }
    }
    $script:Messages.Clear()
    $transcript.Clear()
    $input.Clear()
    $input.Focus()
}

# ── Event wiring ──────────────────────────────────────────────────────────────
$sendBtn.Add_Click({ Send-Message })
$newChatBtn.Add_Click({ Reset-Chat })

$input.Add_KeyDown({
    param($s, $e)
    if ($e.Control -and $e.KeyCode -eq [System.Windows.Forms.Keys]::Enter) {
        $e.SuppressKeyPress = $true
        Send-Message
    }
})

$form.Add_Shown({
    $input.Focus()
    Refresh-Airgap
    Refresh-Tiers
})

$form.Add_FormClosing({
    try { if ($script:CancelSrc) { $script:CancelSrc.Cancel() } } catch { }
    $pollTimer.Stop()
    Dispose-Stream
    try { $script:Http.Dispose() } catch { }
})

# Periodic airgap refresh (cheap, keeps the indicator honest).
$airgapTimer = New-Object System.Windows.Forms.Timer
$airgapTimer.Interval = 15000
$airgapTimer.Add_Tick({ Refresh-Airgap })
$airgapTimer.Start()

[System.Windows.Forms.Application]::Run($form)
