# 9N8 Universal Multi-User Architecture

## Status and implementation boundary

This document defines the next 9N8 release. It does not claim that these features are already implemented.

Verified starting point on 2026-11-25:

- The GitHub default branch main contains the original single-owner Python Telegram bot and direct OKX Spot adapter.
- The connected Supabase project named 9N8 is active. Its deployed telegram-bot Edge Function is currently a single-owner, read-only OKX demo bot.
- The Supabase database currently has no 9N8 application tables or migrations.
- Supabase Vault, pg_cron, and pg_net are installed.
- This branch is a separate foundation track. It must not replace the stable personal bot until the new flow is tested.

## Product goal

One Telegram bot serves multiple independent users. Every user owns their own exchange connections, wallet links, risk limits, strategies, and approvals. Group features are a separate permission domain.

A user must never be able to read another user's balances, API-key metadata, wallet links, strategy runs, trade proposals, or audit history.

### Private-chat features

- Register a user by validated Telegram identity.
- Show a personal control panel.
- Connect and disconnect supported exchange accounts.
- Link public wallet addresses through a wallet connection flow.
- Inspect market data, account status, balances, open orders, and personal activity.
- Start, pause, and stop a personal paper/demo strategy.
- Review a proposed order and approve or reject it.
- Configure per-user symbol allowlists, order limits, loss limits, and kill switch.

Account connection, balance details, trade approvals, API credential setup, and personal risk settings are private-chat or secure Mini App actions only. They never run in a group.

### Group features

- Public market commands and opt-in market summaries.
- Welcome/rules messages and group configuration.
- Optional anti-spam, link filtering, message deletion, and member restrictions.
- Group settings are controlled only by the group owner or an administrator whose current Telegram permissions are verified by the bot.

A group cannot see or use personal exchange connections. A group command must never submit a personal trade. A trade request originating in a group should direct the individual to a private chat for review and approval.

## Secure account onboarding

### Exchange credentials

The bot must not ask a user to paste an API key, secret, passphrase, or password into ordinary Telegram messages.

1. The user chooses Connect exchange in a private chat.
2. The bot opens a Telegram Mini App on an HTTPS origin.
3. The server validates Telegram Mini App initData using the bot token. Never trust initDataUnsafe or a user ID supplied by browser JavaScript.
4. The user creates their own API key in the exchange's official account interface and enters it into the secure form.
5. The backend validates the credentials with a read-only private endpoint before saving.
6. The backend stores the credential payload in Supabase Vault and stores only the Vault secret ID, provider, environment, permission metadata, and status in the application table.
7. The UI clears the secret fields and shows only a masked connection label and validation status.
8. A user can revoke or replace a connection at any time. Disconnecting deletes the Vault secret and disables related runs.

Credentials are user-specific data, not deployment environment variables and not GitHub Actions secrets. GitHub secrets are reserved for CI/CD and deployment credentials. Supabase project secrets are reserved for shared application secrets such as the Telegram bot token and webhook secret.

### Required data separation

- Shared application secrets: Telegram bot token and webhook secret in Supabase Edge Function Secrets.
- Per-user exchange credentials: Supabase Vault secret records, one credential record per connection.
- Public wallet information: provider, chain namespace, chain ID, public address, verification status.
- Never store: seed phrases, wallet private keys, exchange passwords, withdrawal codes, or raw wallet signing keys.

Supabase Vault encrypts secret records at rest and keeps the encryption key separate from the database. Access to the decrypted Vault view must remain limited to trusted backend code.

## Exchange connector model

Implement a provider adapter interface. Do not make the Telegram command handlers call exchange APIs directly.

Initial exchange scope:

| Provider | Initial markets | Safe test environment |
| --- | --- | --- |
| OKX | Spot cash only | OKX Demo Trading; send x-simulated-trading: 1 |
| Binance | Spot only | Spot Testnet |
| Bybit | Spot only | Bybit Testnet |

Provider adapters normalize:

- credential validation and account identity;
- account permissions and environment checks;
- market metadata, symbol format, tick size, lot size, minimum order;
- public ticker/candle data;
- balances and open orders;
- order preview, submit, status lookup, and cancellation;
- provider-specific rate limits, errors, regional API domains, and sandbox behavior.

