"""Deterministic entry rules and exchange-market quality filters for 9N8 paper scans."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from .market_data import Candle
from .strategy import ema, rsi


@dataclass(frozen=True)
class MarketQuality:
    accepted: bool
    reason: str
    spread_bps: float = 0.0
    quote_volume_24h: float = 0.0
    quote_age_seconds: float = 0.0


@dataclass(frozen=True)
class StrategySignal:
    strategy: str
    eligible: bool
    reason: str
    close: float
    indicators: dict[str, float]


def screen_market(
    ticker: dict[str, Any],
    *,
    min_quote_volume_24h: float = 50000.0,
    max_spread_bps: float = 80.0,
    max_quote_age_seconds: float = 20.0,
    now_ms: int | None = None,
) -> MarketQuality:
    """Reject missing, stale, illiquid or excessively wide spot markets."""
    try:
        bid = float(ticker.get("bidPx") or ticker.get("bid") or 0)
        ask = float(ticker.get("askPx") or ticker.get("ask") or 0)
        volume = float(ticker.get("volCcy24h") or 0)
        stamp = int(ticker.get("ts") or 0)
    except (TypeError, ValueError):
        return MarketQuality(False, "Ticker fields are missing or malformed.")
    if not all(math.isfinite(value) for value in (bid, ask, volume)) or bid <= 0 or ask <= 0:
        return MarketQuality(False, "A valid positive bid and ask are required.")
    if ask < bid:
        return MarketQuality(False, "The quote is crossed; market screening fails closed.")
    if not stamp:
        return MarketQuality(False, "The quote has no exchange timestamp.")
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    age = max(0.0, (current_ms - stamp) / 1000.0)
    if age > max_quote_age_seconds:
        return MarketQuality(False, f"Quote is stale ({age:.1f}s old).", quote_age_seconds=age)
    mid = (bid + ask) / 2.0
    spread_bps = ((ask - bid) / mid) * 10000.0
    if spread_bps > max_spread_bps:
        return MarketQuality(
            False,
            f"Spread {spread_bps:.1f} bps exceeds the {max_spread_bps:.1f} bps limit.",
            spread_bps,
            volume,
            age,
        )
    if volume < min_quote_volume_24h:
        return MarketQuality(
            False,
            f"24h quote volume {volume:,.0f} is below the {min_quote_volume_24h:,.0f} minimum.",
            spread_bps,
            volume,
            age,
        )
    return MarketQuality(True, "Market passed spread, freshness and 24h quote-volume filters.", spread_bps, volume, age)


def evaluate_strategy(strategy: str, candles: list[Candle]) -> StrategySignal:
    """Return a binary long-entry decision. This function never submits an order."""
    name = str(strategy).strip().lower()
    if name not in {"momentum", "trend", "mean_reversion", "grid"}:
        return StrategySignal(name, False, "Unsupported strategy.", 0.0, {})
    closes = [float(item.close) for item in candles]
    if len(closes) < 55:
        return StrategySignal(name, False, "At least 55 closed candles are required.", closes[-1] if closes else 0.0, {})
    if any(not math.isfinite(x) or x <= 0 for x in closes):
        return StrategySignal(name, False, "Candle series contains invalid prices.", closes[-1], {})

    latest = candles[-1]
    close = closes[-1]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    momentum_rsi = rsi(closes, 14)
    indicators = {"ema20": ema20, "ema50": ema50, "rsi14": momentum_rsi}

    if name == "trend":
        eligible = close > ema20 > ema50 and 50.0 <= momentum_rsi <= 72.0
        reason = (
            "Trend entry: close > EMA20 > EMA50 and RSI(14) is in [50, 72]."
            if eligible else
            "No trend entry: the EMA alignment, close or RSI condition is not satisfied."
        )
        return StrategySignal(name, eligible, reason, close, indicators)

    if name == "momentum":
        prior = candles[-21:-1]
        prior_high = max(float(item.high) for item in prior)
        prior_volumes = [
            float(item.volume or 0) for item in prior
            if item.volume is not None and float(item.volume) > 0
        ]
        current_volume = float(latest.volume or 0)
        average_volume = sum(prior_volumes) / len(prior_volumes) if prior_volumes else 0.0
        volume_multiple = current_volume / average_volume if average_volume > 0 else 0.0
        indicators.update({"prior_swing_high": prior_high, "volume_multiple": volume_multiple})
        eligible = close > prior_high and close > ema20 and momentum_rsi > 60.0 and volume_multiple >= 3.0
        reason = (
            "Momentum entry: close breaks the prior 20-candle high, stays above EMA20, RSI > 60 and volume >= 3x its prior average."
            if eligible else
            "No momentum entry: breakout, EMA20, RSI > 60 and 3x volume must all be true."
        )
        return StrategySignal(name, eligible, reason, close, indicators)

    if name == "mean_reversion":
        sample = closes[-20:]
        mean = sum(sample) / len(sample)
        variance = sum((value - mean) ** 2 for value in sample) / len(sample)
        lower_band = mean - (2.0 * math.sqrt(variance))
        indicators.update({"bollinger_mid": mean, "bollinger_lower": lower_band})
        eligible = close <= lower_band and momentum_rsi < 30.0 and close > ema50
        reason = (
            "Mean-reversion entry: close is at/below the 20-period lower Bollinger band, RSI < 30 and price remains above EMA50."
            if eligible else
            "No mean-reversion entry: lower-band, RSI < 30 and EMA50 regime filters must all be true."
        )
        return StrategySignal(name, eligible, reason, close, indicators)

    # A simple long-only grid seed: buy near the bottom of a sufficiently wide,
    # recent 40-candle range. Exit management is handled by the paper position engine.
    prior = candles[-41:-1]
    range_high = max(float(item.high) for item in prior)
    range_low = min(float(item.low) for item in prior)
    range_width_pct = ((range_high - range_low) / range_low) * 100.0 if range_low > 0 else 0.0
    lower_quartile = range_low + (range_high - range_low) * 0.25
    indicators.update({
        "range_high": range_high,
        "range_low": range_low,
        "range_width_pct": range_width_pct,
        "grid_lower_quartile": lower_quartile,
    })
    eligible = range_width_pct >= 4.5 and close <= lower_quartile and close > ema50
    reason = (
        "Grid seed: range width >= 4.5%, close is in the lower quartile and price remains above EMA50."
        if eligible else
        "No grid seed: range must be wide enough and price must be in its lower quartile above EMA50."
    )
    return StrategySignal(name, eligible, reason, close, indicators)
