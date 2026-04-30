$WEBHOOK_URL = 'https://discordapp.com/api/webhooks/1494815466315714692/JiEbL88dF1N4Lthxhr2TM1tkS9aIRUeqo6bO0ITag8UvR6fM6m8XROKqIdKXIejjQcbt'

[Console]::In.ReadToEnd() | Out-Null  # consume stdin (stop hook data not needed)

$branch = & git branch --show-current 2>$null
if (-not $branch) { $branch = 'unknown' }

$recentCommits = @(& git log --oneline -5 2>$null)
$commitText = if ($recentCommits.Count -gt 0) { $recentCommits -join "`n" } else { 'No commits found' }

$changedFiles = @(& git status --short 2>$null)
$changedCount = $changedFiles.Count

$fields = [System.Collections.Generic.List[object]]::new()
$fields.Add([ordered]@{ name = 'Branch';             value = $branch;          inline = $true })
$fields.Add([ordered]@{ name = 'Uncommitted Files';  value = "$changedCount";  inline = $true })
$fields.Add([ordered]@{ name = 'Recent Commits';     value = "``````$commitText``````"; inline = $false })

if ($changedCount -gt 0) {
    $changedList = ($changedFiles | Select-Object -First 10) -join "`n"
    $fields.Add([ordered]@{ name = 'Pending Changes'; value = "``````$changedList``````"; inline = $false })
}

$embed = [ordered]@{
    title     = 'Claude Code Session Complete'
    color     = 3066993  # green
    fields    = @($fields)
    timestamp = [DateTime]::UtcNow.ToString('o')
}

$payload = [ordered]@{ embeds = @($embed) } | ConvertTo-Json -Depth 10 -Compress

Invoke-RestMethod -Uri $WEBHOOK_URL -Method Post -ContentType 'application/json' -Body $payload
