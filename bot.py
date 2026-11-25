"""9N8 Telegram market-signal bot. Alerts only; no order execution."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from signal_engine import Candle, analyse

LOG = logging.getLogger("9n8")
STATE_PATH = Path(os.getenv("N8_STATE_PATH", ".state/state.json"))
TWELVE_BASE = "https://api.twelvedata.com"
TELEGRAM_BASE = "https://api.telegram.org"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_state() -> dict[str, Any]:
    try:
        if STATE_PATH.exists():
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("enabled", False)
                data.setdefault("offset", 0)
                data.setdefault("last_scan_epoch", 0)
                data.setdefault("last_results", {})
                data.setdefault("last_directions", {})
                return data
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("Could not read saved state; starting fresh: %s", exc)
    return {
        "enabled": False,
        "offset": 0,
        "last_scan_epoch": 0,
        "last_results": {},
        "last_directions": {},
    }


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(STATE_PATH)


class Telegram:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.session = requests.Session()
        self.base = f"{TELEGRAM_BASE}/bot{token}"

    def call(self, method: str, payload: dict | None = None) -> dict:
        response = self.session.post(
            f"{self.base}/{method}", json=payload or {}, timeout=20
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body.get('description', 'unknown error')}")
        return body

    def send(self, text: str) -> None:
        # Telegram message limit is 4096 characters; all bot messages are kept short.
        self.call("sendMessage", {
            "chat_id": self.chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

    def updates(self, offset: int) -> list[dict]:
        return self.call("getUpdates", {
            "offset": offset,
            "limit": 100,
            "timeout": 0,
            "allowed_updates": ["message"],
        }).get("result", [])


class TwelveData:
    def __init__(self, api_key: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"apikey {api_key}"})

    def candles(self, symbol: str, interval: str, outputsize: int) -> list[Candle]:
        response = self.session.get(
            f"{TWELVE_BASE}/time_series",
            params={"symbol": symbol, "interval": interval, "outputsize": outputsize, "format": "JSON"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "error" or "values" not in data:
            message = data.get("message", "No candle data returned")
            raise RuntimeError(f"{symbol}: {message}")
        candles: list[Candle] = []
        for row in data["values"]:
            try:
                candles.append(Candle(
                    datetime=str(row["datetime"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]) if row.get("volume") not in (None, "") else None,
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(candles, key=lambda candle: candle.datetime)


def html_safe(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_signal(result: dict) -> str:
    direction = result["direction"]
    badge = "🟢 <b>LONG SETUP</b>" if direction == "LONG" else "🔴 <b>SHORT SETUP</b>"
    return (
        f"{badge}\n"
        f"<b>{html_safe(result['symbol'])}</b> · {html_safe(result['interval'])}\n"
        f"Entry reference: <code>{result['price_text']}</code>\n"
        f"Stop: <code>{result['stop_text']}</code>\n"
        f"Target 1: <code>{result['target_1_text']}</code>\n"
        f"Target 2: <code>{result['target_2_text']}</code>\n"
        f"RSI(14): {result['rsi']:.1f} · EMA20/50 trend aligned\n"
        f"Rule alignment: {result['alignment']}/100 (not a win probability)\n"
        f"Candle: {html_safe(result['candle_time'])}\n"
        "<i>Educational market alert only. Confirm prices and risk independently; no trade was placed.</i>"
    )


def scan_markets(data: TwelveData, symbols: list[str], interval: str, outputsize: int) -> tuple[dict[str, dict], list[str]]:
    results: dict[str, dict] = {}
    errors: list[str] = []
    for symbol in symbols:
        try:
            candles = data.candles(symbol, interval, outputsize)
            results[symbol] = analyse(candles, symbol, interval)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            LOG.warning("Scan failed for %s: %s", symbol, exc)
            errors.append(f"{symbol}: {str(exc)[:180]}")
    return results, errors


def html_safe_message(value: str) -> str:
    return html_safe(value)


def command_text(command: str, argument: str, state: dict, telegram: Telegram,
                 force_scan: list[bool]) -> None:
    if command in ("/start", "/resume"):
        state["enabled"] = True
        telegram.send(
            "<b>9N8 is active.</b>\n"
            "I'll scan the configured watchlist when the scheduled runner executes and send new qualifying setups.\n\n"
            "Use /status, /scan, /signals or /pause."
        )
    elif command == "/pause":
        state["enabled"] = False
        telegram.send("<b>9N8 paused.</b> Scheduled market scans are disabled. Send /resume to restart.")
    elif command == "/status":
        status = "ACTIVE" if state.get("enabled") else "PAUSED"
        last = state.get("last_scan_at") or "No scan completed yet"
        telegram.send(f"<b>9N8 status: {status}</b>\nLast scan: {html_safe(str(last))}")
    elif command == "/help":
        telegram.send(
            "<b>9N8 commands</b>\n"
            "/start or /resume — activate scans\n"
            "/pause — pause scans\n"
            "/status — bot status\n"
            "/scan — request a scan now\n"
            "/signals — show last scan results\n"
            "/watchlist — show configured symbols\n"
            "/help — show this help"
        )
    elif command == "/watchlist":
        symbols = state.get("symbols") or default_symbols()
        telegram.send("<b>Watchlist</b>\n" + "\n".join(f"• {html_safe(s)}" for s in symbols))
    elif command == "/signals":
        latest = state.get("last_results", {})
        if not latest:
            telegram.send("No scan results yet. Activate with /start, then use /scan.")
        else:
            active = [render_signal(item) for item in latest.values() if item.get("direction") != "NONE"]
            if active:
                telegram.send("<b>Latest qualifying setups</b>\n\n" + "\n\n".join(active))
            else:
                telegram.send("The last scan found no qualifying setups. No signal is not a trade recommendation.")
    elif command == "/scan":
        if not state.get("enabled"):
            telegram.send("The bot is paused. Send /resume first, then /scan.")
        else:
            force_scan[0] = True
            telegram.send("Manual scan queued. I'll send the results in this run.")


def default_symbols() -> list[str]:
    raw = os.getenv("N8_SYMBOLS", "").strip() or "AAPL,MSFT,EUR/USD,BTC/USD,ETH/USD"
    return [symbol.strip().upper() for symbol in raw.split(",") if symbol.strip()][:20]


def process_updates(telegram: Telegram, state: dict) -> bool:
    force_scan = [False]
    try:
        updates = telegram.updates(int(state.get("offset", 0)))
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        LOG.error("Could not fetch Telegram updates: %s", exc)
        return False

    changed = False
    for update in updates:
        update_id = int(update.get("update_id", 0))
        state["offset"] = max(int(state.get("offset", 0)), update_id + 1)
        changed = True
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id", "")) != telegram.chat_id:
            continue
        text = str(message.get("text", "")).strip()
        if not text.startswith("/"):
            continue
        parts = text.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        command_text(command, argument, state, telegram, force_scan)
    state["_force_scan"] = force_scan[0]
    return changed


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    token = required_env("TELEGRAM_BOT_TOKEN")
    chat_id = required_env("TELEGRAM_CHAT_ID")
    api_key = required_env("TWELVE_DATA_API_KEY")
    interval = os.getenv("N8_INTERVAL", "15min").strip()
    outputsize = max(60, min(int(os.getenv("N8_OUTPUTSIZE", "150")), 5000))
    scan_every = max(5, int(os.getenv("N8_SCAN_EVERY_MINUTES", "15")))
    symbols = default_symbols()

    telegram = Telegram(token, chat_id)
    state = load_state()
    if not state.get("symbols"):
        state["symbols"] = symbols

    process_updates(telegram, state)
    force_scan = bool(state.pop("_force_scan", False))
    now_epoch = int(utc_now().timestamp())
    due = now_epoch - int(state.get("last_scan_epoch", 0)) >= scan_every * 60

    if state.get("enabled") and (due or force_scan):
        market_data = TwelveData(api_key)
        results, errors = scan_markets(market_data, state.get("symbols", symbols), interval, outputsize)
        state["last_scan_epoch"] = now_epoch
        state["last_scan_at"] = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
        state["last_results"] = results
        last_directions = state.setdefault("last_directions", {})
        for symbol, result in results.items():
            direction = result.get("direction", "NONE")
            previous = last_directions.get(symbol, "NONE")
            if direction in ("LONG", "SHORT") and direction != previous:
                try:
                    telegram.send(render_signal(result))
                except (requests.RequestException, RuntimeError) as exc:
                    LOG.error("Could not send signal for %s: %s", symbol, exc)
            last_directions[symbol] = direction

        if force_scan:
            active = [render_signal(result) for result in results.values() if result.get("direction") != "NONE"]
            summary = "<b>Manual scan completed.</b>\n"
            summary += "\n\n".join(active) if active else "No qualifying setups found in the current watchlist."
            if errors:
                summary += "\n\n<b>Data issues</b>\n" + "\n".join(f"• {html_safe(error)}" for error in errors[:5])
            telegram.send(summary)
        elif errors:
            LOG.warning("Some symbols could not be scanned: %s", "; ".join(errors))
    else:
        LOG.info("Bot paused or scan interval not reached; no market scan performed.")

    save_state(state)
    LOG.info("Run complete. enabled=%s; symbols=%d", state.get("enabled"), len(state.get("symbols", [])))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        LOG.exception("9N8 failed")
        raise
