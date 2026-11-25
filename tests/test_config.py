import json

import pytest

from config import Settings
from models import RunMode, TradingError


def base_config():
    return {
        "app": {"database_path": "data/test.sqlite3", "log_level": "INFO"},
        "execution": {
            "mode": "paper",
            "live_trading": False,
            "allow_live_trading": False,
            "require_live_acknowledgement": True,
            "live_acknowledgement": "I_UNDERSTAND_REAL_FUNDS_CAN_BE_LOST",
        },
    }


def test_default_configuration_is_paper(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(base_config()), encoding="utf-8")
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    settings = Settings.load()
    assert settings.mode is RunMode.PAPER
    assert settings.live_enabled is False


def test_live_mode_requires_all_environment_gates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(base_config()), encoding="utf-8")
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("LIVE_TRADING", "true")
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "false")
    monkeypatch.setenv("LIVE_ACKNOWLEDGEMENT", "I_UNDERSTAND_REAL_FUNDS_CAN_BE_LOST")
    with pytest.raises(TradingError):
        Settings.load()


def test_live_mode_requires_exact_acknowledgement(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = base_config()
    cfg["execution"]["allow_live_trading"] = True
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("LIVE_TRADING", "true")
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
    monkeypatch.setenv("LIVE_ACKNOWLEDGEMENT", "yes")
    with pytest.raises(TradingError):
        Settings.load()