Start with read-only account validation and market/balance reads. Add demo/testnet order submission only after the adapter is tested. Keep futures, perpetuals, margin, borrowing, internal transfers, Earn, and withdrawals outside the first release.

### Permission requirements

- Never request or accept withdrawal permission.
- Do not request wallet, transfer, funding-transfer, sub-account transfer, margin, or derivatives permission.
- Prefer separate read and trade keys where a venue supports them.
- OKX documents that its Trade permission can include funding transfers. The application must never implement transfer endpoints; users should use a dedicated, IP-restricted key and understand the permission's wider scope.
- Binance permissions distinguish account-data access from trading; use only the required user-data and Spot trade scopes.
- Bybit's permission information exposes Spot trade separately from Wallet permissions; do not enable Wallet transfer or withdrawal scopes.
- API keys must be created by the account owner. The app validates and stores them; it cannot bypass exchange login, MFA, regional eligibility, or API-key creation controls.

A connector is not considered production-ready merely because a balance endpoint works. It must pass provider contract tests for authentication, symbol metadata, demo/testnet routing, order precision, duplicate prevention, order state reconciliation, cancellations, throttling, and failure handling.

## Wallet model

Use a browser-based wallet connection flow, not wallet secrets in Telegram.

- Reown AppKit / WalletConnect is the cross-wallet connection layer for supported EVM and Solana wallets.
- Trust Wallet supports WalletConnect v2 for EVM chains and Solana.
- Phantom can connect to web applications using its supported provider/adapter flow.
- The first wallet release is address linking and read-only portfolio/transaction monitoring.
- Verify address ownership with a one-time challenge signature when appropriate.
- Store only the public address, chain namespace, chain ID, provider, and verification status.
- Never collect a seed phrase or private key.
- Do not sign, send, swap, or bridge automatically. Any later transaction must be shown to the wallet and approved by the user there.

WalletConnect/Phantom session credentials must not be treated as exchange API credentials or stored in the group configuration.

## Telegram identity and group authorization

### Identity

- Validate every webhook using Telegram's X-Telegram-Bot-Api-Secret-Token header.
- Deduplicate updates by Telegram update_id.
- Derive the actor ID from the verified Telegram update or verified Mini App init data; never trust an ID in command text.
- Enforce rate limits and replay/idempotency checks.
- Keep Telegram bot token and webhook secret in Supabase Edge Function Secrets.

### Groups

- The bot may send messages without being an administrator.
- A group owner must explicitly add/promote the bot to grant moderation rights.
- Request only the rights required for enabled features. Do not request can_promote_members, change-info, invite, or other broad rights unless a feature genuinely requires them.
- Verify a human actor's current group-admin status and the bot's own rights before applying moderation changes.
- Telegram documents that bots added as group administrators receive all group messages even when privacy mode is enabled. Explain this clearly before asking a group to promote the bot.
- Group admin actions are scoped to that chat. They do not create access to members' private exchange or wallet accounts.

Suggested group modes:
1. Market only — no bot admin rights required.
2. Community helper — welcome, rules, commands, and opt-in summaries.
3. Moderator — only the minimum delete/restrict rights needed for the selected moderation features.

## Trading execution and risk policy

Default execution state is paper/demo/testnet. Live trading is locked.

The AI may summarize research, compare signals, or draft a proposed order. It must not bypass the deterministic risk engine.

Every order proposal must pass all checks before a confirmation is shown:

- user is authenticated and owns the exchange connection;
- connection is active and environment is explicit;
- symbol is allowlisted and market is open;
- size, precision, minimum notional, and available balance are valid;
- per-order notional, daily loss, exposure, and trade-count limits pass;
- slippage/spread guard and cooldown pass;
- duplicate/idempotency checks pass;
- kill switch is off;
- approval is bound to the exact user, connection, order payload, and expiry.

Live trading must remain off until a user deliberately enables it through a secure settings page and confirms each live order. No automatic withdrawals or transfers, ever. No group trade execution.

## Runtime and scheduling

Supabase Edge Functions are appropriate for Telegram webhooks, secure form submissions, short API calls, and scheduled worker invocations. They are not a permanent always-on process.

Use the already-installed pg_cron and pg_net for bounded periodic jobs (initially a conservative one-minute cadence for paper/demo scans). Persist run state and use idempotent claims so two invocations cannot execute the same run twice. Keep jobs short and make them safe to retry.

