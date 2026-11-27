from datetime import datetime, timezone
import pytest
from src.n9n8.v2.macro_features import (
    average_pairwise_correlation, elasticity, funding_pressure,
    market_session, open_interest_divergence, supply_to_fdv_ratio,
)

def test_supply_metrics_and_elasticity():
    result = supply_to_fdv_ratio(25, 100, 1_000_000)
    assert result["circulating_to_total_ratio"] == 0.25
    assert result["estimated_fdv_usd"] == 4_000_000
    assert elasticity(100, 110, 1000, 1200) > 0

def test_derivatives_positioning_features_are_descriptive():
    assert funding_pressure(0.002) == "crowded_long_pressure"
    assert open_interest_divergence(0.02, -0.01) == "price_up_open_interest_down"

def test_market_sessions_and_correlation():
    sessions = market_session(datetime(2026,11,27,14,0,tzinfo=timezone.utc))
    assert "London" in sessions and "New York" in sessions
    assert average_pairwise_correlation([[1,2,3],[2,4,6],[3,2,1]]) < 1
    with pytest.raises(ValueError, match="timezone-aware"):
        market_session(datetime(2026,11,27,14,0))
