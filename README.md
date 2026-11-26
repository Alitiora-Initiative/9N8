# 9N8 — Solana Telegram Launchpad Bot

9N8 is a Telegram-first Solana launchpad monitoring and trading application. Telegram runs command handling and notifications; Vercel serves the FastAPI Mini App/API; a Vercel Daemon runs the continuous Python trading worker; a dedicated Neon PostgreSQL project stores encrypted wallets, settings, positions, receipts, risk records, telemetry and worker jobs. The worker connects to Helius RPC/WebSockets, Pump.fun/Raydium detection, Jupiter Swap API v2 and Jito bundles.

**The OKX application has been removed from `main`.** PR #8 merged the Solana-first bot; PR #10 merged Jupiter/Jito and wallet-lifecycle corrections. The Neon/Vercel worker split is being developed separately on `feature/9n8-neon-platform-split`.

## Current implementation status

Implemented in `main` and the current platform branch:

- Per-Telegram-user Solana Ed25519 wallet generation/import, encrypted-at-rest key storage, balance queries, and 60-second key-message deletion attempt.
- Aiogram dashboard, parameter FSM, activation/withdrawal/rotation confirmation flows, and status notifications.
- Exact Decimal/SOL/lamport helpers, slippage bounds, position sizing, PnL, moon-bag split, realized-profit fee calculation, and daily risk persistence.
- Helius JSON-RPC client and reconnecting per-program `logsSubscribe` WebSocket subscriptions for Pump.fun and Raydium program addresses.
- Pump.fun bonding-curve PDA derivation, reserve parsing, and account-change monitoring.
- Mint/freeze authority checks, top-holder concentration screen, indexed liquidity check, and fail-closed Raydium LP-lock screening.
- Jupiter Swap API v2 raw instruction builder and Jito atomic bundle submission with landing-status checks.
- Background position supervision, TP/SL/max-hold and daily risk controls.
- FastAPI Mini App endpoints with Telegram `initData` HMAC validation and per-user WebSocket telemetry.
- Dedicated Neon PostgreSQL schema and a Postgres adapter for user records, Fernet-encrypted wallet secrets, settings, positions, fee/risk ledgers, receipts, telemetry and worker jobs.
- Vercel root FastAPI entrypoint plus a Vercel Daemon Dockerfile running `python -m src.n9n8.worker`; request handlers enqueue panic-sell, withdrawal and wallet-rotation operations for the persistent worker.
- Worker heartbeat and durable telemetry polling for the Mini App.
- Dark-mode responsive Telegram Mini App.
- Dockerfile and Docker Compose configuration with persistent SQLite storage.

Not yet certified or fully implemented:

- EVM/BNB/Base/Robinhood-chain wallet generation, deposit monitoring, and automatic cross-chain bridging are not wired in this branch. Do not send EVM assets to the Solana deposit address.
- Pump.fun direct buy/sell instruction construction and a complete direct Raydium AMM V4/CPMM swap builder are not yet implemented. Detection uses on-chain data; execution routes through Jupiter.
- Developer initial-buy min/max filtering is not yet implemented.
- The Raydium LP-lock parser is deliberately fail-closed, but its RugCheck schema needs live integration tests against representative pools.
- The requested 250 ms exit quote interval is not used. Quote polling is rate-limited to stay within upstream API quotas; actual decision latency depends on quote provider, RPC, and chain conditions.
- Profit fee is included in the exit transaction based on a conservative minimum-output estimate; exact finalized net profit reconciliation and ledger settlement still require an additional post-confirmation accounting pass.
- The application has not been independently audited, tested with real mainnet funds, or certified for unattended trading. Live mode is disabled by default.

A launch detector finding a token is not proof that it is safe or tradeable. Liquidity can disappear, quotes can become stale, token programs may be unusual, transactions can fail or land late, and slippage/MEV can make outcomes differ from estimates. Never assume a 10% target is guaranteed.

## Architecture

```text
Telegram user
   ├── aiogram 3.x dashboard + FSM
   └── Telegram Mini App (HTML/CSS/JS)
          │ Telegram initData HMAC
          ▼
       FastAPI REST + WebSocket
          │
   ┌──────┴──────────────────────────────────────────────┐
   │ Encrypted SQLite │ Decimal risk engine │ Telemetry │
   └──────┬───────────────────────┬─────────────────────┘
          │                       │
   Helius RPC / WS          Jupiter Swap API v2
   Pump.fun / Raydium       Jito Block Engine bundles
          │                       │
          └──── Solana mainnet ───┘
```

## Wallet-data durability warning

