from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from connectors.alpaca import AlpacaConnector
from models import ConnectorUnavailable, MarketType, Side, TradeIntent


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params=None):
        self.calls.append((url, params))
        payload = self.responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload


def connector_with_responses(responses):
    connector = AlpacaConnector("key", "secret", "iex")
    connector.session = FakeSession(responses)
    return connector


@pytest.mark.asyncio
async def test_latest_quote_uses_iex_bid_ask_sizes_and_utc_timestamp():
    connector = connector_with_responses([
        FakeResponse({
            "symbol": "AAPL",
            "quote": {
                "bp": 200.10, "ap": 200.12, "bs": 20, "as": 15,
                "t": "2026-11-25T15:30:00.123456789Z",
            },
        })
    ])

    snapshot = await connector.fetch_snapshot("aapl")

    assert snapshot.market_type is MarketType.STOCK
    assert snapshot.symbol == "AAPL"
    assert snapshot.bid == pytest.approx(200.10)
    assert snapshot.ask == pytest.approx(200.12)
    assert snapshot.last == pytest.approx(200.11)
    assert snapshot.bid_size == pytest.approx(20)
    assert snapshot.ask_size == pytest.approx(15)
    assert snapshot.timestamp == datetime(2026, 11, 25, 15, 30, 0, 123456, tzinfo=timezone.utc)
    assert connector.session.calls[0][1] == {"feed": "iex"}


@pytest.mark.asyncio
async def test_latest_quote_rejects_crossed_or_missing_size_quote():
    crossed = connector_with_responses([
        FakeResponse({
            "quote": {
                "bp": 200.20, "ap": 200.10, "bs": 10, "as": 10,
                "t": "2026-11-25T15:30:00Z",
            },
        })
    ])
    with pytest.raises(ConnectorUnavailable, match="crossed"):
        await crossed.fetch_snapshot("AAPL")

    no_size = connector_with_responses([
        FakeResponse({
            "quote": {
                "bp": 200.10, "ap": 200.12, "bs": 0, "as": 10,
                "t": "2026-11-25T15:30:00Z",
            },
        })
    ])
    with pytest.raises(ConnectorUnavailable, match="bid size"):
        await no_size.fetch_snapshot("AAPL")


@pytest.mark.asyncio
async def test_historical_bars_are_parsed_sorted_and_limited():
    connector = connector_with_responses([
        FakeResponse({
            "bars": {
                "AAPL": [
                    {"t": "2026-11-25T15:10:00Z", "o": 10, "h": 12, "l": 9, "c": 11, "v": 100},
                    {"t": "2026-11-25T15:05:00Z", "o": 8, "h": 10, "l": 7, "c": 9, "v": 90},
                    {"t": "2026-11-25T15:15:00Z", "o": 11, "h": 12, "l": 10, "c": 11.5, "v": 80},
                    {"t": "2026-11-25T15:20:00Z", "o": 9, "h": 8, "l": 7, "c": 8, "v": 20},
                ]
            }
        })
    ])

    candles = await connector.fetch_candles("AAPL", "5m", 2)

    assert len(candles) == 2
    assert candles[0].open == 8
    assert candles[1].close == 11
    url, params = connector.session.calls[0]
    assert url.endswith("/v2/stocks/bars")
    assert params["timeframe"] == "5Min"
    assert params["symbols"] == "AAPL"
    assert params["feed"] == "iex"


@pytest.mark.asyncio
async def test_invalid_symbols_and_unsupported_timeframes_are_rejected():
    connector = connector_with_responses([])
    with pytest.raises(ValueError, match="ticker"):
        await connector.fetch_snapshot("AAPL?limit=100")
    with pytest.raises(ValueError, match="timeframe"):
        await connector.fetch_candles("AAPL", "2w", 30)


@pytest.mark.asyncio
async def test_live_order_methods_are_not_implemented():
    connector = connector_with_responses([])
    intent = TradeIntent(
        "alpaca-stocks", "AAPL", MarketType.STOCK, Side.BUY,
        200, 195, 215, 5, "test",
    )
    with pytest.raises(ConnectorUnavailable, match="paper-data only"):
        await connector.buy(intent, 1, "test-order")
