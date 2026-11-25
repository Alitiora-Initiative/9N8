# 9N8 — Private OKX Spot Trading Bot

9N8 is a **personal, owner-only Telegram bot** for one Telegram account and one OKX account. It is not a public exchange-API service and does not collect API keys from other users.

The bot runs on your Windows laptop at no monthly hosting cost. The laptop must remain on and connected to the internet while you use it. A paid always-on server can be considered later, but it is not required for the local setup.

## Current scope

- OKX v5 REST integration using your own API key, secret and passphrase.
- Spot cash trading only: market and limit orders.
- Demo trading by default, using OKX's simulated-trading request header.
- Live trading requires both `OKX_TRADING_MODE=live` and `OKX_LIVE_TRADING_ENABLED=true` in your local `.env`.
- Every new order is previewed in Telegram and requires pressing a **Confirm order** button before an API order request is sent.
- Every order is capped by `OKX_MAX_ORDER_NOTIONAL` (default 10 quote-currency units).
- Open-order viewing and cancellation, account status and balance reads.
- OKX spot ticker/candles and EMA(9)/EMA(21)/RSI(14) observations.
- Telegram owner lock: only the numeric `TELEGRAM_OWNER_ID` can use the bot, and only in a private chat.

## Explicit exclusions

- No MT5, MetaApi or MetaTrader terminal.
- No Cloudflare Worker or D1 database.
- No other users' exchange accounts or API credential collection.
- No withdrawals, internal transfers, wallet operations or private-key handling.
- No futures, perpetual swaps, margin, borrowing or leverage.
- No unattended autonomous trade execution. Signals never place orders.
- The bot does not attach stop-loss/take-profit algos to exchange orders yet. The order preview warns you that no exchange-side stop is attached.

A confirmed order can be accepted but remain unfilled, partially filled, or be rejected by OKX. Always check `/openorders` and your OKX account. Market orders can execute at a different price from the last displayed quote.

## Setup on Windows (local, no hosting bill)

### 1. Install Python

Install Python 3.11 or newer from https://www.python.org/downloads/windows/. During setup, enable **Add Python to PATH**.

### 2. Download your repository

Download the `exchange-api` branch from GitHub as a ZIP, extract it, then open the extracted `9N8` folder.

Repository branch:
https://github.com/Alitiora-Initiative/9N8/tree/exchange-api

### 3. Install dependencies

Double-click `setup_windows.bat`, or open PowerShell in the repository folder and run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
```

### 4. Create your private environment file

Copy `.env.example` to `.env` in the repository root. Open `.env` in Notepad and enter your own values:

```env
TELEGRAM_BOT_TOKEN=your_botfather_token
TELEGRAM_OWNER_ID=your_numeric_telegram_user_id

OKX_API_KEY=
OKX_API_SECRET=
OKX_API_PASSPHRASE=

OKX_API_BASE=https://www.okx.com
OKX_TRADING_MODE=demo
OKX_LIVE_TRADING_ENABLED=false
OKX_MAX_ORDER_NOTIONAL=10
```

Get your Telegram user ID by starting the bot and sending `/id`. Before the owner lock is set, all commands except `/id` are blocked. Put the returned numeric ID in `TELEGRAM_OWNER_ID`, save `.env`, and restart the bot.

**Never commit or send the real `.env` file.** The repository's `.gitignore` excludes it. Never paste exchange credentials into Telegram, GitHub Issues, code or this chat.

### 5. Create an OKX DEMO API key

In your OKX account, open Demo Trading, then the Demo Trading API page and create a demo API key. Store the API key, secret and passphrase in your local `.env`.

For the first test, use a DEMO API key and keep `OKX_TRADING_MODE=demo`. OKX demo requests require `x-simulated-trading: 1`, which this bot adds automatically in demo mode.

### 6. Secure the API key

- Give the key **Read** and **Trade** permission only if you need order placement.
- Keep **Withdraw** permission OFF.
- Bind the key to your IP address if you have a stable IP.
- Never put the key in a Telegram message or a GitHub secret for this local setup.
- Use a separate API key for this bot and revoke it from OKX if the laptop or local `.env` file is compromised.

OKX notes that API keys have three permissions (Read, Trade and Withdraw), recommends IP binding, and documents a 14-day inactivity expiry for trade/withdraw keys that are not IP-bound. Review OKX's current rules for your account region.

### 7. Start 9N8

In PowerShell, from the repository folder:

```powershell
.\.venv\Scripts\Activate.ps1
py -m src.bot
```

Or double-click `start_9n8.bat` after completing setup.

The bot is reachable from Telegram while the laptop and bot process are running. Closing the terminal or sleeping/shutting down the laptop stops the bot.

## Telegram commands

```text
/id
/start
/help
/okx_status
/balance
/ticker BTC-USDT
/scan BTC-USDT 15m
/order BTC-USDT buy limit 0.001 90000
/order BTC-USDT buy market 10
/order BTC-USDT sell market 0.001
/openorders
/cancel BTC-USDT ORDER_ID
```

Order size rules:
- **Limit buy/sell:** size is in base currency (for BTC-USDT, BTC); include limit price.
- **Market buy:** size is quote-currency spend (for BTC-USDT, USDT).
- **Market sell:** size is base-currency quantity (for BTC-USDT, BTC).

Before submitting, 9N8 checks the spot instrument, market state, lot/tick precision, minimum size and your configured notional cap. It then displays a confirmation button. No order is sent until you press it.

## Switching from demo to live

Do not enable live mode until you have checked demo orders, balances, order states, cancellation and your own risk limits.

To deliberately enable live order submissions, edit your local `.env`:

```env
OKX_TRADING_MODE=live
OKX_LIVE_TRADING_ENABLED=true
```

Restart the bot after changing the file. The bot will then show **LIVE — real funds** in each order preview, and each live order still requires a separate Telegram confirmation. The `OKX_MAX_ORDER_NOTIONAL` cap remains active. To lock live orders again, return to `OKX_TRADING_MODE=demo` and `OKX_LIVE_TRADING_ENABLED=false`.

## OKX account region

The default API domain is `https://www.okx.com`. OKX documents different REST domains for some regional accounts, including `https://eea.okx.com` for EEA accounts and `https://us.okx.com` for US/Australia accounts. Use the domain applicable to the account you registered; set it in `OKX_API_BASE`.

## Tests

```powershell
py -m pip install -r requirements.txt pytest
py -m pytest -q
```

The tests mock the exchange HTTP responses. They do not use your API key, access your funds or place exchange orders.
