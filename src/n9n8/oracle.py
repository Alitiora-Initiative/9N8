from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import aiohttp

from .math_engine import dec

SOL_USD_FEED_ID = "0xef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d"
SOL_MINT = "So11111111111111111111111111111111111111112"


class PriceOracle:
    """Pyth SOL/USD first, with liquid-pair DexScreener fallback.

    A stale or non-positive price is rejected. The source and observation time
    are kept so the fee gate can be audited.
    """
    def __init__(
        self, hermes_url: str = "https://hermes.pyth.network",
        api_key: str = "", cache_seconds: int = 3, max_age_seconds: int = 120,
    ):
        self.hermes_url = hermes_url.rstrip("/")
        self.api_key = api_key
        self.cache_seconds = cache_seconds
        self.max_age_seconds = max_age_seconds
        self._cached_price: Decimal | None = None
        self._cached_at = 0.0
        self._source = "unavailable"
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def source(self) -> str:
        return self._source

    async def sol_usd(self, force: bool = False) -> Decimal:
        if (
            not force and self._cached_price is not None
            and time.monotonic() - self._cached_at <= self.cache_seconds
        ):
            return self._cached_price
        async with self._lock:
            if (
                not force and self._cached_price is not None
                and time.monotonic() - self._cached_at <= self.cache_seconds
            ):
                return self._cached_price
            await self.start()
            try:
                price = await self._pyth_price()
                self._source = "Pyth Hermes"
            except Exception:
                price = await self._dexscreener_price()
                self._source = "DexScreener fallback"
            if price <= 0:
                raise RuntimeError("SOL/USD oracle returned a non-positive price.")
            self._cached_price, self._cached_at = price, time.monotonic()
            return price

    async def _pyth_price(self) -> Decimal:
        assert self._session
        params = [("ids[]", SOL_USD_FEED_ID), ("parsed", "true")]
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = self.hermes_url + "/v2/updates/price/latest"
        async with self._session.get(url, params=params, headers=headers) as response:
            if response.status != 200:
                raise RuntimeError(f"Pyth returned HTTP {response.status}")
            payload = await response.json()
        feeds = payload.get("parsed") or []
        if not feeds:
            raise RuntimeError("Pyth returned no parsed SOL/USD feed")
        price_data = feeds[0].get("price") or {}
        price = dec(price_data.get("price"), "Pyth price")
        exponent = int(price_data.get("expo"))
        publish_time = int(price_data.get("publish_time"))
        age = int(time.time()) - publish_time
        if age < -10 or age > self.max_age_seconds:
            raise RuntimeError(f"Pyth feed is stale ({age}s)")
        return price * (Decimal(10) ** exponent)

    async def _dexscreener_price(self) -> Decimal:
        assert self._session
        url = f"https://api.dexscreener.com/latest/dex/tokens/{SOL_MINT}"
        async with self._session.get(url) as response:
            if response.status != 200:
                raise RuntimeError(f"DexScreener returned HTTP {response.status}")
            payload = await response.json()
        pairs = [
            p for p in (payload.get("pairs") or [])
            if p.get("chainId") == "solana"
            and p.get("priceUsd") is not None
            and (p.get("liquidity") or {}).get("usd") is not None
        ]
        if not pairs:
            raise RuntimeError("DexScreener has no liquid SOL/USD pair")
        best = max(pairs, key=lambda p: dec(p["liquidity"]["usd"], "liquidity"))
        return dec(best["priceUsd"], "SOL/USD")
