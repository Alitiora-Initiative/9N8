# 9N8 Engine V2 — modular rebuild

## Objective

Replace the mixed feature surface with explicit, testable boundaries between market observation, strategy research, risk approval, execution, persistence, Telegram control and operations. This is a staged migration: production webhook and Python worker remain intact until V2 adapters are wired, tested and deployed together.

## Runtime topology

- **Python 3.11+ / asyncio:** long-running orchestration, strategy scheduling, risk evaluation and worker lifecycle.
- **Rust-backed Solana primitives:** Solders / solana-py for public keys, transaction serialization, signatures and RPC structures. A separate Rust service is introduced only if profiling demonstrates a real bottleneck.
- **TypeScript / Deno:** Telegram webhook, Mini App API and existing Solana/Jupiter safety endpoints.
- **SQL / PostgreSQL (Neon):** authoritative state for users, wallet references, risk policy, trade intents, approvals, orders, fills, positions, receipts and worker jobs.
- **Redis (optional):** ephemeral market tick cache and fanout only when measured concurrency requires it. It is never the trade ledger.
- **Docker / Linux:** reproducible worker runtime and process supervision.
- **GitHub Actions:** tests, type checks, dependency audit, lockfile checks and staged deployment.

## V2 package boundaries

| Package | Responsibility | Must not do |
|---|---|---|
| v2/contracts.py | Immutable typed contracts, Decimal amounts, explicit mode | Call providers or sign transactions |
| v2/market_data.py | Normalize OHLCV, UTC, closed candles and quality | Generate trade approvals |
| v2/quant.py | Kelly cap, position sizing, Sharpe, Sortino, drawdown, VaR, Monte Carlo, ATR, VWAP, Bollinger | Place orders or promise predictive edge |
| v2/risk.py | Quote freshness, impact, slippage and liquidity gates | Bypass token safety |
| v2/execution.py | State machine, idempotency, live gates and quote-only Paper adapter | Automatically retry unknown submissions |
| v2/signals.py | Transparent baseline trend and volatility classification | Make ML forecasts or authorize a trade |
| v2/health.py | Sanitized component health snapshots | Expose credentials, private keys or secret query strings |

## Trade lifecycle

SCANNING → CANDIDATE → PREFLIGHT → AWAITING_APPROVAL → SIGNED → SUBMITTED → CONFIRMED

Recovery and terminal states:

- FAILED: provider or chain conclusively rejected the request.
- UNKNOWN: submit may have reached the provider but confirmation is missing. Freeze the intent for reconciliation; never create a fresh order as a timeout retry.
- CANCELLED: cancelled before signing or submission.

Every live intent has a stable idempotency key. PostgreSQL must persist the authoritative intent before external submission. A retry loads the original intent and receipt rather than manufacturing another order.

## Hard isolation rules

1. Paper has separate balances, settings, positions, fills and PnL. Its adapter exposes quotes only; it has no signing, submission or withdrawal method.
2. Live requires the operator switch, user opt-in, verified wallet ownership, current risk approval, fresh quote, slippage approval, explicit owner approval and a clear emergency stop.
3. A Jupiter API key is a routing credential, not a wallet signer or spend authorization.
4. Helius and Jupiter observations are timestamped. Stale or malformed inputs fail closed.
5. An unresolved submission is UNKNOWN; never submit a second order because a request timed out.
6. Exchange keys must not have withdrawal/transfer permission. Exchange connectors are separate from the Solana wallet signer.
7. Health screens report configured, connected and last-success separately. An environment variable being present is not proof of a working provider.

## Market-data and quantitative requirements

- Normalize all timestamps to timezone-aware UTC before storage or indicators.
- Drop unclosed bars from backtest inputs by default.
- Retain failed, rugged and delisted assets in research datasets to reduce survivorship bias.
- Apply rate limits, jittered exponential backoff, bounded queues and cache TTLs. Do not rotate identities or spoof user agents to bypass provider limits.
- Track source, observation time, liquidity, spread, depth, price impact, slippage estimates and missing-field state.
- Keep token-level safety independent from chart indicators. A bullish indicator never overrides a failed mint, freeze, holder or liquidity check.
- Report Sharpe, Sortino, max drawdown, historical VaR, Monte Carlo tail risk and risk-of-ruin as diagnostics, not guarantees.
- Use walk-forward and out-of-sample validation before a learned or regime model affects live order eligibility.

## Technology groups

### Production runtime

Python, asyncio, aiohttp, websockets, FastAPI, aiogram, Solders, Solana RPC, TypeScript/Deno, SQL/PostgreSQL, Docker, Bash and GitHub Actions are the primary runtime and build stack. NumPy remains in the production dependency set. Exchange permission adapters remain isolated from the Solana wallet signer.

### Research-only profile

requirements-research.txt contains CCXT, Pandas/technical-analysis, scikit-learn, VectorBT, yfinance, Scrapy, BeautifulSoup, Playwright, SciPy, Statsmodels and VADER. It must not be installed into the always-on worker as an unreviewed dependency bundle. TA-Lib, Backtrader, PyTorch, TensorFlow, R and Julia workloads belong in isolated research environments.

### Platform adapters reserved for explicit expansion

C++, Go, Solidity, Web3.py, C#, MQL4/MQL5 and GraphQL are not artificial production dependencies for the current Solana-only sniper. They have defined extension roles in TECHNOLOGY_CATALOG_100.md and become deployable adapters only when their corresponding chain, broker, performance or data requirement exists.

## Deployment migration gates

1. V2 domain tests pass; typed contracts and time normalization are stable.
2. Paper consumes the V2 market-data and risk contracts without access to a signer.
3. Live preflight uses V2 gates, database idempotency, the state machine and confirmed-receipt reconciliation.
4. Telegram reads the same PostgreSQL state as the worker and displays real heartbeat, wallet, positions and risk with freshness timestamps.
5. Stop/restart/redeploy recovery passes against staging; stale jobs become UNKNOWN, not repeat orders.
6. A small-value mainnet canary is manually approved and separately monitored. CI success alone never arms live trading.

## Scope boundary

This branch adds V2 domain foundations and verification tests. It does not claim that V2 has already replaced production execution, that every listed technology is installed in the worker, or that live trading is certified.
