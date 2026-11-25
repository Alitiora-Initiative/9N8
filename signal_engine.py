"""Deterministic technical-analysis signal engine for 9N8.

This module calculates indicators locally from Twelve Data OHLC candles. It does
not place trades and does not treat an alignment score as a probability.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class Candle:
    datetime: str
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None


def ema(values: list[float], period: int) -> list[Optional[float]]:
    if period <= 0:
        raise ValueError("period must be positive")
    result: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    multiplier = 2.0 / (period + 1.0)
    for index in range(period, len(values)):
        current = (values[index] - current) * multiplier + current
        result[index] = current
    return result


def rsi(values: list[float], period: int = 14) -> Optional[float]:
    if len(values) <= period:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    recent = changes[-period:]
    gains = sum(change for change in recent if change > 0) / period
    losses = sum(-change for change in recent if change < 0) / period
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    relative_strength = gains / losses
    return 100.0 - (100.0 / (1.0 + relative_strength))


def macd(values: list[float]) -> tuple[Optional[float], Optional[float]]:
    fast = ema(values, 12)
    slow = ema(values, 26)
    macd_values: list[float] = []
    for fast_value, slow_value in zip(fast, slow):
        if fast_value is not None and slow_value is not None:
            macd_values.append(fast_value - slow_value)
    if len(macd_values) < 9:
        return None, None
    signal_values = ema(macd_values, 9)
    signal = signal_values[-1]
    if signal is None:
        return None, None
    return macd_values[-1], signal


def atr(candles: list[Candle], period: int = 14) -> Optional[float]:
    if len(candles) <= period:
        return None
    ranges: list[float] = []
    for index in range(1, len(candles)):
        candle = candles[index]
        previous_close = candles[index - 1].close
        ranges.append(max(
            candle.high - candle.low,
            abs(candle.high - previous_close),
            abs(candle.low - previous_close),
        ))
    recent = ranges[-period:]
    return sum(recent) / len(recent) if recent else None


def format_price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def analyse(candles: Iterable[Candle], symbol: str, interval: str) -> dict:
    ordered = sorted(candles, key=lambda candle: candle.datetime)
    if len(ordered) < 60:
        raise ValueError(f"{symbol}: only {len(ordered)} candles received; at least 60 are needed")

    closes = [candle.close for candle in ordered]
    fast_ema = ema(closes, 20)[-1]
    slow_ema = ema(closes, 50)[-1]
    current_rsi = rsi(closes, 14)
    current_macd, signal_macd = macd(closes)
    current_atr = atr(ordered, 14)
    if None in (fast_ema, slow_ema, current_rsi, current_macd, signal_macd, current_atr):
        raise ValueError(f"{symbol}: insufficient candle history to calculate indicators")

    last = ordered[-1]
    long_points = 0
    short_points = 0
    if fast_ema > slow_ema:
        long_points += 30
    if fast_ema < slow_ema:
        short_points += 30
    if current_macd > signal_macd:
        long_points += 30
    if current_macd < signal_macd:
        short_points += 30
    if 48 <= current_rsi <= 72:
        long_points += 20
    if 28 <= current_rsi <= 52:
        short_points += 20
    if last.close >= fast_ema:
        long_points += 20
    if last.close <= fast_ema:
        short_points += 20

    direction = "NONE"
    alignment = max(long_points, short_points)
    # Require trend + momentum agreement and a minimum 80/100 alignment.
    if fast_ema > slow_ema and current_macd > signal_macd and current_rsi <= 72 and long_points >= 80:
        direction = "LONG"
        alignment = long_points
    elif fast_ema < slow_ema and current_macd < signal_macd and current_rsi >= 28 and short_points >= 80:
        direction = "SHORT"
        alignment = short_points

    result = {
        "symbol": symbol,
        "interval": interval,
        "direction": direction,
        "alignment": alignment,
        "price": last.close,
        "price_text": format_price(last.close),
        "rsi": current_rsi,
        "ema20": fast_ema,
        "ema50": slow_ema,
        "macd": current_macd,
        "macd_signal": signal_macd,
        "atr": current_atr,
        "candle_time": last.datetime,
    }
    if direction != "NONE":
        risk_distance = current_atr * 1.5
        if direction == "LONG":
            stop = last.close - risk_distance
            target_1 = last.close + risk_distance
            target_2 = last.close + (risk_distance * 2)
        else:
            stop = last.close + risk_distance
            target_1 = last.close - risk_distance
            target_2 = last.close - (risk_distance * 2)
        result.update({
            "stop": stop,
            "target_1": target_1,
            "target_2": target_2,
            "stop_text": format_price(stop),
            "target_1_text": format_price(target_1),
            "target_2_text": format_price(target_2),
        })
    return result
