from __future__ import annotations

import asyncio
import email.utils
import json
import math
import os
import random
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Sequence
from urllib.parse import urlparse

from src.n9n8.v2.market_data import normalize_ohlcv


class ProviderError(RuntimeError):
    pass


def retry_delay(attempt: int, retry_after: str | None = None, *, cap_seconds: float = 30.0) -> float:
    """Bounded exponential backoff with server Retry-After support and jitter."""
    if attempt < 0 or cap_seconds <= 0:
        raise ValueError("attempt and cap_seconds are invalid")
    if retry_after:
        raw = retry_after.strip()
        try:
            return min(cap_seconds, max(0.0, float(raw)))
        except ValueError:
            try:
                target = email.utils.parsedate_to_datetime(raw)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                return min(cap_seconds, max(0.0, (target - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    base = min(cap_seconds, 0.5 * (2 ** min(attempt, 8)))
    return min(cap_seconds, base * random.uniform(0.75, 1.25))


async def retry_provider_call(
    operation: Callable[[], Awaitable[Any]],
    *,
    attempts: int = 5,
    retryable: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError),
) -> Any:
    if attempts < 1 or attempts > 10:
        raise ValueError("attempts must be between 1 and 10")
    for attempt in range(attempts):
        try:
            return await operation()
        except retryable:
            if attempt + 1 == attempts:
                raise
            await asyncio.sleep(retry_delay(attempt))
    raise AssertionError("unreachable")


async def fetch_ccxt_ohlcv(
    exchange_id: str,
    symbol: str,
    timeframe: str,
    *,
    limit: int = 500,
) -> tuple:
    """Fetch CEX history with CCXT's built-in rate limiter; closes its client."""
    if not exchange_id.replace("_", "").isalnum() or not symbol or not 1 <= limit <= 5000:
        raise ValueError("exchange_id, symbol or limit is invalid")
    try:
        import ccxt.async_support as ccxt
    except ImportError as exc:
        raise RuntimeError("Install requirements-research.txt for CCXT") from exc
    exchange_type = getattr(ccxt, exchange_id, None)
    if exchange_type is None:
        raise ValueError("CCXT exchange identifier is not available")
    exchange = exchange_type({"enableRateLimit": True, "timeout": 15000})
    try:
        raw = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    finally:
        await exchange.close()
    return normalize_ohlcv(
        raw, symbol=symbol, source=f"ccxt:{exchange_id}", interval=timeframe,
        now=datetime.now(timezone.utc), only_closed=True,
    )


def fetch_yfinance_ohlcv(
    ticker: str,
    *,
    period: str = "1y",
    interval: str = "1d",
) -> tuple:
    """Historical research ingestion only. yfinance is not an execution price feed."""
    if not ticker.strip():
        raise ValueError("ticker is required")
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("Install requirements-research.txt for yfinance") from exc
    frame = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
    if frame.empty:
        raise ProviderError("Yahoo Finance returned no history for the requested symbol")
    rows = []
    for timestamp, item in frame.iterrows():
        rows.append({
            "timestamp": timestamp.to_pydatetime(),
            "open": str(item["Open"]),
            "high": str(item["High"]),
            "low": str(item["Low"]),
            "close": str(item["Close"]),
            "volume": str(item["Volume"]),
        })
    return normalize_ohlcv(
        rows, symbol=ticker, source="yfinance", interval=interval,
        source_timezone="UTC", now=datetime.now(timezone.utc), only_closed=True,
    )


async def fetch_coinmarketcap_listings(
    *,
    limit: int = 100,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Fetch market-cap metadata only; never treat it as an executable quote."""
    credential = (api_key or os.getenv("COINMARKETCAP_API_KEY", "")).strip()
    if not credential:
        raise RuntimeError("COINMARKETCAP_API_KEY is not configured")
    if not 1 <= limit <= 5000:
        raise ValueError("limit must be between 1 and 5000")
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("Install requirements-research.txt for HTTPX") from exc
    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            url,
            params={"limit": limit, "convert": "USD"},
            headers={"X-CMC_PRO_API_KEY": credential, "Accept": "application/json"},
        )
    if response.status_code == 429:
        raise ProviderError("CoinMarketCap rate limit reached; honor Retry-After")
    if response.status_code >= 400:
        raise ProviderError(f"CoinMarketCap metadata request failed: HTTP {response.status_code}")
    payload = response.json()
    return {
        "source": "coinmarketcap",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "data": payload.get("data", []),
    }


async def fetch_graphql(
    endpoint: str,
    query: str,
    *,
    variables: dict[str, Any] | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """Read-only GraphQL metadata adapter, useful for The Graph subgraphs."""
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("GraphQL endpoint must be HTTPS")
    if not query.strip():
        raise ValueError("GraphQL query is required")
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("Install requirements-research.txt for HTTPX") from exc
    headers = {"Accept": "application/json"}
    if bearer_token:
        headers["Authorization"] = "Bearer " + bearer_token
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(endpoint, json={"query": query, "variables": variables or {}}, headers=headers)
    if response.status_code == 429:
        raise ProviderError("GraphQL provider rate limit reached")
    if response.status_code >= 400:
        raise ProviderError(f"GraphQL provider returned HTTP {response.status_code}")
    payload = response.json()
    if payload.get("errors"):
        raise ProviderError("GraphQL request returned provider errors")
    return payload.get("data") or {}


def parse_public_html_metadata(html: str) -> tuple[dict[str, str], ...]:
    """Extract JSON-LD from supplied, permitted HTML; this function makes no network request."""
    if not html.strip():
        return ()
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise RuntimeError("Install requirements-research.txt for BeautifulSoup4") from exc
    soup = BeautifulSoup(html, "html.parser")
    result = []
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            value = json.loads(node.string or node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        records = value if isinstance(value, list) else [value]
        for item in records:
            if isinstance(item, dict):
                result.append({str(key): json.dumps(val, separators=(",", ":")) if isinstance(val, (dict, list)) else str(val)
                               for key, val in item.items()})
    return tuple(result)


async def read_allowlisted_page(url: str, *, allowed_hosts: Sequence[str]) -> str:
    """Optional browser QA/ingestion. HTTPS + exact host allow-list; no proxy/stealth bypass."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in {x.lower() for x in allowed_hosts}:
        raise ValueError("URL is not HTTPS on an explicitly allowed host")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Install requirements-research.txt and run playwright install chromium") from exc
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            return await page.locator("body").inner_text(timeout=5000)
        finally:
            await browser.close()


def vader_sentiment(text: str) -> dict[str, float]:
    if not text.strip():
        raise ValueError("text is required")
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    except ImportError as exc:
        raise RuntimeError("Install requirements-research.txt for VADER") from exc
    return SentimentIntensityAnalyzer().polarity_scores(text)
