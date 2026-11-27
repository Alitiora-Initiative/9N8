from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.n9n8.v2.risk import QuoteRiskInput, RiskLimits, assess_jupiter_quote, assess_quote


NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def test_quote_risk_assessment_accepts_fresh_quote_within_limits():
    quote = QuoteRiskInput(NOW, Decimal("0.005"), 80, Decimal("100000"), 1000)
    result = assess_quote(
        quote, limits=RiskLimits(max_price_impact_fraction=Decimal("0.02"),
                                 max_slippage_bps=150, min_liquidity_usd=Decimal("50000")),
        now=NOW,
    )
    assert result.approved
    assert result.blockers == ()
    assert result.quote_age_seconds == 0


def test_quote_risk_assessment_rejects_stale_impact_slippage_and_thin_liquidity():
    quote = QuoteRiskInput(NOW - timedelta(seconds=40), Decimal("0.05"), 300, Decimal("10"), 1000)
    result = assess_quote(
        quote, limits=RiskLimits(min_liquidity_usd=Decimal("1000")), now=NOW,
    )
    assert not result.approved
    assert set(result.blockers) == {
        "quote_is_stale", "price_impact_exceeds_limit",
        "estimated_slippage_exceeds_limit", "liquidity_below_minimum",
    }


def test_quote_risk_rejects_naive_and_future_timestamps():
    with pytest.raises(ValueError, match="timezone-aware"):
        QuoteRiskInput(datetime(2026, 11, 27, 12, 0), Decimal("0"), 0, Decimal("0"), 1)
    quote = QuoteRiskInput(NOW + timedelta(seconds=10), Decimal("0"), 0, Decimal("0"), 1)
    result = assess_quote(quote, limits=RiskLimits(), now=NOW)
    assert "quote_timestamp_is_in_the_future" in result.blockers



def test_jupiter_quote_preflight_uses_fractional_impact_and_verified_liquidity():
    quote = {"outAmount": "1000", "priceImpactPct": "0.01"}
    result = assess_jupiter_quote(
        quote, observed_at=NOW, liquidity_usd="100000", slippage_bps=100,
        max_price_impact_pct="2", max_slippage_bps=150,
        min_liquidity_usd="50000", now=NOW,
    )
    assert result.approved
    assert result.quote_age_seconds == 0


def test_jupiter_quote_preflight_fails_closed_on_missing_or_excessive_inputs():
    with pytest.raises(ValueError, match="priceImpactPct"):
        assess_jupiter_quote(
            {"outAmount": "100"}, observed_at=NOW, liquidity_usd="100000",
            slippage_bps=50, now=NOW,
        )
    result = assess_jupiter_quote(
        {"outAmount": "1000", "priceImpactPct": "0.03"},
        observed_at=NOW, liquidity_usd="10", slippage_bps=300,
        max_price_impact_pct="2", max_slippage_bps=250,
        min_liquidity_usd="1000", now=NOW,
    )
    assert not result.approved
    assert set(result.blockers) == {
        "price_impact_exceeds_limit",
        "estimated_slippage_exceeds_limit",
        "liquidity_below_minimum",
    }
