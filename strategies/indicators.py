from __future__ import annotations

from collections.abc import Sequence
from models import Candle


def ema(values: Sequence[float], period: int) -> list[float | None]:
    if period < 2:
        raise ValueError("EMA period must be at least 2.")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    current = sum(values[:period]) / period
    out[period - 1] = current
    alpha = 2.0 / (period + 1)
    for i in range(period, len(values)):
        current = (values[i] - current) * alpha + current
        out[i] = current
    return out


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains += max(delta, 0)
        losses += max(-delta, 0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def atr(candles: Sequence[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    ranges = []
    for previous, current in zip(candles, candles[1:]):
        ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    value = sum(ranges[:period]) / period
    for tr in ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def adx(candles: Sequence[Candle], period: int = 14) -> float | None:
    if len(candles) < period * 2 + 1:
        return None
    tr, plus_dm, minus_dm = [], [], []
    for p, c in zip(candles, candles[1:]):
        up = c.high - p.high
        down = p.low - c.low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(c.high-c.low, abs(c.high-p.close), abs(c.low-p.close)))
    sm_tr = sum(tr[:period])
    sm_plus = sum(plus_dm[:period])
    sm_minus = sum(minus_dm[:period])
    dx_values = []
    for i in range(period, len(tr)):
        if i > period:
            sm_tr = sm_tr - sm_tr / period + tr[i]
            sm_plus = sm_plus - sm_plus / period + plus_dm[i]
            sm_minus = sm_minus - sm_minus / period + minus_dm[i]
        if sm_tr <= 0:
            dx_values.append(0.0)
            continue
        pdi = 100 * sm_plus / sm_tr
        mdi = 100 * sm_minus / sm_tr
        total = pdi + mdi
        dx_values.append(100 * abs(pdi-mdi) / total if total else 0.0)
    if len(dx_values) < period:
        return None
    value = sum(dx_values[:period]) / period
    for d in dx_values[period:]:
        value = (value * (period - 1) + d) / period
    return value


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float | None, float | None]:
    if len(values) < slow + signal + 2:
        return None, None
    fast_ema, slow_ema = ema(values, fast), ema(values, slow)
    line: list[float] = []
    for a, b in zip(fast_ema, slow_ema):
        if a is not None and b is not None:
            line.append(a - b)
    sig = ema(line, signal)
    if not sig or sig[-1] is None or sig[-2] is None:
        return None, None
    return line[-1], line[-2] - sig[-2]
