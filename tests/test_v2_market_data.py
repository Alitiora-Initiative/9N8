from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.n9n8.v2.market_data import MarketDataQualityError, normalize_ohlcv


NOW = datetime(2026, 11, 27, 12, 10, tzinfo=timezone.utc)


def test_normalizes_epoch_milliseconds_to_utc_and_sorts():
    rows = [
        [1795781100000, "2", "2.2", "1.9", "2.1", "100"],
        [1795780800000, "1", "1.2", "0.9", "1.1", "90"],
    ]
    result = normalize_ohlcv(
        rows, symbol="SOL/USDC", source="fixture", interval="5m",
        now=datetime(2026, 11, 27, 12, 10, tzinfo=timezone.utc),
    )
    assert [item.open for item in result] == [Decimal("1"), Decimal("2")]
    assert all(item.opened_at.tzinfo == timezone.utc for item in result)


def test_drops_unclosed_bar_by_default():
    rows = [
        {"timestamp": "2026-11-27T12:00:00Z", "open": "10", "high": "11",
         "low": "9", "close": "10.5", "volume": "5"},
        {"timestamp": "2026-11-27T12:10:00Z", "open": "10.5", "high": "11",
         "low": "10", "close": "10.8", "volume": "2"},
    ]
    result = normalize_ohlcv(rows, symbol="SOL/USDC", source="fixture", interval="5m", now=NOW)
    assert len(result) == 1
    assert result[0].opened_at == datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def test_naive_source_time_is_converted_from_declared_timezone():
    row = [["2026-11-27T15:00:00", "10", "11", "9", "10", "5"]]
    result = normalize_ohlcv(
        row, symbol="BTC/USDT", source="fixture", interval="5m",
        source_timezone="Africa/Nairobi", now=NOW,
    )
    assert result[0].opened_at == datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def test_rejects_duplicate_and_invalid_bars():
    duplicate = [
        ["2026-11-27T12:00:00Z", "1", "1.2", "0.9", "1.1", "90"],
        ["2026-11-27T12:00:00Z", "1", "1.2", "0.9", "1.1", "90"],
    ]
    with pytest.raises(MarketDataQualityError, match="Duplicate"):
        normalize_ohlcv(duplicate, symbol="SOL/USDC", source="fixture", interval="5m", now=NOW)
    invalid = [["2026-11-27T12:00:00Z", "1", "0.8", "0.9", "1.1", "90"]]
    with pytest.raises(MarketDataQualityError, match="high"):
        normalize_ohlcv(invalid, symbol="SOL/USDC", source="fixture", interval="5m", now=NOW)


def test_rejects_unknown_timezones_and_future_candles():
    with pytest.raises(MarketDataQualityError, match="Unknown source timezone"):
        normalize_ohlcv(
            [["2026-11-27T12:00:00", "1", "1.2", "0.9", "1.1", "90"]],
            symbol="SOL/USDC", source="fixture", interval="5m",
            source_timezone="Not/AZone", now=NOW,
        )
    with pytest.raises(MarketDataQualityError, match="Future-dated"):
        normalize_ohlcv(
            [["2026-11-27T12:11:00Z", "1", "1.2", "0.9", "1.1", "90"]],
            symbol="SOL/USDC", source="fixture", interval="5m", now=NOW,
        )
