# 9N8 Production Audit — 26 November 2026

Repository: `Alitiora-Initiative/9N8`, branch `main`.

## Executive finding

The Telegram webhook is running through Supabase and the paper-trading data path is distinct from live-trading controls. The persistent Python Solana launch worker is **not verified as deployed or running**. The current Vercel site deployment builds the Vite frontend; the Python API/Daemon configuration is at the repository root and requires a dedicated backend project.

**Live execution remains locked. No on-chain trades were submitted during this audit.**

## Verified snapshot

| Component | Audit result |
|---|---|
| Telegram webhook | Supabase `telegram-bot-next` active (v46 after the Jupiter expiry upgrade); 52 recorded updates, 52 processed, 0 retryable errors |
| Paper store | 2 sessions, 20 virtual positions, 40 virtual trades, 1,890 tick-history records |
| Jupiter live certification | `testing`; manual wallet execution disabled; delegated automation disabled |
| Live swap ledger | 0 recorded swaps |
| Neon worker heartbeat | 0 rows in `n9n8_worker_heartbeats` |
| Vercel Daemon runtime logs | No Daemon logs in the inspected 30-minute windows |
| Vercel website | `9n8-secure-connect`, Vite project; last checked production SHA `1de3603e` |
| Backend secrets | Required worker credentials absent from the existing frontend project |

These are point-in-time observations, not guarantees about future provider state.

## Code upgrades committed

1. Python worker-only mode is now outbound-only for Telegram. Supabase remains the single owner of inbound webhook updates and the bot command menu.
2. Added a Neon worker heartbeat refreshed every 30 seconds, including runtime mode and worker metadata.
3. Fixed task lifecycle so an unavailable optional queue bridge does not immediately terminate the combined local runtime.
4. Launch events are parsed using a bounded queue and eight workers rather than serial transaction lookups.
5. The recent-signature cache is now a bounded 20,000-entry LRU instead of clearing all deduplication history at once.
6. A temporary Pump.fun bonding-curve read failure no longer discards an otherwise valid transaction candidate; required safety checks remain fail-closed.
7. Jupiter quote creation now records route-specific validity. JupiterZ RFQs are capped to provider expireAt minus a 1.5-second safety margin; aggregator orders require lastValidBlockHeight, checked again against Solana getBlockHeight immediately before execution. Unknown router formats and missing expiry data fail closed.
8. Added Deno tests for expired aggregator block heights, missing expiry values, short/invalid JupiterZ RFQ expiry, and unknown routers. The updated telegram-bot-next Edge Function was deployed as active version 46.
9. The GitHub dependency scan now reports package-level advisories and available fix information during CI.
7. The GitHub secret-sync workflow now requires a separate `VERCEL_BACKEND_PROJECT_ID` and refuses to sync backend credentials to the public Vite frontend.
8. Deployment documentation distinguishes Supabase's inbound Telegram webhook from the Python worker's background responsibilities.

## CI verification

GitHub Actions run #446 for commit `7e892482ce1996baa58e6a32f1f533f6fc7d7679` completed successfully: Python checks and pytest passed, Deno paper/exchange/Jupiter expiry checks passed, and the Vite production build passed. The backend secret-sync job was skipped by design. A green CI run does not prove the production Daemon is running.



### Web dependency advisories

The inspected Vercel build log reported **39 npm advisories (16 moderate, 23 high)** during `npm install`. The exact affected packages and fix paths were not included in the deployment summary. CI now runs a report-only `npm audit` and prints package, severity, affected range and available fix metadata before the web build. Do not run `npm audit fix --force` blindly: the wallet-connect dependency tree needs reviewed, tested upgrades rather than unreviewed major-version changes.

## Latest dependency and database audit

### Web dependency scan (CI run #442)

The new reproducible npm audit step on the checked-in web/package-lock.json reports 15 high, 0 critical, 0 moderate and 0 low advisories. This is lower than the older Vercel build-log summary of 39 total advisories (16 moderate and 23 high); the different counts should be reconciled after a fresh Vercel build using the current lockfile rather than assuming the older result is still current.

The current lockfile scan flags these important dependency families:

