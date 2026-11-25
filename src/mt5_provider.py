"""Read-only adapter for the official MetaTrader 5 Python integration.

9N8 calls only terminal/account metadata, symbol metadata, ticks and candle
history. It deliberately never calls order_send or any broker-side trade API.
A DEMO account is required even for this initial paper-only release.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
import platform
from typing import Any

from .market_data import Candle


class MT5Error(RuntimeError):
    """Safe, user-facing MT5 connection or market-data error."""


@dataclass(frozen=True)
class MT5AccountSummary:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    trade_mode: int


@dataclass(frozen=True)
class MT5Tick:
    symbol: str
    bid: float
    ask: float
    time_utc: str


@dataclass(frozen=True)
class MT5SymbolInfo:
    name: str
    digits: int
    point: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int
    currency_profit: str


_TIMEFRAME_NAMES = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}


class MT5Provider:
    """Read-only MT5 market-data access, restricted to a verified demo account."""

    def __init__(self, terminal_path: str = "", mt5_module: Any | None = None):
        self.terminal_path = terminal_path.strip()
        self._mt5 = mt5_module
        self._connected = False
        self._account: MT5AccountSummary | None = None

    def _module(self):
        if self._mt5 is None:
            if platform.system() != "Windows":
                raise MT5Error(
                    "The official MetaTrader5 Python package requires Windows. "
                    "Run 9N8 on the Windows computer where MT5 desktop is installed."
                )
            try:
                self._mt5 = importlib.import_module("MetaTrader5")
            except ImportError as exc:
                raise MT5Error(
                    "MetaTrader5 package is missing. Run: pip install -r requirements.txt"
                ) from exc
        return self._mt5

    def connect(self) -> MT5AccountSummary:
        mt5 = self._module()
        if not self._connected:
            try:
                ok = mt5.initialize(self.terminal_path) if self.terminal_path else mt5.initialize()
            except Exception as exc:
                raise MT5Error(f"Could not initialize the MT5 terminal: {exc}") from exc
            if not ok:
                err = mt5.last_error() if hasattr(mt5, "last_error") else "unknown error"
                raise MT5Error(f"MT5 initialize failed: {err}")
            self._connected = True

        try:
            terminal = mt5.terminal_info()
            account = mt5.account_info()
        except Exception as exc:
            self.shutdown()
            raise MT5Error(f"Could not read MT5 terminal/account information: {exc}") from exc

        if terminal is None or account is None:
            self.shutdown()
            err = mt5.last_error() if hasattr(mt5, "last_error") else "terminal/account unavailable"
            raise MT5Error(f"MT5 terminal or account information unavailable: {err}")

        # Fail closed if this package build does not expose the enum needed to
        # positively identify a demo account. Do not assume enum value zero.
        demo_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", None)
        account_mode = int(getattr(account, "trade_mode", -1))
        if demo_mode is None or account_mode != int(demo_mode):
            self.shutdown()
            raise MT5Error(
                "9N8 refused this MT5 account because DEMO mode could not be verified. "
                "Log in to a broker demo account in the MT5 desktop terminal, then retry."
            )
        if not bool(getattr(terminal, "connected", False)):
            raise MT5Error("MT5 terminal is open but not connected to its broker server.")

        self._account = MT5AccountSummary(
            login=int(getattr(account, "login", 0)),
            server=str(getattr(account, "server", "unknown")),
            currency=str(getattr(account, "currency", "USD")),
            balance=float(getattr(account, "balance", 0.0)),
            equity=float(getattr(account, "equity", 0.0)),
            trade_mode=account_mode,
        )
        return self._account

    def account_summary(self) -> MT5AccountSummary:
        return self.connect()

    def timeframe_constant(self, timeframe: str):
        mt5 = self._module()
        name = _TIMEFRAME_NAMES.get(timeframe.upper())
        if not name or not hasattr(mt5, name):
            raise MT5Error(f"Unsupported MT5 timeframe: {timeframe}")
        return getattr(mt5, name)

    def symbol_info(self, symbol: str) -> MT5SymbolInfo:
        self.connect()
        mt5 = self._module()
        info = mt5.symbol_info(symbol)
        if info is None:
            raise MT5Error(f"MT5 does not recognise symbol '{symbol}'. Check your broker's exact symbol name.")
        if not mt5.symbol_select(symbol, True):
            err = mt5.last_error() if hasattr(mt5, "last_error") else "unknown error"
            raise MT5Error(f"Could not select {symbol} in MT5 Market Watch: {err}")
        result = MT5SymbolInfo(
            name=str(getattr(info, "name", symbol)),
            digits=int(getattr(info, "digits", 5)),
            point=float(getattr(info, "point", 0.0)),
            contract_size=float(getattr(info, "trade_contract_size", 0.0)),
            volume_min=float(getattr(info, "volume_min", 0.0)),
            volume_max=float(getattr(info, "volume_max", 0.0)),
            volume_step=float(getattr(info, "volume_step", 0.0)),
            stops_level=int(getattr(info, "trade_stops_level", 0)),
            currency_profit=str(getattr(info, "currency_profit", "")),
        )
        if result.point <= 0 or result.contract_size <= 0 or result.volume_min <= 0 or result.volume_step <= 0:
            raise MT5Error(f"MT5 returned invalid contract/point/volume metadata for {symbol}.")
        return result

    def tick(self, symbol: str) -> MT5Tick:
        self.connect()
        mt5 = self._module()
        self.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            err = mt5.last_error() if hasattr(mt5, "last_error") else "no tick"
            raise MT5Error(f"No current tick for {symbol}: {err}")
        bid, ask = float(tick.bid), float(tick.ask)
        if bid <= 0 or ask <= 0 or ask < bid:
            raise MT5Error(f"MT5 returned an invalid bid/ask for {symbol}.")
        tick_time = int(getattr(tick, "time", 0))
        stamp = datetime.fromtimestamp(tick_time, tz=timezone.utc).isoformat() if tick_time else "unknown"
        return MT5Tick(symbol=symbol, bid=bid, ask=ask, time_utc=stamp)

    def candles(self, symbol: str, timeframe: str = "M15", count: int = 120) -> list[Candle]:
        if count < 32 or count > 5000:
            raise MT5Error("Candle count must be between 32 and 5000.")
        self.connect()
        mt5 = self._module()
        self.symbol_info(symbol)
        rates = mt5.copy_rates_from_pos(symbol, self.timeframe_constant(timeframe), 0, count)
        if rates is None or len(rates) == 0:
            err = mt5.last_error() if hasattr(mt5, "last_error") else "no rates"
            raise MT5Error(f"No candle history for {symbol}: {err}")
        candles: list[Candle] = []
        try:
            for row in rates:
                ts = int(row["time"])
                candles.append(Candle(
                    datetime=datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                    open=float(row["open"]), high=float(row["high"]),
                    low=float(row["low"]), close=float(row["close"]),
                    volume=float(row["tick_volume"]) if "tick_volume" in row.dtype.names else None,
                ))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise MT5Error(f"MT5 returned malformed candle data for {symbol}.") from exc
        candles.sort(key=lambda candle: candle.datetime)
        # MT5 bar index 0 is the still-forming current candle.
        return candles[:-1] if len(candles) > 1 else candles

    def symbols(self, query: str = "") -> list[str]:
        self.connect()
        raw = self._module().symbols_get()
        if raw is None:
            return []
        needle = query.strip().upper()
        names = [str(getattr(item, "name", "")) for item in raw]
        names = [name for name in names if name and (not needle or needle in name.upper())]
        return names[:80]

    def shutdown(self) -> None:
        if self._mt5 is not None and self._connected:
            try:
                self._mt5.shutdown()
            finally:
                self._connected = False
                self._account = None
