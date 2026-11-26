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
