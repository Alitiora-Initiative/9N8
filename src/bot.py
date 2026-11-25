"""9N8 personal Telegram trading assistant — one owner, OKX spot only.

All trade actions are owner-only and require a second confirmation button in
Telegram. Demo trading is the default. There is no unattended auto-trading,
withdrawal, transfer, futures, margin, or leverage code.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
)

from dotenv import load_dotenv

load_dotenv()

from .okx_provider import OKXClient, OKXError
from .market_data import Candle
from .strategy import analyse

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("9n8")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("TELEGRAM_OWNER_ID", "").strip()
try:
    OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW else 0
except ValueError:
    OWNER_ID = 0

TRADING_MODE = os.getenv("OKX_TRADING_MODE", "demo").strip().lower()
LIVE_ENABLED = os.getenv("OKX_LIVE_TRADING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
OKX_BASE = os.getenv("OKX_API_BASE", "https://www.okx.com").strip().rstrip("/")
try:
    MAX_ORDER_NOTIONAL = Decimal(os.getenv("OKX_MAX_ORDER_NOTIONAL", "10").strip())
except InvalidOperation:
    MAX_ORDER_NOTIONAL = Decimal("10")

if TRADING_MODE not in {"demo", "live"}:
    raise ValueError("OKX_TRADING_MODE must be 'demo' or 'live'.")
if MAX_ORDER_NOTIONAL <= 0:
    raise ValueError("OKX_MAX_ORDER_NOTIONAL must be greater than zero.")
if not OKX_BASE.startswith("https://"):
    raise ValueError("OKX_API_BASE must use HTTPS.")

okx = OKXClient(
    api_key=os.getenv("OKX_API_KEY", ""),
    api_secret=os.getenv("OKX_API_SECRET", ""),
    passphrase=os.getenv("OKX_API_PASSPHRASE", ""),
    base_url=OKX_BASE,
    demo=(TRADING_MODE == "demo"),
)

def _private(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")

async def owner_guard(update: Update) -> bool:
    if not _private(update):
        if update.effective_message:
            await update.effective_message.reply_text("For safety, 9N8 only accepts commands in your private Telegram chat.")
        return False
    if not OWNER_ID:
        await update.effective_message.reply_text(
            "9N8 owner lock is not configured yet. Send /id, put the numeric ID in TELEGRAM_OWNER_ID in your private .env file, then restart the bot."
        )
        return False
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        if update.effective_message:
            await update.effective_message.reply_text("This is a private, owner-only 9N8 bot. Your Telegram account is not authorized.")
        return False
    return True

def _d(value: str, name: str) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a valid number.")
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return result

def _step_ok(value: Decimal, step_text: str) -> bool:
    try:
        step = Decimal(step_text)
        if step <= 0:
            return False
        return value % step == 0
    except (InvalidOperation, ZeroDivisionError):
        return False

def _fmt(value: object, places: int = 8) -> str:
    try:
        number = Decimal(str(value))
        rendered = f"{number:,.{places}f}".rstrip("0").rstrip(".")
        return rendered if rendered not in {"", "-0"} else "0"
    except (InvalidOperation, ValueError):
        return str(value)

def _mode_label() -> str:
    return "DEMO — simulated funds" if TRADING_MODE == "demo" else "LIVE — real funds"

def _assert_execution_enabled() -> None:
    if TRADING_MODE == "live" and not LIVE_ENABLED:
        raise OKXError(
            "Live orders are locked. To deliberately enable them, set "
            "OKX_TRADING_MODE=live and OKX_LIVE_TRADING_ENABLED=true in your private .env, then restart."
        )
    if not okx.credentials_configured:
        raise OKXError(
            "OKX API credentials are missing. Add OKX_API_KEY, OKX_API_SECRET and "
            "OKX_API_PASSPHRASE to your private .env file. Never send them in Telegram."
        )

def _verify_safe_key() -> dict:
    """Fail closed if OKX reports a Withdraw permission on this API key."""
    account = okx.account_config()
    raw_permissions = str(account.get("perm", "")).strip().lower()
    permissions = {part.strip() for part in raw_permissions.split(",") if part.strip()}
    if not permissions:
        raise OKXError("OKX did not return this API key's permission list. Refusing to stage an order.")
    if "withdraw" in permissions:
        raise OKXError(
            "This OKX API key has Withdraw permission. 9N8 refuses to stage orders with that key. "
            "Revoke it at OKX and create a new key with Withdraw turned OFF."
        )
    if "trade" not in permissions:
        raise OKXError(
            "This OKX API key does not report Trade permission. Add Trade permission in OKX API settings, keep Withdraw OFF, and retry."
        )
    return account

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        await update.effective_message.reply_text(
            f"Your Telegram user ID: {user.id}\nUse this number as TELEGRAM_OWNER_ID in your private .env file. Do not share your exchange API credentials."
        )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_guard(update):
        return
    await update.effective_message.reply_text(
        "9N8 private trading assistant is online.\n\n"
        f"Exchange: OKX spot\nTrading environment: {_mode_label()}\n"
        "Every order requires your explicit Telegram confirmation.\n"
        "New orders are not automatically placed by signals.\n\n"
        "Use /help for commands. API keys belong in your local .env file, never in chat."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_guard(update):
        return
    await update.effective_message.reply_text(
        "9N8 — YOUR PRIVATE OKX SPOT BOT\n\n"
        "/okx_status — check API connection and trading mode\n"
        "/balance — show available OKX balances\n"
        "/ticker BTC-USDT — current spot quote\n"
        "/scan BTC-USDT [15m] — EMA 9/21 and RSI 14 observation\n"
        "/order BTC-USDT buy limit 0.001 90000 — stage a limit order\n"
        "/order BTC-USDT buy market 10 — spend 10 quote currency (e.g. USDT)\n"
        "/order BTC-USDT sell market 0.001 — sell 0.001 base currency\n"
        "/openorders — show active spot orders\n"
        "/cancel BTC-USDT ORDER_ID — stage cancellation\n\n"
        "Order rules:\n"
        "• Spot cash only; no futures, leverage or margin.\n"
        "• Each order must be confirmed with a Telegram button.\n"
        f"• Maximum order notional: {MAX_ORDER_NOTIONAL} quote currency units.\n"
        "• Demo trading is the default.\n"
        "• No withdrawals, internal transfers or unattended auto-trading.\n\n"
        "Limit order format: /order SYMBOL buy|sell limit SIZE PRICE\n"
        "Market order format: /order SYMBOL buy|sell market SIZE\n"
        "For market BUY, SIZE is quote currency spend; for market SELL, SIZE is base currency quantity."
    )

async def cmd_okx_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_guard(update):
        return
    if not okx.credentials_configured:
        await update.effective_message.reply_text(
            "OKX connection: NOT CONFIGURED\n"
            f"Environment: {_mode_label()}\n"
            "Missing: OKX_API_KEY, OKX_API_SECRET and/or OKX_API_PASSPHRASE.\n"
            "Add them in your private .env file, not in Telegram."
        )
        return
    try:
        _assert_credentials()
        account = await asyncio.to_thread(okx.account_config)
        balance = await asyncio.to_thread(okx.balance)
        await update.effective_message.reply_text(
            "OKX PRIVATE CONNECTION\n"
            "API status: authenticated\n"
            f"Trading environment: {_mode_label()}\n"
            f"Account level: {account.get('acctLv', 'not returned')}\n"
            f"Position mode: {account.get('posMode', 'not returned')}\n"
            f"API key permissions: {account.get('perm', 'not returned')}\n"
            f"Total equity: {_fmt(balance.get('totalEq', '0'), 4)}\n"
            + ("WARNING: this key has Withdraw permission. Do not trade with it; replace it." if any(part.strip() == "withdraw" for part in str(account.get("perm", "")).lower().split(",")) else "")
            + "\n9N8 does not call withdrawal or transfer endpoints."
        )
    except OKXError as exc:
        await update.effective_message.reply_text(f"OKX connection failed: {exc}")

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_guard(update):
        return
    try:
        _assert_credentials()
        data = await asyncio.to_thread(okx.balance)
        details = data.get("details") or []
        rows = []
        for item in details:
            available = Decimal(str(item.get("availBal") or "0"))
            equity = Decimal(str(item.get("eq") or "0"))
            if available > 0 or equity > 0:
                rows.append(f"{item.get('ccy', '?')}: available {_fmt(available, 8)} | equity {_fmt(equity, 8)}")
        message = (
            "YOUR OKX TRADING ACCOUNT\n"
            f"Total equity: {_fmt(data.get('totalEq', '0'), 4)}\n"
            f"Adjusted equity: {_fmt(data.get('adjEq', '0'), 4)}\n\n"
            + ("\n".join(rows[:25]) if rows else "No non-zero currency balances were returned.")
        )
        await update.effective_message.reply_text(message)
    except OKXError as exc:
        await update.effective_message.reply_text(f"Balance request failed: {exc}")

async def cmd_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_guard(update):
        return
    if len(context.args) != 1 or not re.fullmatch(r"[A-Za-z0-9-]{3,30}", context.args[0]):
        await update.effective_message.reply_text("Usage: /ticker BTC-USDT")
        return
    symbol = context.args[0].upper()
    try:
        item = await asyncio.to_thread(okx.ticker, symbol)
        await update.effective_message.reply_text(
            f"{symbol} — OKX SPOT\n"
            f"Last: {_fmt(item.get('last'))}\n"
            f"Bid: {_fmt(item.get('bidPx'))} | Ask: {_fmt(item.get('askPx'))}\n"
            f"24h high: {_fmt(item.get('high24h'))} | 24h low: {_fmt(item.get('low24h'))}\n"
            f"24h volume: {_fmt(item.get('vol24h'))}"
        )
    except OKXError as exc:
        await update.effective_message.reply_text(f"Ticker request failed: {exc}")

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_guard(update):
        return
    if len(context.args) not in {1, 2} or not re.fullmatch(r"[A-Za-z0-9-]{3,30}", context.args[0]):
        await update.effective_message.reply_text("Usage: /scan BTC-USDT [15m]")
        return
    symbol = context.args[0].upper()
    bar = context.args[1] if len(context.args) == 2 else "15m"
    if bar not in {"1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H", "6H", "12H", "1D", "1W"}:
        await update.effective_message.reply_text("Unsupported candle timeframe.")
        return
    try:
        raw = await asyncio.to_thread(okx.candles, symbol, bar, 100)
        candles = []
        for row in raw:
            if len(row) < 9 or str(row[8]) != "1":
                continue
            stamp = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).isoformat()
            candles.append(Candle(
                datetime=stamp,
                open=float(row[1]), high=float(row[2]), low=float(row[3]),
                close=float(row[4]), volume=float(row[5]),
            ))
        signal = analyse(symbol, candles)
        await update.effective_message.reply_text(
            f"9N8 MARKET OBSERVATION\n{symbol} · {bar}\n"
            f"{signal.label}\nLast close: {_fmt(signal.close)}\n"
            f"EMA 9: {_fmt(signal.ema_fast)}\nEMA 21: {_fmt(signal.ema_slow)}\n"
            f"RSI 14: {_fmt(signal.rsi, 2)}\n"
            f"Candle: {signal.candle_time}\n{signal.explanation}\n\n"
            "Indicator observation only. No order was placed."
        )
    except (OKXError, ValueError) as exc:
        await update.effective_message.reply_text(f"Market scan failed: {exc}")

def _build_order(args: list[str]) -> tuple[dict, Decimal, str]:
    if len(args) not in {4, 5}:
        raise ValueError(
            "Use /order BTC-USDT buy limit SIZE PRICE or /order BTC-USDT buy market QUOTE_AMOUNT."
        )
    inst_id, side, order_type = args[0].upper(), args[1].lower(), args[2].lower()
    if not re.fullmatch(r"[A-Z0-9-]{3,30}", inst_id):
        raise ValueError("Invalid spot instrument format. Example: BTC-USDT.")
    if side not in {"buy", "sell"}:
        raise ValueError("Side must be buy or sell.")
    if order_type not in {"limit", "market"}:
        raise ValueError("Only limit and market spot orders are enabled.")
    if (order_type == "limit" and len(args) != 5) or (order_type == "market" and len(args) != 4):
        raise ValueError("Limit: /order BTC-USDT buy limit SIZE PRICE. Market: /order BTC-USDT buy market QUOTE_AMOUNT.")
    size = _d(args[3], "Size")
    order = {
        "instId": inst_id,
        "tdMode": "cash",
        "side": side,
        "ordType": order_type,
        "sz": str(size),
        "clOrdId": uuid.uuid4().hex,
    }
    notional = Decimal("0")
    details = ""
    if order_type == "limit":
        price = _d(args[4], "Limit price")
        order["px"] = str(price)
        notional = price * size
        details = f"Price: {price}\nBase size: {size}"
    else:
        if side == "buy":
            order["tgtCcy"] = "quote_ccy"
            details = f"Quote spend: {size} (quote currency)"
        else:
            order["tgtCcy"] = "base_ccy"
            details = f"Base quantity: {size}"
    return order, notional, details

async def cmd_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_guard(update):
        return
    try:
        _assert_execution_enabled()
        order, notional, details = _build_order(context.args)
        await asyncio.to_thread(_verify_safe_key)
        instrument, ticker = await asyncio.gather(
            asyncio.to_thread(okx.instrument, order["instId"]),
            asyncio.to_thread(okx.ticker, order["instId"]),
        )
        lot_step = str(instrument.get("lotSz", "0"))
        minimum = Decimal(str(instrument.get("minSz", "0")))
        size = Decimal(order["sz"])
        if not (order["ordType"] == "market" and order["side"] == "buy"):
            if not _step_ok(size, lot_step):
                raise ValueError(f"Size must follow the exchange lot step: {lot_step}.")
            if size < minimum:
                raise ValueError(f"Size is below the exchange minimum: {minimum}.")
        if order["ordType"] == "limit":
            tick_step = str(instrument.get("tickSz", "0"))
            price = Decimal(order["px"])
            if not _step_ok(price, tick_step):
                raise ValueError(f"Limit price must follow the exchange tick size: {tick_step}.")
        elif order["side"] == "buy":
            notional = size
            ask = Decimal(str(ticker.get("askPx") or "0"))
            if ask <= 0 or size / ask < minimum:
                raise ValueError(f"Quote spend is below the estimated minimum order size for {order['instId']}.")
        else:
            last = Decimal(str(ticker.get("last") or "0"))
            notional = size * last
        if notional <= 0:
            raise ValueError("Could not calculate a positive order notional.")
        if notional > MAX_ORDER_NOTIONAL:
            raise ValueError(
                f"Order notional {_fmt(notional, 4)} exceeds your configured cap of "
                f"{_fmt(MAX_ORDER_NOTIONAL, 4)}. Change OKX_MAX_ORDER_NOTIONAL in .env only after reviewing your risk."
            )
        if order["ordType"] == "market":
            display_details = details + f"\nApprox. notional: {_fmt(notional, 4)}"
        else:
            display_details = details + f"\nNotional: {_fmt(notional, 4)}"
        if TRADING_MODE == "live" and not LIVE_ENABLED:
            raise OKXError("Live order confirmation is locked. Set both live environment switches deliberately, then restart.")
        nonce = uuid.uuid4().hex[:20]
        context.user_data["pending_okx_action"] = {
            "nonce": nonce,
            "kind": "order",
            "order": order,
            "expires": time.time() + 90,
        }
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Confirm order", callback_data=f"okx_confirm:{nonce}"),
            InlineKeyboardButton("Cancel", callback_data=f"okx_cancel:{nonce}"),
        ]])
        await update.effective_message.reply_text(
            "REVIEW OKX SPOT ORDER — NOT SENT YET\n"
            f"Environment: {_mode_label()}\n"
            f"Instrument: {order['instId']}\nSide: {order['side'].upper()}\n"
            f"Type: {order['ordType'].upper()}\n{display_details}\n"
            f"Configured maximum: {_fmt(MAX_ORDER_NOTIONAL, 4)} quote units\n"
            "No leverage or margin. This order has no attached stop-loss/take-profit.\n"
            "It will only be submitted if you press Confirm within 90 seconds.",
            reply_markup=keyboard,
        )
    except (ValueError, OKXError) as exc:
        await update.effective_message.reply_text(f"Order not staged: {exc}")

async def cmd_open_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_guard(update):
        return
    try:
        _assert_credentials()
        inst_id = context.args[0].upper() if context.args else None
        orders = await asyncio.to_thread(okx.open_orders, inst_id)
        if not orders:
            await update.effective_message.reply_text("No active OKX spot orders.")
            return
        lines = []
        for item in orders[:20]:
            lines.append(
                f"{item.get('instId')} {str(item.get('side', '')).upper()} {item.get('ordType')} "
                f"size {item.get('sz')} price {item.get('px') or 'market'}\n"
                f"Order ID: {item.get('ordId')} | State: {item.get('state')}"
            )
        await update.effective_message.reply_text("YOUR ACTIVE OKX SPOT ORDERS\n\n" + "\n\n".join(lines))
    except OKXError as exc:
        await update.effective_message.reply_text(f"Could not read open orders: {exc}")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_guard(update):
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text("Usage: /cancel BTC-USDT ORDER_ID")
        return
    inst_id, order_id = context.args[0].upper(), context.args[1]
    if not re.fullmatch(r"[A-Z0-9-]{3,30}", inst_id) or not order_id.isdigit():
        await update.effective_message.reply_text("Instrument or order ID is invalid.")
        return
    nonce = uuid.uuid4().hex[:20]
    context.user_data["pending_okx_action"] = {
        "nonce": nonce, "kind": "cancel", "inst_id": inst_id,
        "order_id": order_id, "expires": time.time() + 90,
    }
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirm cancellation", callback_data=f"okx_confirm:{nonce}"),
        InlineKeyboardButton("Keep order", callback_data=f"okx_cancel:{nonce}"),
    ]])
    await update.effective_message.reply_text(
        f"REVIEW CANCELLATION\n{inst_id}\nOrder ID: {order_id}\n"
        "The order has not been cancelled yet. Confirm within 90 seconds.",
        reply_markup=keyboard,
    )

async def callback_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not _private(update) or not OWNER_ID or query.from_user.id != OWNER_ID:
        await query.edit_message_text("Unauthorized. This bot is restricted to its owner.")
        return
    action = context.user_data.get("pending_okx_action")
    data = query.data or ""
    try:
        prefix, nonce = data.split(":", 1)
    except ValueError:
        await query.edit_message_text("Invalid confirmation.")
        return
    if not action or action.get("nonce") != nonce:
        await query.edit_message_text("This confirmation is no longer available. Stage the action again.")
        return
    if time.time() > float(action.get("expires", 0)):
        context.user_data.pop("pending_okx_action", None)
        await query.edit_message_text("Confirmation expired. No exchange action was submitted.")
        return
    if prefix == "okx_cancel":
        context.user_data.pop("pending_okx_action", None)
        await query.edit_message_text("Cancelled. No exchange request was sent.")
        return
    if prefix != "okx_confirm":
        await query.edit_message_text("Unknown confirmation action.")
        return
    try:
        _assert_credentials()
        if action["kind"] == "order":
            _assert_execution_enabled()
            await asyncio.to_thread(_verify_safe_key)
            result = await asyncio.to_thread(okx.place_spot_order, action["order"])
            message = (
                "OKX ORDER ACCEPTED\n"
                f"Environment: {_mode_label()}\n"
                f"Instrument: {action['order']['instId']}\n"
                f"Side: {action['order']['side'].upper()}\n"
                f"Order ID: {result.get('ordId', 'not returned')}\n"
                f"Client order ID: {result.get('clOrdId', action['order'].get('clOrdId'))}\n"
                f"Status: {result.get('sCode', 'accepted')}\n"
                "Acceptance does not guarantee a fill. Check /openorders for status."
            )
        elif action["kind"] == "cancel":
            result = await asyncio.to_thread(okx.cancel_spot_order, action["inst_id"], action["order_id"])
            message = (
                "OKX CANCELLATION ACCEPTED\n"
                f"Instrument: {action['inst_id']}\n"
                f"Order ID: {result.get('ordId', action['order_id'])}\n"
                "Check /openorders to confirm final state."
            )
        else:
            raise OKXError("Unknown pending action type.")
        context.user_data.pop("pending_okx_action", None)
        await query.edit_message_text(message)
    except (OKXError, ValueError) as exc:
        context.user_data.pop("pending_okx_action", None)
        await query.edit_message_text(f"Exchange action failed: {exc}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled bot error: %s", type(context.error).__name__ if context.error else "unknown")
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("An internal error occurred. No credentials are written to the logs.")
        except Exception:
            pass

def main() -> None:
    if not TELEGRAM_TOKEN or not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", TELEGRAM_TOKEN):
        raise RuntimeError("Set a valid TELEGRAM_BOT_TOKEN in your private .env file.")
    if not OWNER_ID:
        logger.warning("TELEGRAM_OWNER_ID is not set. Only /id will work until the owner lock is configured.")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    handlers = {
        "id": cmd_id,
        "start": cmd_start,
        "help": cmd_help,
        "okx_status": cmd_okx_status,
        "balance": cmd_balance,
        "ticker": cmd_ticker,
        "price": cmd_ticker,
        "scan": cmd_scan,
        "order": cmd_order,
        "openorders": cmd_open_orders,
        "cancel": cmd_cancel,
    }
    for name, handler in handlers.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(callback_order, pattern=r"^okx_(confirm|cancel):"))
    app.add_error_handler(error_handler)
    logger.info(
        "Starting private 9N8 bot; OKX mode=%s; owner lock=%s; spot-only; per-order confirmation enabled",
        TRADING_MODE, "configured" if OWNER_ID else "not configured",
    )
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        okx.close()

if __name__ == "__main__":
    main()