This is not high-frequency trading. Sub-second execution, long-lived WebSocket sessions, or sustained heavy concurrency would require a separate always-on worker with a known egress IP. Do not add paid hosting until the scheduled Supabase path has been tested and its resource limits are understood.

## Initial database model

All application tables must have RLS enabled. No anonymous or browser role receives access to credential records or Vault data. The first implementation should use backend-only access and explicit ownership checks.

Expected tables:

- app_users: Telegram identity, status, platform role.
- exchange_connections: owner, provider, environment, Vault secret ID, safe permission metadata, validation state.
- wallet_connections: owner, provider, chain namespace/ID, public address, verification state.
- group_settings: chat-scoped options and creator.
- risk_policies: user-specific guardrails and live lock.
- strategy_runs: user-owned run/stop/pause state.
- trade_intents: expiring user-bound proposals and approval results.
- telegram_updates: webhook idempotency.
- audit_events: redacted security and user-action events; never log raw credentials or webhook bodies.

Use server-side Telegram identity validation and explicit owner predicates for every credential read/write. A service_role key must remain in Edge Function runtime only and never reach the Mini App.

## Delivery phases

### Phase 0 — Preserve and document
- Keep the stable personal bot on main.
- Version the deployed Edge Function source in GitHub.
- Establish security, privacy, and connector contract tests.

### Phase 1 — Multi-user foundation
- Add user registration and Telegram identity verification.
- Add database tables, RLS, Vault-backed exchange connection records, audit trail, and idempotent webhook handling.
- Add a secure Telegram Mini App connection form.
- No trade execution in this phase.

### Phase 2 — Read-only connections
- Connect and validate OKX Demo, Binance Spot Testnet, and Bybit Testnet.
- Add balances, account permissions, tickers, candles, and open-order reads.
- Add read-only Trust Wallet and Phantom wallet linking.

### Phase 3 — Paper/demo execution
- Add normalized spot order previews and testnet/demo execution.
- Add confirmation buttons, idempotency, risk limits, and order reconciliation.
- No live trading.

### Phase 4 — Group tools
- Market-only group mode first.
- Add opt-in moderation features with permission checks and per-chat configuration.
- No private account data or trades in groups.

### Phase 5 — Live trading review
- Independent security review and full venue contract tests.
- Explicit user opt-in, small caps, strong IP restrictions where available, and live confirmation for every trade.
- Keep live automation disabled until a separate review and user activation.

## Release acceptance criteria

Do not mark a feature complete until:

- automated tests verify cross-user isolation and denied access;
- no API key, secret, passphrase, seed phrase, or raw webhook payload appears in logs;
- credentials are never sent as a Telegram message;
- the provider test environment is verified before any order request;
- no withdrawal/transfer endpoints exist in the first release;
- duplicate Telegram updates and duplicate trade intents are safely rejected;
- a kill switch stops all user runs;
- the bot cannot act on group trade instructions or another user's connection;
- Supabase security and performance advisors have been reviewed;
- the active Edge Function and GitHub source are in sync.


## Implementation status — 2026-11-26

This section describes the current deployed Supabase candidate and the separate Python development branch. "Implemented" means source and backend handlers exist; it does not imply that every user's external account has been configured or that real-money trading is available.

### Deployed Supabase candidate

Verified against the connected 9N8 Supabase project:

- `telegram-bot-next` is ACTIVE at deployed version 37 when inspected; it is the multi-user Telegram/Mini App candidate, separate from the preserved legacy function.
- `n8-paper-worker` is ACTIVE at version 2, and a one-minute `pg_cron` job invokes it.
- The database contained a running crypto paper session with recurring successful ticks. Recent worker rows reported 10 symbols scanned and 0 symbol failures; a tick with no signal intentionally creates no trade.
- The deployed paper worker uses Binance public Spot market data and a virtual balance. It does not submit an exchange order.

### Implemented in the deployed candidate

