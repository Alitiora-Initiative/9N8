from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from .quant import atr


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    regime: str
    fast_average: Decimal
    slow_average: Decimal
    atr_fraction: Decimal


def classify_simple_regime(
    highs: Sequence[object],
    lows: Sequence[object],
    closes: Sequence[object],
    *,
    fast_period: int = 20,
    slow_period: int = 50,
    high_volatility_fraction: object = "0.08",
) -> RegimeSnapshot:
    """Transparent baseline regime label; not an ML prediction or trade signal."""
    if fast_period < 2 or slow_period <= fast_period or len(closes) < slow_period + 1:
        raise ValueError("Regime classifier requires slow_period > fast_period >= 2 and enough candles")
    close_values = [Decimal(str(value)) for value in closes]
    if any(not value.is_finite() or value <= 0 for value in close_values):
        raise ValueError("close values must be finite and positive")
    fast = sum(close_values[-fast_period:], Decimal(0)) / Decimal(fast_period)
    slow = sum(close_values[-slow_period:], Decimal(0)) / Decimal(slow_period)
    volatility = atr(highs, lows, closes, period=min(14, len(closes) - 1)) / close_values[-1]
    threshold = Decimal(str(high_volatility_fraction))
    if not threshold.is_finite() or threshold <= 0:
        raise ValueError("high_volatility_fraction must be finite and positive")
    if volatility >= threshold:
        regime = "high_volatility"
    elif fast > slow:
        regime = "risk_on_trend"
    elif fast < slow:
        regime = "risk_off_trend"
    else:
        regime = "range_bound"
    return RegimeSnapshot(regime, fast, slow, volatility)
