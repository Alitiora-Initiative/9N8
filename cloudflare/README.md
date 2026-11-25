# 9N8 Cloudflare Worker (paper-trading only)

This is a Cloudflare-native deployment target for 9N8. It is a **separate migration branch**; the existing Python/Docker application on `main` is not replaced.

## What this version does

- Telegram webhook receiver protected by Telegram's `X-Telegram-Bot-Api-Secret-Token` header.
- Multi-user private account registration and isolated settings in Cloudflare D1.
- Per-user lot cap, maximum open positions, default SL/TP and break-even trigger/offset.
- Paper-only entries and exits. Each paper entry requires a positively verified MetaApi MT5 DEMO account and fresh read-only quote/specification data.
- Scheduled break-even, simulated SL/TP monitoring (up to 30 open positions per scheduler run).
- Twelve Data price/EMA(9)/EMA(21)/RSI(14) observations, saved signals and notification opt-in.
- User notifications, explicit anonymous-statistics opt-in, verified group/channel report destinations and weekly aggregate reports.
- Platform emergency pause for new simulated entries.
- D1 database persistence across deployments.

**This Worker does not send MT5 orders, manage broker funds, make withdrawals, or promise profitable signals.** All trade records are simulated and P&L remains in the instrument's quote currency; it is not converted to account currency. The $10,000 paper reference capital is not money and has no withdrawal value.

## Free-plan operating envelope

The Cloudflare Workers Free plan currently includes up to 100,000 requests/day and 10 ms CPU time per invocation. D1 Free currently includes 5 GB total storage, 5 million rows read/day and 100,000 rows written/day. These are hard usage limits; Cloudflare can reject operations after a limit is reached. Scheduled monitoring uses one Cron Trigger every minute, scans at most 30 open positions per run, and scheduled Twelve Data scans run about every 15 minutes. This is a low-cost starter deployment, not an unlimited or guaranteed low-latency trading service.

Official references:
- https://developers.cloudflare.com/workers/platform/limits/
- https://developers.cloudflare.com/d1/platform/pricing/
- https://developers.cloudflare.com/workers/configuration/cron-triggers/
- https://core.telegram.org/bots/api#setwebhook

## Required Cloudflare resources

1. A Cloudflare account on the Free plan.
2. One Worker named **`9n8`**.
3. One D1 database named **`9n8-db`**, bound to the Worker as **`DB`**.
4. The SQL migration in `migrations/0001_initial.sql` applied to the remote D1 database.
5. A `workers.dev` URL enabled. A custom domain is not required.

## Deploy from the dashboard

1. Open https://dash.cloudflare.com/ and sign in.
2. Go to **Workers & Pages** → **Create application** → **Workers** → **Connect to Git** (or the GitHub import option shown).
3. Connect the GitHub account that owns `Alitiora-Initiative/9N8`.
4. Select repository `9N8`, branch **`cloudflare-worker`**.
5. Set the project root / root directory to **`cloudflare`**.
6. The Worker name must be **`9n8`** to match `wrangler.toml`.
7. Use build command `npm install` and deploy command `npx wrangler deploy`.
8. Do not deploy until the D1 database ID in `wrangler.toml` has been replaced with your real D1 database ID (see below).

Cloudflare Workers Builds can deploy this branch on future Git pushes. It must have access to the GitHub repository through the Cloudflare Workers & Pages GitHub App.

## Create and connect D1

In Cloudflare Dashboard:

1. Go to **Storage & databases** → **D1 SQL Database** → **Create database**.
2. Name it `9n8-db`.
3. Copy the database ID from the database details page. The ID is not a password or API secret.
4. Replace the placeholder `REPLACE_WITH_D1_DATABASE_ID` in `wrangler.toml` with that ID and commit the change to the `cloudflare-worker` branch.
5. In the D1 database's **Console**, execute the complete contents of `migrations/0001_initial.sql` once. Alternatively, use the Wrangler migration command below.

CLI alternative (requires Node.js 20+ and Wrangler):
```powershell
cd cloudflare
npm install
npx wrangler login
npx wrangler d1 create 9n8-db
# Copy the returned database_id into wrangler.toml.
npx wrangler d1 migrations apply 9n8-db --remote
```

Do not execute the SQL migration twice unless you first remove the existing tables; the migration uses CREATE TABLE IF NOT EXISTS but includes indexes, so it is designed to be repeatable in the normal first-time setup.

## Configure encrypted Worker secrets

Open **Workers & Pages** → **9n8** → **Settings** → **Variables and Secrets**. Add these values as encrypted secrets (not plain committed text):

