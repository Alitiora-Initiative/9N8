# 9N8 — 100-technology implementation catalogue

This catalogue accounts for every item in the requested specification. ACTIVE means used by the application or V2 foundations; RESEARCH means isolated from the live worker; RESERVED means an extension point exists but that platform is not deployed; NOT USED means adding the item would not improve the current Solana-only runtime.

## Part 1 — Languages and runtimes (1–15)

1. **Python — ACTIVE.** Primary asyncio worker, strategy coordinator, risk and Telegram business logic.
2. **C++ — RESERVED.** Consider only after profiling a genuine CPU-bound hot path; no duplicate executor is added for appearance.
3. **Rust — ACTIVE through native bindings.** Solders provides Rust-backed Solana primitives. A standalone Rust service is reserved for measured bottlenecks.
4. **JavaScript / Node.js — ACTIVE in tooling.** Browser Mini App and Node-based Vite/npm build; Node is not the long-running trading supervisor.
5. **TypeScript — ACTIVE.** Deno Telegram webhook, Jupiter and wallet API boundaries.
6. **SQL — ACTIVE.** PostgreSQL/Neon authoritative state and migrations.
7. **Solidity — RESERVED.** Only for a separately audited EVM token/contract adapter; current execution is Solana-only.
8. **Go — RESERVED.** Possible independent market-data fanout after measured concurrency requirements.
9. **C# — RESERVED.** Only if a future institutional terminal/broker integration requires it.
10. **Bash / Shell — ACTIVE.** CI, Docker entrypoints and deployment operations.
11. **Assembly — NOT A SOURCE-LANGUAGE REQUIREMENT.** Compilers generate machine code; raw assembly is unnecessary.
12. **Pine Script — RESEARCH.** TradingView visualization and external indicator validation, not transaction execution.
13. **MQL4 / MQL5 — RESERVED.** Only for a separately scoped MetaTrader Forex/CFD adapter.
14. **R — RESEARCH.** Statistical validation and reproducible research, not the always-on order process.
15. **Julia — RESEARCH.** Optional numerical experiments after a benchmark justifies it.

## Part 2 — Python libraries and AI frameworks (16–30)

16. **CCXT — RESEARCH ADAPTER.** Unified exchange market-data/backtest connector; live orders remain in separately permission-tested exchange adapters.
17. **Pandas — RESEARCH.** DataFrame preparation and historical dataset inspection in the research profile.
18. **NumPy — ACTIVE.** Numerical primitives; financial money and lamports remain Decimal/integer.
19. **TA-Lib — RESEARCH.** Optional native C technical indicators, not a required runtime install.
20. **Pandas-TA — RESEARCH.** pandas-ta-classic is isolated for indicator experiments.
21. **Web3.py — RESERVED.** EVM-only; never imported by the Solana executor.
22. **Solana-py / Solders — ACTIVE.** Python Solana RPC and Rust-backed key/transaction primitives.
23. **Asyncio — ACTIVE.** Concurrent listeners, supervisors, Telegram tasks and worker lifecycle.
24. **Aiohttp / HTTPX — ACTIVE.** Aiohttp powers providers; HTTPX remains a test/client utility, not a second execution stack.
25. **Scikit-Learn — RESEARCH.** Model selection and regime experiments; models cannot bypass safety gates.
26. **PyTorch / TensorFlow — RESEARCH ONLY.** GPU/model serving belongs in an isolated environment, not the latency-sensitive execution process.
27. **VectorBT — RESEARCH.** Vectorized backtests in an isolated environment.
28. **Backtrader — RESEARCH.** Event-driven fee/slippage and portfolio simulation; isolated from mainnet adapters.
29. **Aiogram / python-telegram-bot — ACTIVE CHOICE.** Aiogram owns Python-side bot logic; do not run two polling consumers for one token.
30. **FastAPI — ACTIVE.** Authenticated request and telemetry API.

## Part 3 — Data sourcing and history (31–45)

