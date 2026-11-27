from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from typing import Iterable, Sequence


ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


def _d(value: object, field: str = "value") -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _series(values: Iterable[object], field: str) -> list[Decimal]:
    result = [_d(item, field) for item in values]
    if not result:
        raise ValueError(f"{field} cannot be empty")
    return result


def _positive_series(values: Iterable[object], field: str) -> list[Decimal]:
    result = _series(values, field)
    if any(item <= ZERO for item in result):
        raise ValueError(f"{field} values must be positive")
    return result


def kelly_fraction(
    win_probability: object,
    payoff_ratio: object,
    *,
    fractional_kelly: object = "0.25",
    max_fraction: object = "0.02",
) -> Decimal:
    """Fractional Kelly sizing, capped at a conservative equity fraction."""
    probability = _d(win_probability, "win_probability")
    payoff = _d(payoff_ratio, "payoff_ratio")
    fraction = _d(fractional_kelly, "fractional_kelly")
    cap = _d(max_fraction, "max_fraction")
    if not ZERO <= probability <= ONE or payoff <= ZERO:
        raise ValueError("win_probability must be in [0, 1] and payoff_ratio positive")
    if not ZERO <= fraction <= ONE or not ZERO <= cap <= ONE:
        raise ValueError("Kelly fraction and cap must be in [0, 1]")
    raw = probability - ((ONE - probability) / payoff)
    return min(cap, max(ZERO, raw * fraction))


def position_size_for_risk(
    equity: object,
    stop_loss_fraction: object,
    *,
    risk_fraction: object = "0.01",
    max_position_fraction: object = "0.10",
) -> Decimal:
    """Return notional sized to the smaller of stop-loss risk and exposure caps."""
    account = _d(equity, "equity")
    stop = _d(stop_loss_fraction, "stop_loss_fraction")
    risk = _d(risk_fraction, "risk_fraction")
    exposure = _d(max_position_fraction, "max_position_fraction")
    if account <= ZERO or stop <= ZERO:
        raise ValueError("equity and stop_loss_fraction must be positive")
    if not ZERO < risk <= ONE or not ZERO < exposure <= ONE:
        raise ValueError("risk_fraction and max_position_fraction must be in (0, 1]")
    return min(account * exposure, account * risk / stop)


def max_drawdown_pct(equity_curve: Sequence[object]) -> Decimal:
    values = _positive_series(equity_curve, "equity")
    peak = values[0]
    worst = ZERO
    for value in values:
        peak = max(peak, value)
        worst = max(worst, (peak - value) / peak)
    return worst * HUNDRED


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("at least one observation is required")
    return sum(values, ZERO) / Decimal(len(values))


def sharpe_ratio(
    returns: Sequence[object],
    *,
    risk_free_per_period: object = "0",
    periods_per_year: int = 365,
) -> Decimal | None:
    values = _series(returns, "return")
    if len(values) < 2 or periods_per_year <= 0:
        raise ValueError("Sharpe requires at least two returns and a positive annualization factor")
    risk_free = _d(risk_free_per_period, "risk_free_per_period")
    excess = [value - risk_free for value in values]
    mean = _mean(excess)
    variance = sum(((item - mean) ** 2 for item in excess), ZERO) / Decimal(len(excess) - 1)
    if variance == ZERO:
        return None
    return (mean / variance.sqrt()) * Decimal(periods_per_year).sqrt()


def sortino_ratio(
    returns: Sequence[object],
    *,
    target_per_period: object = "0",
    periods_per_year: int = 365,
) -> Decimal | None:
    values = _series(returns, "return")
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    target = _d(target_per_period, "target_per_period")
    downside = [min(ZERO, value - target) for value in values]
    deviation = (sum((item ** 2 for item in downside), ZERO) / Decimal(len(downside))).sqrt()
    if deviation == ZERO:
        return None
    return (_mean([value - target for value in values]) / deviation) * Decimal(periods_per_year).sqrt()


def historical_var(
    returns: Sequence[object],
    *,
    confidence: object = "0.95",
    portfolio_value: object = "1",
) -> Decimal:
    """Historical one-period VaR as a non-negative amount, using nearest-rank tail."""
    values = _series(returns, "return")
    level = _d(confidence, "confidence")
    notional = _d(portfolio_value, "portfolio_value")
    if len(values) < 2 or not ZERO < level < ONE or notional < ZERO:
        raise ValueError("VaR requires two returns, confidence in (0, 1) and non-negative notional")
    ordered = sorted(values)
    tail_count = ((ONE - level) * Decimal(len(ordered))).to_integral_value(rounding=ROUND_CEILING)
    index = max(0, min(len(ordered) - 1, int(tail_count) - 1))
    return max(ZERO, -ordered[index]) * notional


