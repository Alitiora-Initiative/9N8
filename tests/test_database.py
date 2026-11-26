import pytest
from cryptography.fernet import Fernet

from src.n9n8.database import Database


@pytest.mark.asyncio
async def test_wallet_secret_is_encrypted_and_round_trips(tmp_path):
    path = str(tmp_path / "9n8.sqlite3")
    key = Fernet.generate_key()
    db = Database(path, key)
    await db.initialize()
    await db.ensure_user(123456)
    await db.save_wallet(123456, "11111111111111111111111111111111", "private-key-test")
    assert await db.load_secret(123456) == "private-key-test"
    user = await db.get_user(123456)
    assert user["solana_address"] == "11111111111111111111111111111111"
    assert b"private-key-test" not in bytes(user["encrypted_secret"])


@pytest.mark.asyncio
async def test_settings_are_persisted_and_unknown_settings_rejected(tmp_path):
    db = Database(str(tmp_path / "db.sqlite3"), Fernet.generate_key())
    await db.initialize()
    await db.ensure_user(5)
    settings = await db.update_settings(5, {"take_profit_pct": "12.5", "mev_shield": False})
    assert settings["take_profit_pct"] == "12.5"
    assert settings["mev_shield"] is False
    with pytest.raises(ValueError):
        await db.update_settings(5, {"not_a_setting": True})


@pytest.mark.asyncio
async def test_daily_risk_ledger_persists_trade_count_and_pnl(tmp_path):
    db = Database(str(tmp_path / "db.sqlite3"), Fernet.generate_key())
    await db.initialize()
    await db.ensure_user(8)
    await db.start_daily_risk(8, "2026-11-26", 1_000_000)
    await db.record_daily_entry(8, "2026-11-26")
    await db.record_daily_pnl(8, "2026-11-26", -250)
    result = await db.daily_risk_snapshot(8, "2026-11-26")
    assert result == {
        "starting_equity_lamports": 1_000_000,
        "realized_pnl_lamports": -250,
        "trade_count": 1,
    }


@pytest.mark.asyncio
async def test_fee_receipt_is_idempotency_lookup_and_daily_baseline_can_initialize(tmp_path):
    db = Database(str(tmp_path / "db.sqlite3"), Fernet.generate_key())
    await db.initialize()
    await db.ensure_user(22)
    await db.record_fee(22, "activation", 5_000_000, "sig-123", "activation:22")
    receipt = await db.fee_receipt("activation:22")
    assert receipt is not None
    assert receipt["tx_signature"] == "sig-123"
    assert receipt["amount_lamports"] == 5_000_000

    await db.start_daily_risk(22, "2026-11-26", 0)
    await db.start_daily_risk(22, "2026-11-26", 700_000)
    snapshot = await db.daily_risk_snapshot(22, "2026-11-26")
    assert snapshot["starting_equity_lamports"] == 700_000


@pytest.mark.asyncio
async def test_wallet_retirement_clears_activation_and_key(tmp_path):
    db = Database(str(tmp_path / "db.sqlite3"), Fernet.generate_key())
    await db.initialize()
    await db.ensure_user(77)
    await db.save_wallet(77, "11111111111111111111111111111111", "secret-material")
    await db.set_activation_paid(77, True)
    await db.delete_wallet_secret(77)

    user = await db.get_user(77)
    assert user["solana_address"] is None
    assert user["encrypted_secret"] is None
    assert user["activation_paid"] == 0
    assert user["enabled"] == 0
