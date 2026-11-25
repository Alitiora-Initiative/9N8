# 9N8 Python Multi-Asset Engine — Development Guide

This document applies to the **multi-asset-python feature branch**. It is a separate Python application from the existing Supabase/Telegram paper pilot. It has not been merged into main, deployed to a trading server, or certified for real-money execution.

## Directory tree

~~~text
9N8/
├── config.json
├── .env.example
├── .gitignore
├── requirements.txt
├── main.py
├── config.py
├── models.py
├── storage.py
├── logging_setup.py
├── connectors/
│   ├── __init__.py
│   ├── base.py
│   ├── retry.py
│   ├── ccxt_connector.py
│   ├── oanda.py
│   ├── paper_broker.py
│   ├── solana_jupiter.py
│   └── evm_0x.py
├── strategies/
│   ├── __init__.py
│   ├── indicators.py
│   ├── multi_timeframe.py
│   └── memecoin_momentum.py
├── risk_manager/
│   ├── __init__.py
│   └── risk_manager.py
├── security/
│   ├── __init__.py
│   └── dex_gate.py
├── core/
│   ├── __init__.py
│   ├── engine.py
│   └── telegram_controller.py
├── tests/
│   ├── __init__.py
│   ├── test_indicators.py
│   ├── test_paper_broker.py
│   ├── test_approval_store.py
│   ├── test_retry.py
│   ├── test_telegram_controller.py
│   ├── test_engine_safety.py
│   ├── test_engine_worker_isolation.py
│   └── test_risk_manager.py
└── .github/
    └── workflows/
        └── python-trading.yml
~~~

## Current implementation scope

| Area | Current implementation | Safety state |
|---|---|---|
| Crypto spot | CCXT Pro market data, order-book checks, market execution with attached SL/TP only when the exchange advertises support | Paper and exchange sandbox |
| Crypto perpetuals | CCXT unified swap market, isolated margin request, leverage ceiling, contract-size-aware sizing | Disabled by default; configure testnet only |
| Forex and gold | OANDA v20 REST pricing/candles/account/order APIs; stopLossOnFill and takeProfitOnFill | Practice by default |
| Solana DEX | Solana RPC, Jupiter Price/Swap V2 connector, Birdeye OHLCV and GoPlus token-risk adapter | Data/integration path only; trading blocked |
| EVM DEX | Web3, 0x Swap API v2, DexScreener and GoPlus adapter | Data/integration path only; trading blocked |
| Strategy | H1 EMA 50/200 regime, M5 EMA 9/21 crossover, RSI, MACD histogram cross, ADX and ATR | Active for configured CEX/OANDA feeds |
| Paper execution | Actual public/provider bid/ask, configurable adverse slippage and fees, atomic SQLite fill/cash/position ledger, entry-fee allocation, TP1, ATR trailing, hard stop and stagnation time stop | Available |
| Telegram | /start, /menu, /status, /paper, /pending, /positions, /history, /pause, /resume and confirmed /panic; private numeric allowlist; approval-card recovery and polling reconnect | Available when configured |

MT5 is not included; the chosen FX connector is OANDA v20. OANDA uses REST polling in this build, not its streaming endpoint. Configure any additional OANDA instrument names only after confirming that your account exposes them.



## Provider timeouts and retry policy

Read-only exchange/price/RPC/security calls use a bounded three-attempt exponential backoff and a 10-second outer timeout. Order submissions and transaction broadcasts are never automatically retried: a timeout can mean the provider accepted the order. CEX/OANDA/0x transaction broadcasts are bounded to 5 seconds at the submission call; Solana's Jupiter execute HTTP request is bounded to 5 seconds. A timeout after a live/testnet submission starts triggers the persistent reconciliation lock and requires manual inspection before the bot can resume.

## Approval and operator controls

Every scanner-generated entry is a stored proposal, not an order. The owner receives an inline Approve Trade / Deny keyboard in the Telegram private chat. An approval expires after 120 seconds by default; when pressed, the bot fetches a fresh quote, rejects excessive price drift (0.50% by default), rechecks position count, account exposure, spread and order-book slippage, then submits only in the configured execution mode. Denied, expired and duplicate signal proposals cannot be submitted.

If a live/testnet provider submission starts but the response cannot be reconciled, 9N8 marks the approval RECONCILIATION_REQUIRED, pauses entries, sets the kill switch and sends an urgent alert. It never automatically retries an uncertain order. Check the provider's order/fill/transaction history and balances manually before taking further action.

Telegram is restricted to numeric IDs in TELEGRAM_ALLOWED_USER_IDS and private chats. /start and /menu open the button dashboard; /pending, /positions, /history, /pause, /resume and /status are also available. /panic asks for a second confirmation before the emergency flatten routine.