| Secret | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | BotFather token |
| `TELEGRAM_WEBHOOK_SECRET` | Yes | Random secret for webhook verification and one-time webhook setup |
| `TELEGRAM_OWNER_ID` | Initially set to `0` | Your numeric Telegram user ID; set after first bot test |
| `TWELVEDATA_API_KEY` | Optional | /price, /scan and scheduled signal observations |
| `METAAPI_TOKEN` | Optional initially | Read-only MT5 DEMO market-data API |
| `METAAPI_ACCOUNT_ID` | Optional initially | MetaApi DEMO account identifier |

Optional non-secret variables: `WATCHLIST`, `TIMEFRAME`, `OUTPUT_SIZE`, `PAPER_INITIAL_BALANCE`, `PAPER_CURRENCY`, `PAPER_SLIPPAGE_POINTS`, `PAPER_MAX_TICK_AGE_SECONDS`, `REPORT_DAY`, `REPORT_TIME`, `REPORT_TIMEZONE`, `METAAPI_API_BASE`, `METAAPI_MARKET_DATA_BASE`.

Default report schedule: Monday 09:00 Africa/Nairobi. Default signal watchlist: XAU/USD, EUR/USD, GBP/USD, USD/JPY and XAG/USD.

**Do not paste tokens into GitHub source, commit them to the repository, or send them in chat.** GitHub Actions secrets are not automatically copied into Cloudflare. Enter the values directly in Cloudflare's encrypted secret settings. If a Telegram token has ever been posted publicly or shared in an untrusted location, revoke it in BotFather and use a new token.

If MetaApi credentials are not configured, Telegram commands and Twelve Data signal functions can work, but MT5 commands and paper-position opening/monitoring will report that DEMO market data is unavailable. Do not try this against a live MetaTrader account.

## Apply database and deploy

After the D1 ID is updated and the migration is applied, deploy the Worker from Cloudflare. The Worker should expose:
- `GET /` and `GET /health` — health checks.
- `POST /setup` — registers the Telegram webhook; requires the webhook secret as a Bearer token.
- `POST /webhook` — Telegram updates; validates Telegram's secret-token header.

## Register the Telegram webhook

After deployment, copy the public `workers.dev` URL from Cloudflare. On Windows PowerShell, use this interactive command (it does not display the secret while you type):

```powershell
$worker = Read-Host "Paste your Worker URL (https://...workers.dev)"
$secret = Read-Host "Enter TELEGRAM_WEBHOOK_SECRET"
$headers = @{ Authorization = "Bearer $secret" }
Invoke-RestMethod -Method Post -Uri "$worker/setup" -Headers $headers
```

A successful response contains `"ok": true`. The Worker uses the same secret as Telegram's webhook header validation.

Then open your Telegram bot and send `/start`, followed by `/id`. Copy the numeric ID shown. In Cloudflare's Worker settings, update `TELEGRAM_OWNER_ID` to that number and deploy the updated settings. This enables the administrator-only reporting and pause commands.

## Commands

Personal: `/start`, `/help`, `/id`, `/settings`, `/status`, `/setlots`, `/setmaxpositions`, `/setsl`, `/settp`, `/setbe`, `/notifications`, `/share_stats`, `/paper_enable`, `/paper_pause`, `/paper_open`, `/paper_positions`, `/paper_close`, `/paper_check`, `/paper_history`, `/paper_balance`, `/stats`, `/mt5_status`, `/mt5_symbols`, `/mt5_price`, `/mt5_scan`, `/price`, `/scan`, `/signals`.

Administrator (private chat only): `/users`, `/pause_all`, `/resume_all`, `/report_addchat`, `/report_removechat`, `/report_chats`, `/reportschedule`, `/report_now`, `/announce`.

## Important migration notes

- This branch starts a **new D1 database**. It does not automatically import an existing SQLite file. Keep the original Python app and its history on `main`.
- Webhook mode replaces Telegram long polling. Do not run the Python polling process and this webhook deployment against the same bot token at the same time.
- The Worker checks paper positions once per minute, not every 10 seconds as the original Python worker did. The scheduled run checks at most 30 open positions; this is not a high-frequency or guaranteed execution service.
- Telegram users must open a private chat and send `/start` before the bot can send them direct messages.
- New paper positions are paused by default. The owner can pause all new entries with `/pause_all`; existing simulations continue to be monitored where market data is configured.
- The original Python/Docker service on `main` is unchanged. Do not deploy both polling and webhook versions with the same Telegram token concurrently.

## Local checks

```powershell
cd cloudflare
npm install
npm test
```
