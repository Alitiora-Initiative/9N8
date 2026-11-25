from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import aiohttp

from connectors.base import ExecutionConnector
from connectors.retry import RetryableReadError, retry_read
from models import (
    AccountState, Candle, ConnectorUnavailable, MarketSnapshot, MarketType,
    OpenPosition, Side, TradeIntent, TradeReceipt,
)


class AlpacaConnector(ExecutionConnector):
    """Read-only U.S. equity market-data connector for virtual paper fills.

    This class deliberately does not implement account, order, cancel, or wallet
    endpoints. The paper broker consumes only current bid/ask quotes and closed
    historical bars; all fills remain virtual.
    """

    venue = "alpaca-stocks"
    _FEEDS = {"iex", "sip", "delayed_sip", "boats", "overnight", "otc"}
    _TIMEFRAMES = {
        "1m": ("1Min", 60),
        "5m": ("5Min", 300),
        "15m": ("15Min", 900),
        "30m": ("30Min", 1800),
        "1h": ("1Hour", 3600),
        "4h": ("4Hour", 14400),
        "1d": ("1Day", 86400),
    }

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        data_feed: str | None = None,
    ):
        self.api_key = (api_key or os.getenv("ALPACA_API_KEY", "")).strip()
        self.api_secret = (api_secret or os.getenv("ALPACA_API_SECRET", "")).strip()
        self.data_feed = (data_feed or os.getenv("ALPACA_DATA_FEED", "iex")).strip().lower()
        self.session: aiohttp.ClientSession | None = None

    async def connect(self) -> None:
        if not self.api_key or not self.api_secret:
            raise ConnectorUnavailable(
                "ALPACA_API_KEY and ALPACA_API_SECRET are required for stock market data."
            )
        if self.data_feed not in self._FEEDS:
            raise ConnectorUnavailable(
                "ALPACA_DATA_FEED must be one of: " + ", ".join(sorted(self._FEEDS))
            )
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={
                    "APCA-API-KEY-ID": self.api_key,
                    "APCA-API-SECRET-KEY": self.api_secret,
                    "accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=15, connect=5),
            )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.session:
            raise ConnectorUnavailable("Alpaca market-data connector is not connected.")
        url = "https://data.alpaca.markets" + path
        async with self.session.get(url, params=params) as response:
            if response.status in {408, 425, 429, 500, 502, 503, 504}:
                retry = response.headers.get("Retry-After")
                exc = RetryableReadError(
                    f"Alpaca market-data temporarily unavailable (HTTP {response.status})."
                )
                if retry:
                    try:
                        exc.retry_after = float(retry)
                    except ValueError:
                        pass
                raise exc
            if response.status in {401, 403}:
                raise ConnectorUnavailable(
                    "Alpaca rejected the market-data credentials or this feed is not included in the subscription."
                )
            if response.status >= 400:
                raise ConnectorUnavailable(
                    f"Alpaca market-data request failed (HTTP {response.status})."
                )
            try:
                payload = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError) as exc:
                raise ConnectorUnavailable("Alpaca returned an invalid market-data response.") from exc
        if not isinstance(payload, dict):
            raise ConnectorUnavailable("Alpaca market-data response had an unexpected shape.")
        return payload

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        if not isinstance(value, str) or not value.strip():
            raise ConnectorUnavailable("Alpaca quote did not include a timestamp.")
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConnectorUnavailable("Alpaca quote timestamp was invalid.") from exc
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)

    @staticmethod
    def _positive(value: Any, field: str) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ConnectorUnavailable(f"Alpaca quote is missing a valid {field}.") from exc
        if not math.isfinite(result) or result <= 0:
            raise ConnectorUnavailable(f"Alpaca quote has an invalid {field}.")
        return result

    @staticmethod
    def _symbol(symbol: str) -> str:
        normalized = str(symbol).strip().upper()
        if not normalized or len(normalized) > 15 or not all(
            char.isalnum() or char in ".-" for char in normalized
        ):
            raise ValueError("Use a valid U.S. stock or ETF ticker symbol.")
        return normalized

    @retry_read(attempts=3, timeout_seconds=10)
    async def fetch_snapshot(self, symbol: str) -> MarketSnapshot:
        ticker = self._symbol(symbol)
        payload = await self._get(
            f"/v2/stocks/{quote(ticker, safe='')}/quotes/latest",
            {"feed": self.data_feed},
        )
        quote_data = payload.get("quote")
        if not isinstance(quote_data, dict):
            raise ConnectorUnavailable(f"Alpaca returned no latest quote for {ticker}.")
        bid = self._positive(quote_data.get("bp"), "best bid")
        ask = self._positive(quote_data.get("ap"), "best ask")
        if ask < bid:
            raise ConnectorUnavailable(f"Alpaca returned a crossed quote for {ticker}.")
        bid_size = self._positive(quote_data.get("bs"), "bid size")
        ask_size = self._positive(quote_data.get("as"), "ask size")
        timestamp = self._timestamp(quote_data.get("t"))
        midpoint = (bid + ask) / 2.0
        return MarketSnapshot(
            self.venue,
            ticker,
            MarketType.STOCK,
            bid,
            ask,
            midpoint,
            timestamp=timestamp,
            bid_size=bid_size,
            ask_size=ask_size,
        )

    @retry_read(attempts=3, timeout_seconds=10)
    async def fetch_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        ticker = self._symbol(symbol)
        normalized_timeframe = str(timeframe).strip().lower()
        if normalized_timeframe not in self._TIMEFRAMES:
            raise ValueError("Unsupported Alpaca stock timeframe: " + normalized_timeframe)
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("Candle limit must be a positive integer.")
        alpaca_timeframe, seconds = self._TIMEFRAMES[normalized_timeframe]
        # Use a wide calendar window to allow for nights, weekends and market
        # holidays while keeping the returned bar count bounded.
        now = datetime.now(timezone.utc)
        calendar_window = seconds * max(limit, 200) * 8
        start = now - timedelta(seconds=calendar_window)
        response = await self._get(
            "/v2/stocks/bars",
            {
                "symbols": ticker,
                "timeframe": alpaca_timeframe,
                "start": start.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "end": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "limit": min(max(limit * 8, 500), 10000),
                "feed": self.data_feed,
                "sort": "asc",
            },
        )
        raw_bars = response.get("bars")
        rows = raw_bars.get(ticker) if isinstance(raw_bars, dict) else None
        if not isinstance(rows, list):
            raise ConnectorUnavailable(f"Alpaca returned no historical bars for {ticker}.")
        candles: list[Candle] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                timestamp = self._timestamp(row.get("t"))
                values = [float(row[name]) for name in ("o", "h", "l", "c", "v")]
            except (KeyError, TypeError, ValueError):
                continue
            open_price, high, low, close, volume = values
            if not all(math.isfinite(value) for value in values):
                continue
            if min(open_price, high, low, close) <= 0 or volume < 0:
                continue
            if high < max(open_price, close, low) or low > min(open_price, close, high):
                continue
            candles.append(
                Candle(int(timestamp.timestamp() * 1000), open_price, high, low, close, volume)
            )
        candles.sort(key=lambda candle: candle.timestamp)
        deduplicated = {candle.timestamp: candle for candle in candles}
        return list(sorted(deduplicated.values(), key=lambda candle: candle.timestamp))[-limit:]

    async def get_balance(self) -> AccountState:
        raise ConnectorUnavailable(
            "Alpaca connector is market-data-only. Stock paper risk uses the virtual paper account."
        )

    async def get_open_positions(self) -> list[OpenPosition]:
        return []

    async def buy(self, intent: TradeIntent, quantity: float, client_order_id: str) -> TradeReceipt:
        raise ConnectorUnavailable("Alpaca live stock orders are not implemented; this connector is paper-data only.")

    async def sell(self, intent: TradeIntent, quantity: float, client_order_id: str) -> TradeReceipt:
        raise ConnectorUnavailable("Alpaca live stock orders are not implemented; this connector is paper-data only.")

    async def close_position(self, position: OpenPosition, quantity: float | None = None) -> TradeReceipt:
        raise ConnectorUnavailable("Alpaca live stock orders are not implemented; this connector is paper-data only.")

    async def cancel_all_orders(self, symbol: str | None = None) -> None:
        raise ConnectorUnavailable("Alpaca connector does not submit orders and cannot cancel broker orders.")

    async def provider_health(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "connected": bool(self.session and not self.session.closed),
            "feed": self.data_feed,
            "market_data_only": True,
            "live_orders_enabled": False,
        }
