# NEXUS Architecture Blueprint

**Status:** Architecture proposal for review  
**Base repository:** `Alitiora-Initiative/9N8`  
**Branch:** `nexus/architecture-blueprint`  
**Product direction:** Personal-first, multi-user-ready  
**Execution policy:** Rule-limited automation for explicitly allowed strategies; all other eligible trades require owner approval

> This document is a design blueprint. It does not enable live trading, add credentials, deploy infrastructure, or change the current bot runtime.

## 1. Product definition

NEXUS evolves 9N8 from a private OKX spot bot into a personal market-intelligence and controlled-execution agent.

It should:
- Research markets using timestamped, attributable sources.
- Analyze prices and market conditions using deterministic, testable strategies.
- Turn analysis into a structured trade proposal with entry, invalidation, target, size, estimated costs, and evidence.
- Automatically execute only when an owner-configured policy explicitly permits the proposal.
- Send every other eligible proposal to Telegram for approval.
- Reject proposals that violate hard safety limits; approval must not override those limits.
- Reconcile orders, fills, positions, and protective orders after restarts or network failures.
- Keep account credentials and trading data isolated by tenant so a later multi-user product does not require a redesign.

The first release stays narrow: **OKX spot, long-only, no margin, no leverage, no derivatives, no withdrawals, and no wallet private-key custody.** Other markets are added through tested adapters.

## 2. Findings from the current repository

The current `main` and `exchange-api` branches contain the same basic implementation:
- `src/bot.py` is a single Telegram application and contains most command, order-staging, authorization, and callback logic.
- `src/okx_provider.py` is a native OKX v5 REST client for public market data, account reads, spot orders, open orders, and cancellations.
- `src/strategy.py` calculates EMA(9), EMA(21), and RSI(14). It returns observations; it does not generate or execute orders.
- `tests/test_okx_provider.py` mocks exchange HTTP responses, and `tests/test_strategy.py` tests indicator calculations.
- `requirements.txt` contains python-telegram-bot, python-dotenv, and httpx.
- The current bot is owner-only, uses Telegram long polling, keeps pending actions in `context.user_data`, and requires a callback confirmation for every order.
- `.env.example` has OKX credentials and mode switches, but no database or per-connector credential model.
- `docker-compose.yml` contains legacy `PAPER_DB_PATH`, `MT5_PROVIDER`, and `MODE` values that do not match the current README and active OKX-only code. Remove or replace these when container configuration is refactored.

Preserve the working OKX signing, instrument validation, demo header, Decimal-based order sizing, and mocked provider tests. Do not replace them wholesale.

## 3. Target architecture

Use a **modular monolith** for the first release: one Python codebase, one long-running container, one database, clearly separated modules. Do not start with microservices, Kafka, Redis, or a multi-agent framework.

### Runtime components

1. **Telegram interface**
   - Keep python-telegram-bot and long polling for the private bot.
   - Commands, inline approval buttons, account connection status, reports, and owner-only controls live here.
   - Long polling uses outbound HTTPS and does not require a public inbound webhook or a webhook IP. Telegram supports either long polling or webhooks, not both at once.
   - Use one active bot process per Telegram token. Add a database lease/heartbeat so a second replica cannot also consume updates or submit orders.

2. **Application and domain layer**
   - Contains users/tenants, connector accounts, market observations, strategies, trade proposals, risk policies, approvals, order intents, orders, fills, and audit events.
   - Uses typed models and Decimal for prices, quantities, fees, and notional calculations.
   - Does not depend on Telegram types or vendor-specific API response objects.

3. **Research and market-data layer**
   - Pulls prices, candles, order books, account data, exchange status, company/market news, filings, and social context through separate providers.
   - Every observation records source, symbol, event time, retrieval time, and freshness.
   - Yahoo Finance may be an optional research reference, but unofficial scraping must not be the source of truth for live order decisions. Use documented broker/exchange feeds for execution-critical prices.

4. **Strategy and proposal layer**
   - Converts market observations into versioned, explainable signals.
   - Generates a structured `TradeProposal`; never calls an exchange directly.
   - Stores the strategy version, input snapshot IDs, explanation, and any data-quality warnings.

