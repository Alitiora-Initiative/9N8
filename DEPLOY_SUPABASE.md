# 9N8 hosted Telegram setup (Supabase)

This hosted, event-driven Telegram entry point removes the need to keep a laptop running just to receive Telegram commands.

## What it supports

- Telegram webhook delivery to Supabase over HTTPS.
- /start opens the 9N8 menu.
- /id, /help, /okx_status, /balance, /ticker, and /scan.
- OKX private reads use the demo-trading request header.
- Only one configured Telegram user ID can use the bot, only in a private chat.
- This hosted first release is read-only: it does not place or cancel orders.

## Add Edge Function secrets

In Supabase Dashboard → project 9N8 → Edge Functions → Secrets, add:

| Name | Value |
|---|---|
| TELEGRAM_BOT_TOKEN | Token from BotFather |
| TELEGRAM_WEBHOOK_SECRET | Random 32+ character string using only letters, numbers, underscore and hyphen |
| TELEGRAM_OWNER_ID | Leave empty initially; set after /id |
| OKX_API_KEY | Your OKX demo API key |
| OKX_API_SECRET | Your OKX demo Secret Key |
| OKX_API_PASSPHRASE | Your new private passphrase |
| OKX_API_BASE | https://www.okx.com (or the official regional domain for your OKX account) |

Do not put these values in GitHub, Telegram, source files, or this chat. Keep OKX Withdraw permission OFF. The trusted-IP field can remain empty for demo testing.

## Register the Telegram webhook from PowerShell

After saving TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET in Supabase Secrets, open PowerShell and paste this block. It prompts locally; do not share the values:

~~~powershell
$botToken = Read-Host "Paste your Telegram bot token"
$webhookSecret = Read-Host "Paste the exact TELEGRAM_WEBHOOK_SECRET saved in Supabase"
$body = @{
  url = "https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot"
  secret_token = $webhookSecret
  allowed_updates = @("message", "callback_query")
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$botToken/setWebhook" -ContentType "application/json" -Body $body
~~~

A successful response includes ok : True. Then send /id to your Telegram bot and add the returned number as TELEGRAM_OWNER_ID in Supabase Edge Function Secrets. Secret changes are available to deployed functions without redeployment.

## Verify

1. Open https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot in a browser. It should return a small JSON health response.
2. Send /id and set the owner ID.
3. Send /start, then /okx_status.
4. Send /balance and /scan BTC-USDT 15m.

## Limitations

This webhook handles Telegram updates whenever they arrive. It does not run a continuous market scanner between messages. This initial hosted release is read-only; it does not submit orders. A scheduled monitoring engine and a hosted order-confirmation flow should be added and tested before any trading is enabled.

Supabase Free projects can pause after a week of inactivity. Check the project dashboard if Telegram stops receiving updates after a long period without use.
