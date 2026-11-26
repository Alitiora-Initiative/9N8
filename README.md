# 9N8 — Telegram Trading Intelligence and Live-Market Paper Simulator

9N8 is a Telegram-first trading intelligence workspace with a private web app, a real-market paper simulator, Binance/Bybit/OKX account connections, self-custody wallet sessions, and risk controls. The current crypto paper pilot uses public Binance Spot data. Paper trades use virtual balances and never submit exchange orders.

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

### Binance, Bybit and OKX exchange account connections

- The Telegram Mini App accepts live account API keys for Binance, Bybit and OKX. Users select the markets they want the key to cover: Spot, Margin, USDT/USDC Futures, Coin-M Futures and Options. Binance additionally offers eligible stock-linked products under its combined Spot & Margin & Stock Trading permission.
- **Binance:** the validator reads the signed API restrictions endpoint. Spot and eligible stock-linked products use `enableSpotAndMarginTrading`; Margin additionally requires the reported margin flag; USDT-M and COIN-M Futures use the reported Futures flag; Options uses the Vanilla Options flag. A key is no longer rejected merely because it has Futures or Margin enabled when those products are selected. Withdrawals, internal transfers and universal transfers are still rejected.
- **Bybit:** the connector supports the V5 `query-api` endpoint on the selected regional host. Spot, Contract/Derivatives and Options permissions are checked separately. Bybit does not return a distinct Margin API-key scope, so Margin is recorded as requiring the correct Unified Account setting. Any Wallet transfer or withdrawal scope is rejected. The connector uses system-generated HMAC API keys.
- **OKX:** the connector signs the account configuration request against the selected Global, US/Australia or EEA host. OKX has broad `Read` / `Trade` / `Withdraw` scopes rather than one API-key permission per product. A Trade key is required for selected trading markets, Withdraw is rejected, and the selected account mode is recorded. OKX Trade may also permit internal funding transfers; 9N8 does not call transfer or withdrawal endpoints.
- The form reports which selected permissions were verified and which depend on account mode or regional product availability. It never claims that an unsupported or unreported scope is verified.
- Exchange credentials are stored server-side in Supabase Vault. They are not returned to the browser or posted into Telegram. Never send keys or secrets in Telegram chat.
- The existing balance view can read Binance Spot, OKX account balance and Bybit Unified Account balances. The current readiness/risk gate still uses its existing USDT account evidence and does not certify every derivatives product.
- **Connecting an account does not place an order.** Spot, Margin, Futures, Options and stock-linked live order execution remain separately locked; no live orders are enabled by this connection change.

### Jupiter live swaps and Trigger automation

- The private wallet panel includes a Solana BUY/SELL terminal backed by Jupiter Swap API V2. Quotes use the connected, verified Solana-mainnet wallet as the taker.
- The manual terminal uses Jupiter order/execute, checks the exact unsigned transaction and wallet fee payer, and requires the wallet to sign the transaction. The server verifies the signed transaction message hash and Ed25519 owner signature before provider submission.
- Transactions are limited to version 0 in this release. Unsupported version 1 transactions are rejected rather than sent to a wallet.
- Conservative terminal limits are 0.05 SOL-equivalent per swap, 0.10 SOL-equivalent per rolling 24 hours, and 2% maximum price impact. Only one unresolved quote or execution can exist per wallet at a time.
- A provider timeout after a transaction is claimed is recorded as `unknown`; 9N8 checks Solana signature status and never blindly resubmits it.
- **Manual live execution remains locked** until the Jupiter connector certification record is passed with message-integrity, owner-signature, idempotency and unknown-outcome reconciliation checks. Live quotes also remain unavailable until the Jupiter API key is configured.
- Jupiter Swap V2 and Trigger V2 require a `JUPITER_API_KEY` Edge Function secret from the Jupiter developer portal. The key must be set as a Supabase secret for `telegram-bot-next`; it is never sent to the browser. Trigger tokens are held in module memory only and refreshed through the access/refresh flow; they are not stored in Postgres or browser local storage.
- DCA funds are deposited into a Jupiter/Privy-managed vault and are no longer in the original wallet while scheduled orders are active. The owner must sign the deposit and any cancellation/withdrawal transaction.
- 9N8 caps DCA deposits at 0.50 SOL per rolling 24 hours per wallet and requires at least $10 estimated value per scheduled DCA round. The create and cancel flows verify the exact wallet-signed transaction.
- **Delegated automation remains locked** until vault registration/deposit integrity, DCA creation, cancellation, withdrawal and unknown-outcome tests have been certified.

### Telegram and wallets

- Telegram commands include `/start`, `/paper`, `/simulator`, `/connect`, `/connections`, `/wallets`, `/migrate`, `/approvals`, `/status`, `/risk`, `/pause` and `/stop`.
- The private Mini App has separate exchange-account, wallet-session/watch and paper-simulator sections.
- Wallet sessions support EVM and Solana mainnets through Reown AppKit/WalletConnect, including MetaMask, Trust Wallet, Phantom and compatible providers. If Reown QR is not configured, injected wallet browsers can connect directly where the wallet exposes an injected provider.
- Wallet ownership is verified by a one-time message signature. That signature is not a swap, token approval, transfer or trading authorization. A watch-only public address also remains read-only.

## What is not enabled yet

- **No real Binance, Bybit or OKX order is submitted by the exchange connection flow.** The connection layer checks key access and reads account information; it is not a released order executor.
- The standalone Binance order helper module validates Spot market-order parameters and signs requests, but it is not the same as a fully released, approval-to-order execution and reconciliation service.
- Exchange withdrawals and account transfers are not supported. Futures, Margin and Options API permissions can be checked and stored, but their real order submission is not enabled. Unattended wallet signing remains disabled. Jupiter transactions require owner wallet signing; Trigger automation uses its separate managed-vault model and remains disabled until certification.
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
deno test supabase/functions/telegram-bot-next/jupiter-live_test.ts
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


## Universal exchange-key permission update

The branch `feature/universal-exchange-api-permissions` adds market selection and provider-specific checks for Binance, Bybit and OKX. It also adds regional Bybit API hosts and rewrites the Telegram `/start` and bot descriptions in plain language. Apply `supabase/migrations/20261126222000_n8_universal_exchange_api_permissions.sql` before deploying the edge function; otherwise the database provider constraint will reject Bybit connections. This work does not enable live exchange order placement.
