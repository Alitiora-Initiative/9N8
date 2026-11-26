# 9N8 hosted runtime

## Environments and endpoints

### Existing legacy function — preserved, not the universal bot
- Project: 9N8 (ref `qlfcazdfxexcnzdgpqqm`)
- Function: `telegram-bot`
- Endpoint: `https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot`
- Deployed version inspected: 2 (ACTIVE)
- Behavior: original single-owner, read-only OKX demo assistant.
- The legacy function's Telegram webhook has not been intentionally changed during the universal migration.

### Universal candidate — deployed, awaiting secrets and webhook registration
- Function: `telegram-bot-next`
- API/webhook endpoint: `https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot-next`
- Deployed version inspected: 5 (ACTIVE)
- JWT gateway verification: false; webhook traffic is authenticated by the Telegram secret-token header; Mini App actions validate Telegram-signed `initData`.
- Telegram webhook is not registered to this endpoint until the owner adds the missing runtime secrets and submits the setup form.

### Secure frontend
- Vercel project: `9n8-secure-connect`
- Public app URL: `https://9n8-secure-connect.vercel.app/connect`
- Webhook setup page: `https://9n8-secure-connect.vercel.app/setup`
- Frontend is a static HTTPS app. It never contains Supabase service-role/secret keys or the Telegram bot token.
- It sends Mini App `initData` and user-entered credentials directly to the Supabase function over HTTPS. The backend validates identity and stores exchange credentials in Supabase Vault.

## Shared runtime secrets still required

The universal candidate health check currently reported that these two user-managed secrets were missing in Supabase:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`

Add them in the Supabase Edge Function Secrets UI:
`https://supabase.com/dashboard/project/qlfcazdfxexcnzdgpqqm/functions/secrets`

The webhook secret must use only letters, digits, underscores, and hyphens. Keep the same value for Supabase's secret and the setup form. The setup page passes it to the server-side Telegram `setWebhook` call. Do not put these values in GitHub repository files or chat messages.

Supabase-managed `SUPABASE_URL` and `SUPABASE_SECRET_KEYS` are read inside the Edge Function runtime. The health check confirmed the database secret key is present. Never put a Supabase service/secret key in the frontend.

The new universal candidate does not read `TELEGRAM_OWNER_ID` or any global `OKX_API_*` variable. The legacy function still has its old environment-variable contract. Do not move a shared exchange key into the universal function; each user supplies their own demo/testnet API credentials through the secure Mini App.

## Current candidate capabilities

Implemented and deployed:
- automatic user registration from verified Telegram updates;
- per-user database records with RLS and backend-only access;
- Vault-backed per-user exchange credential storage and owner-scoped retrieval/revocation RPCs;
- account credential validation for OKX Demo, Binance Spot Testnet, and Bybit Demo;
- watch-only public EVM/Solana wallet address records labeled for Trust Wallet, Phantom, WalletConnect or manual entry;
- private account connection and wallet pages in the Vercel Mini App;
- Telegram group configuration gated by current human admin status and bot admin rights;
- manual `/n8delete`, `/n8ban`, `/n8unban` commands in moderator mode;
- webhook update leases with duplicate suppression and retry after an expired lease;
- audit records that do not store raw API credentials.

Not implemented yet:
- trading/order placement or cancellation in the universal candidate;
- automated strategies, scheduled scanning, or trade execution;
- live trading;
- actual WalletConnect/Phantom wallet sessions, transaction signing or token balance/transaction indexing;
- complete credential management/revoke UI and account dashboard;
- group market-news feeds and automatic anti-spam moderation.

The current universal candidate is **read/validation only**. It is not an autonomous trading bot yet.

## Safe operating rules

- Demo/testnet only; do not submit live API keys in this initial release.
- Never request withdrawal, wallet-transfer, funding-transfer, margin, futures or derivatives permissions.
- OKX Trade permission can include funding-transfer capability; the app does not call transfer endpoints.
- Never collect seed phrases, wallet private keys, exchange passwords, OTP codes or withdrawal codes.
- Never ask users to paste API credentials into ordinary Telegram messages.
- Personal balances and exchange connection records are private-chat only.
- Group commands cannot read or use members' personal exchange or wallet accounts.
- Promoting a Telegram bot to administrator can expose group messages to it. Explain this to group owners before requesting moderation rights.
- Keep GitHub Actions secrets, Supabase project secrets and per-user Vault credentials in their distinct roles.
