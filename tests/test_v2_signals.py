from decimal import Decimal

import pytest

from src.n9n8.v2.signals import classify_simple_regime


def test_simple_regime_classifier_is_transparent_and_deterministic():
    closes = [Decimal("10") + Decimal(index) / Decimal("10") for index in range(60)]
    highs = [value + Decimal("0.2") for value in closes]
    lows = [value - Decimal("0.2") for value in closes]
    result = classify_simple_regime(highs, lows, closes)
    assert result.regime == "risk_on_trend"
    assert result.fast_average > result.slow_average


def test_simple_regime_rejects_short_series():
    with pytest.raises(ValueError, match="enough candles"):
        classify_simple_regime([1, 2, 3], [1, 2, 3], [1, 2, 3])
