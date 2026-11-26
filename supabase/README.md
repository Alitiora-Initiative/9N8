# 9N8 hosted runtime

## Environments and endpoints

### Preserved legacy function
- Project: 9N8 (ref `qlfcazdfxexcnzdgpqqm`)
- Function: `telegram-bot`
- Endpoint: `https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot`
- The original single-owner bot is retained separately from the universal candidate. Do not replace it without a separately reviewed migration.

### Multi-user candidate
- Function: `telegram-bot-next`
- Endpoint: `https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot-next`
- Deployed version observed in this audit: 37 (ACTIVE).
- JWT gateway verification remains disabled as configured previously; Telegram webhook traffic is authenticated with the Telegram secret-token header, while Mini App actions validate Telegram-signed `initData`. Destructive Mini App actions (exchange/wallet revocation, pause and transaction-related actions) require fresh five-minute initData; only non-mutating account/status reads may use the extended read window.
- The exact current Telegram webhook registration state must be checked through the secure `/setup` flow; source code being deployed does not by itself prove that Telegram is currently delivering updates to this endpoint.

### Scheduled crypto paper worker
- Function: `n8-paper-worker`
- Deployed version observed in this audit: 2 (ACTIVE).
- A `pg_cron` job named `n8-paper-worker-every-minute` invokes it every minute.
- The worker uses Binance public crypto Spot market data and simulates fills against virtual cash. It does not submit real exchange orders.

### Secure frontend
- Vercel project: `9n8-secure-connect`
- Public app URL: `https://9n8-secure-connect.vercel.app/connect`
- Webhook setup page: `https://9n8-secure-connect.vercel.app/setup`
- The static HTTPS frontend must never contain Supabase service-role/secret keys or the Telegram bot token.
- Mini App requests carry Telegram-signed `initData`; backend handlers verify it before reading or changing per-user data.

## Runtime secrets and credential boundaries

Shared runtime secrets are held in Supabase Edge Function Secrets:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`

Supabase-managed `SUPABASE_URL` and `SUPABASE_SECRET_KEYS` are read only inside the Edge Function runtime. Never put any of these values in a public repository, Mini App JavaScript, Telegram messages, GitHub Issues or this chat.

Per-user exchange credentials are stored in Supabase Vault. The application stores a Vault secret ID and safe permission/status metadata, not the raw API key or secret in the user-facing database row.

Current candidate onboarding accepts **Binance Spot Live and OKX Spot Live** account credentials. It does not accept Bybit for live account connection in this deployed release. Demo/testnet, margin and derivatives account migration are not enabled by the current live-account onboarding route.

## Current candidate capabilities

Implemented in the active source:
- validated Telegram user registration and private account scoping;
- RLS-backed per-user database records, audit events and backend-only privileged database access;
- Vault-backed Binance/OKX credential validation, storage, retrieval and owner-scoped revoke procedure;
- Binance Spot and OKX Spot account-read actions in the secure Mini App;
- watch-only EVM/Solana address links and signed wallet-ownership verification for supported wallet sessions;
- wallet-link/session revocation;
- Telegram group configuration with human-admin checks, group-scoped settings and manual moderator commands;
- atomic webhook update claims with a short retryable lease and duplicate suppression;
- crypto paper sessions with a $1,000 virtual starting balance, live Binance public Spot candles/ticker/order-book depth, simulated fills, fee/slippage estimates, position/trade/equity history and risk controls;
- a one-minute scheduled paper worker that persists tick and session state;
- emergency pause/stop and controlled paper resume;
- confirmed exchange disconnect in the candidate: `/connections` lists owner-scoped connection IDs and `/disconnect CONNECTION_ID` requests confirmation before deleting the stored Vault secret and marking that user's connection revoked;
- private Mini App exchange panel lists the user's own account connections, reads balances for the selected Binance/OKX connection, and supports confirmed owner-scoped disconnect;
- private Mini App wallet panel lists verified and watch-only EVM/Solana links and supports connection-ID-based wallet-link revocation;
- private Mini App Jupiter Swap V2 quote preview creates a real wallet-bound route and unsigned transaction preview; it does not sign or broadcast.

Important exchange-key distinction: `/disconnect` removes the API credential from 9N8 and deletes its Vault record. It cannot revoke the API key at Binance or OKX. The owner must separately delete/revoke the key in the exchange's own security settings for complete credential invalidation.

## Actual feature limits

Available but deliberately constrained in the deployed Supabase candidate:
- Jupiter Swap V2 can create a live route/quote and an unsigned wallet-bound transaction preview in the private Mini App. The preview is not an order or a fill; it does not request a signature or broadcast a transaction.
- Manual Jupiter signed-transaction execution remains blocked by the database certification gate. The active connector status was observed as testing, live_manual_wallet_execution_enabled=false, and delegated_automation_enabled=false.
- Jupiter Trigger DCA/delegated automation remains unavailable. Its deposit flow transfers assets from the original wallet into a Jupiter/Privy-managed vault, so it remains disabled until separate custody, deposit, order, cancellation and withdrawal certification is complete.

Not enabled in the deployed Supabase candidate:
- Binance/OKX order placement, order cancellation, autonomous exchange trading, withdrawals or transfers;
- wallet signing for swaps, token approvals, bridge transactions, background wallet execution or automated wallet transactions;
- stocks, Forex, commodities, perpetual futures, margin or derivatives inside the scheduled crypto paper worker;
- Solana/EVM DEX execution without the explicit Jupiter certification gate;
- automated group-news feeds and automatic anti-spam moderation.

The Jupiter quote helper uses the keyless /swap/v2/order route when no Jupiter API key is configured; this is rate-limited by Jupiter. Trigger automation still requires JUPITER_API_KEY.

The separate `multi-asset-python` feature branch contains a Python paper engine, OANDA practice connector and Alpaca U.S. stock/ETF market-data adapter. That code is not the deployed Supabase paper worker and must not be described as active in the hosted Telegram bot. The Alpaca adapter is data-only; it does not read brokerage balances or send stock orders. The branch has 68 passing tests in both Python workflows, the Deno paper/Jupiter tests and Edge Function checks pass, and the Vite web build passes. The latest Vercel preview creation was rejected by the team's Hobby daily deployment API limit; no new preview was created.

The deployed paper worker may scan valid market data and produce no signal. A no-signal tick is a valid strategy result, not an error and not a reason to fabricate a paper fill.

## Safe operating rules

- Keep Supabase paper trading in virtual mode; no real exchange order is sent by the scheduled worker.
- Never use a seed phrase or private key in chat, an issue, a repository, or an environment variable.
- Never request withdrawal, internal transfer, funding transfer, margin, futures or derivatives permissions for the current spot account connection.
- OKX Trade permission may carry broader exchange-level permissions; use a dedicated, IP-restricted API key with withdrawals and transfers disabled.
- Binance credentials should be dedicated to 9N8 and limited to the required read/Spot permission set.
- Personal balances and exchange connection records are private-chat/Mini-App only.
- Group commands cannot read or use members' personal exchange or wallet accounts.
- Promoting a Telegram bot to group administrator can expose group messages to it; explain this to group owners before requesting moderation rights.
- Keep GitHub Actions secrets, Supabase project secrets and per-user Vault credentials in their distinct roles.
