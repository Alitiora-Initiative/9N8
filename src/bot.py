"""9N8 multi-user Telegram interface: MT5 demo data + isolated paper accounts."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from .config import config
from .market_data import MarketDataError, TwelveDataClient
from .mt5_provider import MT5Error, MT5Provider
from .metaapi_provider import MetaApiProvider
from .paper import PaperBroker, PaperTradingError, PaperPosition
from .risk import policy
from .strategy import analyse
from .user_store import UserStore

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("9n8")

market_data = TwelveDataClient(config.twelve_data_api_key)
if config.mt5_provider == "metaapi":
    mt5_provider = MetaApiProvider(
        token=config.metaapi_token, account_id=config.metaapi_account_id,
        api_base=config.metaapi_api_base, market_data_base=config.metaapi_market_data_base,
    )
else:
    mt5_provider = MT5Provider(config.mt5_terminal_path)

paper_broker = PaperBroker(
    provider=mt5_provider, db_path=config.paper_db_path,
    initial_balance=config.paper_initial_balance, max_lots=config.paper_max_lots,
    max_open_positions=config.paper_max_open_positions,
    slippage_points=config.paper_slippage_points,
    max_tick_age_seconds=config.paper_max_tick_age_seconds,
)
user_store = UserStore(
    config.paper_db_path,
    max_lots=config.paper_max_lots,
    max_positions=config.paper_max_open_positions,
    default_sl=config.paper_default_sl_points,
    default_tp=config.paper_default_tp_points,
    default_be_trigger=config.paper_default_be_trigger_points,
    default_be_offset=config.paper_default_be_offset_points,
)
# Persist the global emergency pause across worker restarts.
policy.paused = user_store.get_state("global_emergency_pause", "0") == "1"
last_signals: dict[str, object] = {}


DAY_INDEX = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user and config.owner_id and user.id == config.owner_id)


async def reply(update: Update, text: str) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text)


async def private_user_guard(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        await reply(update, "This command requires a Telegram user account.")
        return False
    if chat.type != "private":
        await reply(update, "For account privacy, use this command in a private chat with 9N8.")
        return False
    account = user_store.get_user(user.id)
    if not account:
        await reply(update, "Welcome to 9N8. Send /start first to create your private paper account.")
        return False
    return True


async def admin_guard(update: Update) -> bool:
    if not config.owner_id:
        await reply(update, "Admin setup incomplete. Send /id, set TELEGRAM_OWNER_ID in your hosting environment, then restart 9N8.")
        return False
    if not is_owner(update):
        await reply(update, "This command is restricted to the 9N8 platform administrator.")
        return False
    if update.effective_chat and update.effective_chat.type != "private":
        await reply(update, "Run admin commands in your private chat with 9N8.")
        return False
    return True


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        await reply(update, f"Your Telegram user ID: {user.id}\nThis numeric ID is safe to use as TELEGRAM_OWNER_ID; never share your bot token.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, chat = update.effective_user, update.effective_chat
    if not user or not chat or chat.type != "private":
        await reply(update, "Open a private chat with 9N8 and send /start to create your own paper-trading account.")
        return
    account = user_store.register(user.id, chat.id, user.username, user.first_name)
    await reply(update, (
        f"Welcome to 9N8{', ' + user.first_name if user.first_name else ''}.\n\n"
        "Your personal paper account is ready. All user positions and settings are isolated.\n"
        "Market prices are read from the connected MT5 DEMO feed. Your trades are simulated in 9N8 only.\n\n"
        f"Paper entries: {'ENABLED' if account['paper_enabled'] else 'PAUSED by default'}\n"
        "Start with /settings, then /paper_enable when you are ready to test.\n"
        "Use /help for commands. No live orders, withdrawals or transfers are implemented."
    ))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update):
        return
    await reply(update, (
        "9N8 USER COMMANDS\n"
        "/settings — your parameters\n"
        "/setlots 0.05 — personal maximum lot size (also a per-trade cap)\n"
        "/setmaxpositions 3 — max simultaneous paper positions\n"
        "/setsl 500 — default stop distance in broker points\n"
        "/settp 1000 — default target distance in broker points\n"
        "/setbe 150 0 — move stop to break-even after 150 points; optional offset\n"
        "/setbe off — disable automatic break-even movement\n"
        "/paper_enable or /paper_pause — enable/pause your entries\n"
        "/paper_open XAUUSD BUY 0.01 [SL_POINTS TP_POINTS] — simulated trade\n"
        "/paper_positions, /paper_close TICKET, /paper_history, /paper_balance\n"
        "/mt5_status, /mt5_symbols [FILTER], /mt5_price SYMBOL, /mt5_scan [SYMBOL]\n"
        "/scan [SYMBOL], /price SYMBOL — optional Twelve Data scanner\n"
        "/stats — your current week's paper statistics\n"
        "/notifications on|off — personal trade-exit DMs\n"
        "/share_stats on|off — consent to anonymous aggregate group reports\n"
        "/id — your Telegram user ID\n\n"
        "All P&L is simulated and may exclude fees, swaps and currency conversion."
    ))


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update):
        return
    account = user_store.get_user(update.effective_user.id)
    be = "OFF" if int(account["be_trigger_points"]) == 0 else f"after {account['be_trigger_points']} points, offset {account['be_offset_points']} points"
    await reply(update, (
        "YOUR 9N8 PAPER SETTINGS\n"
        f"Paper entries: {'ENABLED' if account['paper_enabled'] else 'PAUSED'}\n"
        f"Max lots per trade: {account['max_lots']:g}\n"
        f"Max open positions: {account['max_open_positions']}\n"
        f"Default stop: {account['default_sl_points']} points\n"
        f"Default target: {account['default_tp_points']} points\n"
        f"Automatic break-even: {be}\n"
        f"Trade notifications: {'ON' if account['notifications'] else 'OFF'}\n"
        f"Anonymous group statistics: {'OPTED IN' if account['share_stats'] else 'OPTED OUT'}\n\n"
        "Change settings with /setlots, /setmaxpositions, /setsl, /settp, /setbe, /notifications and /share_stats.\n"
        f"Platform hard limits: {config.paper_max_lots:g} lots, {config.paper_max_open_positions} open positions."
    ))


def _one_number(args: list[str], label: str, minimum: float, maximum: float) -> float:
    if len(args) != 1:
        raise ValueError(f"Usage: provide one {label} value.")
    value = float(args[0])
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return value


def _one_integer(args: list[str], label: str, minimum: int, maximum: int) -> int:
    if len(args) != 1 or not re.fullmatch(r"\d+", args[0]):
        raise ValueError(f"{label} must be a whole number.")
    value = int(args[0])
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return value


async def setlots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    try:
        value = _one_number(context.args, "max lots", 0.01, config.paper_max_lots)
        user_store.update(update.effective_user.id, "max_lots", value)
        await reply(update, f"Your per-trade lot cap is now {value:g}. The platform maximum remains {config.paper_max_lots:g}.")
    except (ValueError, TypeError) as exc: await reply(update, f"Could not update setting: {exc}\nUsage: /setlots 0.05")


async def setmaxpositions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    try:
        value = _one_integer(context.args, "max positions", 1, config.paper_max_open_positions)
        user_store.update(update.effective_user.id, "max_open_positions", value)
        await reply(update, f"Your simultaneous open-position cap is now {value}.")
    except (ValueError, TypeError) as exc: await reply(update, f"Could not update setting: {exc}\nUsage: /setmaxpositions 3")


async def set_sl_tp(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, label: str) -> None:
    if not await private_user_guard(update): return
    try:
        value = _one_integer(context.args, label, 1, 1_000_000)
        user_store.update(update.effective_user.id, key, value)
        await reply(update, f"Your default {label} is now {value} broker points.")
    except (ValueError, TypeError) as exc: await reply(update, f"Could not update setting: {exc}\nUsage: /{ 'setsl' if key == 'default_sl_points' else 'settp'} 500")


async def setsl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_sl_tp(update, context, "default_sl_points", "stop-loss distance")


async def settp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_sl_tp(update, context, "default_tp_points", "take-profit distance")


async def setbe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    if len(context.args) == 1 and context.args[0].lower() == "off":
        user_store.update(update.effective_user.id, "be_trigger_points", 0)
        user_store.update(update.effective_user.id, "be_offset_points", 0)
        await reply(update, "Automatic break-even is OFF for new positions. Existing positions keep their saved settings.")
        return
    if len(context.args) not in {1, 2}:
        await reply(update, "Usage: /setbe 150 [OFFSET_POINTS] or /setbe off")
        return
    try:
        if not re.fullmatch(r"\d+", context.args[0]) or (len(context.args) == 2 and not re.fullmatch(r"\d+", context.args[1])):
            raise ValueError("Trigger and offset must be whole numbers of broker points.")
        trigger = int(context.args[0]); offset = int(context.args[1]) if len(context.args) == 2 else 0
        if not 1 <= trigger <= 1_000_000 or not 0 <= offset < trigger:
            raise ValueError("Trigger must be 1–1,000,000 points and offset must be non-negative and smaller than the trigger.")
        user_store.update(update.effective_user.id, "be_trigger_points", trigger)
        user_store.update(update.effective_user.id, "be_offset_points", offset)
        await reply(update, f"Break-even enabled for new positions: move the stop after +{trigger} favorable points, to entry {offset:+} points.")
    except (ValueError, TypeError) as exc: await reply(update, f"Could not update break-even: {exc}")


async def _toggle_setting(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, label: str) -> None:
    if not await private_user_guard(update): return
    if len(context.args) != 1 or context.args[0].lower() not in {"on", "off"}:
        await reply(update, f"Usage: /{label} on|off")
        return
    value = int(context.args[0].lower() == "on")
    user_store.update(update.effective_user.id, key, value)
    await reply(update, f"{label.replace('_', ' ').title()} {'enabled' if value else 'disabled'}.")


async def notifications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _toggle_setting(update, context, "notifications", "notifications")


async def share_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _toggle_setting(update, context, "share_stats", "share_stats")


async def paper_enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    user_store.update(update.effective_user.id, "paper_enabled", 1)
    if policy.paused:
        await reply(update, "Your account is enabled, but the platform emergency pause is active. Ask the 9N8 administrator to resume the paper engine.")
        return
    await reply(update, "Your paper entries are ENABLED. This applies only to your isolated simulated account; it does not enable broker orders.")


async def paper_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    user_store.update(update.effective_user.id, "paper_enabled", 0)
    await reply(update, "Your new paper entries are PAUSED. Existing simulated positions will continue to be monitored for SL/TP and break-even.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    account = user_store.get_user(update.effective_user.id)
    await reply(update, (
        "9N8 PLATFORM STATUS\nMode: PAPER ONLY\n"
        f"MT5 provider: {config.mt5_provider}\nMT5 timeframe: {config.mt5_timeframe}\n"
        f"Twelve Data: {'configured' if config.twelve_data_api_key else 'not configured'}\n"
        f"Your paper entries: {'ENABLED' if account['paper_enabled'] else 'PAUSED'}\n"
        f"Global emergency pause: {'ON' if policy.paused else 'OFF'}\n"
        "MT5 order submission: NOT USED\nWithdrawals/transfers: NOT IMPLEMENTED"
    ))


async def markets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    await reply(update, "Twelve Data watchlist:\n" + "\n".join(f"• {symbol}" for symbol in config.watchlist))


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    if not context.args:
        await reply(update, "Usage: /price XAU/USD"); return
    symbol = " ".join(context.args).strip().upper()
    try:
        candles = await market_data.time_series(symbol, config.timeframe, 2)
        candle = candles[-1]
        await reply(update, f"{symbol}\nLatest candle close: {candle.close}\nTime: {candle.datetime}\nInterval: {config.timeframe}\nSource: Twelve Data")
    except MarketDataError as exc: await reply(update, f"Market-data error: {exc}")


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    if not config.twelve_data_api_key:
        await reply(update, "Scanner not configured. Add TWELVEDATA_API_KEY to hosting secrets."); return
    symbols = [" ".join(context.args).strip().upper()] if context.args else config.watchlist
    if len(symbols) > 10:
        await reply(update, "Scan limited to 10 symbols per command to limit provider credits."); return
    await reply(update, f"Scanning {len(symbols)} symbol(s) with {config.timeframe} candles. Analysis only; no orders are placed.")
    results: list[str] = []
    for symbol in symbols:
        try:
            candles = await market_data.time_series(symbol, config.timeframe, config.output_size)
            signal = analyse(symbol, candles); last_signals[symbol] = signal
            results.append(f"{signal.label} — {symbol}\nClose: {signal.close}\nEMA 9: {signal.ema_fast:.5f} | EMA 21: {signal.ema_slow:.5f}\nRSI 14: {signal.rsi:.1f}\nCandle: {signal.candle_time}\n{signal.explanation}")
        except (MarketDataError, ValueError) as exc: results.append(f"UNAVAILABLE — {symbol}\n{exc}")
    for index in range(0, len(results), 2): await reply(update, "\n\n".join(results[index:index + 2]))
    await reply(update, "Scan complete. Technical observations are not guaranteed predictions or trade instructions.")


async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    if not last_signals:
        await reply(update, "No scan results yet. Run /scan or /scan XAU/USD."); return
    await reply(update, "Latest observations:\n" + "\n".join(f"{item.label}: {item.symbol} | close {item.close} | RSI {item.rsi:.1f}" for item in last_signals.values()))


async def mt5_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    try:
        account = await asyncio.to_thread(mt5_provider.account_summary)
        await reply(update, ("MT5 CONNECTION OK\nAccount type: DEMO (verified)\n"
            f"Server: {account.server}\nLogin: {account.login}\nCurrency: {account.currency}\n"
            f"Demo balance: {account.balance:.2f}\nDemo equity: {account.equity:.2f}\n"
            "Access used: market-data reads only\nOrder submission: NOT USED"))
    except MT5Error as exc: await reply(update, f"MT5 unavailable: {exc}")


async def mt5_symbols(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    query = " ".join(context.args) if context.args else ""
    try:
        names = await asyncio.to_thread(mt5_provider.symbols, query)
        await reply(update, "MT5 broker symbols:\n" + ("\n".join(names) if names else "No matching symbols. Check MetaApi deployment and broker symbol names."))
    except MT5Error as exc: await reply(update, f"MT5 unavailable: {exc}")


async def mt5_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    if not context.args:
        await reply(update, "Usage: /mt5_price XAUUSD"); return
    symbol = context.args[0].upper()
    try:
        tick = await asyncio.to_thread(mt5_provider.tick, symbol)
        info = await asyncio.to_thread(mt5_provider.symbol_info, symbol)
        await reply(update, f"{symbol}\nBid: {tick.bid:.{info.digits}f}\nAsk: {tick.ask:.{info.digits}f}\nSpread: {(tick.ask-tick.bid):.{info.digits}f}\nUTC tick: {tick.time_utc}\nSource: MT5 DEMO via {config.mt5_provider}")
    except MT5Error as exc: await reply(update, f"MT5 price error: {exc}")


async def mt5_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    symbols = [context.args[0].upper()] if context.args else config.mt5_symbols
    if len(symbols) > 10:
        await reply(update, "MT5 scan limited to 10 symbols per command."); return
    results = []
    for symbol in symbols:
        try:
            candles = await asyncio.to_thread(mt5_provider.candles, symbol, config.mt5_timeframe, config.mt5_bars)
            signal = analyse(symbol, candles)
            results.append(f"{signal.label} — {symbol}\nClose: {signal.close}\nEMA 9: {signal.ema_fast:.5f} | EMA 21: {signal.ema_slow:.5f}\nRSI 14: {signal.rsi:.1f}\nCandle UTC: {signal.candle_time}\n{signal.explanation}")
        except (MT5Error, ValueError) as exc: results.append(f"UNAVAILABLE — {symbol}\n{exc}")
    await reply(update, "MT5 DEMO MARKET SCAN\n\n" + "\n\n".join(results) + "\n\nAnalysis only; no broker order was submitted.")


async def paper_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    summary = paper_broker.summary(owner_id=update.effective_user.id)
    groups = summary["realized_by_currency"]
    pnl_text = "No closed paper trades yet."
    if groups: pnl_text = "\n".join(f"{ccy}: {values['pnl']:+.2f} ({values['trades']} closed)" for ccy, values in groups.items())
    await reply(update, f"YOUR 9N8 PAPER LEDGER\nReference capital: {summary['reference_capital']:.2f} {config.paper_currency} (simulation only)\nOpen positions: {summary['open_positions']}\nTotal paper trades: {summary['total_trades']}\nRealized P&L by profit currency:\n{pnl_text}\nP&L is estimated; it is not live account equity or converted between currencies.")


async def paper_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    account = user_store.get_user(update.effective_user.id)
    if not account["paper_enabled"]:
        await reply(update, "Your paper entries are paused. Review /settings, then send /paper_enable."); return
    if policy.paused:
        await reply(update, "The platform emergency pause is active. New entries are disabled for everyone."); return
    if len(context.args) not in {3, 5}:
        await reply(update, "Usage: /paper_open SYMBOL BUY|SELL LOTS [SL_POINTS TP_POINTS]\nIf SL/TP are omitted, your /settings defaults are used."); return
    symbol, side = context.args[0].upper(), context.args[1].upper()
    try:
        lots = float(context.args[2])
        sl_points = int(context.args[3]) if len(context.args) == 5 else int(account["default_sl_points"])
        tp_points = int(context.args[4]) if len(context.args) == 5 else int(account["default_tp_points"])
        position = await asyncio.to_thread(
            paper_broker.open_position, symbol, side, lots, sl_points, tp_points,
            update.effective_user.id, float(account["max_lots"]), int(account["max_open_positions"]),
            int(account["be_trigger_points"]), int(account["be_offset_points"]),
        )
        be = "OFF" if position.be_trigger_points == 0 else f"after {position.be_trigger_points} points"
        await reply(update, (f"PAPER POSITION OPENED — ticket #{position.ticket}\n{position.symbol} {position.side} {position.lots:g} lots\n"
            f"Simulated entry: {position.entry_price}\nSL: {position.stop_loss}\nTP: {position.take_profit}\n"
            f"Break-even: {be}\nProfit currency: {position.profit_currency}\nPersonal paper ledger: SQLite\nMT5 broker order: NOT SENT"))
    except (ValueError, PaperTradingError, MT5Error) as exc: await reply(update, f"Paper position rejected: {exc}")


async def paper_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    positions = paper_broker.open_positions(owner_id=update.effective_user.id)
    if not positions:
        await reply(update, "You have no open paper positions."); return
    lines = []
    for pos in positions:
        try:
            tick = await asyncio.to_thread(mt5_provider.tick, pos.symbol)
            mark = tick.bid if pos.side == "BUY" else tick.ask
            direction = 1 if pos.side == "BUY" else -1
            info = await asyncio.to_thread(mt5_provider.symbol_info, pos.symbol)
            pnl = (mark - pos.entry_price) * direction * pos.lots * info.contract_size
            be = "BE ACTIVE" if pos.be_activated else (f"BE at +{pos.be_trigger_points}pt" if pos.be_trigger_points else "BE off")
            lines.append(f"#{pos.ticket} {pos.symbol} {pos.side} {pos.lots:g}\nEntry {pos.entry_price} | Mark {mark}\nEst. P&L {pnl:+.2f} {pos.profit_currency}\nSL {pos.stop_loss} | TP {pos.take_profit} | {be}")
        except MT5Error as exc: lines.append(f"#{pos.ticket} {pos.symbol}: mark unavailable ({exc})")
    await reply(update, "YOUR OPEN PAPER POSITIONS\n\n" + "\n\n".join(lines) + "\n\nUnrealized P&L is estimated and excludes fees/conversion.")


async def paper_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await reply(update, "Usage: /paper_close TICKET"); return
    try:
        result = await asyncio.to_thread(paper_broker.close_position, int(context.args[0]), "MANUAL", update.effective_user.id)
        await reply(update, f"PAPER POSITION CLOSED — #{result.ticket}\n{result.symbol} {result.side} {result.lots:g}\nEntry: {result.entry_price}\nExit: {result.close_price}\nEstimated realized P&L: {result.pnl_quote:+.2f} {result.currency}\nReason: {result.reason}\nNo MT5 order was sent.")
    except (PaperTradingError, MT5Error) as exc: await reply(update, f"Paper close failed: {exc}")


async def paper_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    try:
        closed = await asyncio.to_thread(paper_broker.check_stops, update.effective_user.id)
        if not closed:
            await reply(update, "Paper stop/target/break-even check complete. No position closed during this check."); return
        lines = [f"#{item.ticket} {item.symbol} closed at {item.close_price}; P&L {item.pnl_quote:+.2f} {item.currency} ({item.reason})" for item in closed]
        await reply(update, "PAPER EXIT EVENTS\n" + "\n".join(lines) + "\nLocal simulated closes only.")
    except MT5Error as exc: await reply(update, f"Paper monitor error: {exc}")


async def paper_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    rows = paper_broker.history(10, owner_id=update.effective_user.id)
    if not rows:
        await reply(update, "No paper trades recorded in your account yet."); return
    lines = []
    for row in rows:
        if row["status"] == "OPEN": lines.append(f"#{row['ticket']} OPEN {row['symbol']} {row['side']} {row['lots']:g} @ {row['entry_price']}")
        else: lines.append(f"#{row['ticket']} CLOSED {row['symbol']} {row['side']} P&L {row['pnl_quote']:+.2f} {row['profit_currency']} ({row.get('close_reason') or 'CLOSED'})")
    await reply(update, "YOUR RECENT PAPER TRADES\n" + "\n".join(lines))


def _week_bounds_previous(tz: ZoneInfo) -> tuple[datetime, datetime]:
    now = datetime.now(tz)
    this_monday = datetime(now.year, now.month, now.day, tzinfo=tz) - timedelta(days=now.weekday())
    start = this_monday - timedelta(days=7)
    return start.astimezone(timezone.utc), this_monday.astimezone(timezone.utc)


def _format_stats(stats: dict, title: str, start: datetime, end: datetime,
                  display_start: datetime | None = None, display_end: datetime | None = None) -> str:
    period_start = display_start or start
    period_last_day = ((display_end or end) - timedelta(seconds=1)).date()
    if not stats["trades"]:
        return f"{title}\nPeriod: {period_start.date()} to {period_last_day}\nNo closed paper trades in this period."
    pnl = "\n".join(f"{currency}: {values['pnl']:+.2f} ({values['trades']} trades)" for currency, values in sorted(stats["by_currency"].items()))
    return (
        f"{title}\nPeriod: {period_start.date()} to {period_last_day}\n"
        f"Closed trades: {stats['trades']} | Wins: {stats['wins']} | Losses: {stats['losses']} | Breakeven: {stats['breakevens']}\n"
        f"Win rate: {stats['win_rate']:.1f}% | Break-even moves activated: {stats['be_moves']}\n"
        f"Estimated realized P&L by profit currency:\n{pnl}"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await private_user_guard(update): return
    now = datetime.now(timezone.utc)
    monday = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) - timedelta(days=now.weekday())
    start = monday
    stats_row = paper_broker.period_stats(start.isoformat(), now.isoformat(), [update.effective_user.id])
    await reply(update, _format_stats(stats_row, "YOUR CURRENT WEEKLY PAPER P&L", start, now))


async def admin_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update): return
    await reply(update, (
        "9N8 ADMIN RISK STATUS\nMode: PAPER ONLY\n"
        f"Hard maximum lots: {config.paper_max_lots:g}\nHard maximum positions per user: {config.paper_max_open_positions}\n"
        f"Maximum quote age: {config.paper_max_tick_age_seconds}s\nGlobal emergency pause: {'ON' if policy.paused else 'OFF'}\n"
        "MT5 order submit: absent\nWithdrawals/transfers: absent\nBreak-even manager: simulated stop adjustment only"
    ))


async def pause_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update): return
    policy.paused = True
    user_store.set_state("global_emergency_pause", "1")
    await reply(update, "GLOBAL EMERGENCY PAUSE enabled. All new paper entries are blocked. Existing paper positions continue to be monitored.")


async def resume_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update): return
    policy.paused = False
    user_store.set_state("global_emergency_pause", "0")
    await reply(update, "Global emergency pause cleared. Each user must separately enable their own paper entries with /paper_enable.")


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update): return
    await reply(update, f"Registered private users: {user_store.registered_count()}\nUsers can control their own paper settings. Group statistics require explicit /share_stats on consent.")


async def report_addchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update): return
    if len(context.args) != 1:
        await reply(update, "Usage: /report_addchat CHAT_ID_OR_@USERNAME\nAdd 9N8 as an administrator in that group/channel first."); return
    target = context.args[0]
    try:
        bot_member = await context.bot.get_chat_member(target, context.bot.id)
        if getattr(bot_member, "status", "") not in {"administrator", "creator"}:
            await reply(update, "9N8 is not an administrator in that chat. Promote the bot and retry."); return
        chat = await context.bot.get_chat(target)
        if chat.type == "channel" and not bool(getattr(bot_member, "can_post_messages", False)):
            await reply(update, "9N8 is an admin but lacks the channel 'Post Messages' permission."); return
        if chat.type not in {"group", "supergroup", "channel"}:
            await reply(update, "Only groups, supergroups and channels can be report destinations."); return
        user_store.add_report_chat(chat.id, chat.title or str(chat.id), chat.type, update.effective_user.id)
        await reply(update, f"Weekly reporting destination added: {chat.title or chat.id} ({chat.type}). 9N8 verified administrator status.")
    except Exception as exc:
        logger.warning("Could not verify report target (%s)", type(exc).__name__)
        await reply(update, "Could not verify that destination. Check the chat ID/username, add 9N8 as an administrator, and ensure it can post messages.")


async def report_removechat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update): return
    if len(context.args) != 1:
        await reply(update, "Usage: /report_removechat CHAT_ID"); return
    removed = user_store.remove_report_chat(context.args[0])
    await reply(update, "Report destination removed." if removed else "That report destination was not found. Use /report_chats.")


async def report_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update): return
    chats = user_store.report_chats()
    if not chats:
        await reply(update, "No report destinations configured. Add the bot as an admin, then use /report_addchat CHAT_ID_OR_@USERNAME."); return
    await reply(update, "WEEKLY REPORT DESTINATIONS\n" + "\n".join(f"{row['chat_title']} — {row['chat_id']} ({row['chat_type']})" for row in chats))


async def reportschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update): return
    if len(context.args) not in {2, 3}:
        await reply(update, "Usage: /reportschedule MON 09:00 [UTC]\nTimezone must be an IANA name, for example Africa/Nairobi or Europe/London."); return
    day, time_value = context.args[0].upper(), context.args[1]
    tz_name = context.args[2] if len(context.args) == 3 else config.report_timezone
    if day not in DAY_INDEX or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_value):
        await reply(update, "Invalid schedule. Example: /reportschedule MON 09:00 UTC"); return
    try: ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        await reply(update, "Unknown timezone. Use a valid IANA timezone such as UTC or Africa/Nairobi."); return
    user_store.set_state("report_schedule", f"{day}|{time_value}|{tz_name}")
    await reply(update, f"Weekly reports scheduled for {day} at {time_value} ({tz_name}). The report covers the previous complete Monday–Sunday week.")


async def _weekly_report_text() -> tuple[str, str]:
    stored = user_store.get_state("report_schedule", f"{config.report_day}|{config.report_time}|{config.report_timezone}")
    try: _, _, timezone_name = stored.split("|", 2); tz = ZoneInfo(timezone_name)
    except Exception: tz = ZoneInfo("UTC")
    now_local = datetime.now(tz)
    this_monday_local = datetime(now_local.year, now_local.month, now_local.day, tzinfo=tz) - timedelta(days=now_local.weekday())
    previous_monday_local = this_monday_local - timedelta(days=7)
    start, end = previous_monday_local.astimezone(timezone.utc), this_monday_local.astimezone(timezone.utc)
    opted_in = user_store.opted_in_user_ids()
    aggregates = paper_broker.period_stats(start.isoformat(), end.isoformat(), opted_in)
    text = _format_stats(aggregates, "9N8 WEEKLY COMMUNITY PAPER PERFORMANCE", start, end,
                         display_start=previous_monday_local, display_end=this_monday_local)
    text += (f"\nOpted-in contributors with closed trades: {aggregates['contributors']}\n"
             "Anonymous aggregate from users who opted in. Paper simulation only; gross estimated P&L may exclude fees, swaps and currency conversion. Not live trading returns or investment advice.")
    return text, start.date().isoformat()


async def report_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update): return
    chats = user_store.report_chats()
    if not chats:
        await reply(update, "No report destinations configured. Use /report_addchat CHAT_ID_OR_@USERNAME first."); return
    text, _ = await _weekly_report_text()
    delivered, failed = 0, 0
    for item in chats:
        try:
            await context.bot.send_message(chat_id=item["chat_id"], text=text)
            delivered += 1
        except Exception:
            failed += 1
            logger.exception("Weekly P&L report delivery failed to chat=%s", item["chat_id"])
    await reply(update, f"Weekly report delivery finished. Sent: {delivered}; failed: {failed}.")


async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update): return
    message = " ".join(context.args).strip()
    if not message:
        await reply(update, "Usage: /announce Your community update text"); return
    subscribers = user_store.notification_users()
    sent = failed = 0
    for user in subscribers:
        try:
            await context.bot.send_message(chat_id=user["chat_id"], text=f"9N8 UPDATE\n\n{message}")
            sent += 1
        except Exception:
            failed += 1
    await reply(update, f"Update delivery complete. Sent: {sent}; failed: {failed}. Only users with private bot chats and notifications enabled are messaged.")


async def _send_break_even_notice(application: Application, position: PaperPosition) -> None:
    user = user_store.get_user(position.owner_id)
    if not user or not user["notifications"]: return
    try:
        await application.bot.send_message(
            chat_id=user["chat_id"],
            text=(f"BREAK-EVEN STOP UPDATED — #{position.ticket}\n{position.symbol} {position.side}\n"
                  f"New simulated stop: {position.stop_loss}\nEntry: {position.entry_price}\n"
                  "This is a local paper-ledger adjustment; no MT5 order or modification was sent."),
        )
    except Exception as exc: logger.info("Could not send BE notification (%s)", type(exc).__name__)


async def _send_close_notice(application: Application, item) -> None:
    user = user_store.get_user(item.owner_id)
    if not user or not user["notifications"]: return
    try:
        await application.bot.send_message(
            chat_id=user["chat_id"],
            text=(f"PAPER EXIT — #{item.ticket} {item.symbol} {item.side}\nExit: {item.close_price}\n"
                  f"Estimated P&L: {item.pnl_quote:+.2f} {item.currency}\nReason: {item.reason}\n"
                  "Simulated close only; no MT5 order was sent."),
        )
    except Exception as exc: logger.info("Could not send paper exit notification (%s)", type(exc).__name__)


async def paper_stop_monitor(application: Application) -> None:
    """Monitor all user paper positions and send private break-even/exit alerts."""
    while True:
        try:
            before = {p.ticket: p.be_activated for p in paper_broker.open_positions()}
            if before:
                closed = await asyncio.to_thread(paper_broker.check_stops)
                after = paper_broker.open_positions()
                for position in after:
                    if position.ticket in before and not before[position.ticket] and position.be_activated:
                        await _send_break_even_notice(application, position)
                for item in closed:
                    await _send_close_notice(application, item)
        except asyncio.CancelledError: raise
        except Exception:
            logger.exception("Paper SL/TP/break-even monitor could not complete this cycle")
        await asyncio.sleep(10)


async def weekly_report_monitor(application: Application) -> None:
    """Send one weekly aggregate at the configured local weekday/time."""
    while True:
        try:
            chats = user_store.report_chats()
            if chats:
                stored = user_store.get_state("report_schedule", f"{config.report_day}|{config.report_time}|{config.report_timezone}")
                day, time_value, tz_name = stored.split("|", 2)
                tz = ZoneInfo(tz_name)
                now = datetime.now(tz)
                week_key = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
                if now.weekday() == DAY_INDEX[day] and now.strftime("%H:%M") == time_value and user_store.get_state("last_weekly_report_key") != week_key:
                    text, _ = await _weekly_report_text()
                    all_sent = True
                    for item in chats:
                        target_key = f"weekly_sent:{week_key}:{item['chat_id']}"
                        if user_store.get_state(target_key) == "1":
                            continue
                        try:
                            await application.bot.send_message(chat_id=item["chat_id"], text=text)
                            user_store.set_state(target_key, "1")
                        except Exception:
                            all_sent = False
                            logger.exception("Scheduled weekly report failed for target %s", item["chat_id"])
                    if all_sent: user_store.set_state("last_weekly_report_key", week_key)
        except asyncio.CancelledError: raise
        except Exception: logger.exception("Weekly report scheduler cycle failed")
        await asyncio.sleep(30)


async def twelve_data_signal_monitor(application: Application) -> None:
    """Scan the Twelve Data watchlist and alert registered users on signal changes."""
    if not config.twelve_data_api_key:
        logger.warning("Automatic Twelve Data alerts are disabled: no Twelve Data API key configured")
        return

    active_labels = {"BULLISH SETUP", "BEARISH SETUP"}
    interval_seconds = config.signal_scan_interval_seconds
    logger.info(
        "Automatic Twelve Data signal monitor started; watchlist=%d; interval=%ds",
        len(config.watchlist), interval_seconds,
    )

    while True:
        try:
            for symbol in config.watchlist:
                try:
                    candles = await market_data.time_series(
                        symbol, config.timeframe, config.output_size
                    )
                    signal = analyse(symbol, candles)
                    last_signals[symbol] = signal

                    state_key = f"twelve_signal:{symbol}"
                    previous = user_store.get_state(state_key, "")
                    current_label = signal.label
                    recipients = user_store.notification_users()

                    # Do not mark the first result as seen until at least one user
                    # has started the bot and can receive the alert.
                    if current_label != previous and recipients:
                        if current_label in active_labels:
                            message = (
                                "9N8 AUTOMATIC MARKET SIGNAL\n"
                                f"{current_label} — {signal.symbol}\n"
                                f"Timeframe: {config.timeframe}\n"
                                f"Last candle close: {signal.close}\n"
                                f"EMA 9: {signal.ema_fast:.5f} | EMA 21: {signal.ema_slow:.5f}\n"
                                f"RSI 14: {signal.rsi:.1f}\n"
                                f"Candle time: {signal.candle_time}\n"
                                f"Reason: {signal.explanation}\n\n"
                                "This is a rule-based market observation, not a probability or guarantee. "
                                "Check current prices and risk independently. No order was placed."
                            )
                        elif previous in active_labels and current_label == "NO CLEAR SETUP":
                            message = (
                                f"9N8 SETUP UPDATE — {signal.symbol}\n"
                                "The previous bullish/bearish conditions are no longer aligned.\n"
                                f"Latest close: {signal.close} | RSI 14: {signal.rsi:.1f}\n"
                                "This is an indicator update only; no order was placed."
                            )
                        else:
                            message = ""

                        if message:
                            for user in recipients:
                                try:
                                    await application.bot.send_message(
                                        chat_id=user["chat_id"], text=message
                                    )
                                except Exception as exc:
                                    logger.warning(
                                        "Signal notification could not be delivered (%s)",
                                        type(exc).__name__,
                                    )
                        user_store.set_state(state_key, current_label)
                    elif previous and current_label != previous and not recipients:
                        # Keep prior state until a user with notifications enabled can receive it.
                        logger.info("Signal changed for %s, but there are no opted-in notification recipients", symbol)

                except (MarketDataError, ValueError) as exc:
                    logger.warning("Automatic Twelve Data scan failed for %s: %s", symbol, exc)
                await asyncio.sleep(0.5)

            user_store.set_state(
                "last_twelve_data_scan_at",
                datetime.now(timezone.utc).isoformat(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automatic Twelve Data signal scan cycle failed")

        await asyncio.sleep(interval_seconds)


async def post_init(application: Application) -> None:
    application.bot_data["paper_monitor_task"] = asyncio.create_task(paper_stop_monitor(application), name="9n8-paper-monitor")
    application.bot_data["weekly_report_task"] = asyncio.create_task(weekly_report_monitor(application), name="9n8-weekly-reports")
    application.bot_data["signal_monitor_task"] = asyncio.create_task(twelve_data_signal_monitor(application), name="9n8-twelve-data-signals")


async def post_stop(application: Application) -> None:
    for key in ("paper_monitor_task", "weekly_report_task", "signal_monitor_task"):
        task = application.bot_data.pop(key, None)
        if task is not None:
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass


async def post_shutdown(application: Application) -> None:
    await asyncio.to_thread(mt5_provider.shutdown)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled Telegram handler error: %s", type(context.error).__name__ if context.error else "unknown")
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("An internal error occurred. Check service logs; 9N8 does not log secret values.")


def main() -> None:
    if not config.telegram_token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in your cloud service environment variables using the complete BotFather token.")
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", config.telegram_token):
        raise RuntimeError("TELEGRAM_BOT_TOKEN is malformed. Copy the complete token from BotFather; do not paste it into chat or GitHub.")
    app = (Application.builder().token(config.telegram_token).post_init(post_init).post_stop(post_stop).post_shutdown(post_shutdown).build())
    handlers = {
        "id": id_cmd, "start": start, "help": help_cmd, "settings": settings, "status": status,
        "setlots": setlots, "setmaxpositions": setmaxpositions, "setsl": setsl, "settp": settp, "setbe": setbe,
        "notifications": notifications, "share_stats": share_stats,
        "paper_enable": paper_enable, "paper_pause": paper_pause, "resume": paper_enable, "pause": paper_pause,
        "markets": markets, "price": price, "scan": scan, "signals": signals,
        "mt5_status": mt5_status, "mt5_symbols": mt5_symbols, "mt5_price": mt5_price, "mt5_scan": mt5_scan,
        "paper_balance": paper_balance, "paper_open": paper_open, "paper_positions": paper_positions,
        "paper_close": paper_close, "paper_check": paper_check, "paper_history": paper_history, "stats": stats,
        "admin_risk": admin_risk, "pause_all": pause_all, "resume_all": resume_all, "users": users_cmd,
        "report_addchat": report_addchat, "report_removechat": report_removechat, "report_chats": report_chats,
        "reportschedule": reportschedule, "report_now": report_now, "announce": announce,
    }
    for command, handler in handlers.items(): app.add_handler(CommandHandler(command, handler))
    app.add_error_handler(error_handler)
    logger.info("Starting 9N8 multi-user paper mode; provider=%s; no broker trade/withdrawal methods loaded", config.mt5_provider)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