5. **Risk and permission engine**
   - A deterministic policy service evaluates every proposed order, whether manual or automatic.
   - It is the only component allowed to issue an execution authorization.
   - It applies hard safety rules first, then determines whether the proposal is inside the auto-trade envelope or requires human approval.

6. **Execution and reconciliation layer**
   - Uses exchange adapters with a common internal contract.
   - Persists an order intent before sending an exchange request.
   - Uses a stable client order ID/idempotency key and reconciles ambiguous timeouts before any retry.
   - Tracks acknowledgements, partial fills, full fills, cancellations, rejections, and unknown states.
   - Monitors protective orders and positions after entry.

7. **Persistence and background jobs**
   - Supabase Postgres is the durable system of record.
   - Use a small durable job/outbox table with leases and retry counters initially. Supabase Queues/PGMQ can be evaluated later; it is currently marked Public Alpha, so it should not be a hard production dependency yet.
   - Store audit events append-only. Do not rely on in-memory Telegram user data for approvals or order state.

8. **AI research assistant**
   - AI can summarize news, compare scenarios, extract facts, explain indicators, and draft proposals.
   - AI cannot access API secrets, sign transactions, bypass risk checks, or call exchange order endpoints directly.
   - All model output is validated against typed schemas and deterministic rules before it can become a proposal.
   - Keep model/provider choice configurable rather than making the trading system depend on one LLM vendor.

### High-level order flow

```text
Telegram / Scheduled Scan
          |
          v
Market Data + News Providers
          |
          v
Strategy / AI Research
          |
          v
Structured TradeProposal
          |
          v
Deterministic Risk & Permission Engine
       /          |           \
      /           |            \
  HARD BLOCK   AUTO-ELIGIBLE   MANUAL-ONLY
      |           |                |
   Reject     Durable job      Telegram approval
                  |                |
                  +-------> Revalidate
                               |
                               v
                        Order Intent
                               |
                               v
                       Exchange Adapter
                               |
                               v
                     Order/Fills Stream
                               |
                               v
                     Reconciliation + Audit
```

## 4. Technology choices

| Area | Choice | Reason |
|---|---|---|
| Language | Python 3.11+ initially | Preserves the existing working project and its libraries |
| Telegram | python-telegram-bot v22.x | Async handlers, polling, callbacks, and a migration path to webhooks |
| HTTP | httpx async client | Reuse the current HTTP stack; set explicit timeouts and bounded retries |
| Validation | Pydantic v2 | Typed provider payloads, settings, policies, and model output |
| Database | Supabase Postgres | Durable users, policies, approvals, orders, fills, and audit trail |
| SQL layer | SQLAlchemy 2 async + psycopg 3, or a small psycopg repository layer | Persistent service connections and explicit transactions |
| Migrations | Supabase migrations / SQL files | Reproducible schema changes |
| Secrets | Environment secrets for the single-owner local/VPS deployment; Supabase Vault or a dedicated KMS-backed secret store for per-user credentials later | Keeps API keys out of Telegram, GitHub, prompts, and client code |
| Exchange integration | Existing native OKX adapter first; CCXT async for selected common crypto operations later; native adapters for exchange-specific features | Common exchange interface without hiding important vendor differences |
| Brokerage | alpaca-py for a paper-trading adapter | Offers a documented paper environment and Python SDK |
| Container | Docker + Docker Compose | Matches the current repository and supports local Windows development and VPS deployment |
| Tests | pytest; provider contract tests and sandbox integration tests | Keeps execution behaviour testable without touching live funds |
| Observability | structured, redacted logs + health/heartbeat records | Diagnose failures without logging secrets |

### Important platform choices

