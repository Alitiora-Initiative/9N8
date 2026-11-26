import pytest
from cryptography.fernet import Fernet

from src.n9n8.database import Database
from src.n9n8.networks import NETWORKS, execution_enabled, get_network


def test_network_selector_exposes_four_networks_but_only_sol_is_executable():
    assert [network.code for network in NETWORKS] == [
        "solana", "base", "ethereum", "bnb"
    ]
    assert execution_enabled("solana") is True
    assert execution_enabled("base") is False
    assert execution_enabled("ethereum") is False
    assert execution_enabled("bnb") is False
    assert get_network("BASE").name == "Base"
    assert get_network("invalid") is None


@pytest.mark.asyncio
async def test_wallet_reveal_marker_is_one_time_and_bound_to_wallet_address(tmp_path):
    db = Database(str(tmp_path / "wallet-reveal.sqlite3"), Fernet.generate_key())
    await db.initialize()
    await db.ensure_user(98765)
    await db.save_wallet(98765, "solana-address-one", "encrypted-private-key-one")

    assert await db.wallet_key_was_revealed(98765, "solana-address-one") is False
    await db.security_event(98765, "wallet_key_revealed", "solana-address-one")
    assert await db.wallet_key_was_revealed(98765, "solana-address-one") is True

    # A rotated wallet gets a fresh one-time reveal.
    await db.save_wallet(98765, "solana-address-two", "encrypted-private-key-two")
    assert await db.wallet_key_was_revealed(98765, "solana-address-two") is False

    # Reveal state is isolated by Telegram account.
    await db.ensure_user(98766)
    assert await db.wallet_key_was_revealed(98766, "solana-address-one") is False


@pytest.mark.asyncio
async def test_rejection_history_is_available_for_telegram_trade_history(tmp_path):
    db = Database(str(tmp_path / "rejections.sqlite3"), Fernet.generate_key())
    await db.initialize()
    await db.ensure_user(98767)
    await db.security_event(
        98767,
        "token_rejected",
        "SoMint111 | top-10 wallet concentration exceeded 25%",
    )

    rows = await db.list_security_events(98767, 10)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "token_rejected"
    assert "top-10 wallet concentration" in rows[0]["detail"]
