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

Requirements: Python 3.11+, a Telegram bot token, a Helius API key, a Solana treasury address, and a persistent local database folder.

1. Download this branch:
   https://github.com/Alitiora-Initiative/9N8/tree/feature/solana-telegram-sniper
2. Run `setup_windows.bat`.
3. Open `.env` and fill in the required values below.
4. Start the bot with `start_9n8.bat`.
5. Send `/start` to your Telegram bot.

Required environment settings:

```env
TELEGRAM_BOT_TOKEN=your_botfather_token
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
- $1.00 equivalent full withdrawal fee.
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

## Telegram network selector update

The `feature/telegram-network-switcher-and-dashboard` branch adds a Telegram network selector and address-aware quick-snipe entry point without claiming unsupported chain execution.

- `/chains` and `/network` open the selector for Solana, Base, Ethereum and BNB Chain.
- Solana Mainnet is the only execution-enabled network in this runtime. Its wallet, Helius/Pump.fun/Raydium listener, token-risk checks, Jupiter routing, Jito submission and position monitoring remain the integrated path.
- Base, Ethereum and BNB Chain are displayed as **NOT CONNECTED**. Their selection is rejected until separate EVM wallet custody, RPC and pool listeners, GoPlus/Honeypot risk checks, router execution, transaction receipt reconciliation and position monitoring are implemented and tested.
- The selector's wallet-management screen only describes the existing Solana wallet. It explicitly warns against sending EVM assets to the Solana address.
- A valid pasted Solana mint opens a choice of Paper preview or Live safety preview. A pasted EVM address is recognized but no EVM RPC request, quote or transaction is attempted.
- `/start` and `/chains` reuse the current control-panel message while the bot process remains alive. The message identifier is in process memory; after a worker restart, the next command creates a fresh panel.

This update changes Telegram presentation and routing safeguards only. It does not deploy the worker, enable live trading, add EVM execution, or certify unattended trading. Paper and Live remain separate, and live trading stays subject to both operator and per-account controls.

## Expanded Telegram sniper controls

The same feature branch also extends the Telegram control surface and risk audit trail:

- First `/start` creates a dedicated Solana wallet and shows its address, current RPC balance response, and Helius latency. The encrypted key is hidden until a one-time reveal action; reveal state is recorded against the wallet address. The key delivery message is auto-deleting after 60 seconds, which cannot prevent screenshots or notification copies.
- Wallet rotation returns to the onboarding screen so the new key is revealed explicitly; it is not pushed automatically.
- The dashboard now summarizes the actual Solana scanner state, separate Paper and Live status, wallet balance, marked PnL, and live settings. It deliberately does not fabricate pool-count or win-rate statistics.
- Live settings expose separate buy and sell slippage values, buy amount, Jito tip, priority fee, TP1, trailing stop, hard stop, maximum hold, liquidity minimum, LP lock minimum, holder concentration, developer concentration, and daily risk limits.
- Mint and freeze authority checks stay mandatory and cannot be toggled off. The safety menu exposes editable numeric thresholds and documents fail-closed behavior for unverifiable checks.
- Position actions support 50% and 100% exits, live chart links, refresh, and a separately confirmed sequential panic-sell-all action. Exits for a given position are serialized to reduce races between manual actions and the watchdog.
- Automatic TP1/TP2, trailing-stop, hard-stop and panic-exit notifications include a position reference and actionable chart/explorer/remaining-bag controls where a balance remains.
- Safety rejections are written to the security event ledger and shown in Live Trade History.
- Base, Ethereum and BNB Chain continue to appear in the selector but remain non-executable. No EVM wallet is generated, no EVM balance is shown, and no EVM swap can be initiated in this branch.

These updates are Telegram controls around the existing Solana services; they do not constitute an independent security audit or certify unattended mainnet trading. The operator-wide live switch and per-account readiness gates remain in place.