@dataclass(frozen=True, slots=True)
class MonteCarloRisk:
    simulations: int
    probability_of_ruin: Decimal
    median_max_drawdown_pct: Decimal
    p95_max_drawdown_pct: Decimal


def monte_carlo_risk(
    trade_returns: Sequence[object],
    *,
    simulations: int = 10_000,
    trades_per_simulation: int | None = None,
    ruin_drawdown_pct: object = "50",
    seed: int = 9108,
) -> MonteCarloRisk:
    """Seeded bootstrap of observed trade returns; not a profit forecast."""
    values = _series(trade_returns, "trade_return")
    if any(value <= -ONE for value in values):
        raise ValueError("trade returns cannot be less than or equal to -100%")
    if not 100 <= simulations <= 100_000:
        raise ValueError("simulations must be between 100 and 100000")
    count = trades_per_simulation or len(values)
    if not 1 <= count <= 100_000:
        raise ValueError("trades_per_simulation must be between 1 and 100000")
    threshold = _d(ruin_drawdown_pct, "ruin_drawdown_pct")
    if not ZERO < threshold <= HUNDRED:
        raise ValueError("ruin_drawdown_pct must be in (0, 100]")
    rng = random.Random(seed)
    drawdowns: list[Decimal] = []
    ruined = 0
    for _ in range(simulations):
        equity = ONE
        peak = ONE
        worst = ZERO
        for _ in range(count):
            equity *= ONE + rng.choice(values)
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak)
        drawdown_pct_value = worst * HUNDRED
        drawdowns.append(drawdown_pct_value)
        if drawdown_pct_value >= threshold:
            ruined += 1
    ordered = sorted(drawdowns)

    def percentile(fraction: Decimal) -> Decimal:
        index = int((Decimal(len(ordered) - 1) * fraction).to_integral_value(rounding=ROUND_HALF_UP))
        return ordered[max(0, min(len(ordered) - 1, index))]

    return MonteCarloRisk(
        simulations=simulations,
        probability_of_ruin=Decimal(ruined) / Decimal(simulations),
        median_max_drawdown_pct=percentile(Decimal("0.50")),
        p95_max_drawdown_pct=percentile(Decimal("0.95")),
    )


def atr(highs: Sequence[object], lows: Sequence[object], closes: Sequence[object], period: int = 14) -> Decimal:
    if period < 1 or len(highs) != len(lows) or len(lows) != len(closes) or len(closes) <= period:
        raise ValueError("ATR needs equal OHLC arrays with more bars than its positive period")
    high_values = [_d(value, "high") for value in highs]
    low_values = [_d(value, "low") for value in lows]
    close_values = [_d(value, "close") for value in closes]
    true_ranges: list[Decimal] = []
    for index in range(1, len(close_values)):
        if high_values[index] < low_values[index] or min(
            high_values[index], low_values[index], close_values[index],
            close_values[index - 1],
        ) <= ZERO:
            raise ValueError("Invalid OHLC values")
        true_ranges.append(max(
            high_values[index] - low_values[index],
            abs(high_values[index] - close_values[index - 1]),
            abs(low_values[index] - close_values[index - 1]),
        ))
    return sum(true_ranges[-period:], ZERO) / Decimal(period)


def vwap(prices: Sequence[object], volumes: Sequence[object]) -> Decimal:
    if len(prices) != len(volumes) or not prices:
        raise ValueError("VWAP requires equally sized non-empty price and volume arrays")
    price_values = [_d(value, "price") for value in prices]
    volume_values = [_d(value, "volume") for value in volumes]
    if any(price <= ZERO for price in price_values) or any(volume < ZERO for volume in volume_values):
        raise ValueError("Prices must be positive and volumes cannot be negative")
    total_volume = sum(volume_values, ZERO)
    if total_volume <= ZERO:
        raise ValueError("VWAP requires positive total volume")
    return sum((price * volume for price, volume in zip(price_values, volume_values)), ZERO) / total_volume


@dataclass(frozen=True, slots=True)
class BollingerBands:
    lower: Decimal
    middle: Decimal
    upper: Decimal


def bollinger_bands(
    closes: Sequence[object],
    *,
    period: int = 20,
    standard_deviations: object = "2",
) -> BollingerBands:
    values = _positive_series(closes, "close")
    multiplier = _d(standard_deviations, "standard_deviations")
    if period < 2 or len(values) < period or multiplier <= ZERO:
        raise ValueError("Bollinger Bands require period >= 2, enough closes and positive multiplier")
    window = values[-period:]
    middle = _mean(window)
    variance = sum(((value - middle) ** 2 for value in window), ZERO) / Decimal(period)
    deviation = variance.sqrt()
    width = deviation * multiplier
    return BollingerBands(lower=middle - width, middle=middle, upper=middle + width)
