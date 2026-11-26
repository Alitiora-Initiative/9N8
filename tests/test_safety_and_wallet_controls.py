from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from src.n9n8.bot_handlers import _validated_setting
from src.n9n8.database import Database


@pytest.mark.asyncio
async def test_wallet_reveal_is_one_time_and_scoped_to_wallet_address(tmp_path):
    db = Database(str(tmp_path / "9n8-test.sqlite3"), Fernet.generate_key())
    await db.initialize()
    await db.ensure_user(90123)
    await db.save_wallet(90123, "solana-wallet-one", "private-key-one")

    assert await db.wallet_key_was_revealed(90123, "solana-wallet-one") is False
    await db.security_event(90123, "wallet_key_revealed", "solana-wallet-one")
    assert await db.wallet_key_was_revealed(90123, "solana-wallet-one") is True
    assert await db.wallet_key_was_revealed(90123, "solana-wallet-two") is False

    await db.save_wallet(90123, "solana-wallet-two", "private-key-two")
    assert await db.wallet_key_was_revealed(90123, "solana-wallet-two") is False


@pytest.mark.asyncio
async def test_rejected_tokens_are_persisted_for_history(tmp_path):
    db = Database(str(tmp_path / "9n8-test.sqlite3"), Fernet.generate_key())
    await db.initialize()
    await db.ensure_user(90124)
    await db.security_event(90124, "token_rejected", "MintA | top-10 holders exceeded 25%")
    await db.security_event(90124, "wallet_created", "wallet created")

    rows = await db.recent_rejections(90124)
    assert len(rows) == 1
    assert rows[0]["detail"] == "MintA | top-10 holders exceeded 25%"


@pytest.mark.asyncio
async def test_live_and_paper_settings_include_separate_sell_slippage(tmp_path):
    db = Database(str(tmp_path / "9n8-test.sqlite3"), Fernet.generate_key())
    await db.initialize()
    await db.ensure_user(90125)

    live = await db.get_settings(90125)
    paper = await db.get_settings(90125, mode="paper")
    assert live["sell_slippage_pct"] == "20"
    assert paper["sell_slippage_pct"] == "20"

    saved = await db.update_settings(90125, {"sell_slippage_pct": "18.5"})
    assert saved["sell_slippage_pct"] == "18.5"


def test_sell_slippage_is_validated_separately():
    assert _validated_setting("sell_slippage_pct", "20") == "20"
    with pytest.raises(ValueError):
        _validated_setting("sell_slippage_pct", "75")