31. **yfinance — RESEARCH.** Historical equities/macro data, not live Solana execution prices.
32. **CoinMarketCap API — OPTIONAL METADATA.** Store source/time; never treat market-cap estimates as executable prices.
33. **BeautifulSoup4 — RESEARCH INGESTION.** Parse permitted public HTML only; prefer documented APIs.
34. **Playwright / Selenium — QA/RESEARCH.** Browser-rendered collection and UI regression, not hidden trading dependencies.
35. **Scrapy — RESEARCH INGESTION.** Bounded historical collection with source-specific crawl policy.
36. **WebSockets (WSS) — ACTIVE.** Helius logs/account subscriptions and provider event streams.
37. **REST APIs — ACTIVE.** Helius, Jupiter, exchange account and market-data calls.
38. **GraphQL — RESERVED.** The Graph / Uniswap data only after an EVM adapter is deployed.
39. **RPC nodes — ACTIVE.** Helius Solana RPC/WebSocket; fallback providers must preserve chain and commitment semantics.
40. **Proxy rotation / user-agent spoofing — NOT USED.** Do not evade provider controls. Use API keys, caching, concurrency bounds and documented retries.
41. **Rate-limit backoff — ACTIVE REQUIREMENT.** Bounded exponential backoff with jitter and per-provider request budgets.
42. **OHLCV normalization — V2 IMPLEMENTED.** Decimal bars, validation, deduplication and closed-candle filtering.
43. **UTC synchronization — V2 IMPLEMENTED.** Every normalized candle is timezone-aware UTC.
44. **Survivorship-bias-free datasets — RESEARCH REQUIREMENT.** Retain delisted/rugged assets and failed listings in historical evaluations.
45. **Sentiment NLP — RESEARCH.** VADER first; FinBERT only after licensed, timestamped news/social data exists.

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
57. **Cointegration / pairs trading — RESEARCH.** Requires multi-asset history and shorting/hedging venues; not a token-launch buy rule.
58. **Kalman filters — RESEARCH.** Noise filtering and state estimation evaluated out of sample.
59. **Hidden Markov Models — RESEARCH.** Regime experiments; no live model promotion without walk-forward validation.
60. **Geometric Brownian Motion — RESEARCH SCENARIO.** Baseline scenario only, not a claim that meme-token returns are normally distributed.
61. **Autocorrelation / ADF stationarity — RESEARCH.** Test assumptions before mean-reversion strategies.
62. **Walk-forward optimization — REQUIRED RESEARCH GATE.** Train/validate/test chronologically; no random-shuffle leakage.
63. **Maker/taker fee calculus — EXCHANGE-SPECIFIC.** Required for CEX order strategies; Solana DEX routes use actual route fees, impact and network costs.
64. **AMM / impermanent-loss math — ACTIVE FOUNDATION.** Existing constant-product math; extend per pool/program and validate against chain state.
65. **Position sizing / risk-of-ruin — V2 IMPLEMENTED.** Risk-per-trade cap and Monte Carlo ruin estimate; portfolio risk remains a live-supervisor gate.

## Part 5 — Economics and microstructure (66–85)