- **Supabase is the database/control plane, not the 24/7 trading worker.** Hosted Edge Functions have bounded duration and CPU limits; use them for short webhooks or lightweight endpoints, not a persistent trading loop.
- **Use Supabase RLS for any user-facing dashboard tables.** Every tenant-owned row must carry a tenant/user ID. Enable RLS on exposed tables and test both allowed and denied reads/writes. Never expose a Supabase secret/service-role key in a browser.
- **Supabase Vault encrypts secrets at rest, but decrypted-secret access must be tightly restricted.** Never grant a normal user-facing role access to the decrypted secrets view.
- **Use a persistent VPS/container for unattended monitoring.** Local Docker on Windows remains the development path. For production, a small VPS with a fixed outbound IPv4 is useful for exchange API IP allowlisting. A Hetzner CX23 has a published server price of about US$6.49/month before IPv4 and tax; its Primary IPv4 is listed at US$0.60/month before tax. Verify the checkout price and region before ordering.
- Railway is an alternative for simpler deployment, but its static outbound IPv4 feature is documented for the Pro plan, so it is not the first choice if IP allowlisting is a priority.
- Supabase Free projects can pause after a week with too little database activity. Use Free for development, and do not make unattended live execution depend on an unreliable database tier. Upgrade only when the project is ready and the recurring cost is accepted.
- Telegram long polling is the simplest fit for the current private bot. A webhook can be added later if needed; do not spend time configuring a public webhook/IP for the existing polling model.

## 5. Exchange and wallet integration plan

### Common exchange adapter contract

Each adapter should implement a small interface:
- `get_capabilities()` — markets, order types, test environment, stop/protection support, streaming support.
- `get_market_data(symbol)` and `get_candles(symbol, timeframe, limit)`.
- `get_account_state()`, `get_balances()`, and `get_positions()`.
- `validate_order(proposal)` — exchange lot/tick/minimums and market state.
- `submit_order(intent)`, `get_order(client_order_id)`, `list_open_orders()`, and `cancel_order(order_id)`.
- `stream_order_updates()` where supported.

The adapter must declare what it does **not** support. The risk engine must not assume that all exchanges share order semantics, test environments, stop-loss behaviour, or available markets.

### Recommended integration sequence

1. **OKX spot — preserve and harden the existing native adapter.** Add WebSocket/private order updates, order-history reconciliation, attached or standalone protective-order workflows only after validating the exact spot/account-mode behaviour, and stable client order IDs.
2. **Alpaca paper — add stock/crypto paper experiments through alpaca-py.** Alpaca documents a global paper-only account, but it is simulation only and paper-only users receive IEX market data. Do not assume that a paper account means a live brokerage account is available in every country.
3. **Bybit demo — add after the common adapter contract is stable.** Bybit explicitly states that its demo service does not expose every API.
4. **Binance Spot Testnet — add as a separate sandbox adapter.** Use testnet credentials and testnet endpoints; permissions should be read-only/user-data and trading only as required.
5. **More venues — add one at a time**, with a contract test suite and feature/capability declaration.
6. **Phantom and Trust Wallet — wallet connection/read-only first.** The wallet owns the private key. Construct an unsigned transaction and pass it to the connected wallet for the user's explicit signature. Do not collect seed phrases, private keys, or attempt unattended signing from Telegram.

CCXT can speed up common crypto REST operations and supports asynchronous Python. It still exposes exchange-specific parameters, and sandbox keys are not interchangeable with production keys. Keep native code for the critical OKX path and any feature whose behaviour must be exact.

## 6. Trading modes and risk policy

### Three explicit modes

- `PAPER`: simulated orders only. Default on every new connection.
- `LIVE_APPROVAL`: real orders require an explicit Telegram approval.
- `LIVE_RULED`: real orders may execute automatically only if the proposal satisfies the user's saved auto-trade policy.

New deployments and new connectors start with `PAPER`. `LIVE_RULED` is disabled until the owner explicitly enables it for a specific account, strategy, market type, and symbol allowlist.

### Two levels of limits

**Hard limits** are never bypassed by a Telegram approval:
- Unsupported product/account mode, disabled venue, or disallowed symbol.
- Withdrawals, transfers, margin, borrowing, derivatives, or leverage in the initial release.
- Invalid price/quantity precision, below-minimum order, insufficient available balance, or invalid market state.
- Stale or inconsistent price/account data, or an exchange connection that cannot be verified.
- Maximum account exposure, maximum daily loss/drawdown, and maximum order notional.
- Duplicate intent, duplicate client order ID, or unresolved prior order state.
- Missing required protective-order capability for a strategy that depends on a stop.