The default implementation stores encrypted wallet records in a local SQLite database at `data/9n8-solana.sqlite3`. Encryption does not protect against the database file being deleted by the hosting platform.

**Do not host funded/live user wallets on a free Render Web Service or a free Koyeb instance using this local SQLite configuration.** Render Free Web Services have an ephemeral filesystem and lose SQLite files on restart, redeploy, or scale-down; Free Render Web Services do not support persistent disks. Koyeb Free Instances cannot attach persistent volumes. A lost SQLite file means the bot loses its encrypted private-key records, even if the Fernet key still exists.

Safe current options:
- Run locally on a computer with reliable storage and keep encrypted backups of both the SQLite database and Fernet key.
- Use a hosting plan that provides a durable mounted disk for the SQLite database, with secure backups.
- Before using an ephemeral free host for live users, implement and test a durable external database (for example PostgreSQL) for encrypted keys, users, trades, and fee ledger. This repository currently does not have a production-tested PostgreSQL wallet-vault adapter.

Do not treat UptimeRobot pings as storage durability. Keeping a free instance awake does not turn its filesystem into persistent storage.

## Deployment topology

- **Telegram:** the persistent worker runs aiogram polling for commands, approvals, wallet actions and notifications. Telegram Mini App `initData` is validated by the FastAPI service.
- **Vercel request app:** root `index.py` exposes FastAPI routes for the Mini App, settings, status and queue submission. It does not run Helius subscriptions or trade-monitor loops.
- **Neon:** use the separate `9n8-trading` project and `n9n8` database. Do not point 9N8 at the unrelated `vyra-markets` brokerage database. Apply `migrations/001_n9n8_postgres.sql` before runtime initialization.
- **Vercel Daemon:** `worker/Dockerfile` runs `python -m src.n9n8.worker`. It polls the `9n8-worker` Vercel Queue topic for wake-up messages, then reads/claims the durable job from Neon. It keeps Helius subscriptions and position monitors in memory while the process is running; no durable state is stored on its filesystem.

The Daemon is a Vercel public-beta product and starts only on production deployments. It is configured for one `iad1` instance. The Vercel request service sends queue wake-ups using `x-vercel-oidc-token`; the Daemon uses its runtime `VERCEL_OIDC_TOKEN` to poll and acknowledge messages. The Daemon must fail closed if no valid OIDC token is exposed to its container. Vercel Queues delivers at least once, so Neon job rows and their claim state remain authoritative and prevent repeat execution. Vercel bills Daemon CPU and provisioned memory beyond plan inclusions; Queues is metered separately. Review current project usage and pricing before starting it. See <https://vercel.com/docs/queues>, <https://vercel.com/changelog/vercel-daemons-is-now-in-public-beta> and <https://vercel.com/pricing>. Do not start the daemon until runtime secrets and billing are reviewed.

## Windows setup

Requirements: Python 3.11+, a Telegram bot token, the one authorized Telegram numeric user ID, a Helius API key, a Solana treasury address, a stable Fernet encryption key, and durable database storage.

1. Download this branch:
   https://github.com/Alitiora-Initiative/9N8/tree/feature/solana-telegram-sniper
2. Run `setup_windows.bat`.
3. Open `.env` and fill in the required values below.
4. Start the bot with `start_9n8.bat`.
5. Send `/start` to your Telegram bot.

Required environment settings:

```env
TELEGRAM_BOT_TOKEN=your_botfather_token
AUTHORIZED_USER_ID=your_telegram_numeric_user_id
HELIUS_API_KEY=your_helius_api_key
MASTER_TREASURY_ADDRESS=your_solana_treasury_public_key
ENCRYPTION_MASTER_KEY=your_fernet_key
DATABASE_URL=your_9n8_neon_postgresql_url
N9N8_REQUIRE_POSTGRES=true
N9N8_TRADING_MODE=paper
LIVE_TRADING_ENABLED=false
WEBAPP_URL=https://your-https-host.example
```

Generate the Fernet encryption key once:

