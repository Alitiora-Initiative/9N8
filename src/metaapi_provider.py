"""Cloud-hosted, read-only MetaApi adapter for MT5 demo market data.

This provider lets the Telegram bot run on a cloud worker while the user
controls it from phones, tablets, or computers. It intentionally implements
only account/symbol/quote/candle reads. There is no order or transfer method.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote
from typing import Any

import httpx

from .market_data import Candle
from .mt5_provider import MT5AccountSummary, MT5Error, MT5SymbolInfo, MT5Tick

_TIMEFRAMES = {
    "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1h", "H4": "4h", "D1": "1d",
}


class MetaApiProvider:
    """Read-only cloud API access, requiring a positively verified DEMO account."""

    def __init__(
        self,
        token: str,
        account_id: str,
        api_base: str = "https://mt-client-api-v1.new-york.agiliumtrade.ai",
        market_data_base: str = "https://mt-market-data-client-api-v1.new-york.agiliumtrade.ai",
        timeout: float = 30.0,
        client: Any | None = None,
    ):
        self.token = token.strip()
        self.account_id = account_id.strip()
        self.api_base = api_base.rstrip("/")
        self.market_data_base = market_data_base.rstrip("/")
        self.timeout = timeout
        self._client = client
        self._account: MT5AccountSummary | None = None

    def _require_config(self) -> None:
        if not self.token or not self.account_id:
            raise MT5Error(
                "Cloud MT5 is not configured. Add METAAPI_TOKEN and METAAPI_ACCOUNT_ID "
                "to your cloud service environment variables."
            )

    def _http(self):
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def _get(self, base: str, path: str, params: dict | None = None):
        self._require_config()
        url = f"{base}/users/current/accounts/{quote(self.account_id, safe='')}/{path.lstrip('/')}"
        try:
            response = self._http().get(
                url,
                headers={"auth-token": self.token, "Accept": "application/json"},
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                raise MT5Error("MetaApi rejected the API token. Check METAAPI_TOKEN.") from exc
            if status == 404:
                raise MT5Error("MetaApi account/symbol was not found or is not provisioned yet.") from exc
            if status == 429:
                raise MT5Error("MetaApi rate limit reached. Wait before retrying.") from exc
            raise MT5Error(f"MetaApi returned HTTP {status}.") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise MT5Error(f"Could not read MetaApi response: {exc}") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise MT5Error(f"MetaApi: {payload.get('message', 'provider error')}")
        return payload

    def connect(self) -> MT5AccountSummary:
        payload = self._get(
            self.api_base,
            "account-information",
            params={"refreshTerminalState": "true"},
        )
        account_type = str(payload.get("type", "")).upper()
        if account_type != "ACCOUNT_TRADE_MODE_DEMO":
            raise MT5Error(
                "9N8 refused this MetaApi account because it is not positively identified "
                "as DEMO. Connect a broker demo account; a live or unknown account is rejected."
            )
        try:
            self._account = MT5AccountSummary(
                login=int(payload.get("login", 0)),
                server=str(payload.get("server", "unknown")),
                currency=str(payload.get("currency", "USD")),
                balance=float(payload.get("balance", 0.0)),
                equity=float(payload.get("equity", 0.0)),
                trade_mode=0,
            )
        except (TypeError, ValueError) as exc:
            raise MT5Error("MetaApi returned malformed demo-account information.") from exc
        return self._account

    def account_summary(self) -> MT5AccountSummary:
        return self.connect()

    def symbols(self, query: str = "") -> list[str]:
        self.connect()
        raw = self._get(self.api_base, "symbols")
        if not isinstance(raw, list):
            raise MT5Error("MetaApi returned an invalid symbol list.")
        needle = query.strip().upper()
        return [str(name) for name in raw if isinstance(name, str) and (not needle or needle in name.upper())][:80]

    def symbol_info(self, symbol: str) -> MT5SymbolInfo:
        self.connect()
        raw = self._get(self.api_base, f"symbols/{quote(symbol, safe='')}/specification")
        try:
            tick_size = float(raw.get("tickSize", 0.0))
            return MT5SymbolInfo(
                name=str(raw.get("symbol", symbol)),
                digits=int(raw.get("digits", 5)),
                point=tick_size,
                contract_size=float(raw.get("contractSize", 0.0)),
                volume_min=float(raw.get("minVolume", 0.0)),
                volume_max=float(raw.get("maxVolume", 0.0)),
                volume_step=float(raw.get("volumeStep", 0.0)),
                stops_level=int(raw.get("stopsLevel", 0) or 0),
                currency_profit=str(raw.get("profitCurrency", "")),
            )
        except (TypeError, ValueError) as exc:
            raise MT5Error(f"MetaApi returned malformed contract metadata for {symbol}.") from exc

    def tick(self, symbol: str) -> MT5Tick:
        self.connect()
        raw = self._get(self.api_base, f"symbols/{quote(symbol, safe='')}/current-price")
        try:
            bid, ask = float(raw["bid"]), float(raw["ask"])
            stamp = str(raw.get("time", ""))
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            stamp = parsed.astimezone(timezone.utc).isoformat()
        except (KeyError, TypeError, ValueError) as exc:
            raise MT5Error(f"MetaApi returned malformed quote data for {symbol}.") from exc
        if bid <= 0 or ask <= 0 or ask < bid:
            raise MT5Error(f"MetaApi returned an invalid bid/ask for {symbol}.")
        return MT5Tick(symbol=symbol, bid=bid, ask=ask, time_utc=stamp)

    def candles(self, symbol: str, timeframe: str = "M15", count: int = 120) -> list[Candle]:
        if count < 32 or count > 1000:
            raise MT5Error("Candle count must be between 32 and 1000 for MetaApi.")
        tf = _TIMEFRAMES.get(timeframe.upper())
        if not tf:
            raise MT5Error(f"Unsupported MetaApi timeframe: {timeframe}")
        raw = self._get(
            self.market_data_base,
            f"historical-market-data/symbols/{quote(symbol, safe='')}/timeframes/{tf}/candles",
            params={"limit": count},
        )
        if not isinstance(raw, list) or not raw:
            raise MT5Error(f"MetaApi returned no candle history for {symbol}.")
        result: list[Candle] = []
        try:
            for item in raw:
                stamp = str(item.get("time", ""))
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                result.append(Candle(
                    datetime=parsed.astimezone(timezone.utc).isoformat(),
                    open=float(item["open"]), high=float(item["high"]),
                    low=float(item["low"]), close=float(item["close"]),
                    volume=float(item.get("tickVolume", item.get("volume", 0.0))),
                ))
        except (KeyError, TypeError, ValueError) as exc:
            raise MT5Error(f"MetaApi returned malformed candle history for {symbol}.") from exc
        result.sort(key=lambda candle: candle.datetime)
        # Exclude the current/forming candle, like the desktop provider.
        return result[:-1] if len(result) > 1 else result

    def shutdown(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None
                self._account = None
