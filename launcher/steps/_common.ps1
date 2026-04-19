# Shared helpers for step scripts. Each step emits a single JSON line on stdout.
# Schema: { ok: bool, message: string, needsUser?: bool, actionLabel?: string, actionUrl?: string }

function Emit-Result {
    param(
        [Parameter(Mandatory=$true)][bool]$Ok,
        [Parameter(Mandatory=$true)][string]$Message,
        [bool]$NeedsUser = $false,
        [string]$ActionLabel = "",
        [string]$ActionUrl = ""
    )
    $obj = [ordered]@{
        ok        = $Ok
        message   = $Message
        needsUser = $NeedsUser
    }
    if ($ActionLabel) { $obj.actionLabel = $ActionLabel }
    if ($ActionUrl)   { $obj.actionUrl = $ActionUrl }
    $obj | ConvertTo-Json -Compress | Write-Output
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}
