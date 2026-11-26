from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.engine import TradingEngine
from models import MarketSnapshot, MarketType


class FakeStore:
    def __init__(self, position):
        self._position = position

    async def positions(self):
        return [dict(self._position)]

    async def set_control(self, key, value):
        return None


class FakeConnector:
    def __init__(self):
        self.cancel_calls = 0
        self.snapshot = MarketSnapshot(
            "okx", "BTC/USDT", MarketType.CRYPTO_SPOT, 99.9, 100.0, 99.95
        )

    async def cancel_all_orders(self, symbol=None):
        self.cancel_calls += 1

    async def fetch_snapshot(self, symbol):
        return self.snapshot


def make_engine(mode):
    row = {
        "position_key": "okx:BTC/USDT",
        "venue": "okx",
        "symbol": "BTC/USDT",
        "market_type": "crypto_spot",
        "side": "buy",
        "quantity": 0.01,
        "entry_price": 100,
        "mark_price": 100,
        "stop_loss": 95,
        "take_profit": 110,
        "opened_at": "2026-11-25T00:00:00+00:00",
    }
    engine = TradingEngine.__new__(TradingEngine)
    engine.settings = SimpleNamespace(mode=SimpleNamespace(value=mode))
    engine.store = FakeStore(row)
    connector = FakeConnector()
    engine.connectors = {"okx:spot": connector}
    engine.specs = [{
        "venue": "okx",
        "symbol": "BTC/USDT",
        "connector": "okx:spot",
    }]
    engine.snapshots = {}
    engine._close_position_row = AsyncMock()
    return engine, connector, row


@pytest.mark.asyncio
async def test_paper_emergency_flatten_never_cancels_real_exchange_orders():
    engine, connector, row = make_engine("paper")
    await engine._flatten_all_unlocked()
    assert connector.cancel_calls == 0
    engine._close_position_row.assert_awaited_once()
    args = engine._close_position_row.await_args.args
    assert args[0]["position_key"] == row["position_key"]
    assert args[2] == "emergency_flatten"


@pytest.mark.asyncio
async def test_non_paper_emergency_flatten_cancels_provider_orders_before_close():
    engine, connector, row = make_engine("testnet")
    await engine._flatten_all_unlocked()
    assert connector.cancel_calls == 1
    engine._close_position_row.assert_awaited_once()