- Direct Reown pins: @reown/appkit@2.0.0, @reown/appkit-adapter-solana@2.0.0, and @reown/appkit-adapter-wagmi@2.0.0. npm marks these direct findings high and reports no automatic compatible fix. The public npm latest tag still points to stable 1.8.24 while 2.0.0 is published separately; do not downgrade or major-upgrade the wallet SDK without a wallet-connect regression test.
- bigint-buffer@1.1.5 is flagged for GHSA-3gc7-fjrx-p6mg / CVE-2025-3194. GitHub's advisory says versions through 1.1.5 are affected and lists no patched release. A package override cannot honestly claim remediation while upstream has no fix.
- zod@3.25.76 is flagged for GHSA-mrhx-c7xc-m3w5. The reviewed advisory says the fix is in Zod 4.6.6, while the 3.x line has no patched release. Forcing Zod 4 into consumers whose dependency graph still expects Zod 3 may break wallet SDK schemas; replace or upgrade the parent libraries to compatible versions first.
- Other high findings are reported in the Reown, Coinbase/Base and Solana dependency trees. The CI report records direct/transitive status and fixAvailable; those with no compatible fix remain open risk.

The CI audit is currently report-only and does not block a working build. Next remediation should be a reviewed Reown + wallet adapter dependency update or replacement, then regenerate the lockfile, test Phantom/Solflare/WalletConnect and EVM wallet paths, and rescan. Do not use npm audit fix --force as an unreviewed one-command fix.

### Supabase database advisors

The live Supabase advisors currently report:

- One security warning: pg_net is installed in the public schema. Review extension dependencies and Supabase guidance before moving it.
- 24 foreign-key columns without covering indexes. The advisor currently reports these as performance information, not a security failure. Review the high-use joins first and add indexes in a reviewed migration rather than indexing all columns indiscriminately.
- No additional health advisor findings at the time of this check.

The current data snapshot is 52 Telegram updates processed with zero retryable errors, 2 paper sessions, 20 paper positions and 40 paper trades. There are zero Jupiter live swap records. Jupiter manual-wallet certification remains disabled and delegated automation remains disabled.

## Hosting and deployment gap

The repository contains two distinct runtime shapes:

- Frontend: `web/package.json`, `web/vercel.json`, Vite output `web/dist`.
- Backend/worker: root `index.py`, `requirements.txt`, `src/n9n8/`, `worker/Dockerfile`, root `vercel.toml`.

The existing website project builds the Vite app. Its successful deployment is not evidence that the repository-root `vercel.toml` Daemon configuration was deployed.

**Safe layout:** keep `9n8-secure-connect` rooted at `web/`; create or correctly link a separate backend Vercel project to the same Git repository with root directory `.`. Confirm it loads the root `vercel.toml`, builds the FastAPI service and starts the configured Daemon. The inspected `9n8-trading-agent` project was not linked to this Git repository and lacked the Python worker environment variables.

Vercel Daemons are in public beta. Its pricing page lists 5 active CPU hours/month included, followed by usage-based active CPU and provisioned-memory charges. Review billing and set spend alerts before enabling a persistent process.

## Backend environment checklist

Configure these on the **backend** project, not the public frontend:

| Variable | Requirement |
|---|---|
| `DATABASE_URL` | Neon PostgreSQL connection string |
| `N9N8_REQUIRE_POSTGRES` | `true` |
| `N9N8_WORKER_BRIDGE_SECRET` | Random secret of at least 32 characters, shared by API and Daemon |
| `TELEGRAM_BOT_TOKEN` | Bot token for Python API notifications |
| `AUTHORIZED_USER_ID` | Positive private Telegram ID; validated `TELEGRAM_CHAT_ID` fallback is supported |
| `HELIUS_API_KEY` | Private RPC/WebSocket key |
| `MASTER_TREASURY_ADDRESS` | Valid public Solana address controlled by the operator |
| `ENCRYPTION_MASTER_KEY` | Existing Fernet key; do not rotate if encrypted wallet records exist |
| `WEBAPP_URL` | `https://9n8-secure-connect.vercel.app` |
| `N9N8_TRADING_MODE` | `paper` |
| `LIVE_TRADING_ENABLED` | `false` |
| `JUPITER_API_KEY` | Optional API quota/analytics key |

