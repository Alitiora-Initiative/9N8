# 9N8 security model

## Personal-only access

- 9N8 is designed for one owner and one personal OKX account.
- All Telegram commands except `/id` require a private chat and the configured `TELEGRAM_OWNER_ID`.
- Other Telegram users are denied. The project does not onboard other people's exchange accounts.
- The bot does not request or store exchange credentials in Telegram chat.

## OKX API boundaries

Implemented OKX operations:
- Public spot market ticker, instruments and candles.
- Private account configuration and balance reads.
- Private open spot orders.
- Spot cash market/limit order placement, only after an owner confirmation button.
- Spot order cancellation, only after an owner confirmation button.

Not implemented:
- Withdrawals, internal asset transfers, deposits, wallets, sub-account management.
- Futures, swaps, margin, borrowing, leverage, options or copy trading.
- Unattended autonomous order submission.
- Exchange-side attached stop-loss/take-profit algos.

Use Read and Trade permissions only if you need trading. Keep **Withdraw** disabled; the bot checks the returned permission list and refuses to stage new orders if Withdraw is present. OKX documents that Trade permission can include funding transfers as well as orders/cancellations. This application contains no transfer endpoints, but the API key itself may have capabilities beyond this app. Use a dedicated key and bind it to your IP where possible. OKX notes that unbound Trade/Withdraw keys can expire after 14 days of inactivity; check the current policy for your account.

## Demo-first and execution confirmation

- `OKX_TRADING_MODE=demo` is the default.
- Demo requests add `x-simulated-trading: 1`.
- Live order submission is blocked unless both `OKX_TRADING_MODE=live` and `OKX_LIVE_TRADING_ENABLED=true` are configured locally.
- Every order requires an owner confirmation button, expires after 90 seconds, and includes the configured max notional cap.
- Signals never submit orders.
- Limit/market order acceptance is not a fill guarantee.

## Secret handling

- Store `TELEGRAM_BOT_TOKEN`, `OKX_API_KEY`, `OKX_API_SECRET` and `OKX_API_PASSPHRASE` only in a local, untracked `.env` file.
- Never commit `.env`, paste credentials into Telegram, GitHub Issues, chat, logs or screenshots.
- The OKX REST signature is computed in memory; credentials are never included in request URLs or user-facing messages.
- If credentials are exposed, revoke the OKX API key in the OKX security/API page and create a replacement.

## Deployment

The zero-monthly-cost path is local Windows polling. The laptop and internet connection must remain available. A remote always-on server would introduce hosting and secret-management requirements; do not configure one without reviewing its cost and access controls.
