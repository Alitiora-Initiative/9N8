"""Transparent indicator calculations; produces observations, never orders."""
from __future__ import annotations
from dataclasses import dataclass
from .market_data import Candle


@dataclass(frozen=True)
class MarketSignal:
    symbol: str
    label: str
    close: float
    ema_fast: float
    ema_slow: float
    rsi: float
    candle_time: str
    explanation: str


def ema(values: list[float], period: int) -> float:
    if period < 1 or len(values) < period:
        raise ValueError(f"At least {period} observations are required for EMA({period}).")
    alpha = 2.0 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = (value * alpha) + (result * (1 - alpha))
    return result


def rsi(values: list[float], period: int = 14) -> float:
    if period < 2 or len(values) <= period:
        raise ValueError(f"At least {period + 1} closes are required for RSI({period}).")
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def analyse(symbol: str, candles: list[Candle]) -> MarketSignal:
    closes = [candle.close for candle in candles]
    if len(closes) < 30:
        raise ValueError("Need at least 30 candles to calculate the initial indicators.")
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    momentum = rsi(closes, 14)
    latest = candles[-1]

    if fast > slow and latest.close > fast and 50 <= momentum <= 70:
        label = "BULLISH SETUP"
        explanation = "EMA(9) is above EMA(21), price is above EMA(9), and RSI is between 50 and 70."
    elif fast < slow and latest.close < fast and 30 <= momentum <= 50:
        label = "BEARISH SETUP"
        explanation = "EMA(9) is below EMA(21), price is below EMA(9), and RSI is between 30 and 50."
    else:
        label = "NO CLEAR SETUP"
        explanation = "The configured trend and RSI conditions are not aligned."

    return MarketSignal(
        symbol=symbol,
        label=label,
        close=latest.close,
        ema_fast=fast,
        ema_slow=slow,
        rsi=momentum,
        candle_time=latest.datetime,
        explanation=explanation,
    )
