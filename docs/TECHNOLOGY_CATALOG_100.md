# 9N8 — 100-technology implementation catalogue

This catalogue accounts for every item in the requested specification. ACTIVE means used by the application or V2 foundations; RESEARCH means isolated from the live worker; RESERVED means an extension point exists but that platform is not deployed; NOT USED means adding the item would not improve the current Solana-only runtime.

## Part 1 — Languages and runtimes (1–15)

1. **Python — ACTIVE.** Primary asyncio worker, strategy coordinator, risk and Telegram business logic.
2. **C++ — ACTIVE SPECIALIST MODULE.** The native-risk library computes order-book spread, depth imbalance and size-aware market impact; CI compiles/tests it and archives generated assembly.
3. **Rust — ACTIVE.** Solders provides Rust-backed Python Solana primitives; services/solana-core adds checked integer AMM math and bounded signature deduplication.
4. **JavaScript / Node.js — ACTIVE in tooling.** Browser Mini App and Node-based Vite/npm build; Node is not the long-running trading supervisor.
5. **TypeScript — ACTIVE.** Deno Telegram webhook, Jupiter and wallet API boundaries.
6. **SQL — ACTIVE.** PostgreSQL/Neon authoritative state and migrations.
7. **Solidity — ACTIVE OPTIONAL EVM POLICY MODULE.** The 0.8.26 risk policy compiles in CI but is not deployed and has no access to the Solana signer.
8. **Go — ACTIVE SPECIALIST SERVICE.** An authenticated OHLCV gateway validates RFC3339, UTC, duplicates, closed bars and field integrity.
9. **C# — ACTIVE BROKER RISK ADAPTER.** A .NET 8 module validates bid/ask spread, quote freshness and position exposure; it is not a broker order sender.
10. **Bash / Shell — ACTIVE.** CI, Docker entrypoints and deployment operations.
11. **Assembly — COMPILER OUTPUT.** C++ CI generates and archives optimized assembly listings; no hand-written assembly executor is required.
12. **Pine Script — RESEARCH ADAPTER.** The TradingView v6 script plots EMA, ATR, VWAP and Bollinger signals and emits alerts only.
13. **MQL4 / MQL5 — SIGNAL-ONLY ADAPTERS.** Both terminal scripts calculate EMA/ATR and relay signals over HTTPS; no OrderSend/CTrade order placement is present.
14. **R — RESEARCH.** Statistical validation and reproducible research, not the always-on order process.
15. **Julia — RESEARCH.** Optional numerical experiments after a benchmark justifies it.

## Part 2 — Python libraries and AI frameworks (16–30)

16. **CCXT — IMPLEMENTED RESEARCH CONNECTOR.** Async exchange OHLCV uses the built-in rate limiter and normalizes timestamps/bars; live exchange orders stay behind separate permission checks.
17. **Pandas — RESEARCH / BACKTEST INPUT.** Reads chronological OHLCV history for indicator comparisons, VectorBT and Backtrader.
18. **NumPy — ACTIVE.** Numerical primitives; financial money and lamports remain Decimal/integer.
19. **TA-Lib — RESEARCH / OPTIONAL NATIVE INDICATORS.** Indicator comparison script checks RSI results against pandas-ta-classic; native C library stays out of the live worker.
20. **Pandas-TA — RESEARCH.** pandas-ta-classic is isolated for indicator experiments.
21. **Web3.py — READ-ONLY EVM CLIENT.** Optional client reads the Solidity risk registry and cannot sign or send transactions.
22. **Solana-py / Solders — ACTIVE.** Python Solana RPC and Rust-backed key/transaction primitives.
23. **Asyncio — ACTIVE.** Concurrent listeners, supervisors, Telegram tasks and worker lifecycle.
24. **Aiohttp / HTTPX — ACTIVE.** Aiohttp powers providers; HTTPX remains a test/client utility, not a second execution stack.
25. **Scikit-Learn — RESEARCH IMPLEMENTED.** Random Forest regime experiment uses a chronological holdout; no model output overrides risk/approval gates.
26. **PyTorch / TensorFlow — RESEARCH ONLY.** GPU/model serving belongs in an isolated environment, not the latency-sensitive execution process.
27. **VectorBT — RESEARCH RUNNER.** A CSV-driven moving-average backtest includes fees and does not submit orders.
28. **Backtrader — RESEARCH RUNNER.** Event-driven pandas-feed strategy uses a simulated broker with explicit commission and no live connection.
29. **Aiogram / python-telegram-bot — ACTIVE CHOICE.** Aiogram owns Python-side bot logic; do not run two polling consumers for one token.
30. **FastAPI — ACTIVE.** Authenticated request and telemetry API.

