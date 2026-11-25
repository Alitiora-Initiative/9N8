# 9N8 — Multi-user Telegram MT5 paper-trading platform

9N8 is a Telegram-controlled, cross-device MT5 market scanner and paper-trading platform. Users can access it from Android, iPhone, tablets, desktop Telegram or Telegram Web. The bot and paper engine run as an always-on cloud worker, so a user's phone or laptop does not need to stay online.

## Safety and current scope

- **Paper trading only.** No MT5 trade-submission endpoint or order-send method is used.
- **No withdrawals or transfers.** No wallet, sweep, private-key or fund-transfer functionality is present.
- **MT5 account verification.** The MetaApi connector rejects live accounts and unknown account types. Use an MT5 DEMO account and preferably its investor/read-only password.
- **Individual paper accounts.** Each Telegram user has their own isolated paper positions, history and settings.
- **Per-user risk caps.** Personal lot and position limits cannot exceed platform hard limits.
- **Break-even manager.** A position can move its simulated stop to entry (or entry plus/minus an offset) after a configurable favorable move. This is a local simulation; the MT5 account is not modified.
- **Community reporting is opt-in.** Weekly group/channel P&L reports include only users who explicitly enable `/share_stats on`; individual user identities and per-user trades are not included.

Paper P&L is an estimate in each instrument's profit currency and may exclude fees, swaps, execution gaps and currency conversion. A simulated break-even stop can still close at a different price after a quote gap. Nothing in 9N8 guarantees profitability or reduces the possibility of loss in future live trading.

## Architecture

```text
Phone / tablet / laptop
        | Telegram private chat
        v
Telegram Bot API
        |
        v
9N8 always-on cloud worker ---------------- persistent SQLite volume
        |                                      |-- user accounts/settings
        |                                      |-- isolated paper positions
        |                                      |-- report destinations/schedule
        |
        +-- MetaApi cloud REST (MT5 DEMO quotes, specifications, candles)
        +-- Twelve Data (optional separate scanner)
        +-- Telegram groups/channels (weekly aggregate reports)
```

MT5 market data is obtained via MetaApi's cloud REST API, so a local MT5 terminal is not required for the normal cloud setup. MetaApi is a separate service and may charge for its account/API service. 9N8 does not create or fund a MetaApi account.

## Multi-user onboarding

1. A user opens the bot in a private Telegram chat and sends `/start`.
2. 9N8 creates a personal paper account with paper entries paused by default.
3. The user reviews `/settings`, changes parameters and uses `/paper_enable` to begin paper testing.
4. The user can turn trade notifications on/off and separately opt into or out of anonymous group performance aggregation.

Telegram bots can send private messages only after a user has opened the bot and started a chat. A user can disable those messages with `/notifications off`.

## Personal Telegram commands

```text
/start
/help
/settings
/status
/id

/setlots 0.05
/setmaxpositions 3
/setsl 500
/settp 1000
/setbe 150 0
/setbe off
/notifications on|off
/share_stats on|off

/mt5_status
/mt5_symbols [FILTER]
/mt5_price XAUUSD
/mt5_scan [SYMBOL]
/markets
/price XAU/USD
/scan [SYMBOL]
/signals

/paper_enable
/paper_pause
/paper_open XAUUSD BUY 0.01 [SL_POINTS TP_POINTS]
/paper_positions
/paper_close TICKET
/paper_check
/paper_history
/paper_balance
/stats
```

`/paper_open` uses that user's saved default SL/TP values when point distances are omitted. The current maximum volume and simultaneous-position limits are enforced per user and also bounded by the platform hard caps.

### Break-even behavior

- `/setbe 150 0` moves the paper stop to entry after 150 favorable broker points.
- `/setbe 150 2` moves it to entry plus 2 points for BUY positions or entry minus 2 points for SELL positions.
- `/setbe off` disables break-even movement for new positions; existing positions retain the settings saved when they were opened.
- The background monitor checks fresh MT5 bid/ask prices every 10 seconds and privately notifies the user when break-even is activated or a simulated position exits.
- A broker point is not universally equal to a pip. Check the symbol's specification and broker digits.

## Group and channel reporting

The platform owner is `TELEGRAM_OWNER_ID`. Configure that ID in the cloud worker's secret/environment settings. Only this owner can configure report destinations or send platform-wide announcements.

