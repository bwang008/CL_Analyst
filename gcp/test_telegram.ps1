function Send-TelegramAlert {
    param([string]$Message)
    $envPath = '.env'
    $env_vars = @{}
    if (Test-Path $envPath) {
        Get-Content $envPath | ForEach-Object {
            if ($_ -match '^([A-Z_][A-Z0-9_]*)=(.*)$') {
                $env_vars[$Matches[1]] = $Matches[2].Trim()
            }
        }
    }
    $token = $env_vars['TELEGRAM_BOT_TOKEN']
    $chatId = $env_vars['TELEGRAM_CHAT_ID']
    if (-not $token) { Write-Host 'NO TOKEN'; return }
    $bodyObj = @{ chat_id = $chatId; text = $Message }
    $bodyJson = $bodyObj | ConvertTo-Json -Compress
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($bodyJson)
    try {
        $response = Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$token/sendMessage" -ContentType 'application/json; charset=utf-8' -Body $bodyBytes
        Write-Host "Telegram message sent successfully!"
    } catch {
        Write-Host "Failed to send telegram message: $_"
    }
}
Send-TelegramAlert 'Test Message from Antigravity Agent!'
