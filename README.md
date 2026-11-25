# 9N8 — Telegram Trading Intelligence and Live-Market Paper Simulator

9N8 is a Telegram-first trading intelligence workspace with a private web app, a real-market paper simulator, Binance and OKX Spot account connections, self-custody wallet sessions, and risk controls. The current crypto paper pilot uses public Binance Spot data. Paper trades use virtual balances and never submit exchange orders.

**Current release state:** real-market paper trading and Binance/OKX account-connection APIs are deployed in Supabase. OKX account reads are validated, but OKX trade/withdraw/transfer permissions are not certified. The hosted web preview is behind the latest working branch while the Vercel Hobby deployment quota is exhausted. Real Binance or OKX order placement is not enabled in this release.

## What works

### Live-market paper simulator

- Reads genuine Binance Spot 1-minute candles, ticker data and order-book depth.
- Displays live candles and volume with EMA/RSI overlays.
- The current strategy is EMA 9 / EMA 21 crossover with RSI 14 confirmation.
- Paper trades use a virtual $1,000 starting balance, a $50 maximum entry, up to three open paper positions, an estimated 0.1% fee, and spread/slippage guards.
- Simulated fills consume visible bid/ask depth and calculate an estimated VWAP. This is a model based on the visible book, not a promise of the exchange's eventual fill.
- The paper engine is server-side and continues scanning while a session is running. The web app is a view and control surface.
- Paper results and positions are stored separately from live exchange balances and orders.

### Binance and OKX Spot account connections

- The private Telegram Mini App accepts Binance Spot LIVE and OKX Spot LIVE account credentials. Demo/testnet, margin and derivatives are not enabled by this connection layer.
- Binance validation checks the API key's read and Spot-trade scopes and rejects keys that expose withdrawal or transfer permissions.
- OKX validation signs account-config and account-balance reads using the API key, secret and passphrase. The UI supports Global, US/Australia and EEA API regions.
- OKX account-read responses do not expose API-key trade/withdraw/transfer scopes. 9N8 therefore records those scopes as **not verified**; users must create restricted keys with Read + Trade only, Withdraw disabled, and no funding transfer permissions.
- Exchange credentials are stored server-side in Supabase Vault. They are not returned to the browser or posted into Telegram.
- Balance refreshes store timestamped USDT evidence for the server-side risk assessment. OKX USDT is recorded with a nominal 1.00 USD valuation assumption.
- The account-connection layer is not an order executor. Both providers remain unable to submit live orders in this release.

### Telegram and wallets

- Telegram commands include `/start`, `/paper`, `/simulator`, `/connect`, `/connections`, `/wallets`, `/migrate`, `/approvals`, `/status`, `/risk`, `/pause` and `/stop`.
- The private Mini App has separate exchange-account, wallet-session/watch and paper-simulator sections.
- Wallet sessions support EVM and Solana mainnets through Reown AppKit/WalletConnect, including MetaMask, Trust Wallet, Phantom and compatible providers. If Reown QR is not configured, injected wallet browsers can connect directly where the wallet exposes an injected provider.
- Wallet ownership is verified by a one-time message signature. That signature is not a swap, token approval, transfer or trading authorization. A watch-only public address also remains read-only.

## What is not enabled yet

- **No real Binance or OKX order is currently submitted by the deployed bot.** Both connections validate account access and read balances; neither is a released live order executor.
- The standalone Binance order helper module validates Spot market-order parameters and signs requests, but it is not the same as a fully released, approval-to-order execution and reconciliation service.
- No withdrawals, transfers, wallet signing, futures, margin or unattended autonomous trading are available.
- The 9N8 paper run must not be treated as a live account or a guarantee of profitability.

Do not deposit funds on the assumption that real trading is ready. Do not share an API secret, private key or seed phrase in Telegram, GitHub issues, source code or this chat.

## Live-trading eligibility gates

The database has a server-side eligibility assessment and an exact-payload approval preflight. They are safeguards, not proof that a user is eligible. The 30-day review currently requires:

1. A completed 30-day forward paper session using genuine market data.
2. At least 100 closed paper exits per enabled strategy, with realized P&L and recorded fees/slippage.
3. Positive net expectancy, drawdown within the configured policy, at least three profitable chronological folds, and a profitable final out-of-sample segment.
4. No open virtual positions, unresolved live orders or pending live proposals.
5. A current Binance Spot LIVE connection, a fresh account-balance check and a connector-readiness record that has passed security and reconciliation checks.
6. At least $10 of available USDT quote balance in the selected exchange account, where provider-specific balance evidence and live connector checks have passed. This minimum is enforced by the database, not inferred from a separate self-custody wallet balance.

A seven-day review is only a progress checkpoint. The 30-day finalizer refuses to complete a session before the window expires, while a worker tick is active, when the worker is stale/unhealthy, or while virtual positions remain open. Live trading stays disabled until a separate owner-confirmed release is correctly implemented and the connector is certified.

A few dollars in Trust Wallet are not an exchange Spot balance. Exchange-based live execution would require assets to be available on the connected exchange and the provider-specific balance gate to be satisfied. Wallet balances are not automatically treated as exchange collateral.

## Architecture

- **Frontend:** Vite app in `web/`, Lightweight Charts, Telegram Web App integration, wallet watch/connect surface.
- **Telegram API:** Supabase Edge Function `telegram-bot-next`.
- **Paper worker:** Supabase Edge Function `n8-paper-worker`, using Binance public Spot market data.
- **Database:** Supabase Postgres for users, paper sessions/trades, exchange connection metadata, risk gates and audit events.
- **Credential vault:** Supabase Vault stores per-user Binance or OKX API credentials. The browser receives connection status and balance information, not the secret. Wallet private keys remain in the wallet provider.
- **Hosting:** Vercel project `9n8-secure-connect`; primary domain `https://9n8-secure-connect.vercel.app`.
- **Working branch:** `universal-multi-user`.

## Local web build

Use Node.js 22 or newer:

```bash
cd web
npm install
npm run dev
```

Create a production build with:

```bash
npm run build
```

## Backend tests

Install Deno 2 and run the focused tests:

```bash
deno test supabase/functions/n8-paper-worker/paper-engine_test.ts
deno test supabase/functions/telegram-bot-next/binance-live_test.ts
deno test supabase/functions/telegram-bot-next/okx-live_test.ts
```

Type-check the Edge Functions:

```bash
deno check supabase/functions/telegram-bot-next/index.ts supabase/functions/n8-paper-worker/index.ts
```

GitHub Actions runs the Python regression suite, web production build, Deno tests and Edge Function type-check on `universal-multi-user`.

## Deployment notes

The Supabase functions and database migrations are deployed. The Vercel team is currently on the Hobby plan and hit the 100-deployments-per-24-hour limit. That stopped the latest branch build from becoming the active website. The current stable Vercel URL therefore may not include the newest UI changes until the deployment quota resets or the project owner changes the Vercel plan.

Do not bypass this deployment restriction by promoting an unrelated branch or an unverified build. Deploy the `universal-multi-user` branch after Vercel accepts another build, confirm it reaches READY, then assign the stable domain alias.

## Safety and operational rules

- Keep API credentials out of source control and Telegram messages.
- Use a dedicated Binance API key; disable withdrawals and all transfers.
- Use IP allowlisting where possible.
- Treat HTTP timeouts and Binance 5XX order responses as **unknown execution state**, not as proof the order failed. Reconcile by client order ID before any retry.
- Do not turn on live trading or connector readiness manually to bypass the database gates.
- Paper P&L is virtual and does not predict live profitability.