1. Add the 9N8 bot to your Telegram group or channel.
2. Promote it to administrator. For a channel, grant **Post Messages** permission.
3. In your private chat with 9N8, run `/report_addchat CHAT_ID_OR_@USERNAME`.
4. Set the weekly schedule, for example `/reportschedule MON 09:00 UTC` or `/reportschedule MON 09:00 Africa/Nairobi`.
5. Users who want their simulated results included must independently run `/share_stats on`.
6. Use `/report_now` to send the previous complete Monday–Sunday period immediately.

Admin commands:

```text
/users
/admin_risk
/pause_all
/resume_all
/report_addchat CHAT_ID_OR_@USERNAME
/report_removechat CHAT_ID
/report_chats
/reportschedule MON 09:00 [IANA_TIMEZONE]
/report_now
/announce Your update text
```

`/announce` sends a direct update only to registered users who have left `/notifications` enabled. It cannot start a private chat with users who have never pressed `/start`. Weekly public reports contain anonymous aggregate P&L only from users who opted in. They do not expose account IDs, usernames or individual positions.

The weekly schedule defaults to Monday 09:00 UTC. It can be changed using `/reportschedule`. Reports cover the previous complete Monday–Sunday week. A single worker instance is required so the SQLite scheduler and paper monitor do not run twice.

## Cloud deployment

Use a host that supports a continuously running Docker/background worker and a persistent disk/volume. Do not deploy as a short-lived serverless function. Keep one replica and mount persistent storage at `/app/data`.

### Required environment variables

```env
TELEGRAM_BOT_TOKEN=YOUR_COMPLETE_BOTFATHER_TOKEN
TELEGRAM_OWNER_ID=YOUR_NUMERIC_TELEGRAM_USER_ID
MODE=paper
MT5_PROVIDER=metaapi
METAAPI_TOKEN=YOUR_METAAPI_TOKEN
METAAPI_ACCOUNT_ID=YOUR_METAAPI_DEMO_ACCOUNT_ID
METAAPI_API_BASE=https://mt-client-api-v1.new-york.agiliumtrade.ai
METAAPI_MARKET_DATA_BASE=https://mt-market-data-client-api-v1.new-york.agiliumtrade.ai
PAPER_DB_PATH=/app/data/9n8_paper.sqlite3
PAPER_INITIAL_BALANCE=10000
PAPER_CURRENCY=USD
PAPER_MAX_LOTS=0.10
PAPER_MAX_OPEN_POSITIONS=3
PAPER_SLIPPAGE_POINTS=2
PAPER_MAX_TICK_AGE_SECONDS=900
PAPER_DEFAULT_SL_POINTS=500
PAPER_DEFAULT_TP_POINTS=1000
PAPER_DEFAULT_BE_TRIGGER_POINTS=150
PAPER_DEFAULT_BE_OFFSET_POINTS=0
REPORT_DAY=MON
REPORT_TIME=09:00
REPORT_TIMEZONE=UTC
```

Optional Twelve Data fields:

```env
TWELVEDATA_API_KEY=
WATCHLIST=XAU/USD,EUR/USD,GBP/USD,USD/JPY,XAG/USD
TIMEFRAME=15min
OUTPUT_SIZE=80
```

The regional MetaApi base URLs above are defaults. Use the API hostnames assigned to your MetaApi account if they differ. Keep all credentials in your hosting provider's encrypted environment/secret manager; never commit `.env`.

### Run with Docker

```bash
cp .env.example .env
# Edit .env locally and add secrets

docker compose up --build -d
docker compose logs -f
```

For cloud use, connect the GitHub repository to a persistent worker service, build from the included Dockerfile, mount `/app/data`, set the environment variables above, and deploy one worker replica. The included Procfile is also `worker: python -m src.bot`.

After deployment, message the bot with `/start`. As owner, use `/id` to identify your numeric Telegram ID, then set that number as `TELEGRAM_OWNER_ID` in the hosting dashboard and restart the worker. The owner should also run `/start` in the private chat to create their own paper account.

## Tests

```bash
python -m pip install -r requirements.txt
python -m pip install -r requirements.txt pytest
python -m pytest -q
```

Tests use mocked MT5/MetaApi responses and temporary SQLite databases. They do not prove external credentials work or place broker orders. After deployment, `/mt5_status` verifies that the configured MetaApi connection is reachable and positively identifies an MT5 DEMO account.

## Security

See [SECURITY.md](SECURITY.md). Never paste Telegram tokens, MetaApi credentials, broker passwords, seed phrases or private keys into chat. The Telegram token and market-data key previously shared in conversation should be rotated before use.
