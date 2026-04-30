$WEBHOOK_URL = 'https://discordapp.com/api/webhooks/1494815466315714692/JiEbL88dF1N4Lthxhr2TM1tkS9aIRUeqo6bO0ITag8UvR6fM6m8XROKqIdKXIejjQcbt'

$raw = [Console]::In.ReadToEnd()
$hookData = $null
if ($raw) { $hookData = $raw | ConvertFrom-Json }

$message = if ($hookData -and $hookData.message) { $hookData.message } else { 'Claude Code needs your attention' }
$title   = if ($hookData -and $hookData.title)   { $hookData.title }   else { 'Waiting for Your Input' }

$embed = [ordered]@{
    title       = $title
    description = $message
    color       = 15844367  # gold
    fields      = @(
        [ordered]@{ name = 'How to Respond'; value = 'Open Chrome Remote Desktop'; inline = $false }
    )
    timestamp   = [DateTime]::UtcNow.ToString('o')
}

$payload = [ordered]@{ embeds = @($embed) } | ConvertTo-Json -Depth 10 -Compress

Invoke-RestMethod -Uri $WEBHOOK_URL -Method Post -ContentType 'application/json' -Body $payload
