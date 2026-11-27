from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Sequence
from zoneinfo import ZoneInfo


def supply_to_fdv_ratio(circulating_supply: object, total_supply: object, market_cap_usd: object) -> dict[str, float]:
    circulating, total, cap = float(circulating_supply), float(total_supply), float(market_cap_usd)
    if not all(isfinite(x) for x in (circulating, total, cap)) or circulating <= 0 or total <= 0 or cap < 0 or circulating > total:
        raise ValueError("supply and market-cap inputs are invalid")
    return {"circulating_to_total_ratio": circulating / total, "market_cap_usd": cap, "estimated_fdv_usd": cap * total / circulating}


def elasticity(price_before: object, price_after: object, quantity_before: object, quantity_after: object) -> float:
    p0, p1, q0, q1 = map(float, (price_before, price_after, quantity_before, quantity_after))
    if not all(isfinite(x) for x in (p0, p1, q0, q1)) or min(p0, p1, q0, q1) <= 0:
        raise ValueError("elasticity inputs must be finite and positive")
    change_price, change_quantity = (p1 - p0) / p0, (q1 - q0) / q0
    if change_price == 0:
        raise ValueError("price did not change; elasticity is undefined")
    return change_quantity / change_price


def funding_pressure(funding_rate: object, *, extreme_positive: float = 0.001, extreme_negative: float = -0.001) -> str:
    value = float(funding_rate)
    if not isfinite(value) or extreme_positive <= 0 or extreme_negative >= 0:
        raise ValueError("funding inputs are invalid")
    if value >= extreme_positive:
        return "crowded_long_pressure"
    if value <= extreme_negative:
        return "crowded_short_pressure"
    return "neutral"


def open_interest_divergence(price_change_fraction: object, open_interest_change_fraction: object) -> str:
    price, oi = float(price_change_fraction), float(open_interest_change_fraction)
    if not isfinite(price) or not isfinite(oi):
        raise ValueError("changes must be finite")
    if price > 0 and oi > 0:
        return "price_up_open_interest_up"
    if price > 0 and oi < 0:
        return "price_up_open_interest_down"
    if price < 0 and oi > 0:
        return "price_down_open_interest_up"
    if price < 0 and oi < 0:
        return "price_down_open_interest_down"
    return "mixed_or_flat"


def market_session(timestamp: datetime) -> tuple[str, ...]:
    """Return overlapping major liquidity-session labels using DST-aware IANA zones."""
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    sessions = (
        ("Asia", "Asia/Tokyo", 9, 17),
        ("London", "Europe/London", 8, 16),
        ("New York", "America/New_York", 8, 17),
    )
    labels = []
    for name, zone, start, end in sessions:
        local = timestamp.astimezone(ZoneInfo(zone))
        if start <= local.hour < end:
            labels.append(name)
    return tuple(labels)


def average_pairwise_correlation(returns: Sequence[Sequence[object]]) -> float:
    rows = [[float(value) for value in series] for series in returns]
    if len(rows) < 2 or not rows[0]:
        raise ValueError("at least two non-empty return series are required")
    size = len(rows[0])
    if any(len(row) != size or any(not isfinite(value) for value in row) for row in rows):
        raise ValueError("return matrix must be rectangular and finite")
    correlations = []
    for index in range(len(rows)):
        for other in range(index + 1, len(rows)):
            a, b = rows[index], rows[other]
            ma, mb = sum(a) / size, sum(b) / size
            va, vb = sum((x - ma) ** 2 for x in a), sum((x - mb) ** 2 for x in b)
            if va == 0 or vb == 0:
                continue
            covariance = sum((x - ma) * (y - mb) for x, y in zip(a, b))
            correlations.append(covariance / (va * vb) ** 0.5)
    if not correlations:
        raise ValueError("correlation is undefined for constant return series")
    return sum(correlations) / len(correlations)