## Part 3 — Data sourcing and history (31–45)

31. **yfinance — IMPLEMENTED RESEARCH CONNECTOR.** Pulls historical OHLCV only; timestamp normalization and closed-bar filtering happen before indicators.
32. **CoinMarketCap API — IMPLEMENTED METADATA CONNECTOR.** Uses the official API key header and returns source/time-stamped listings; never treats market cap as an execution quote.
33. **BeautifulSoup4 — RESEARCH INGESTION.** Parse permitted public HTML only; prefer documented APIs.
34. **Playwright / Selenium — IMPLEMENTED QA/RESEARCH HELPERS.** Playwright uses HTTPS host allow-lists; Selenium smoke test only checks page load/title. Neither bypasses access controls.
35. **Scrapy — RESEARCH INGESTION.** Bounded historical collection with source-specific crawl policy.
36. **WebSockets (WSS) — ACTIVE.** Helius logs/account subscriptions and provider event streams.
37. **REST APIs — ACTIVE.** Helius, Jupiter, exchange account and market-data calls.
38. **GraphQL — IMPLEMENTED READ-ONLY CONNECTOR.** HTTPS GraphQL query helper supports The Graph-style metadata endpoints; no write/mutation path is exposed.
39. **RPC nodes — ACTIVE.** Helius Solana RPC/WebSocket; fallback providers must preserve chain and commitment semantics.
40. **Proxy rotation / user-agent spoofing — NOT USED.** Do not evade provider controls. Use API keys, caching, concurrency bounds and documented retries.
41. **Rate-limit backoff — IMPLEMENTED.** Bounded exponential retry with jitter plus numeric/date Retry-After handling; providers retain their own rate limits.
42. **OHLCV normalization — V2 IMPLEMENTED.** Decimal bars, validation, deduplication and closed-candle filtering.
43. **UTC synchronization — V2 IMPLEMENTED.** Every normalized candle is timezone-aware UTC.
44. **Survivorship-bias-free datasets — RESEARCH REQUIREMENT.** Retain delisted/rugged assets and failed listings in historical evaluations.
45. **Sentiment NLP — RESEARCH IMPLEMENTED.** VADER helper and optional FinBERT scoring are isolated from private Telegram content and live order eligibility.

## Part 4 — Quantitative mathematics (46–65)

46. **Kelly Criterion — V2 IMPLEMENTED.** Fractional Kelly is capped; it is not permission to risk full Kelly.
47. **Sharpe Ratio — V2 IMPLEMENTED.** Annualized from supplied periodic returns; undefined for zero variance.
48. **Sortino Ratio — V2 IMPLEMENTED.** Downside-only deviation with an explicit target return.
49. **Maximum Drawdown — V2 IMPLEMENTED.** Peak-to-trough equity drawdown.
50. **Value at Risk — V2 IMPLEMENTED.** Historical one-period VaR; compare parametric and expected shortfall models in research.
51. **Monte Carlo — V2 IMPLEMENTED.** Seeded bootstrap, median and 95th percentile drawdown, ruin threshold.
52. **Slippage modeling — ACTIVE / EXTEND.** Existing CEX depth estimate and quote-impact controls; extend per venue and DEX pool.
53. **Order-book depth / imbalance — ACTIVE FOR CEX; EXTEND FOR DEX.** Never extrapolate beyond visible book depth.
54. **VWAP — V2 IMPLEMENTED.** Volume-weighted average price with zero-volume rejection.
55. **ATR — V2 IMPLEMENTED.** True range from closed OHLC bars.
56. **Bollinger Bands — V2 IMPLEMENTED.** Decimal mean and standard deviation.
57. **Cointegration — RESEARCH IMPLEMENTED.** Optional Engle-Granger test and spread half-life helper; actual pairs execution remains unavailable without borrow/short venues.
58. **Kalman filters — RESEARCH IMPLEMENTED.** A scalar filter smooths noisy price observations; no trade permission is produced.
59. **Hidden Markov Models — OPTIONAL RESEARCH IMPLEMENTED.** GaussianHMM regime labeling runs only in the isolated ML profile.
60. **Geometric Brownian Motion — RESEARCH IMPLEMENTED.** Seeded path generator is for baseline scenario analysis, not a meme-token forecast.
61. **Autocorrelation / ADF — RESEARCH IMPLEMENTED.** Sample autocorrelation and optional statsmodels ADF helpers test time-series assumptions.
62. **Walk-forward optimization — REQUIRED RESEARCH GATE.** Train/validate/test chronologically; no random-shuffle leakage.
63. **Maker/taker fee calculus — EXCHANGE-SPECIFIC.** Required for CEX order strategies; Solana DEX routes use actual route fees, impact and network costs.
64. **AMM / impermanent-loss math — ACTIVE FOUNDATION.** Existing constant-product math; extend per pool/program and validate against chain state.
65. **Position sizing / risk-of-ruin — V2 IMPLEMENTED.** Risk-per-trade cap and Monte Carlo ruin estimate; portfolio risk remains a live-supervisor gate.

