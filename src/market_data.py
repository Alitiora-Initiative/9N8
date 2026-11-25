"""Twelve Data market-data adapter. This module only reads market data."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import httpx

BASE_URL = "https://api.twelvedata.com"


class MarketDataError(RuntimeError):
    """Raised when the market-data provider returns an error or bad payload."""


@dataclass(frozen=True)
class Candle:
    datetime: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


class TwelveDataClient:
    def __init__(self, api_key: str, timeout: float = 20.0):
        self.api_key = api_key.strip()
        self.timeout = timeout

    def _require_key(self) -> None:
        if not self.api_key:
            raise MarketDataError(
                "Twelve Data API key is not configured. Add TWELVEDATA_API_KEY to .env."
            )

    async def time_series(
        self, symbol: str, interval: str = "15min", outputsize: int = 80
    ) -> list[Candle]:
        self._require_key()
        params = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": self.api_key,
            "format": "JSON",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{BASE_URL}/time_series", params=params)
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
        except httpx.TimeoutException as exc:
            raise MarketDataError("Twelve Data request timed out. Try again.") from exc
        except httpx.HTTPStatusError as exc:
            raise MarketDataError(
                f"Twelve Data returned HTTP {exc.response.status_code}."
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError(f"Could not read Twelve Data response: {exc}") from exc

        if payload.get("status") == "error" or payload.get("code"):
            message = payload.get("message", "Provider returned an error.")
            raise MarketDataError(f"Twelve Data: {message}")

        raw_values = payload.get("values")
        if not isinstance(raw_values, list) or not raw_values:
            raise MarketDataError(
                "No candle data returned. Check the symbol, interval and API plan."
            )

        candles: list[Candle] = []
        try:
            # Twelve Data normally returns newest-first; reverse to chronological order.
            for item in reversed(raw_values):
                candles.append(
                    Candle(
                        datetime=str(item["datetime"]),
                        open=float(item["open"]),
                        high=float(item["high"]),
                        low=float(item["low"]),
                        close=float(item["close"]),
                        volume=(float(item["volume"]) if item.get("volume") not in (None, "") else None),
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketDataError("Twelve Data returned malformed candle data.") from exc
        return candles