The GitHub workflow additionally needs the Actions variable `VERCEL_BACKEND_PROJECT_ID` and the secret `VERCEL_TOKEN`. It syncs Telegram, user ID, Helius, treasury, encryption and optional Jupiter credentials. Configure the database URL, bridge secret, queue values, web app URL and safety-mode variables on the backend project separately.

Never paste credentials, wallet keys, Fernet keys or database URLs into chat or source control.

## Deep research relevant to trading safety

### Launch detection

Solana `logsSubscribe` supports `processed`, `confirmed` and `finalized` commitment. The listener uses `processed` to see launch signals early, while `getTransaction` asks for `confirmed` data. A transaction may therefore be temporarily unavailable just after the log event arrives. Retrying before marking a signature complete is necessary.

Helius documents that Standard and Enhanced WebSockets now use LaserStream infrastructure, including multi-node ingestion, automatic failover and up to 48-hour replay. This helps during disconnects, but local deduplication, bounded queues, persistent idempotency and monitoring are still required.

### Swap submission

Jupiter's current recommended Swap API v2 path is `/order` → sign → `/execute`. An order can include `requestId`, `transactionVersion`, `lastValidBlockHeight` (aggregator) or `expireAt` (RFQ). Reject empty transactions, deserialize the returned version correctly, and submit the matching request ID to `/execute`. Jupiter manages landing and confirmation for supported Order transactions.

For custom RPC/Jito execution, an RPC acceptance response is not proof that a transaction landed. Keep preflight enabled, persist signature and blockhash validity, poll status, and reconcile actual balances and fees before net PnL. Never create a newly signed replacement while the first transaction's blockhash remains valid; both could land. Re-broadcasting the exact same signed transaction is safer than re-signing.

### Telegram

Telegram cannot deliver updates through long polling while an outgoing webhook is set. Supabase must remain the single owner of inbound updates and its command menu; the Python worker must not start competing `getUpdates` polling.

## Release gates

1. Create/link a dedicated backend Vercel project rooted at `.`; confirm the root config and Daemon are included in its deployment.
2. Configure all backend environment variables above. Add `VERCEL_BACKEND_PROJECT_ID` as a GitHub Actions variable and `VERCEL_TOKEN` plus the other listed credentials as GitHub Actions secrets.
3. Manually run the **9N8 Solana tests** workflow. Confirm the sync job targets the backend ID and completes. Never point it at the public frontend.
4. Verify a fresh Neon heartbeat, healthy worker-status endpoint, Helius subscription, queue receive/ack and recovery after restart.
5. Test paper mode against real market data with no wallet signing, including reconnect/replay, duplicates, queue redelivery, timeouts and worker restarts.
6. Keep live locked until a dedicated low-value test wallet independently passes buy, partial/full sell, auto-exit, unknown-outcome reconciliation, recovery and withdrawal tests with confirmed/finalized receipts and net-of-fees PnL.

Do not enable `LIVE_TRADING_ENABLED`, delegated signing or automated live entries as part of this audit.

## Official references

- Solana logsSubscribe: https://solana.com/developers/docs/rpc/websocket/logssubscribe
- Solana getTransaction: https://solana.com/developers/docs/rpc/http/gettransaction
- Solana transaction retry and blockhash expiry: https://solana.com/developers/cookbook/transactions/retry
- Helius LaserStream/WebSocket reliability: https://www.helius.dev/blog/laserstream-websockets
- Jupiter Swap API v2 Order & Execute: https://developers.jup.ag/docs/swap/order-and-execute
- Jupiter route expiry and execution response fields: https://github.com/jup-ag/docs/blob/main/swap/order-and-execute.mdx
- Zod advisory GHSA-mrhx-c7xc-m3w5: https://github.com/advisories/GHSA-mrhx-c7xc-m3w5
- bigint-buffer advisory GHSA-3gc7-fjrx-p6mg: https://github.com/advisories/GHSA-3gc7-fjrx-p6mg
- Telegram Bot API FAQ: https://core.telegram.org/bots/faq
- Vercel Daemons: https://vercel.com/docs/daemons
- Vercel pricing: https://vercel.com/pricing
