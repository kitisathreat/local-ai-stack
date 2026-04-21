<#
.SYNOPSIS
    LocalAIStack launcher — silent Windows GUI orchestrator.
.DESCRIPTION
    Shows a progress window while starting Docker and the containerized stack
    (backend + frontend + Ollama + optional Cloudflare Tunnel). Spawns all
    child processes hidden. On success, opens the chat UI in the default
    browser and minimizes to the system tray.
.NOTES
    Compiled to .exe via launcher/build.ps1 (uses ps2exe).
    Must not print to a console — all output goes to $logPath.
#>

param(
    [switch]$DevMode
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# ── Paths ─────────────────────────────────────────────────────────────────────
$repoRoot    = Split-Path $PSScriptRoot -Parent
$stepsDir    = Join-Path $PSScriptRoot "steps"
$appDataDir  = Join-Path $env:APPDATA "LocalAIStack"
$logPath     = Join-Path $appDataDir "launcher.log"
$iconPath    = Join-Path $PSScriptRoot "assets\icon.ico"

# Default to local frontend. Override by setting PUBLIC_BASE_URL in .env.local
# (e.g. to the Cloudflare Tunnel hostname) to open the public URL instead.
$chatUrl = "http://localhost:3000"
$envLocal = Join-Path $repoRoot ".env.local"
if (Test-Path $envLocal) {
    $m = Select-String -Path $envLocal -Pattern "^PUBLIC_BASE_URL=(.+)$" | Select-Object -First 1
    if ($m) { $chatUrl = $m.Matches[0].Groups[1].Value.Trim() }
}

if (-not (Test-Path $appDataDir)) { New-Item -ItemType Directory -Path $appDataDir | Out-Null }

# ── Logging (file-only; rotates at 2MB, keeps 5) ──────────────────────────────
function Rotate-Log {
    if ((Test-Path $logPath) -and (Get-Item $logPath).Length -gt 2MB) {
        for ($i = 4; $i -ge 1; $i--) {
            $src = "$logPath.$i"; $dst = "$logPath.$($i+1)"
            if (Test-Path $src) { Move-Item $src $dst -Force }
        }
        Move-Item $logPath "$logPath.1" -Force
    }
}
Rotate-Log

function Write-Log {
    param([string]$Level, [string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
    "[$ts] [$Level] $Message" | Out-File -FilePath $logPath -Append -Encoding utf8
}

# ── Steps: each returns @{ok=$bool; message=$string; needsUser=$bool} ─────────
$steps = @(
    @{ Name = "Starting Docker Desktop";            Script = "ensure-docker.ps1" },
    @{ Name = "Checking tunnel config";             Script = "ensure-tunnel.ps1" },
    @{ Name = "Bringing up services";               Script = "compose-up.ps1" },
    @{ Name = "Waiting for services to be ready";   Script = "wait-ready.ps1" }
)

# ── Progress window ───────────────────────────────────────────────────────────
$form                  = New-Object System.Windows.Forms.Form
$form.Text             = "LocalAIStack"
$form.FormBorderStyle  = "FixedSingle"
$form.StartPosition    = "CenterScreen"
$form.ClientSize       = New-Object System.Drawing.Size(420, 220)
$form.BackColor        = [System.Drawing.Color]::FromArgb(15, 15, 15)
$form.ForeColor        = [System.Drawing.Color]::White
$form.MaximizeBox      = $false
$form.MinimizeBox      = $true
$form.TopMost          = $true
if (Test-Path $iconPath) { $form.Icon = New-Object System.Drawing.Icon($iconPath) }

$titleLabel            = New-Object System.Windows.Forms.Label
$titleLabel.Text       = "LocalAIStack"
$titleLabel.Font       = New-Object System.Drawing.Font("Segoe UI Semibold", 14)
$titleLabel.ForeColor  = [System.Drawing.Color]::White
$titleLabel.Location   = New-Object System.Drawing.Point(20, 20)
$titleLabel.Size       = New-Object System.Drawing.Size(380, 28)
$form.Controls.Add($titleLabel)

$statusLabel           = New-Object System.Windows.Forms.Label
$statusLabel.Text      = "Starting..."
$statusLabel.Font      = New-Object System.Drawing.Font("Segoe UI", 9)
$statusLabel.ForeColor = [System.Drawing.Color]::FromArgb(160, 160, 160)
$statusLabel.Location  = New-Object System.Drawing.Point(20, 60)
$statusLabel.Size      = New-Object System.Drawing.Size(380, 20)
$form.Controls.Add($statusLabel)

$progressBar           = New-Object System.Windows.Forms.ProgressBar
$progressBar.Location  = New-Object System.Drawing.Point(20, 95)
$progressBar.Size      = New-Object System.Drawing.Size(380, 8)
$progressBar.Minimum   = 0
$progressBar.Maximum   = $steps.Count
$progressBar.Value     = 0
$progressBar.Style     = "Continuous"
$form.Controls.Add($progressBar)

$detailLabel           = New-Object System.Windows.Forms.Label
$detailLabel.Font      = New-Object System.Drawing.Font("Segoe UI", 8)
$detailLabel.ForeColor = [System.Drawing.Color]::FromArgb(100, 100, 100)
$detailLabel.Location  = New-Object System.Drawing.Point(20, 115)
$detailLabel.Size      = New-Object System.Drawing.Size(380, 60)
$form.Controls.Add($detailLabel)

$logsButton            = New-Object System.Windows.Forms.LinkLabel
$logsButton.Text       = "View logs"
$logsButton.Location   = New-Object System.Drawing.Point(20, 185)
$logsButton.Size       = New-Object System.Drawing.Size(80, 20)
$logsButton.LinkColor  = [System.Drawing.Color]::FromArgb(120, 140, 220)
$logsButton.Add_LinkClicked({ Start-Process notepad.exe $logPath })
$form.Controls.Add($logsButton)

# ── Tray icon ─────────────────────────────────────────────────────────────────
$tray                  = New-Object System.Windows.Forms.NotifyIcon
$tray.Text             = "LocalAIStack"
$tray.Visible          = $false
if (Test-Path $iconPath) { $tray.Icon = New-Object System.Drawing.Icon($iconPath) }
else { $tray.Icon = [System.Drawing.SystemIcons]::Application }

# Path to the airgap desktop chat app. Prefer the packaged .exe (same dir
# when running from dist/, or launcher/dist/ when running the .ps1 directly);
# fall back to executing the .ps1 via PowerShell if the .exe isn't present.
$airgapChatExe = Join-Path $PSScriptRoot "AirgapChat.exe"
if (-not (Test-Path $airgapChatExe)) {
    $airgapChatExe = Join-Path $PSScriptRoot "dist\AirgapChat.exe"
}
$airgapChatPs1 = Join-Path $PSScriptRoot "AirgapChat.ps1"

function Open-AirgapChat {
    if (Test-Path $airgapChatExe) {
        Start-Process -FilePath $airgapChatExe
    } elseif (Test-Path $airgapChatPs1) {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName        = (Get-Command pwsh -ErrorAction SilentlyContinue).Path
        if (-not $psi.FileName) { $psi.FileName = "powershell.exe" }
        $psi.Arguments       = "-NoProfile -ExecutionPolicy Bypass -File `"$airgapChatPs1`""
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow  = $true
        [System.Diagnostics.Process]::Start($psi) | Out-Null
    } else {
        [System.Windows.Forms.MessageBox]::Show(
            "AirgapChat.exe not found. Build it with launcher\build.ps1.",
            "LocalAIStack",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
    }
}

$trayMenu              = New-Object System.Windows.Forms.ContextMenuStrip
$openItem              = $trayMenu.Items.Add("Open Chat")
$openItem.Add_Click({ Start-Process $chatUrl })
$airgapChatItem        = $trayMenu.Items.Add("Open Airgap Chat (desktop)")
$airgapChatItem.Add_Click({ Open-AirgapChat })
$logsItem              = $trayMenu.Items.Add("View Logs")
$logsItem.Add_Click({ Start-Process notepad.exe $logPath })
$restartItem           = $trayMenu.Items.Add("Restart")
$restartItem.Add_Click({
    Start-Process -FilePath ([System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName)
    $tray.Visible = $false; $form.Close()
})
$stopItem              = $trayMenu.Items.Add("Stop && Exit")
$stopItem.Add_Click({ Stop-Stack; $tray.Visible = $false; $form.Close() })
$tray.ContextMenuStrip = $trayMenu
$tray.Add_MouseClick({ param($s, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) { Start-Process $chatUrl }
})

# ── Run a step hidden, capture output to log ──────────────────────────────────
function Invoke-Step {
    param([string]$ScriptName, [string]$DisplayName)
    $scriptPath = Join-Path $stepsDir $ScriptName
    Write-Log "INFO" "Running step: $DisplayName ($ScriptName)"

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName               = (Get-Command pwsh -ErrorAction SilentlyContinue).Path
    if (-not $psi.FileName)     { $psi.FileName = "powershell.exe" }
    $psi.Arguments              = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$scriptPath`" -RepoRoot `"$repoRoot`""
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.UseShellExecute        = $false
    $psi.CreateNoWindow         = $true
    $psi.WindowStyle            = "Hidden"

    $proc = [System.Diagnostics.Process]::Start($psi)
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()

    if ($stdout) { Write-Log "STDOUT" ($stdout.TrimEnd()) }
    if ($stderr) { Write-Log "STDERR" ($stderr.TrimEnd()) }

    try   { $result = $stdout | ConvertFrom-Json }
    catch { $result = @{ ok = ($proc.ExitCode -eq 0); message = $stdout; needsUser = $false } }

    return $result
}

# ── User-input dialog (only when we genuinely can't auto-remediate) ───────────
function Show-UserDialog {
    param([string]$Title, [string]$Message, [string]$ActionLabel = "Install", [string]$ActionUrl = "")
    $dlg                 = New-Object System.Windows.Forms.Form
    $dlg.Text            = $Title
    $dlg.FormBorderStyle = "FixedDialog"
    $dlg.StartPosition   = "CenterParent"
    $dlg.ClientSize      = New-Object System.Drawing.Size(420, 180)
    $dlg.BackColor       = [System.Drawing.Color]::FromArgb(15, 15, 15)
    $dlg.ForeColor       = [System.Drawing.Color]::White
    $dlg.TopMost         = $true

    $msg               = New-Object System.Windows.Forms.Label
    $msg.Text          = $Message
    $msg.Font          = New-Object System.Drawing.Font("Segoe UI", 10)
    $msg.Location      = New-Object System.Drawing.Point(20, 20)
    $msg.Size          = New-Object System.Drawing.Size(380, 90)
    $dlg.Controls.Add($msg)

    $actionBtn         = New-Object System.Windows.Forms.Button
    $actionBtn.Text    = $ActionLabel
    $actionBtn.Size    = New-Object System.Drawing.Size(100, 28)
    $actionBtn.Location = New-Object System.Drawing.Point(200, 130)
    $actionBtn.Add_Click({ if ($ActionUrl) { Start-Process $ActionUrl }; $dlg.DialogResult = "OK"; $dlg.Close() })
    $dlg.Controls.Add($actionBtn)
    $dlg.AcceptButton  = $actionBtn

    $cancelBtn         = New-Object System.Windows.Forms.Button
    $cancelBtn.Text    = "Cancel"
    $cancelBtn.Size    = New-Object System.Drawing.Size(90, 28)
    $cancelBtn.Location = New-Object System.Drawing.Point(310, 130)
    $cancelBtn.Add_Click({ $dlg.DialogResult = "Cancel"; $dlg.Close() })
    $dlg.Controls.Add($cancelBtn)
    $dlg.CancelButton  = $cancelBtn

    return $dlg.ShowDialog($form)
}

# ── Stop orchestrator ─────────────────────────────────────────────────────────
function Stop-Stack {
    Write-Log "INFO" "Stopping stack..."
    $stopScript = Join-Path $repoRoot "scripts\stop.ps1"
    if (Test-Path $stopScript) {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName        = "powershell.exe"
        $psi.Arguments       = "-NoProfile -ExecutionPolicy Bypass -File `"$stopScript`""
        $psi.CreateNoWindow  = $true
        $psi.WindowStyle     = "Hidden"
        $psi.UseShellExecute = $false
        [System.Diagnostics.Process]::Start($psi) | Out-Null
    }
}

# ── Run steps on a background thread so UI stays responsive ───────────────────
$form.Add_Shown({
    $form.Refresh()
    $stepIndex = 0
    foreach ($step in $steps) {
        $statusLabel.Text = $step.Name + "..."
        $form.Refresh()

        $res = Invoke-Step -ScriptName $step.Script -DisplayName $step.Name

        if (-not $res.ok) {
            if ($res.needsUser) {
                $actionLabel = if ($res.actionLabel) { $res.actionLabel } else { "OK" }
                $actionUrl   = if ($res.actionUrl)   { $res.actionUrl }   else { "" }
                $dlgResult = Show-UserDialog -Title "LocalAIStack" -Message $res.message `
                    -ActionLabel $actionLabel -ActionUrl $actionUrl
                if ($dlgResult -ne "OK") {
                    Write-Log "WARN" "User cancelled at step $($step.Name)"
                    $form.Close(); return
                }
                $res = Invoke-Step -ScriptName $step.Script -DisplayName $step.Name
                if (-not $res.ok) {
                    Show-UserDialog -Title "LocalAIStack — cannot continue" -Message $res.message `
                        -ActionLabel "Open Logs" -ActionUrl $logPath | Out-Null
                    $form.Close(); return
                }
            } else {
                Show-UserDialog -Title "LocalAIStack — error" -Message $res.message `
                    -ActionLabel "Open Logs" -ActionUrl $logPath | Out-Null
                $form.Close(); return
            }
        }

        $stepIndex++
        $progressBar.Value = $stepIndex
        $detailLabel.Text  = $res.message
        $form.Refresh()
    }

    # All steps succeeded
    $statusLabel.Text = "Ready"
    $detailLabel.Text = "Opening $chatUrl..."
    $form.Refresh()
    Start-Sleep -Milliseconds 500
    Start-Process $chatUrl
    $tray.Visible = $true
    $tray.ShowBalloonTip(2000, "LocalAIStack", "Stack is ready at $chatUrl", "Info")
    $form.Hide()
})

$form.Add_FormClosing({
    $tray.Visible = $false
    $tray.Dispose()
})

[System.Windows.Forms.Application]::Run($form)
