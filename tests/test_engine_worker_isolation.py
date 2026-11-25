import pytest
from unittest.mock import AsyncMock

from core.engine import TradingEngine


class FakePositionStore:
    def __init__(self, rows):
        self.rows = rows
        self.events = []

    async def positions(self):
        return list(self.rows)

    async def event(self, level, event_type, message, venue=None, symbol=None, metadata=None):
        self.events.append({
            "level": level,
            "event_type": event_type,
            "message": message,
            "venue": venue,
            "symbol": symbol,
            "metadata": metadata or {},
        })


def _row(key, symbol):
    return {
        "position_key": key,
        "venue": "okx",
        "symbol": symbol,
    }


@pytest.mark.asyncio
async def test_one_position_monitor_failure_does_not_skip_other_positions():
    engine = TradingEngine.__new__(TradingEngine)
    first = _row("okx:BAD/USDT", "BAD/USDT")
    second = _row("okx:GOOD/USDT", "GOOD/USDT")
    engine.store = FakePositionStore([first, second])
    engine.notifier = None
    engine._monitor_position_row = AsyncMock(
        side_effect=[RuntimeError("bad stale quote"), None]
    )

    await engine._monitor_positions_once()

    assert engine._monitor_position_row.await_count == 2
    assert engine._monitor_position_row.await_args_list[0].args[0]["symbol"] == "BAD/USDT"
    assert engine._monitor_position_row.await_args_list[1].args[0]["symbol"] == "GOOD/USDT"
    assert len(engine.store.events) == 1
    assert engine.store.events[0]["event_type"] == "position_monitor_error"


@pytest.mark.asyncio
async def test_repeated_position_monitor_failure_is_rate_limited():
    engine = TradingEngine.__new__(TradingEngine)
    row = _row("okx:BAD/USDT", "BAD/USDT")
    engine.store = FakePositionStore([row])
    engine.notifier = None
    engine._monitor_position_row = AsyncMock(side_effect=RuntimeError("offline"))

    await engine._monitor_positions_once()
    await engine._monitor_positions_once()
    await engine._monitor_positions_once()

    assert engine._monitor_position_row.await_count == 3
    assert len(engine.store.events) == 1
