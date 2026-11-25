# 9N8 hosted runtime

## Current live endpoint

- Supabase project: 9N8 (ref qlfcazdfxexcnzdgpqqm)
- Function: telegram-bot
- Endpoint: https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot
- Deployed version when this file was written: 2
- Status: ACTIVE
- JWT gateway verification: false; the function validates the Telegram webhook secret-token header itself.

## Current live behavior at the time of the snapshot

The deployed telegram-bot function is a single-owner, read-only OKX demo assistant. It uses a single TELEGRAM_OWNER_ID and a single set of global OKX credentials. It supports /start, /help, /id, OKX status, demo balance, ticker and indicator scans. It does not place or cancel orders, onboard multiple users, connect wallets, or administer groups.

The exact deployed source at snapshot time is stored in functions/telegram-bot/index.ts. The universal candidate is developed separately and must be tested before webhook cutover.

## Runtime secrets

The existing function expects these Supabase Edge Function secrets:

- TELEGRAM_BOT_TOKEN
- TELEGRAM_OWNER_ID
- TELEGRAM_WEBHOOK_SECRET
- OKX_API_KEY
- OKX_API_SECRET
- OKX_API_PASSPHRASE
- OKX_API_BASE (optional)

Do not copy real values into this repository or GitHub Actions. Never read or print them in logs.

The next release uses:
- TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET as shared runtime secrets.
- Supabase's server-side project secret/service role key only inside Edge Functions for backend database operations.
- Individual users' exchange credentials stored as Vault secrets, referenced by n8_exchange_connections.
- No exchange API keys in GitHub Actions secrets, public source code, or Telegram chat messages.

The legacy OKX_* environment variables may remain temporarily for the existing owner-only bot. They must not be used as a fallback for other users. After the owner migrates their own connection into Vault and verifies it, remove the legacy OKX_* shared secrets.

## Webhook URL is not a secret

Telegram webhook endpoint:
https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot

Storing this URL as TELEGRAM_WEBHOOK_URL in GitHub Actions does not register the webhook. Telegram registration uses setWebhook with the URL and the same TELEGRAM_WEBHOOK_SECRET configured in Supabase Edge Function Secrets. Never place the bot token in a public URL or GitHub file.

## Safety baseline

- Demo/testnet only until a separately reviewed live-trading release.
- No withdrawals, transfers, leverage, or futures.
- Never collect seed phrases or private keys.
- Never ask users to paste API credentials in ordinary Telegram messages.
- Personal account activity and trade approvals are private-chat only.
- Groups are a separate permission scope; never expose or use personal account connections in groups.
