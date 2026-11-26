import pytest
from cryptography.fernet import Fernet

from src.n9n8.config import Settings


def test_authorized_user_id_is_required(monkeypatch):
    monkeypatch.delenv("AUTHORIZED_USER_ID", raising=False)
    with pytest.raises(RuntimeError, match="AUTHORIZED_USER_ID"):
        Settings.from_env()


@pytest.mark.parametrize("value", ["0", "-3", "abc", "1.25"])
def test_authorized_user_id_must_be_a_positive_integer(monkeypatch, value):
    monkeypatch.setenv("AUTHORIZED_USER_ID", value)
    with pytest.raises(RuntimeError, match="AUTHORIZED_USER_ID"):
        Settings.from_env()


def test_settings_load_the_single_authorized_telegram_user(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-telegram-token")
    monkeypatch.setenv("AUTHORIZED_USER_ID", "123456789")
    monkeypatch.setenv("HELIUS_API_KEY", "test-helius-key")
    monkeypatch.setenv("MASTER_TREASURY_ADDRESS", "11111111111111111111111111111111")
    monkeypatch.setenv("ENCRYPTION_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("N9N8_TRADING_MODE", "paper")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("N9N8_REQUIRE_POSTGRES", "false")

    settings = Settings.from_env()

    assert settings.authorized_user_id == 123456789
    assert settings.live_trading_enabled is False
    assert settings.trading_mode == "paper"