**Auto-trade envelope** is narrower than the hard limits:
- Approved strategy IDs and versions.
- Approved account, venue, market type, and symbols.
- User-configured risk per trade, per-order notional, daily notional, open position count, and total portfolio exposure.
- Maximum spread and slippage, minimum liquidity, maximum data age, trade cooldown, and allowed time windows.
- Required stop-loss / take-profit rules and a tested way to verify those orders are active.

Decision:
- Inside the auto envelope: queue for automatic execution.
- Outside the auto envelope but inside every hard limit: request Telegram approval.
- Any hard-limit breach: reject, explain the reason, and do not offer an approval bypass.

If a strategy requires stop-loss protection, automatic execution remains disabled for that connector/market until the exchange-side protective workflow is implemented and verified. A stop-loss does not guarantee a particular exit price during gaps or illiquid markets.

### Execution safety

Before any order:
1. Persist the proposal and order intent.
2. Lock the account/policy scope and reserve the intended risk budget.
3. Refresh balance, instrument metadata, order book/quote, and market state.
4. Recalculate size, notional, spread, slippage, and policy eligibility.
5. Generate a unique client order ID and submit once.
6. If the request times out, mark it `UNKNOWN` and query the exchange by client order ID/order history before considering a retry. Never blindly repeat a timed-out order POST.
7. Track acknowledgements and fills separately. An accepted order is not the same as a filled order.
8. Reconcile exchange state against the database after restart and periodically during operation.

Suggested states:
`DRAFT -> VALIDATING -> PENDING_APPROVAL | QUEUED_AUTO -> SUBMITTING -> ACCEPTED -> PARTIALLY_FILLED -> FILLED`

Terminal/exception states include `REJECTED`, `CANCELLED`, `EXPIRED`, and `UNKNOWN`. Any unresolved `UNKNOWN` state blocks further automated trading for that account until reconciliation completes.

## 7. Telegram approval and multi-user authorization

- Replace `context.user_data["pending_okx_action"]` with a durable database approval record.
- Each approval is one-time, expires, and is bound to tenant, Telegram user ID, chat ID, message ID, account, exact proposal hash, and canonical order payload hash.
- On button press, verify the caller and re-run market, balance, connector, and risk validations. If price, size, or risk changed beyond the approved tolerance, invalidate the approval and issue a fresh proposal.
- Store every approve/reject/expiry and every policy change in the audit log.
- Keep the first version owner-only.
- For groups, separate group administration permissions from personal trading permissions. Group admins may moderate or publish alerts, but group admin status never grants access to a member's exchange account.
- For future users, onboard with an authenticated HTTPS setup page or a local setup wizard. Do not ask users to paste exchange secrets into Telegram messages or GitHub issues. Credential entry should bypass the LLM and be redacted from logs.

## 8. Initial database model

All account-owned tables include `tenant_id` from day one, even while there is only one tenant.

| Table | Purpose |
|---|---|
| `tenants`, `tenant_memberships` | Personal owner now; future user/team boundaries |
| `telegram_identities` | Telegram user/chat mapping and status |
| `connector_accounts` | Venue, account label, test/live environment, capabilities, status |
| `credential_refs` | Vault/KMS secret references only; never plaintext credentials |
| `risk_profiles` | Hard limits and auto-trade envelope |
| `strategies`, `strategy_versions` | Versioned strategy parameters and enablement |
| `watchlists`, `market_observations` | Tracked symbols and timestamped provider data |
| `trade_proposals` | Structured ideas, evidence, model/strategy version, expiry |
| `approvals` | Durable Telegram approvals and decision history |
| `order_intents` | Canonical order payload, idempotency key, validation result |
| `orders`, `fills`, `positions` | Exchange order and portfolio state |
| `risk_snapshots` | Equity, exposure, limits, drawdown, and circuit-breaker state |
| `audit_events` | Append-only security and trading audit trail |
| `worker_leases`, `job_outbox` | Single-active-worker lock and durable background work |

