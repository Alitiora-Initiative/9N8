# 9N8 security model

## Paper-only boundary

This release is a market-data reader and local paper-trading simulator. `MetaApiProvider` uses HTTP GET requests for account information, symbols, specifications, current prices and historical candles. The desktop MT5 adapter reads account/symbol/tick/candle data only. The project contains no MT5 `order_send`, MetaApi trade endpoint, wallet signer, withdrawal, transfer or sweep feature.

The configured MT5 account must be positively identified as DEMO. Unknown or live account types are rejected. Prefer an investor/read-only password for the MT5 demo account where supported. Treat the MetaApi token as sensitive even though this build makes read-only requests.

## User isolation and privacy

- Paper positions, histories, settings and exit alerts are scoped to the Telegram numeric user ID.
- Personal trading commands are accepted only in a private chat.
- New users start with paper entries paused.
- User lot/position limits are bounded by platform hard caps.
- Private exit notifications are sent only to users who started the bot and have notifications enabled.
- Group/channel performance uses only users who explicitly opt in with `/share_stats on`.
- Group reports aggregate P&L by profit currency and never disclose usernames, Telegram IDs or individual positions.
- The platform owner is the numeric `TELEGRAM_OWNER_ID`; only that owner may configure public report destinations or send community announcements.

## Group/channel posting

The owner adds a destination with `/report_addchat`. Before saving it, 9N8 checks that the bot is an administrator of the target. For channels it also checks the `can_post_messages` permission. Telegram permissions can change after setup; send failures are logged without recording tokens or user trading details.

## Secret handling

- Store `TELEGRAM_BOT_TOKEN`, `METAAPI_TOKEN`, MetaApi account ID and optional Twelve Data key only in the hosting provider's encrypted secret/environment manager.
- Never commit `.env`, SQLite databases, broker passwords, API tokens, wallet seed phrases or private keys.
- The `.gitignore` excludes `.env`, local databases, data folders and Python caches.
- Do not log request Authorization headers or secret values.
- Credentials pasted into chat or an untrusted environment must be revoked/rotated.

## Operational recommendations

- Use an always-on worker with a persistent volume mounted at `/app/data`.
- Run exactly one worker replica while using SQLite. Multiple replicas could run duplicate monitors and weekly reports.
- Back up the persistent SQLite database securely. Restrict access to backups because they contain account identifiers and trading history.
- Keep paper mode enforced. Any future live execution requires a separate risk/security review, explicit user authorization, provider write-permission review and withdrawal-disabled account credentials; this release does not include live execution.

## Known simulation limitations

Simulated P&L is estimated in the instrument's profit currency. It may exclude commissions, swaps, conversion, variable spread and stop gaps. The paper engine's break-even stop updates only its local database. It does not modify broker-side orders. A stop triggered after a price gap can close at a different current quote and produce a small loss despite a break-even stop setting.
