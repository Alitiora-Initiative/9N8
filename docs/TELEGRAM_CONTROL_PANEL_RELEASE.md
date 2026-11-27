# 9N8 Telegram control-panel release

## Changes
- One persistent Telegram dashboard message is stored on each n8_users row and edited as the user navigates.
- Inline controls expose Paper, gated Live, Manual Quick-Snipe, Positions, Settings, Anti-Rug, Networks, History and Withdrawal Guidance.
- Adds /snipe, /history and /withdraw command handlers.
- Network selection reports only Solana as execution-enabled; Base, Ethereum and BNB remain unavailable.
- Positions and history use the Telegram account's actual Supabase ledger rows and do not invent fills or PnL.
- The screens explain that the separate Python worker and Neon ledger are not synchronized to this webhook.

## Production topology
Telegram inbound updates are handled by Supabase Edge Function telegram-bot-next. Python aiogram is a separate worker and polling is disabled in webhook production mode. A Python UI edit alone therefore does not update Telegram.

Vercel backend project 9N8-trading-agent is configured with root directory src even though index.py, vercel.toml, requirements.txt and worker are at repository root. Its latest deployment failed before build with NOW_SANDBOX_WORKER_ROOTDIR_NOT_EXIST.

The Vercel backend lacks runtime values required by config.py and serverless_app.py: DATABASE_URL, TELEGRAM_BOT_TOKEN, AUTHORIZED_USER_ID, HELIUS_API_KEY (or both explicit RPC/WSS endpoints), MASTER_TREASURY_ADDRESS, ENCRYPTION_MASTER_KEY and N9N8_WORKER_BRIDGE_SECRET. The daemon also needs valid Vercel Queue bindings. The Python scanner and Neon position ledger cannot be expected to run until these are configured and health-checked.

## Execution safety
- Paper remains virtual and distinct from Live.
- This release does not enable Live trading, bypass Jupiter certification or enable EVM execution.
- Pasted token mints are format-checked only. The webhook does not claim to run an on-chain scan or buy.
- Withdrawal guidance is self-custody only; the webhook does not custody SOL or submit transfers.

## Release verification
1. Apply migration 20261126224000_n8_telegram_dashboard_message.sql before deploying the function.
2. Deploy telegram-bot-next with index.ts, existing dependencies and telegram-ui.ts.
3. Run Deno network and Telegram UI tests and type-check the function.
4. Test /start, Networks, Settings and Back to Dashboard; verify one message is edited and EVM selections remain blocked.
5. Repair the Vercel backend root-directory setting and configure runtime secrets before expecting the Python daemon to run.

## Engine & Diagnostics Telegram screen

- The dashboard now exposes **Engine & Diagnostics**, with `/engine` and `/diagnostics` aliases.
- It inventories the actual Telegram webhook, Jupiter Swap V2 safeguards, separate paper worker/ledger, Python Solana modules and exchange API-permission inspection.
- Environment status is intentionally a presence check only. It does not claim credentials are valid or providers are reachable.
- The screen explicitly identifies the Python worker/Neon wallet, positions, PnL, heartbeat and strategy settings as not synchronized to this Telegram webhook. Repository code does not prove a production worker is running.
- Base, Ethereum and BNB Chain remain unavailable; Live mode remains gated. Opening diagnostics never submits a transaction.
- Deno unit tests cover both all-configured and all-missing cases and guard against false worker-health claims.