## Part 5 — Economics and microstructure (66–85)

66. **Supply/demand elasticity — RESEARCH IMPLEMENTED.** Price/quantity elasticity helper is descriptive and requires sourced observations.
67. **Liquidity cycles — RESEARCH IMPLEMENTED.** DST-aware Tokyo/London/New York session labels support measured time-of-day comparisons.
68. **Bid-ask spread — ACTIVE FOR EXCHANGE BOOKS.** Spread is a cost and liquidity filter.
69. **Market-maker inventory behavior — IMPLEMENTED DIAGNOSTIC.** The inventory-pressure helper maps an account's measured inventory deviation from target into capped quote skew; it does not claim access to other makers' private inventory.
70. **Arbitrage / Law of One Price — IMPLEMENTED OPPORTUNITY FILTER.** The cross-venue calculator accounts for fresh bid/ask quotes, visible depth, taker fees, transfer/network costs and net-edge thresholds; it does not place paired orders.
71. **Funding rates — IMPLEMENTED CALCULATOR.** Funding cash flow is signed by long/short direction and rate; live funding observations still require a configured derivatives market-data provider.
72. **Open interest divergence — FEATURE CALCULATION IMPLEMENTED.** Descriptive price/OI quadrant labels exist; actual funding/OI data needs a permitted derivatives provider.
73. **CPI economic calendar — IMPLEMENTED BLACKOUT CONTRACT.** A high-impact CPI/FOMC/NFP record must be timestamped and sourced; the blackout helper blocks entries only inside the configured event window.
74. **FOMC rates — RESEARCH MACRO FEATURE.** Timestamp and source required.
75. **DXY correlation — RESEARCH MACRO FEATURE.** Correlation is regime-dependent, not a guaranteed inverse relationship.
76. **Tokenomics / vesting — ACTIVE RESEARCH REQUIREMENT.** Unlock schedules and authority state must be sourced and time-stamped.
77. **Circulating supply vs FDV — RESEARCH IMPLEMENTED.** Supply ratio and estimated FDV helper rejects inconsistent/unknown supply inputs.
78. **Metcalfe / active wallets — IMPLEMENTED DESCRIPTIVE INDEX.** Active-wallet counts produce a normalized N-squared activity index; this is not token fair value or a price target.
79. **Risk-on / risk-off regime — V2 BASELINE EXTENSION.** Start with transparent thresholds; HMM/ML remains research-only.
80. **Reflexivity — IMPLEMENTED DESCRIPTIVE SCORE.** Price return, active-wallet growth and net flow are combined into a co-directional feedback diagnostic, not a causal model or forecast.
81. **Fat tails / black swans — V2 SCENARIO GATE.** Monte Carlo and stress cases; normal-distribution VaR alone is insufficient.
82. **Herding / momentum cascades — RESEARCH.** Require deduplicated, timestamped social/flow data.
83. **Mean reversion — RESEARCH STRATEGY.** Bollinger/ADF signals never override token safety and liquidity checks.
84. **Correlation breakdown — RESEARCH IMPLEMENTED.** Pairwise portfolio correlation helper supports stress analysis; it is not a diversification guarantee.
85. **Zero-sum vs positive-sum mechanics — IMPLEMENTED ACCOUNTING CHECK.** Gross winners/losers are reconciled separately from fees and external capital flows so deposits are not counted as trading profit.

