from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .contracts import decimal_value


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_price_impact_fraction: Decimal = Decimal("0.02")
    max_slippage_bps: int = 150
    max_daily_drawdown_pct: Decimal = Decimal("10")
    max_position_fraction: Decimal = Decimal("0.10")
    min_liquidity_usd: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not Decimal("0") < self.max_price_impact_fraction <= Decimal("1"):
            raise ValueError("max_price_impact_fraction must be in (0, 1]")
        if not 0 <= self.max_slippage_bps <= 10_000:
            raise ValueError("max_slippage_bps must be between 0 and 10000")
        if not Decimal("0") < self.max_daily_drawdown_pct <= Decimal("100"):
            raise ValueError("max_daily_drawdown_pct must be in (0, 100]")
        if not Decimal("0") < self.max_position_fraction <= Decimal("1"):
            raise ValueError("max_position_fraction must be in (0, 1]")
        if self.min_liquidity_usd < 0:
            raise ValueError("min_liquidity_usd cannot be negative")


@dataclass(frozen=True, slots=True)
class QuoteRiskInput:
    observed_at: datetime
    price_impact_fraction: Decimal
    estimated_slippage_bps: int
    liquidity_usd: Decimal
    expected_out_raw: int

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("quote observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(timezone.utc))
        object.__setattr__(
            self, "price_impact_fraction",
            decimal_value(self.price_impact_fraction, "price_impact_fraction"),
        )
        object.__setattr__(self, "liquidity_usd", decimal_value(self.liquidity_usd, "liquidity_usd"))
        if self.price_impact_fraction < 0 or self.liquidity_usd < 0:
            raise ValueError("price impact and liquidity cannot be negative")
        if self.estimated_slippage_bps < 0 or self.expected_out_raw <= 0:
            raise ValueError("slippage must be non-negative and expected output positive")


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    approved: bool
    blockers: tuple[str, ...]
    quote_age_seconds: int


def assess_quote(
    quote: QuoteRiskInput,
    *,
    limits: RiskLimits,
    now: datetime | None = None,
    max_quote_age_seconds: int = 20,
) -> RiskAssessment:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    age = int((current - quote.observed_at).total_seconds())
    blockers: list[str] = []
    if age < -2:
        blockers.append("quote_timestamp_is_in_the_future")
    elif age > max_quote_age_seconds:
        blockers.append("quote_is_stale")
    if quote.price_impact_fraction > limits.max_price_impact_fraction:
        blockers.append("price_impact_exceeds_limit")
    if quote.estimated_slippage_bps > limits.max_slippage_bps:
        blockers.append("estimated_slippage_exceeds_limit")
    if quote.liquidity_usd < limits.min_liquidity_usd:
        blockers.append("liquidity_below_minimum")
    return RiskAssessment(not blockers, tuple(blockers), max(0, age))



def assess_jupiter_quote(
    quote: object,
    *,
    observed_at: datetime,
    liquidity_usd: object,
    slippage_bps: int,
    max_price_impact_pct: object = "2",
    max_slippage_bps: int = 5000,
    min_liquidity_usd: object = "0",
    now: datetime | None = None,
    max_quote_age_seconds: int = 20,
) -> RiskAssessment:
    """Validate Jupiter's actual quote against the V2 execution risk policy.

    Jupiter's priceImpactPct is a fraction (0.01 = 1%). The configured maximum
    price impact is a percentage (1 = 1%). Missing fields fail closed.
    """
    from collections.abc import Mapping

    if not isinstance(quote, Mapping):
        raise ValueError("Jupiter quote must be an object")
    impact_raw = quote.get("priceImpactPct")
    output_raw = quote.get("outAmount")
    if impact_raw is None or output_raw is None:
        raise ValueError("Jupiter quote is missing priceImpactPct or outAmount")
    try:
        expected_out = int(str(output_raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("Jupiter quote outAmount is invalid") from exc
    if expected_out <= 0:
        raise ValueError("Jupiter quote outAmount must be positive")
    impact_pct = decimal_value(max_price_impact_pct, "max_price_impact_pct")
    if impact_pct <= 0 or impact_pct > Decimal("100"):
        raise ValueError("max_price_impact_pct must be in (0, 100]")
    limits = RiskLimits(
        max_price_impact_fraction=impact_pct / Decimal("100"),
        max_slippage_bps=int(max_slippage_bps),
        min_liquidity_usd=decimal_value(min_liquidity_usd, "min_liquidity_usd"),
    )
    input_value = QuoteRiskInput(
        observed_at=observed_at,
        price_impact_fraction=decimal_value(impact_raw, "priceImpactPct"),
        estimated_slippage_bps=int(slippage_bps),
        liquidity_usd=decimal_value(liquidity_usd, "liquidity_usd"),
        expected_out_raw=expected_out,
    )
    return assess_quote(
        input_value,
        limits=limits,
        now=now,
        max_quote_age_seconds=max_quote_age_seconds,
    )