66. **Supply/demand elasticity — RESEARCH.** Liquidity-impact and depth scenarios.
67. **Liquidity cycles — RESEARCH.** Session/time-of-day performance by venue, not a hard-coded clock assumption.
68. **Bid-ask spread — ACTIVE FOR EXCHANGE BOOKS.** Spread is a cost and liquidity filter.
69. **Market-maker inventory behavior — RESEARCH.** Do not present as observable fact without order-flow evidence.
70. **Arbitrage / Law of One Price — RESERVED.** Requires synchronized venues, transfer constraints and net-cost accounting.
71. **Funding rates — RESERVED.** Perpetual futures only after a dedicated derivatives adapter is reviewed.
72. **Open interest divergence — RESERVED.** Same derivatives scope; source and timestamps required.
73. **CPI economic calendar — RESERVED MACRO FILTER.** Never scrape an unverified calendar and silently pause trades.
74. **FOMC rates — RESEARCH MACRO FEATURE.** Timestamp and source required.
75. **DXY correlation — RESEARCH MACRO FEATURE.** Correlation is regime-dependent, not a guaranteed inverse relationship.
76. **Tokenomics / vesting — ACTIVE RESEARCH REQUIREMENT.** Unlock schedules and authority state must be sourced and time-stamped.
77. **Circulating supply vs FDV — ACTIVE RESEARCH REQUIREMENT.** Unknown supply fields fail closed for valuation rules.
78. **Metcalfe / active wallets — RESEARCH.** On-chain activity is context, not direct value prediction.
79. **Risk-on / risk-off regime — V2 BASELINE EXTENSION.** Start with transparent thresholds; HMM/ML remains research-only.
80. **Reflexivity — RESEARCH.** Convert into measurable feedback variables before it changes strategy weights.
81. **Fat tails / black swans — V2 SCENARIO GATE.** Monte Carlo and stress cases; normal-distribution VaR alone is insufficient.
82. **Herding / momentum cascades — RESEARCH.** Require deduplicated, timestamped social/flow data.
83. **Mean reversion — RESEARCH STRATEGY.** Bollinger/ADF signals never override token safety and liquidity checks.
84. **Correlation breakdown — PORTFOLIO RESEARCH.** Stress correlations rising during market-wide shocks.
85. **Zero-sum vs positive-sum mechanics — STRATEGY NOTES.** Evaluate short-horizon performance net of fees, slippage and competition.

## Part 6 — Infrastructure, safety and execution (86–100)

86. **PostgreSQL / TimescaleDB — ACTIVE DATABASE / OPTIONAL EXTENSION.** Neon PostgreSQL is authoritative; TimescaleDB is not assumed enabled until verified.
87. **Redis — OPTIONAL CACHE.** Never replace PostgreSQL as the trade/receipt ledger.
88. **Docker — ACTIVE.** Reproducible worker image and local service orchestration.
89. **Linux VPS administration — ACTIVE DEPLOYMENT COMPETENCY.** Managed API/daemon infrastructure is used; region and latency are measured before placement.
90. **Environment variables / secrets management — ACTIVE.** Secrets remain server-side and never enter the public Mini App bundle.
91. **Idempotency keys — V2 IMPLEMENTED.** Stable intent hash and no duplicate submission on retries.
92. **State machine architecture — V2 IMPLEMENTED.** Unknown submission is a reconciliation state, not a retry.
93. **Dead-man switch / heartbeat — V2 TELEMETRY CONTRACT.** Production synchronization remains pending; the dashboard must show a real worker heartbeat before claiming health.
94. **Circuit breakers — ACTIVE FOUNDATION.** Global live lock, per-user limits and daily drawdown controls; portfolio reconciliation remains a gate.
95. **Paper / shadow mode — ACTIVE.** Isolated ledger and quote-only adapter; virtual money cannot reach signing code.
96. **Structured logging / telemetry — ACTIVE / EXTEND.** Sanitized events only; no secrets or signed transaction material in logs.
97. **Smart-contract / token safety — ACTIVE FOR SOLANA; EVM DEFERRED.** Mint/freeze, holder concentration, Token-2022 extensions and liquidity checks are independent gates.
98. **MEV protection — ACTIVE OPTIONAL JITO PATH.** Bundle acknowledgement is not confirmation; no provider guarantees complete MEV protection.
99. **Memory / leak prevention — ACTIVE REQUIREMENT.** Bounded queues, deduplication windows, session cleanup and worker restart tests.
100. **CI/CD — ACTIVE.** Python tests, Deno tests/type-check, Vite build, security audit, lockfile validation and deployment gates.

## Exclusion rule

A technology appearing in this catalogue does not mean it is installed in production. Research and future adapters remain isolated until their data source, license, runtime, permissions, security tests and deployment requirements are satisfied.