Use database constraints and transactions for uniqueness, tenant ownership, and one-time approvals. Enable RLS on every exposed user-data table, and add SQL tests for both permitted and denied access. Never put exchange API keys in ordinary table columns or user-editable Auth metadata.

## 9. Repository structure after the refactor

```text
src/
  bot.py                     # process entrypoint
  application/
    telegram_handlers.py
    approval_callbacks.py
    authorization.py
  domain/
    models.py                # TradeProposal, OrderIntent, RiskDecision
    errors.py
    state_machine.py
  market_data/
    service.py
    providers/
      okx_market.py
      alpaca_market.py
      news_rss.py
  research/
    orchestrator.py
    source_registry.py
    summarizer.py
  strategies/
    base.py
    indicators.py
    registry.py
  risk/
    policy.py
    sizing.py
    circuit_breaker.py
  exchanges/
    base.py
    okx.py                   # refactor existing src/okx_provider.py
    alpaca.py
    bybit.py
    binance.py
  execution/
    service.py
    approvals.py
    reconciliation.py
    protection.py
  persistence/
    database.py
    repositories/
    migrations/
  security/
    secrets.py
    redaction.py
  jobs/
    worker.py
    monitors.py
tests/
  unit/
  contract/
  integration/
supabase/
  migrations/
  tests/
docs/
```

Refactor incrementally. The first pull request should not move every file at once; first extract the domain types, adapter interface, and tests while keeping the existing Telegram commands working.

## 10. Delivery plan and acceptance gates

### Phase 0 — repository safety and baseline
- Keep the current default branch unchanged.
- Add this blueprint on a feature branch.
- Record the current OKX demo behaviour and existing test baseline.
- Fix stale Compose settings only when replacing them with the new documented configuration.
- Add dependency lockfile and test/lint commands.

**Gate:** existing mocked OKX and strategy tests pass unchanged.

### Phase 1 — modular foundation
- Extract domain types and the `ExchangeAdapter` protocol.
- Move existing OKX calls behind the adapter.
- Separate Telegram handlers from business logic.
- Replace global constants with validated settings.
- Add structured redacted logging and a health check.

**Gate:** Telegram commands work as before in local demo mode; adapter tests pass.

### Phase 2 — durable data and approvals
- Connect Supabase Postgres.
- Add migrations and tenant-scoped schema.
- Persist connector references, risk profiles, proposals, order intents, approvals, orders, fills, and audit events.
- Replace in-memory pending actions with durable approvals and one-time payload hashes.
- Add RLS and SQL allow/deny tests.

**Gate:** restart the bot between staging and approving; no stale or replayed approval can submit an order.

### Phase 3 — risk engine and reconciler
- Implement hard limits and auto-trade envelope.
- Add account exposure, daily loss, concentration, cooldown, spread, slippage, and data-freshness checks.
- Add circuit breakers and a global/per-account pause.
- Add client-order-ID reconciliation and recovery from timeouts, restarts, partial fills, and exchange errors.

**Gate:** fault-injection tests show no duplicate order after retries/restarts and no order on any hard-limit breach.

### Phase 4 — research and paper execution
- Keep the existing OKX indicator calculations as the first strategy.
- Add structured proposals with data sources and timestamps.
- Add OKX demo and Alpaca paper adapters.
- Compare paper results with conservative fees/slippage assumptions.
- Add performance reports; do not optimize solely on win rate.

**Gate:** paper orders, cancellations, fills, and portfolio reports reconcile against provider state.

### Phase 5 — rule-limited automation in paper mode
- Implement per-account strategy allowlists and risk profiles.
- Run the auto policy in paper mode with complete event logs.
- Exercise stale data, high spreads, unavailable exchange, low balance, duplicate signal, and circuit-breaker scenarios.
- Keep live automatic execution disabled.

**Gate:** all test scenarios pass; no unexplained order-state mismatches remain.

