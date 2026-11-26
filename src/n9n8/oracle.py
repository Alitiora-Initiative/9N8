from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation

import aiohttp

from .math_engine import dec


class PriceOracle:
    """SOL/USD spot observation from the deepest available DexScreener SOL pair.

    This is a valuation input, not an execution guarantee. A failed/stale quote
    returns an error instead of silently using a hard-coded price.
    """
    SOL_MINT = "So11111111111111111111111111111111111111112"

    def __init__(self, cache_seconds: int = 5):
        self.cache_seconds = cache_seconds
        self._cached_price: Decimal | None = None
        self._cached_at = 0.0
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def sol_usd(self, force: bool = False) -> Decimal:
        if not force and self._cached_price is not None and time.monotonic() - self._cached_at <= self.cache_seconds:
            return self._cached_price
        await self.start()
        assert self._session
        url = f"https://api.dexscreener.com/latest/dex/tokens/{self.SOL_MINT}"
        try:
            async with self._session.get(url) as response:
                if response.status != 200:
                    raise RuntimeError(f"Price provider returned HTTP {response.status}.")
                payload = await response.json()
            pairs = payload.get("pairs") or []
            sol_pairs = [
                p for p in pairs
                if p.get("chainId") == "solana"
                and p.get("priceUsd") is not None
                and (p.get("liquidity") or {}).get("usd") is not None
            ]
            if not sol_pairs:
                raise RuntimeError("No valid SOL/USD market pair is available.")
            best = max(sol_pairs, key=lambda p: Decimal(str(p["liquidity"]["usd"])))
            price = dec(best["priceUsd"], "SOL/USD")
            if price <= 0:
                raise RuntimeError("Price provider returned a non-positive SOL price.")
            self._cached_price, self._cached_at = price, time.monotonic()
            return price
        except (aiohttp.ClientError, ValueError, InvalidOperation) as exc:
            raise RuntimeError(f"SOL/USD quote unavailable: {type(exc).__name__}") from exc