```powershell
py -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Store the generated key in a password manager and keep a separate offline backup. If it is lost, the encrypted wallet records cannot be decrypted. Changing it after users have wallets will make old encrypted secrets unreadable unless you perform a deliberate key migration.

**Deployed Vercel API and Daemon:** configure the same production `DATABASE_URL`, `ENCRYPTION_MASTER_KEY`, `TELEGRAM_BOT_TOKEN`, `HELIUS_API_KEY`, `MASTER_TREASURY_ADDRESS`, `WEBAPP_URL`, `N9N8_REQUIRE_POSTGRES=true`, `N9N8_TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false` in the Vercel production environment. Do not use the SQLite file on Vercel. The Neon connection string is a secret and must not be committed or printed.\n\nHelius endpoints are derived automatically from `HELIUS_API_KEY`. You can instead set both `SOLANA_RPC_URL` and `SOLANA_WSS_URL` explicitly.

## GitHub Secrets vs runtime environment

Adding `HELIUS_API_KEY` under **GitHub Actions Secrets** does not automatically make it available to a bot running on a Windows laptop or on Render/Koyeb/Fly.io.

- For GitHub Actions, explicitly map it into the workflow step's `env:` block if a workflow needs to access it.
- For a hosted service, add `HELIUS_API_KEY` to that provider's service environment variables.
- For local Windows, add it to the untracked `.env` file in the repository root.

Never print secrets in logs or commit `.env`.

## Paper and live modes

Paper mode is the default. The persistent worker listens to real launchpad events and reports discoveries, but it does not claim to fill simulated positions or manufacture PnL.

Live mode requires both:

```env
N9N8_TRADING_MODE=live
LIVE_TRADING_ENABLED=true
```

Do not enable these flags until you have tested wallet creation/import, key backups, token-risk screens, Jupiter transaction construction, Jito bundle landing, partial exits, stop-loss/max-hold exits, account closure, daily circuit breakers, withdrawals, and key rotation with a dedicated small-value test wallet. Live execution can irreversibly lose funds.

## Local Docker

```bash
docker compose up --build -d
docker compose logs -f
```

SQLite data is stored in the Docker named volume `9n8-data`. Back up this volume securely. The app exposes `/health` on port 8080.

**Persistent storage warning:** SQLite contains encrypted wallet recovery keys and bot state. Many free web-service tiers use ephemeral filesystems or sleep/restart containers; the SQLite database can be lost unless a persistent disk or durable database is attached. Back up the SQLite file and Fernet master key separately before accepting deposits. Do not assume a free instance's local filesystem is persistent.


## Tests

```bash
python -m pip install -r requirements.txt pytest pytest-asyncio httpx
python -m compileall -q src tests
python -m pytest -q
```

The test suite should use mocked network calls. Never put private keys, real wallet funds, Helius API keys, or a live treasury private key into test fixtures.

## Telegram Mini App

Telegram Mini Apps require an HTTPS URL. Set `WEBAPP_URL` to the public HTTPS base URL of the FastAPI service. Configure the same URL as the bot's Web App domain in Telegram's bot settings where required.

## Treasury fees

- $1.00 equivalent activation fee (first live activation).
- 0.1% fee on positive realized trade profit estimates.
- Selected 25% / 50% / 100% / custom withdrawals charge no platform fee; the actual network fee is quoted before signing.
- $1.00 equivalent stop-and-rotate fee.
- Network fees and Jito tips are separate.

The $1 equivalent is converted from a fresh SOL/USD oracle observation. If a trustworthy fresh price is unavailable or the wallet cannot cover the fee and network cost, the operation must fail rather than use a hard-coded exchange rate.

## Security

- Per-user private keys are Fernet-encrypted in SQLite.
- Telegram recovery-key messages are scheduled for deletion after 60 seconds, but deletion cannot remove screenshots, notification previews, backups or messages already copied elsewhere.
- Use dedicated wallets. Do not import a wallet holding long-term savings.
- The Mini App validates Telegram `initData`; do not expose unauthenticated wallet endpoints behind a reverse proxy.
- Use an HTTPS host, persistent disk, restricted access to environment variables, regular encrypted backups and key rotation procedures.
- Do not treat a clean test run as an audit or guarantee of trading safety.

## Separate Paper and Live Solana trading

9N8 keeps practice trading and real-wallet trading in separate services and tables.

### Paper mode — virtual funds only

- Start and stop with `/paper_on` and `/paper_off`; review the account with `/paper`.
- New practice accounts start with a **$1,000 virtual value** converted to SOL at the current SOL/USD quote.
- Paper settings, balances, positions, practice-day records and trade history are stored in the separate `n9n8_paper_*` tables (or local `paper_*` SQLite tables).
- Paper quotes use a restricted quote-only adapter. The PaperTradingEngine receives no swap signer, transaction builder, send method or Jito submission method.
- `/paper_snipe MINT` creates a security/quote preview and records virtual trades only. Paper positions can be closed with virtual quotes; they cannot withdraw SOL.

### Live mode — real wallet and real transactions

- `/live` displays the global operator switch, user activation, wallet balance and seven-day paper-practice requirement.
- `/live_on` opens the live readiness/activation flow. Live mode is not enabled just because a wallet exists. The global `LIVE_TRADING_ENABLED` switch must also be explicitly enabled by the operator.
- `/live_off` or `/stop` prevents new live entries; the existing live position watchdog remains active so open positions can still be exited.
- `/snipe MINT` and `/live_snipe MINT` create a real-wallet preview. The bot rechecks the live switch, wallet, risk limits, mint safety, liquidity, holders, rent/fee reserve and Jupiter quote before a separate Confirm Live Buy action can submit anything.
- Pasting a valid public Solana mint address in the private chat shows separate Paper Preview and Live Preview choices. Pasting an address alone never buys a token.
- `/positions` shows two separate sections; `/paper_positions` and `/live_positions` show one book at a time. Live positions include Sell 50% and Panic Sell 100% controls. Paper sell buttons only change virtual positions.
- `/paper_settings` / `/live_settings` keep the parameter stores separate. Critical mint/freeze authority checks are locked ON; an unavailable or unverified required safety result blocks the entry.
- `/paper_history` lists simulated fills; `/live_history` lists on-chain receipts. A signature with an unknown confirmation state must be checked before any manual retry.
- `/withdraw` offers 25%, 50%, 100% or a custom SOL amount from the real wallet. All live positions must be closed first. The network fee is quoted and reserved before the exact amount is signed. Withdrawals have no platform fee in this version; Paper balances are never withdrawable.

### Default live risk parameters

| Rule | Default |
|---|---:|
| Buy amount | 0.05 SOL |
| Buy slippage | 15% |
| Sell slippage | 20% |
| Jito tip | 0.002 SOL |
| Minimum pool liquidity | 5 SOL equivalent |
| Minimum LP locked/burned | 80% |
| Maximum top-10 holder share | 25% |
| Maximum developer wallet share | 5% |
| Take Profit 1 | +100%, sell 50% |
| Take Profit 2 | +300%, sell 25% of remaining |
| Trailing stop | 20% after the profit phase starts |
| Hard stop | -30% |
| Maximum hold | 4 hours |

The settings screen allows buy and sell slippage to be changed separately, and adjusts the corresponding Jupiter v2 `slippageBps` parameter. A fixed slippage override changes Jupiter from its default automatic slippage mode. All numeric limits are bounded by hard-coded caps. Mint/freeze authority revocation requirements cannot be switched off.

### Telegram command reference

| Command | Purpose |
|---|---|
| `/start` | Main dashboard: virtual account and real wallet shown separately |
| `/paper`, `/paper_on`, `/paper_off` | View/start/stop virtual practice |
| `/paper_snipe MINT` | Security and quote preview for a virtual buy |
| `/paper_positions`, `/paper_history`, `/paper_settings` | Paper-only positions, history and rules |
| `/live`, `/live_on`, `/live_off` | Readiness, arm after checks, or stop new entries |
| `/snipe MINT`, `/live_snipe MINT` | Guarded real-wallet preview and separate buy confirmation |
| `/live_positions`, `/live_history`, `/live_settings` | Live positions, on-chain receipts and live risk limits |
| `/settings`, `/safety`, `/positions`, `/history` | Menus and combined views |
| `/withdraw` | Confirmed real-wallet SOL withdrawal |
| `/stop` | Stop new live entries; existing exits continue |
| `/help` | Command guide |

### Single-user deployment gate

Set `AUTHORIZED_USER_ID` to the numeric Telegram user ID that is permitted to use the bot. It is required at startup and checked by both the Aiogram router and Mini App HTTP/WebSocket authentication. The Telegram bot token is not an access-control substitute. Do not share API keys, wallet private keys or seed phrases in Telegram.

### Important live-trading limitations

- Live trading remains disabled by default. The operator must configure the global switch, and the user must pass the separate live readiness and confirmation steps.
- The listener subscribes to Helius program logs for Pump.fun and Raydium-related launch programs. Pump.fun buys wait for migration/completion; live execution routes through Jupiter rather than using bespoke direct Pump.fun or direct Raydium instruction builders.
- Launch detection, quote response, Jito bundle landing and exit timing are affected by provider latency and Solana network conditions. The application does not guarantee detection or execution in 300 ms, transaction inclusion, or profitable fills.
- The exit watchdog is rate-limited against upstream quote/RPC availability. It is not a promise of exactly one-second execution.
- The current readiness gate includes a $1 first-live activation transfer, at least seven recorded UTC practice days, a connected live wallet, an operator-enabled live switch and at least $10 approximate SOL wallet value.
- This change has not been independently audited or certified with real mainnet funds. Do not enable live trading until a dedicated test wallet has been used to verify buy, partial sell, full sell, auto-exit, unknown-outcome reconciliation, withdrawal and recovery procedures.