### Phase 6 — live approval mode, then narrowly scoped live automation
- Start with live approval mode and a small owner-controlled spot account.
- Verify key permissions, IP restrictions, market rules, actual fills, cancellation, and protective orders.
- Only after those checks, enable live rule-limited mode for one connector, one strategy, and a small symbol allowlist.
- Keep a conservative order cap and daily loss cap; the owner must set the actual figures.
- Provide an emergency pause that blocks new orders immediately.

**Gate:** explicit owner enablement, successful rehearsal, reconciled order history, and protective-order verification. If any are missing, stay in demo or live-approval mode.

### Phase 7 — more connectors and multi-user readiness
- Add Bybit demo and Binance Spot Testnet.
- Add read-only Phantom/Trust Wallet portfolio connections, then user-signed transaction handoff.
- Add authenticated web onboarding/dashboard, per-user credential references, RBAC, tenant isolation, and group mode.
- Re-run cross-tenant security tests before inviting any other user.

**Gate:** one tenant cannot read or change another tenant's credentials, policies, orders, or audit history.

## 11. Immediate next implementation tasks

In priority order:
1. Add typed domain models and adapter contract without changing the existing order behaviour.
2. Add a proper settings layer and pin dependencies.
3. Refactor `src/bot.py` into Telegram handlers plus application services.
4. Fix the Compose configuration so it matches the active OKX-only application.
5. Add Supabase migration files and database repositories.
6. Persist approvals and order intents.
7. Implement the deterministic risk policy and reconciliation worker.
8. Only then add new exchange adapters or enable automatic live execution.

## 12. Security decisions that are non-negotiable

- No seed phrases, private keys, withdrawal permission, or transfer functionality.
- The existing OKX documentation warns that its Trade permission can include funding-transfer capabilities. Keep Withdraw disabled, use a dedicated low-balance trading subaccount/account where possible, bind API keys to the worker IP, and never expose transfer methods in the application.
- Exchange credentials never appear in Telegram, GitHub, prompts, error messages, or client-side code.
- A Supabase secret/service-role key is server-only and must never be sent to a browser.
- The AI cannot modify risk settings, enable live mode, approve its own proposal, or execute directly.
- If the database, risk engine, market data, exchange status, or reconciliation system is unavailable, new automated orders fail closed.
- The emergency pause must be available independently of the strategy/AI layer.

## 13. Research references

- Telegram Bot API — long polling and webhooks: https://core.telegram.org/bots/api
- python-telegram-bot v22 Application: https://docs.python-telegram-bot.org/en/stable/telegram.ext.application.html
- Supabase Row Level Security: https://supabase.com/docs/guides/database/postgres/row-level-security
- Supabase Vault: https://supabase.com/docs/guides/database/vault
- Supabase connection pooling: https://supabase.com/docs/guides/database/connecting-to-postgres/pooling-and-limits
- Supabase Queues / PGMQ: https://supabase.com/docs/guides/queues
- Supabase Edge Function limits: https://supabase.com/docs/guides/functions/limits
- Supabase free-project pausing: https://supabase.com/docs/guides/platform/free-project-pausing
- Supabase CLI v3 migration notes (November 2026): https://supabase.com/docs/guides/local-development/cli/migrating-to-v3
- OKX API v5 documentation: https://www.okx.com/docs-v5/en/
- Alpaca paper trading: https://docs.alpaca.markets/us/docs/paper-trading
- CCXT manual: https://docs.ccxt.com/docs/manual
- Bybit demo trading: https://bybit-exchange.github.io/docs/v5/demo
- Binance Spot Testnet: https://developers.binance.com/docs/binance-spot-api-docs/testnet
- Phantom sign-and-send transaction flow: https://docs.phantom.com/phantom-deeplinks/provider-methods/signandsendtransaction
- Hetzner Cloud price adjustment (June 2026): https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/
- Hetzner Primary IP pricing: https://docs.hetzner.com/cloud/servers/primary-ips/overview/
- Railway static outbound IPs: https://docs.railway.com/networking/static-outbound-ips