- Telegram identity registration, private-chat account scoping, backend-only access and RLS-backed user data.
- Vault-backed user-specific Binance Spot Live and OKX Spot Live credential storage, validation, retrieval and owner-scoped revoke procedure.
- Binance/OKX account read actions in the secure Mini App. These reads do not activate order placement.
- EVM and Solana wallet ownership proof through challenge/signature verification, plus public watch-only address linking.
- Wallet-link/session revocation; these are address/ownership features, not wallet trading or signing automation.
- Jupiter Swap V2 can generate a wallet-bound quote and unsigned transaction preview in the private Mini App, with a 55-second quote expiry, maximum 2% price impact, maximum 0.05 SOL-equivalent per request, 0.10 SOL-equivalent rolling 24-hour cap and one unresolved quote limit.
- Jupiter signed execution remains locked because the active certification row is testing, with manual live execution and delegated automation both false. Jupiter Trigger DCA remains unavailable because it uses a Jupiter/Privy-managed vault deposit.
- Paper crypto session lifecycle, Binance public one-minute candle/ticker/order-book reads, simulated visible-depth fills, virtual fees/slippage, persisted trades/positions/equity, pause/stop/resume controls and a one-minute scheduled worker.
- Telegram webhook update claims with leases, duplicate suppression and retry handling.
- Group setup and permission-checked manual moderator commands.
- Exchange disconnect path is deployed: `/connections` displays a connection ID and `/disconnect CONNECTION_ID` asks for confirmation, then removes the corresponding 9N8 Vault secret through the owner-scoped database procedure. This removes the stored credential from 9N8; the user must separately revoke the API key at Binance or OKX.
- The Mini App displays owner-scoped Binance/OKX connections, refreshes balances for a selected connection and supports owner-confirmed exchange-secret revocation. It lists verified/watch-only wallets and allows an owner to revoke a link by connection ID.
- The private Mini App creates live Jupiter Swap V2 quote previews for a verified Solana Mainnet wallet, including expected/minimum output, route, price impact and expiry. No swap signature or broadcast is requested by the quote screen.

### Implemented separately in the Python development branch

- A multi-asset Python paper engine with live provider quotes/candles and virtual fills.
- Bybit public crypto spot defaults, OANDA practice connector and an Alpaca U.S. stock/ETF market-data adapter.
- Alpaca stock data is read-only and paper-only. It does not read Alpaca brokerage balances or submit orders. It is disabled by default and requires the user's own Alpaca market-data credentials and feed entitlement.
- Persistent approval proposals, SQLite atomic paper fills, fee-aware partial exits, risk gates, Telegram private owner controls and restart recovery.
- The stock adapter has automated tests; it has not yet been verified using a user's live Alpaca subscription.

### Not implemented or not certified

- Live exchange order submission in the deployed Supabase candidate.
- Uncertified Jupiter signed swap execution, automated wallet trading, token approvals, bridge operations, and Jupiter Trigger DCA/delegated custody. Quote previews work; execution remains locked.
- Solana/EVM DEX entries in the Python engine; their data/security adapters are integration code only and the engine blocks entries.
- Stock order submission, stock shorting/borrow handling, broker account reads, non-U.S. stock venue coverage, and a live stock connector.
- Live Forex/CFD execution certification, live partial exits/trailing amendments, complete exchange-native bracket reconciliation and post-bracket position reconciliation.
- Automated group anti-spam moderation, news feed publishing, copy trading, sniping and DCA.
- A continuous 48-hour testnet/practice soak, independent execution/security review, and production certification.

### Deployment and source-of-truth boundary

- The deployed Supabase v36 source was synchronized, then the branch updates were deployed as `telegram-bot-next` v37. The deployment adds fresh-initData protection for destructive Mini App actions, per-connection account reads, owner-scoped wallet/exchange revocation and keyless Jupiter Swap V2 quote preview. The deployed function reports ACTIVE; the legacy Telegram function and scheduled paper worker were left unchanged.
- The preserved legacy Telegram function and GitHub `main` branch have not been replaced.
- Pull Request #5 is open and remains a draft, not merged. The active Supabase v37 function was deployed explicitly from its source; this did not merge the branch into GitHub main.
- The Vercel preview for the same branch has not been published because Vercel's Hobby team limit of 100 API deployment requests per day has been reached. The frontend build is verified by GitHub Actions.
- The Telegram webhook's current registration state has not been confirmed in this audit. A successful `/setup` registration/confirmation is required before claiming Telegram delivery is operational for the candidate.
- The production paper worker's successful ticks prove its scheduled market-data scan is running, not that the strategy is profitable or every user's Telegram connection is configured.
