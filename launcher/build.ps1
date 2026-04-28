<#
.SYNOPSIS
    Build LocalAIStack.exe and AirgapChat.exe via ps2exe.
.DESCRIPTION
    Requires: Install-Module -Name ps2exe -Scope CurrentUser
.EXAMPLE
    pwsh -File launcher\build.ps1
#>
$ErrorActionPreference = "Stop"

if (-not (Get-Module -ListAvailable -Name ps2exe)) {
    Write-Host "Installing ps2exe..."
    Install-Module -Name ps2exe -Scope CurrentUser -Force
}
Import-Module ps2exe

$distDir = Join-Path $PSScriptRoot "dist"
if (-not (Test-Path $distDir)) { New-Item -ItemType Directory -Path $distDir | Out-Null }

# The compiled EXE resolves `$PSScriptRoot` to its own directory (dist\), so
# it looks for steps\ and assets\ next to the EXE. Copy them in.
$distSteps = Join-Path $distDir "steps"
if (Test-Path $distSteps) { Remove-Item $distSteps -Recurse -Force }
Copy-Item -Path (Join-Path $PSScriptRoot "steps") -Destination $distSteps -Recurse
$distAssets = Join-Path $distDir "assets"
if (Test-Path (Join-Path $PSScriptRoot "assets")) {
    if (Test-Path $distAssets) { Remove-Item $distAssets -Recurse -Force }
    Copy-Item -Path (Join-Path $PSScriptRoot "assets") -Destination $distAssets -Recurse
}

$iconPath = Join-Path $PSScriptRoot "assets\icon.ico"
$iconArgs = @{}
if (Test-Path $iconPath) { $iconArgs.iconFile = $iconPath }

Invoke-PS2EXE `
    -InputFile  (Join-Path $PSScriptRoot "LocalAIStack.ps1") `
    -OutputFile (Join-Path $distDir "LocalAIStack.exe") `
    -noConsole -title "LocalAIStack" -product "LocalAIStack" `
    -company "kitisathreat" `
    @iconArgs

Invoke-PS2EXE `
    -InputFile  (Join-Path $PSScriptRoot "AirgapChat.ps1") `
    -OutputFile (Join-Path $distDir "AirgapChat.exe") `
    -noConsole -title "LocalAIStack Chat (Airgap)" -product "LocalAIStack" `
    -company "kitisathreat" `
    @iconArgs

Write-Host "Build complete → $distDir"
