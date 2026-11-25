"""Telegram commands and background worker for 9N8 live-data paper trading."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Application,
)

from .market_data import Candle
from .paper_trading import PaperConfig, PaperTradingEngine, PaperTradingError
from .strategy_engine import evaluate_strategy, screen_market

logger = logging.getLogger("9n8.paper")


def _env_decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default).strip()
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise RuntimeError(f"{name} must be a valid decimal number.") from exc
    if not value.is_finite():
        raise RuntimeError(f"{name} must be finite.")
    return value


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    return max(minimum, min(maximum, value))


PAPER_DB_PATH = os.getenv("PAPER_DB_PATH", "data/9n8_paper.sqlite3").strip()
PAPER_QUOTE_CURRENCY = os.getenv("PAPER_QUOTE_CURRENCY", "USDT").strip().upper()
PAPER_TIMEFRAME = os.getenv("PAPER_TIMEFRAME", "5m").strip()
PAPER_WATCHLIST = [
    item.strip().upper()
    for item in os.getenv("PAPER_WATCHLIST", "BTC-USDT,ETH-USDT,SOL-USDT").split(",")
    if re.fullmatch(r"[A-Z0-9-]{3,30}", item.strip().upper())
]
PAPER_MIN_24H_QUOTE_VOLUME = float(_env_decimal("PAPER_MIN_24H_QUOTE_VOLUME", "50000"))
PAPER_MAX_SPREAD_BPS = float(_env_decimal("PAPER_MAX_SPREAD_BPS", "80"))
PAPER_MAX_QUOTE_AGE_SECONDS = float(_env_decimal("PAPER_MAX_QUOTE_AGE_SECONDS", "20"))
PAPER_SCAN_INTERVAL_SECONDS = _env_int("PAPER_SCAN_INTERVAL_SECONDS", 60, 30, 3600)
PAPER_MONITOR_INTERVAL_SECONDS = _env_int("PAPER_MONITOR_INTERVAL_SECONDS", 5, 3, 60)

paper = PaperTradingEngine(
    PAPER_DB_PATH,
    PaperConfig(
        initial_balance=_env_decimal("PAPER_INITIAL_BALANCE", "1000"),
        fee_rate=_env_decimal("PAPER_FEE_RATE", "0.001"),
        slippage_bps=_env_decimal("PAPER_SLIPPAGE_BPS", "5"),
        risk_per_trade_pct=_env_decimal("PAPER_RISK_PER_TRADE_PCT", "1"),
        max_position_pct=_env_decimal("PAPER_MAX_POSITION_PCT", "20"),
        daily_loss_limit_pct=_env_decimal("PAPER_DAILY_LOSS_LIMIT_PCT", "3"),
        hard_stop_loss_pct=_env_decimal("PAPER_HARD_STOP_LOSS_PCT", "5"),
        tp1_target_pct=_env_decimal("PAPER_TP1_TARGET_PCT", "10"),
        tp1_close_pct=_env_decimal("PAPER_TP1_CLOSE_PCT", "50"),
        trailing_activation_pct=_env_decimal("PAPER_TRAILING_ACTIVATION_PCT", "8"),
        trailing_stop_pct=_env_decimal("PAPER_TRAILING_STOP_PCT", "4"),
        max_hold_minutes=_env_int("PAPER_MAX_HOLD_MINUTES", 240, 1, 10080),
        stagnation_profit_pct=_env_decimal("PAPER_STAGNATION_PROFIT_PCT", "1"),
        max_open_positions=_env_int("PAPER_MAX_OPEN_POSITIONS", 3, 1, 20),
        quote_currency=PAPER_QUOTE_CURRENCY,
    ),
)


def _owner(update: Update, owner_id: int) -> bool:
    return bool(
        owner_id
        and update.effective_user
        and update.effective_user.id == owner_id
        and update.effective_chat
        and update.effective_chat.type == "private"
    )


async def _owner_guard(update: Update, owner_id: int) -> bool:
    if _owner(update, owner_id):
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            "This is a private 9N8 control. Use the bot in its owner's private Telegram chat."
        )
    return False


def _fmt(value: Any, places: int = 4) -> str:
    try:
        number = Decimal(str(value))
        return f"{number:,.{places}f}".rstrip("0").rstrip(".")
    except (InvalidOperation, ValueError, TypeError):
        return str(value)


def _parse_candles(rows: list[list[str]]) -> list[Candle]:
    parsed: list[Candle] = []
    for row in rows:
        if len(row) < 9 or str(row[8]) != "1":
            continue
        try:
            stamp = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).isoformat()
            parsed.append(
                Candle(
                    datetime=stamp,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        except (ValueError, TypeError, OverflowError):
            continue
    return parsed


def _fresh_quotes(quotes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    fresh: dict[str, dict[str, Any]] = {}
    for symbol, quote in quotes.items():
        try:
            age = (now_ms - int(quote.get("ts") or 0)) / 1000
        except (TypeError, ValueError):
            continue
        if 0 <= age <= PAPER_MAX_QUOTE_AGE_SECONDS:
            fresh[symbol.upper()] = quote
    return fresh


def _dashboard_text() -> str:
    status = paper.summary()
    state = "🟢 AUTO PAPER SCAN ACTIVE" if status["enabled"] else "⏸️ AUTO PAPER SCAN PAUSED"
    reason = f"\nRisk lock: {status['paused_reason']}" if status["paused_reason"] else ""
    return (
        "9N8 · PAPER TRADING\n"
        f"Market data: LIVE OKX spot quotes + candles\n"
        f"Engine: {state}\n"
        f"Strategy: {status['strategy'].upper()}\n"
        f"Virtual equity: {_fmt(status['equity'])} {status['quote_currency']}\n"
        f"Available paper cash: {_fmt(status['cash_balance'])} {status['quote_currency']}\n"
        f"Realized P&L: {_fmt(status['realized_pnl'])} {status['quote_currency']}\n"
        f"Unrealized P&L: {_fmt(status['unrealized_pnl'])} {status['quote_currency']}\n"
        f"Daily drawdown: {_fmt(status['daily_drawdown_pct'], 2)}% / {_fmt(status['daily_loss_limit_pct'], 2)}%\n"
        f"Open positions: {status['open_positions']}\n"
        "Execution: SIMULATED ONLY — no exchange orders are sent."
        f"{reason}"
    )


def _dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("▶️ Start auto paper", callback_data="paper:start"),
                InlineKeyboardButton("⏸ Pause", callback_data="paper:pause"),
            ],
            [
                InlineKeyboardButton("💰 Paper balance", callback_data="paper:balance"),
                InlineKeyboardButton("📊 Open positions", callback_data="paper:positions"),
            ],
            [
                InlineKeyboardButton("🔎 Scan watchlist", callback_data="paper:scan"),
                InlineKeyboardButton("🔄 Refresh", callback_data="paper:refresh"),
            ],
        ]
    )


async def cmd_paper(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_id: int) -> None:
    if not await _owner_guard(update, owner_id):
        return
    await update.effective_message.reply_text(_dashboard_text(), reply_markup=_dashboard_keyboard())


async def cmd_paper_start(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_id: int) -> None:
    if not await _owner_guard(update, owner_id):
        return
    try:
        await asyncio.to_thread(paper.set_enabled, True)
        await update.effective_message.reply_text(
            "Automatic PAPER scanning is active. 9N8 may open virtual positions from live OKX market data; it cannot place live exchange orders from this engine."
        )
    except PaperTradingError as exc:
        await update.effective_message.reply_text(str(exc))


async def cmd_paper_pause(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_id: int) -> None:
    if not await _owner_guard(update, owner_id):
        return
    await asyncio.to_thread(paper.set_enabled, False)
    await update.effective_message.reply_text(
        "Automatic paper entries are paused. Existing paper positions continue to be monitored for simulated exits."
    )


async def cmd_paper_balance(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_id: int) -> None:
    if not await _owner_guard(update, owner_id):
        return
    await update.effective_message.reply_text(_dashboard_text())


async def cmd_paper_positions(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_id: int) -> None:
    if not await _owner_guard(update, owner_id):
        return
    positions = await asyncio.to_thread(paper.positions)
    if not positions:
        await update.effective_message.reply_text("No open paper positions.")
        return
    lines = []
    for item in positions:
        entry = Decimal(item["entry_price"])
        mark = Decimal(item["last_price"])
        quantity = Decimal(item["quantity"])
        pnl_pct = (mark - entry) / entry * Decimal("100")
        lines.append(
            f"Ticket: {item['ticket']} · {item['symbol']}\n"
            f"Quantity: {_fmt(quantity, 8)}\n"
            f"Entry: {_fmt(entry, 8)} · Last mark: {_fmt(mark, 8)}\n"
            f"Price P&L: {_fmt(pnl_pct, 2)}%\n"
            f"TP1: {'hit' if item['tp1_hit'] else 'waiting'}"
        )
    await update.effective_message.reply_text("OPEN PAPER POSITIONS\n\n" + "\n\n".join(lines))


async def cmd_paper_history(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_id: int) -> None:
    if not await _owner_guard(update, owner_id):
        return
    trades = await asyncio.to_thread(paper.history, 10)
    if not trades:
        await update.effective_message.reply_text("No closed paper trades yet.")
        return
    lines = [
        f"{row['symbol']} · {row['reason']} · net {_fmt(row['net_pnl'])} {PAPER_QUOTE_CURRENCY}"
        for row in trades
    ]
    await update.effective_message.reply_text("RECENT PAPER FILLS\n" + "\n".join(lines))


async def cmd_paper_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_id: int) -> None:
    if not await _owner_guard(update, owner_id):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text(
            "Usage: /paper_strategy momentum|trend|mean_reversion|grid\n"
            "The selected rules only control automatic PAPER entries."
        )
        return
    try:
        await asyncio.to_thread(paper.set_strategy, context.args[0])
        await update.effective_message.reply_text(
            f"Paper strategy set to {paper.strategy().upper()}. No live order was placed."
        )
    except PaperTradingError as exc:
        await update.effective_message.reply_text(str(exc))


async def cmd_paper_trade(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_id: int, okx: Any) -> None:
    if not await _owner_guard(update, owner_id):
        return
    if len(context.args) != 2 or not re.fullmatch(r"[A-Za-z0-9-]{3,30}", context.args[0]):
        await update.effective_message.reply_text("Usage: /paper_trade BTC-USDT 20  (quote-currency budget)")
        return
    symbol = context.args[0].upper()
    try:
        budget = Decimal(context.args[1])
        if not budget.is_finite() or budget <= 0:
            raise ValueError
        ticker = (await asyncio.to_thread(okx.ticker, symbol))
        quality = screen_market(
            ticker,
            min_quote_volume_24h=PAPER_MIN_24H_QUOTE_VOLUME,
            max_spread_bps=PAPER_MAX_SPREAD_BPS,
            max_quote_age_seconds=PAPER_MAX_QUOTE_AGE_SECONDS,
        )
        if not quality.accepted:
            raise PaperTradingError(f"Market filter rejected {symbol}: {quality.reason}")
        await asyncio.to_thread(okx.instrument, symbol)
        result = await asyncio.to_thread(
            paper.open_position,
            symbol,
            budget,
            bid=ticker.get("bidPx"),
            ask=ticker.get("askPx"),
            reason="MANUAL_PAPER_ENTRY",
        )
        await update.effective_message.reply_text(
            "PAPER BUY OPENED — live market quote, virtual funds\n"
            f"Market: {symbol}\nTicket: {result['ticket']}\n"
            f"Entry fill: {_fmt(result['entry_price'], 8)}\n"
            f"Quantity: {_fmt(result['quantity'], 8)}\n"
            f"Estimated entry fee: {_fmt(result['entry_fee'])} {PAPER_QUOTE_CURRENCY}\n"
            f"Spread: {_fmt(quality.spread_bps, 2)} bps\n"
            f"Virtual spend incl. fee: {_fmt(result['quote_spent'])} {PAPER_QUOTE_CURRENCY}\n"
            "No exchange order was submitted."
        )
    except (ValueError, PaperTradingError) as exc:
        await update.effective_message.reply_text(f"Paper entry rejected: {exc}")
    except Exception as exc:
        logger.warning("Manual paper entry failed (%s)", type(exc).__name__)
        await update.effective_message.reply_text("Paper entry failed while reading live market data. No exchange order was sent.")


async def cmd_paper_close(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_id: int, okx: Any) -> None:
    if not await _owner_guard(update, owner_id):
        return
    if len(context.args) != 1 or not re.fullmatch(r"[A-Fa-f0-9]{8,16}", context.args[0]):
        await update.effective_message.reply_text("Usage: /paper_close TICKET")
        return
    ticket = context.args[0].upper()
    try:
        position = next((p for p in await asyncio.to_thread(paper.positions) if p["ticket"] == ticket), None)
        if not position:
            raise PaperTradingError("Open paper ticket not found.")
        ticker = await asyncio.to_thread(okx.ticker, position["symbol"])
        quality = screen_market(
            ticker,
            min_quote_volume_24h=0,
            max_spread_bps=10000,
            max_quote_age_seconds=PAPER_MAX_QUOTE_AGE_SECONDS,
        )
        if not quality.accepted:
            raise PaperTradingError(f"Cannot close using a stale or invalid quote: {quality.reason}")
        result = await asyncio.to_thread(
            paper.close_position,
            ticket,
            bid=ticker.get("bidPx"),
            reason="MANUAL_PAPER_CLOSE",
        )
        await update.effective_message.reply_text(
            f"PAPER POSITION CLOSED · {result['symbol']}\n"
            f"Ticket: {ticket}\nExit: {_fmt(result['exit_price'], 8)}\n"
            f"Net realized P&L: {_fmt(result['net_pnl'])} {PAPER_QUOTE_CURRENCY}\n"
            "Simulated fill only; no exchange order was sent."
        )
    except PaperTradingError as exc:
        await update.effective_message.reply_text(f"Paper close rejected: {exc}")
    except Exception as exc:
        logger.warning("Manual paper close failed (%s)", type(exc).__name__)
        await update.effective_message.reply_text("Could not read a fresh quote. Paper position remains open.")


async def _scan_once(application: Application, okx: Any, owner_id: int, *, allow_entries: bool) -> list[str]:
    if not PAPER_WATCHLIST:
        return ["Watchlist is empty; set PAPER_WATCHLIST in .env."]
    symbols = list(dict.fromkeys(PAPER_WATCHLIST))
    quotes = _fresh_quotes(await asyncio.to_thread(okx.tickers, symbols))
    open_symbols = {str(item["symbol"]).upper() for item in await asyncio.to_thread(paper.positions)}
    messages: list[str] = []
    for symbol in symbols:
        if symbol in open_symbols:
            continue
        ticker = quotes.get(symbol)
        if not ticker:
            messages.append(f"{symbol}: no fresh OKX ticker.")
            continue
        quality = screen_market(
            ticker,
            min_quote_volume_24h=PAPER_MIN_24H_QUOTE_VOLUME,
            max_spread_bps=PAPER_MAX_SPREAD_BPS,
            max_quote_age_seconds=PAPER_MAX_QUOTE_AGE_SECONDS,
        )
        if not quality.accepted:
            messages.append(f"{symbol}: filtered — {quality.reason}")
            continue
        try:
            await asyncio.to_thread(okx.instrument, symbol)
            raw = await asyncio.to_thread(okx.candles, symbol, PAPER_TIMEFRAME, 100)
            candles = _parse_candles(raw)
            signal = evaluate_strategy(paper.strategy(), candles)
            if not signal.eligible:
                messages.append(f"{symbol}: no setup — {signal.reason}")
                continue
            if not allow_entries:
                messages.append(f"{symbol}: {signal.reason} (scanner paused; no entry).")
                continue
            budget = await asyncio.to_thread(paper.suggest_quote_budget)
            if budget <= 0:
                messages.append(f"{symbol}: risk sizing returned zero; no entry.")
                continue
            position = await asyncio.to_thread(
                paper.open_position,
                symbol,
                budget,
                bid=ticker.get("bidPx"),
                ask=ticker.get("askPx"),
                reason=f"AUTO_{paper.strategy().upper()}: {signal.reason}",
            )
            messages.append(
                f"{symbol}: PAPER BUY opened · ticket {position['ticket']} · "
                f"budget {_fmt(position['quote_spent'])} {PAPER_QUOTE_CURRENCY}."
            )
            await application.bot.send_message(
                chat_id=owner_id,
                text=(
                    "9N8 AUTO PAPER ENTRY\n"
                    f"{symbol} · {paper.strategy().upper()}\n"
                    f"Ticket: {position['ticket']}\n"
                    f"Entry: {_fmt(position['entry_price'], 8)}\n"
                    f"Quantity: {_fmt(position['quantity'], 8)}\n"
                    f"Virtual spend: {_fmt(position['quote_spent'])} {PAPER_QUOTE_CURRENCY}\n"
                    "Live market data; virtual funds only."
                ),
            )
        except PaperTradingError as exc:
            messages.append(f"{symbol}: entry rejected — {exc}")
        except Exception as exc:
            logger.warning("Paper scan for %s failed (%s)", symbol, type(exc).__name__)
            messages.append(f"{symbol}: scan failed; no order was sent.")
    return messages


async def cmd_paper_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_id: int, okx: Any) -> None:
    if not await _owner_guard(update, owner_id):
        return
    await update.effective_message.reply_text("Scanning the watchlist using current OKX candles and ticker data…")
    try:
        messages = await _scan_once(
            context.application,
            okx,
            owner_id,
            allow_entries=paper.is_enabled(),
        )
        await update.effective_message.reply_text("PAPER SCAN\n" + "\n".join(messages[:12]))
    except Exception as exc:
        logger.warning("Manual paper scan failed (%s)", type(exc).__name__)
        await update.effective_message.reply_text("Scan failed while reading market data. No live order was sent.")


async def _callback(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_id: int, okx: Any) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not _owner(update, owner_id):
        await query.edit_message_text("Unauthorized. 9N8 is owner-only.")
        return
    action = (query.data or "").split(":", 1)[-1]
    try:
        if action == "start":
            await asyncio.to_thread(paper.set_enabled, True)
            text = _dashboard_text()
        elif action == "pause":
            await asyncio.to_thread(paper.set_enabled, False)
            text = _dashboard_text()
        elif action == "balance" or action == "refresh":
            text = _dashboard_text()
        elif action == "positions":
            positions = await asyncio.to_thread(paper.positions)
            text = "OPEN PAPER POSITIONS\n" + (
                "\n\n".join(
                    f"{p['ticket']} · {p['symbol']} · qty {_fmt(p['quantity'], 8)} · "
                    f"entry {_fmt(p['entry_price'], 8)} · mark {_fmt(p['last_price'], 8)}"
                    for p in positions
                ) if positions else "No open positions."
            )
        elif action == "scan":
            lines = await _scan_once(
                context.application, okx, owner_id, allow_entries=paper.is_enabled()
            )
            text = "PAPER SCAN\n" + "\n".join(lines[:12])
        else:
            text = "Unknown paper control."
        await query.edit_message_text(text, reply_markup=_dashboard_keyboard())
    except PaperTradingError as exc:
        await query.edit_message_text(f"Paper control rejected: {exc}", reply_markup=_dashboard_keyboard())
    except Exception as exc:
        logger.warning("Paper control callback failed (%s)", type(exc).__name__)
        await query.edit_message_text(
            "Paper control failed while reading market data. No live order was sent.",
            reply_markup=_dashboard_keyboard(),
        )


async def paper_worker(application: Application, okx: Any, owner_id: int) -> None:
    """Monitor paper exits continuously and scan entries only after explicit /paper_start."""
    last_scan = 0.0
    while True:
        try:
            open_positions = await asyncio.to_thread(paper.positions)
            symbols = [str(item["symbol"]).upper() for item in open_positions]
            if paper.is_enabled():
                symbols.extend(PAPER_WATCHLIST)
            if symbols:
                quotes = _fresh_quotes(
                    await asyncio.to_thread(okx.tickers, list(dict.fromkeys(symbols)))
                )
                events = await asyncio.to_thread(paper.update_prices, quotes)
                for event in events:
                    await application.bot.send_message(
                        chat_id=owner_id,
                        text=(
                            "9N8 PAPER EXIT\n"
                            f"{event['symbol']} · {event['reason']}\n"
                            f"Ticket: {event['ticket']}\n"
                            f"Quantity sold: {_fmt(event['quantity'], 8)}\n"
                            f"Exit price: {_fmt(event['exit_price'], 8)}\n"
                            f"Net realized P&L: {_fmt(event['net_pnl'])} {PAPER_QUOTE_CURRENCY}\n"
                            f"Remaining quantity: {_fmt(event['remaining_quantity'], 8)}\n"
                            "Simulated exit using live OKX quotes; no exchange order was sent."
                        ),
                    )
            now = time.monotonic()
            if paper.is_enabled() and now - last_scan >= PAPER_SCAN_INTERVAL_SECONDS:
                lines = await _scan_once(application, okx, owner_id, allow_entries=True)
                logger.info("Paper scan completed for %d watchlist symbols.", len(lines))
                last_scan = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Paper worker cycle failed (%s)", type(exc).__name__)
        await asyncio.sleep(PAPER_MONITOR_INTERVAL_SECONDS)


def paper_post_init(okx: Any, owner_id: int):
    async def _start(application: Application) -> None:
        if owner_id:
            application.create_task(
                paper_worker(application, okx, owner_id),
                name="9n8-paper-market-monitor",
            )
    return _start


def register_paper_handlers(application: Application, okx: Any, owner_id: int) -> None:
    application.add_handler(CommandHandler("paper", lambda u, c: cmd_paper(u, c, owner_id)))
    application.add_handler(CommandHandler("paper_start", lambda u, c: cmd_paper_start(u, c, owner_id)))
    application.add_handler(CommandHandler("paper_pause", lambda u, c: cmd_paper_pause(u, c, owner_id)))
    application.add_handler(CommandHandler("paper_balance", lambda u, c: cmd_paper_balance(u, c, owner_id)))
    application.add_handler(CommandHandler("paper_positions", lambda u, c: cmd_paper_positions(u, c, owner_id)))
    application.add_handler(CommandHandler("paper_history", lambda u, c: cmd_paper_history(u, c, owner_id)))
    application.add_handler(CommandHandler("paper_strategy", lambda u, c: cmd_paper_strategy(u, c, owner_id)))
    application.add_handler(CommandHandler("paper_trade", lambda u, c: cmd_paper_trade(u, c, owner_id, okx)))
    application.add_handler(CommandHandler("paper_close", lambda u, c: cmd_paper_close(u, c, owner_id, okx)))
    application.add_handler(CommandHandler("paper_scan", lambda u, c: cmd_paper_scan(u, c, owner_id, okx)))
    application.add_handler(
        CallbackQueryHandler(
            lambda u, c: _callback(u, c, owner_id, okx),
            pattern=r"^paper:(start|pause|balance|positions|scan|refresh)$",
        )
    )
