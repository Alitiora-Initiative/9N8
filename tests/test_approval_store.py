from datetime import datetime, timedelta, timezone

import pytest

from storage import TradeStore


@pytest.mark.asyncio
async def test_approval_is_persisted_and_can_only_be_claimed_once(tmp_path):
    store = TradeStore(tmp_path / "approval.sqlite3")
    await store.open()
    try:
        expires = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
        payload = {
            "intent": {
                "venue": "bybit",
                "symbol": "BTC/USDT",
                "side": "buy",
                "entry_price": 50000.0,
            },
            "size": {"quantity": 0.001, "notional_usd": 50.0, "risk_usd": 0.5},
        }

        assert await store.create_pending_approval(
            "APPROVAL001", "bybit:BTC/USDT:buy:123", expires, payload
        )
        assert not await store.create_pending_approval(
            "APPROVAL002", "bybit:BTC/USDT:buy:123", expires, payload
        )

        pending = await store.get_pending_approval("APPROVAL001")
        assert pending is not None
        assert pending["status"] == "PENDING"
        assert pending["payload"] == payload
        assert len(await store.pending_approvals()) == 1

        assert await store.claim_pending_approval("APPROVAL001")
        assert not await store.claim_pending_approval("APPROVAL001")
        assert await store.pending_approvals() == []

        await store.decide_pending_approval(
            "APPROVAL001", "APPROVED", "123456", "Filled 0.001 at 50001."
        )
        decided = await store.get_pending_approval("APPROVAL001")
        assert decided is not None
        assert decided["status"] == "APPROVED"
        assert decided["decided_by"] == "123456"
        assert decided["result"] == "Filled 0.001 at 50001."
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reconciliation_required_is_a_persistent_terminal_state(tmp_path):
    store = TradeStore(tmp_path / "reconciliation.sqlite3")
    await store.open()
    try:
        expires = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
        assert await store.create_pending_approval(
            "APPROVAL003", "okx:ETH/USDT:buy:456", expires, {"intent": {}}
        )
        assert await store.claim_pending_approval("APPROVAL003")
        await store.decide_pending_approval(
            "APPROVAL003",
            "RECONCILIATION_REQUIRED",
            "123456",
            "Provider submission outcome could not be verified.",
        )
        result = await store.get_pending_approval("APPROVAL003")
        assert result is not None
        assert result["status"] == "RECONCILIATION_REQUIRED"
        assert not await store.claim_pending_approval("APPROVAL003")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stale_pending_approvals_are_expired_automatically(tmp_path):
    store = TradeStore(tmp_path / "expiry.sqlite3")
    await store.open()
    try:
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await store.create_pending_approval(
            "APPROVALEXP", "bybit:BTC/USDT:buy:999", expired, {"intent": {}}
        )
        assert await store.pending_approvals() == []
        row = await store.get_pending_approval("APPROVALEXP")
        assert row is not None
        assert row["status"] == "EXPIRED"
        assert row["decided_by"] == "system"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_restart_recovers_interrupted_execution_and_locks_entries(tmp_path):
    store = TradeStore(tmp_path / "restart-recovery.sqlite3")
    await store.open()
    try:
        expires = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
        await store.create_pending_approval(
            "APPROVALREC", "okx:BTC/USDT:buy:777", expires, {"intent": {}}
        )
        assert await store.claim_pending_approval("APPROVALREC")

        recovered = await store.recover_interrupted_approvals()
        assert recovered == 1
        assert await store.get_control("execution_reconciliation_required") == "true"
        assert await store.get_control("kill_switch") == "true"
        assert await store.get_control("paused") == "true"

        row = await store.get_pending_approval("APPROVALREC")
        assert row is not None
        assert row["status"] == "RECONCILIATION_REQUIRED"
        assert row["decided_by"] == "startup-recovery"
        assert await store.recover_interrupted_approvals() == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_position_metadata_round_trips_through_store(tmp_path):
    store = TradeStore(tmp_path / "position-metadata.sqlite3")
    await store.open()
    try:
        position = {
            "position_key": "okx:BTC/USDT",
            "venue": "okx",
            "symbol": "BTC/USDT",
            "market_type": "crypto_spot",
            "side": "buy",
            "quantity": 0.01,
            "entry_price": 50000.0,
            "mark_price": 50100.0,
            "stop_loss": 49000.0,
            "take_profit": 53000.0,
            "metadata": {
                "order_id": "paper-order-1",
                "entry_fee_paid": 0.5,
                "entry_fee_remaining": 0.25,
                "paper_fees_paid": 0.25,
            },
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await store.upsert_position(position)
        row = (await store.positions())[0]
        assert row["metadata"]["order_id"] == "paper-order-1"
        assert row["metadata"]["entry_fee_remaining"] == pytest.approx(0.25)
        # Re-saving a row read from storage must not erase metadata_json.
        row["mark_price"] = 50200.0
        await store.upsert_position(row)
        again = (await store.positions())[0]
        assert again["metadata"]["paper_fees_paid"] == pytest.approx(0.25)
    finally:
        await store.close()
