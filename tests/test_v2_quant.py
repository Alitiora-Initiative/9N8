from decimal import Decimal

import pytest

from src.n9n8.v2.quant import (
    atr, bollinger_bands, historical_var, kelly_fraction, max_drawdown_pct,
    monte_carlo_risk, position_size_for_risk, sharpe_ratio, sortino_ratio, vwap,
)


def test_fractional_kelly_is_capped_and_never_negative():
    assert kelly_fraction("0.6", "2") == Decimal("0.02")
    assert kelly_fraction("0.2", "1") == Decimal("0")
    assert kelly_fraction("0.6", "2", fractional_kelly="0.25", max_fraction="0.05") == Decimal("0.05")


def test_position_size_caps_risk_and_total_exposure():
    assert position_size_for_risk("1000", "0.10", risk_fraction="0.01", max_position_fraction="0.10") == Decimal("100")
    assert position_size_for_risk("1000", "0.50", risk_fraction="0.01", max_position_fraction="0.10") == Decimal("20")


def test_sharpe_sortino_and_drawdown_are_deterministic():
    returns = ["0.02", "-0.01", "0.03", "-0.02"]
    assert sharpe_ratio(returns) is not None
    assert sortino_ratio(returns) is not None
    assert max_drawdown_pct(["100", "110", "99", "105"]) == Decimal("10")


def test_zero_downside_or_zero_variance_is_reported_as_undefined():
    assert sharpe_ratio(["0.01", "0.01"]) is None
    assert sortino_ratio(["0.01", "0.02"]) is None


def test_historical_var_uses_nearest_rank_tail():
    result = historical_var(["-0.10", "0.02", "-0.03", "0.01"], confidence="0.75", portfolio_value="100")
    assert result == Decimal("10")
    with pytest.raises(ValueError):
        historical_var(["0.1"], confidence="0.95")


def test_monte_carlo_is_reproducible_and_reports_tail_risk():
    first = monte_carlo_risk(["-0.20", "0.05", "0.10"], simulations=500, seed=12)
    second = monte_carlo_risk(["-0.20", "0.05", "0.10"], simulations=500, seed=12)
    assert first == second
    assert first.simulations == 500
    assert Decimal("0") <= first.probability_of_ruin <= Decimal("1")
    assert first.p95_max_drawdown_pct >= first.median_max_drawdown_pct


def test_atr_vwap_and_bollinger_bands():
    assert atr(["10", "12", "11"], ["9", "10", "10"], ["9.5", "11", "10.5"], period=2) == Decimal("1.75")
    assert vwap(["10", "20"], ["1", "3"]) == Decimal("17.5")
    bands = bollinger_bands(["10", "12", "14"], period=3)
    assert bands.lower < bands.middle < bands.upper