## Paper trading controls and lifecycle

The Python paper engine uses live public exchange quotes/candles but virtual cash and simulated fills. The default starting equity is $10,000, the simulated fee is 0.10%, and adverse slippage is 5 basis points. It does not submit CEX, DEX, FX or withdrawal orders while `TRADING_MODE=paper` is active.

New strategy signals are still approval-based: an inline Telegram approval is required before even a paper entry is opened. Each proposal is stored in SQLite, expires after 120 seconds, and is re-priced/re-sized on approval. A rejected/expired proposal cannot execute. If Telegram delivery fails, the proposal remains stored and `/start` or `/pending` resends unexpired approval cards.

Paper fills, cash-balance changes and position changes are saved in one SQLite transaction. A database write failure reloads the in-memory paper broker from the last durable snapshot and pauses new entries. Startup recovery reconciles interrupted paper approvals from the stored virtual position/trade ledger; uncertain external provider submissions instead force `RECONCILIATION_REQUIRED`.

Paper exits are simulated only: hard stop and take-profit thresholds, a 50% TP1 at 1.5R, break-even protection, ATR trailing on the remainder, and a configurable time-based stagnation exit. Entry fees are allocated proportionally on partial exits and included in net realized PnL. The position watcher isolates failures per position so one bad/stale quote does not stop checks for other open positions. Repeated worker alerts are throttled.

A paper `/panic` only closes simulated positions. It never calls the exchange's `cancel_all_orders`. The engine and Telegram controller remain online after panic, while the kill switch keeps new entries blocked.

## Telegram setup and troubleshooting

1. Create or select the bot with `@BotFather` and put its token in `.env` as `TELEGRAM_BOT_TOKEN`. Do not post the token in a chat, issue, log or repository.
2. Get the numeric owner ID from a Telegram user-info bot and put it in `.env` as `TELEGRAM_ALLOWED_USER_IDS`. Multiple authorized owners may be comma-separated, for example `123456789,987654321`; names/usernames are not accepted.
3. Start 9N8 with `python main.py`, then open the bot's **private chat** in Telegram and send `/start`. Telegram bots cannot initiate a private conversation before the user starts it.
4. The dashboard contains Status, Paper account, Positions, Pending approvals, Pause, Resume and Emergency flatten. Typed commands are also available:
   - `/start` or `/menu` — dashboard and resend unexpired approvals
   - `/status` — engine mode, account/position summary and risk state
   - `/paper` — virtual account, total PnL, fees, slippage and daily drawdown
   - `/pending` — resend unexpired Approve/Deny cards
   - `/positions` and `/history` — open positions and recent trades
   - `/pause` and `/resume` — stop or resume new proposals (risk locks cannot be bypassed)
   - `/panic` — show a second confirmation before emergency flatten
5. An empty allowlist disables the Telegram controller. Invalid bot tokens disable it safely. Network/server errors retry with backoff. Unavailable market providers do not prevent Telegram from starting; provider connections retry while the control plane remains online.

**One Telegram token can have only one update receiver at a time.** This Python controller uses long polling. Do not run it using a token that is simultaneously attached to the existing Supabase/Edge Function webhook or another long-polling process. The Python controller checks for an active webhook and refuses to delete it. Stop the other receiver or use a separate bot token, then restart 9N8.

If an approval card is not visible, first send `/start`, then `/pending`. If the bot reports a webhook/polling conflict, resolve the duplicate update receiver before expecting commands or approval buttons to work. If market feeds are down, `/status` and `/paper` remain available while provider retries continue.

## Hard-coded risk vetoes

The risk manager has veto authority over strategy signals. It enforces a maximum 2% of account equity at risk on one trade, a 5% daily equity drawdown stop, a maximum 3x leverage, a maximum 25% portfolio exposure, a maximum 10-position absolute ceiling (with a lower default of 3), CEX slippage capped at 0.5%, DEX slippage capped at 5%, and forex spreads capped at 2 pips. The JSON configuration may make the limits stricter; it cannot raise the hard ceilings.

The risk sizing uses the stop distance and the venue's contract multiplier where available. CEX entries walk the order book to estimate market impact for the proposed quantity before submitting an order. The engine records fills, entry/exit times, commissions, gas fee fields, slippage and realized PnL in SQLite; the event log rotates at 5 MB with five backups.

### Important limits that remain