## Part 6 — Infrastructure, safety and execution (86–100)

86. **PostgreSQL / TimescaleDB — ACTIVE DATABASE / OPTIONAL MIGRATION.** Neon PostgreSQL is authoritative; an opt-in hypertable migration is provided but is not applied unless the target database has TimescaleDB installed.
87. **Redis — IMPLEMENTED OPTIONAL TTL CACHE.** RedisMarketCache only accepts market/research namespaces and rejects wallet/order/receipt/risk-ledger state; PostgreSQL stays authoritative.
88. **Docker — ACTIVE.** Reproducible worker image and local service orchestration.
89. **Linux VPS administration — ACTIVE DEPLOYMENT COMPETENCY.** Managed API/daemon infrastructure is used; region and latency are measured before placement.
90. **Environment variables / secrets management — ACTIVE.** Secrets remain server-side and never enter the public Mini App bundle.
91. **Idempotency keys — V2 IMPLEMENTED.** Stable intent hash and no duplicate submission on retries.
92. **State machine architecture — V2 IMPLEMENTED.** Unknown submission is a reconciliation state, not a retry.
93. **Dead-man switch / heartbeat — NEON HEARTBEAT + V2 LEASE CONTRACT.** The Python worker records Neon heartbeats and V2 checks lease freshness; the Supabase Telegram diagnostics panel remains unsynchronized until a secure cross-service bridge is deployed.
94. **Circuit breakers — ACTIVE FOUNDATION.** Global live lock, per-user limits and daily drawdown controls; portfolio reconciliation remains a gate.
95. **Paper / shadow mode — ACTIVE.** Isolated ledger and quote-only adapter; virtual money cannot reach signing code.
96. **Structured logging / telemetry — ACTIVE / EXTEND.** Sanitized events only; no secrets or signed transaction material in logs.
97. **Smart-contract / token safety — ACTIVE FOR SOLANA; EVM DEFERRED.** Mint/freeze, holder concentration, Token-2022 extensions and liquidity checks are independent gates.
98. **MEV protection — ACTIVE OPTIONAL JITO PATH.** Bundle acknowledgement is not confirmation; no provider guarantees complete MEV protection.
99. **Memory / leak prevention — ACTIVE REQUIREMENT.** Bounded queues, deduplication windows, session cleanup and worker restart tests.
100. **CI/CD — ACTIVE.** Python tests, Deno tests/type-check, Vite build, security audit, lockfile validation and deployment gates.

## Exclusion rule

A technology appearing in this catalogue does not mean it is installed in production. Research and future adapters remain isolated until their data source, license, runtime, permissions, security tests and deployment requirements are satisfied.


## Research connector implementation details

- Provider ingestion lives in research/data_sources.py. CCXT uses its asynchronous client rate limiter; yfinance is historical-only; CoinMarketCap is metadata-only; GraphQL is read-only; BeautifulSoup extracts supplied public JSON-LD; Playwright uses explicit HTTPS host allow-lists; Scrapy obeys robots.txt and AutoThrottle; Selenium is a UI smoke test.
- Optional profiles are requirements-research.txt (data/quant/backtest), requirements-research-ml.txt (PyTorch/TensorFlow/Transformers/HMM/FinBERT), and requirements-evm.txt (Web3.py). They are not installed in the always-on trading worker.
- V2 also includes microstructure.py (depth walking, spread, imbalance, maker/taker costs and impermanent loss), quant_research.py (Kalman, GBM, ADF, cointegration, HMM, Random Forest, walk-forward and FinBERT interfaces), and macro_features.py (elasticity, funding/OI descriptions, time-zone-aware sessions, supply/FDV and correlations).
- Research/browser sources must obey provider terms, robots.txt and rate limits. Proxy rotation or user-agent spoofing to bypass controls is deliberately excluded.
