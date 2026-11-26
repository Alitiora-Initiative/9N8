# 9N8 security model

## User and wallet boundaries

- Each Telegram account receives its own Solana wallet record.
- Private keys are encrypted before storage in SQLite using the configured Fernet key.
- Wallet recovery keys are sent once in a private Telegram message and the bot attempts to delete that message after 60 seconds. Deletion cannot remove screenshots, notification previews, message backups or copies.
- Never import a wallet holding long-term savings. Use a dedicated hot wallet with only the amount you are prepared to lose.
- The current release supports Solana wallets only. EVM deposits and cross-chain bridging are not enabled; never send EVM assets to a Solana address.

## Trading and execution boundaries

- Paper mode is the default and reports detected launches without simulating fills.
- Live mode is locked unless both `N9N8_TRADING_MODE=live` and `LIVE_TRADING_ENABLED=true` are set by the operator.
- Live mode is experimental and has not been independently audited or certified for unattended funds.
- The engine uses Helius program-log subscriptions, Jupiter Swap API v2 routing and optional Jito bundles.
- Jito submission acknowledgement is not proof of landing. The app checks bundle status before recording a trade as confirmed.
- Jito can reduce public-mempool exposure but does not guarantee block inclusion, a price, positive PnL or complete protection from MEV.
- Token security filters are fail-closed for unavailable mint/holder data and the configured Raydium LP lock check. They cannot prove a token is safe or that liquidity will remain.
- Pump.fun direct bonding-curve transaction construction, Raydium direct AMM instruction construction, and developer-buy min/max filtering are not complete.
- Quote polling follows Jupiter rate limits; the bot does not promise 250 ms quote updates or sub-slot exits.
- A take-profit target is a trigger condition, not a guaranteed execution price or profit.

## Financial controls

- Monetary calculations use Python `Decimal` and integer lamports.
- Buy sizing reserves configured trade amount, priority fee estimate, Jito tip, ATA rent reserve and gas buffer.
- Daily trade cap and daily realized drawdown circuit breaker are enforced by the engine.
- Profit fee is computed from a conservative minimum-output estimate before the exit transaction. Exact finalized net-profit reconciliation remains a required hardening step.
- Withdraw and wallet rotation ask for a confirmed Solana destination and submit the platform fee plus payout transaction.
- A timeout after transaction submission is ambiguous: inspect the signature and wallet balance before retrying an operation.
- Network fees, Jito tips, price impact, token taxes/extensions and failed attempts may reduce the final balance.

## Mini App authentication

- FastAPI verifies Telegram WebApp `initData` using HMAC-SHA256 and rejects expired sessions.
- REST requests send the signed payload in the `X-Telegram-Init-Data` header.
- WebSocket connections send signed `initData` in the first WebSocket frame, not the URL query string.
- Serve the Mini App only over HTTPS and keep the bot token private.

## Secret handling and deployment

- Store `TELEGRAM_BOT_TOKEN`, `HELIUS_API_KEY`, `MASTER_TREASURY_ADDRESS` and `ENCRYPTION_MASTER_KEY` only in private runtime environment variables.
- GitHub Actions Secrets are not automatically available to a Windows process or to a hosted web service. Map secrets explicitly into the runtime that actually starts the bot.
- Never log Helius credentials, Fernet keys, private keys, signed transactions or Telegram initData.
- Back up the SQLite volume and encryption key separately, securely. Loss of the encryption key makes existing encrypted private keys unrecoverable.
- Do not host the encrypted wallet database on an ephemeral filesystem. Use a persistent disk or durable database and verify restore procedures before accepting deposits.
- Do not enable live mode until tests, mainnet transaction construction, Jito bundle landing, withdrawal recovery and security review are complete.


## Neon and the persistent worker

- Production wallet ciphertext, settings, positions, risk, transaction receipts and queue data belong in the dedicated `9n8-trading` Neon project, database `n9n8`. Do not reuse the separate brokerage `vyra-markets` database.
- `ENCRYPTION_MASTER_KEY` must be identical for the Vercel request app and the worker, stored as an encrypted environment secret, and backed up separately. Database backups without the key cannot recover wallet secrets; the key without database backups cannot recover the rows.
- The Vercel request app may enqueue a transfer or panic-sell job but must never tell the user it is confirmed until the daemon has actually submitted and reconciled it.
- Worker jobs that may have submitted an on-chain transaction but lost confirmation are marked `unknown`, never blindly retried. The operator/user must inspect the transaction signature and wallet state before retrying.
- Vercel Daemons use ephemeral containers. Never store private keys, receipts, queue state or position state on the daemon filesystem.
- Live execution stays locked until post-confirmation net-PnL accounting, restart/reconciliation, key-backup recovery, worker failover, and small-value mainnet execution have been independently verified.