- **DEX execution is intentionally blocked by the trading engine.** The token-risk module rejects unknown mint/freeze authority, holder, liquidity-lock and tax data. However, a provider report is not proof that a real future sell will succeed. The required independent buy-and-sell transaction simulations, pool-specific checks, reliable chain-position reconciliation and live emergency-exit verification are not complete end-to-end.
- **Jito bundle / MEV protection is not integrated.** Jupiter Swap V2 is used by the standalone connector code, but live Solana DEX orders are not enabled. A Jito bundle is not a guarantee of landing or protection from every sandwich.
- **EVM DEX pool OHLCV and durable token-position reconciliation are not implemented.** The EVM connector refuses candle requests rather than inventing price history.
- **Live partial take-profit and ATR trailing-stop amendment are not enabled.** The paper engine simulates 50% TP1 and ATR trailing. Live CEX/OANDA orders rely on the native initial SL/TP submitted with the entry; the engine does not pretend that changing a local SQLite stop amended an exchange-side order.
- **Exchange bracket support varies.** CCXT live entry is refused unless the selected exchange/symbol advertises attached stop-loss and take-profit support. This still needs venue-specific testnet verification.
- **CEX position reconciliation after an exchange-side bracket triggers needs more venue-specific work**, especially for spot inventory. Do not switch the live certification flag on until this has been independently verified.
- No 48-hour testnet soak has been completed by this branch yet. Passing unit tests is not a trading-performance or execution certification.

For those reasons, execution.live_execution_certified is false in config.json, and the engine refuses to start in live mode while it remains false.

## Install on Windows

Open PowerShell in the downloaded repository folder:

~~~powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
~~~

Open .env in Notepad. Keep the trading mode as paper first. Never commit .env, paste credentials into Telegram, or add private keys/API tokens to GitHub.

## First run — paper mode

The defaults are:
- TRADING_MODE=paper
- LIVE_TRADING=false
- Bybit public spot market data for BTC/USDT, ETH/USDT and SOL/USDT
- $10,000 virtual starting equity
- 0.10% simulated fee and 5 bps adverse slippage

No CEX API key is needed for public market data and local paper fills.

~~~powershell
python -m compileall -q main.py config.py models.py storage.py connectors strategies risk_manager security core tests
pytest -q
python main.py
~~~

If Telegram is enabled, set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS to a comma-separated list of numeric owner IDs. Only those IDs can use /status, /pause and /panic. An empty allowlist disables the Telegram controller.

- /status — balances and locally tracked positions
- /pause — block new entries while position monitoring continues
- /panic — cancel known pending orders and try to close tracked positions; if any close fails, the bot stays paused and reports the failure. Always inspect the exchange/broker account directly.

## 48-hour testnet / practice soak

Do not put real funds or main-wallet keys into the test.

1. Create a **dedicated Bybit testnet** API key with only the required trading/read scopes. Never enable withdrawals or transfers. Set TRADING_MODE=testnet, LIVE_TRADING=false, CEX_API_KEY and CEX_API_SECRET in .env.
2. Leave config.json crypto_spot.enabled=true. The connector sets the exchange sandbox mode when the run mode is testnet.
3. For perpetual testing only, set crypto_perpetuals.enabled=true, execution.allow_perpetuals=true, margin_mode="isolated" and leverage=1. Use a testnet key/account. Do not enable leverage greater than 3x.
4. For FX/gold practice testing, set markets.oanda.enabled=true, add the exact OANDA practice instruments your account supports, set OANDA_ENVIRONMENT=practice, OANDA_ACCOUNT_ID and OANDA_API_TOKEN.
5. Keep Solana on Devnet for RPC/wallet-data checks only. Jupiter's Swap API route used by this connector is a mainnet liquidity/execution path; this project does not claim that Jupiter swaps execute on Devnet.
6. Run continuously for 48 hours with small sandbox/demo balances. Record WebSocket reconnects, API rate-limit errors, order rejections, bracket attachment results, order/fill reconciliation, stop/target trigger behavior, SQLite state after restarts, daily PnL and Telegram delivery.
7. Test /pause, /status and /panic on the sandbox account. A panic test should end with no open orders or positions. Check the provider dashboard; do not rely only on a Telegram success message.

## Live mode stays locked

The settings loader requires all of:
- TRADING_MODE=live
- LIVE_TRADING=true
- ALLOW_LIVE_TRADING=true
- LIVE_ACKNOWLEDGEMENT=I_UNDERSTAND_REAL_FUNDS_CAN_BE_LOST

The engine additionally requires execution.live_execution_certified=true. That certification flag remains false until the 48-hour testnet soak, bracket-order reconciliation and connector/security reviews have actually passed. Flipping environment variables alone cannot enable live execution in this branch.

Use a **new burner wallet** only if a future DEX release is separately approved. Never paste a seed phrase or private key into a chat, issue, repository, or log. CEX keys should be dedicated, IP-restricted and have withdrawals/transfers disabled.
