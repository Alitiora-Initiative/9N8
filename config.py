from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from models import RunMode, TradingError


@dataclass(frozen=True)
class Settings:
    root: Path
    config: dict[str, Any]
    mode: RunMode
    live_trading: bool
    database_path: Path
    log_level: str

    @classmethod
    def load(cls, config_path: str = "config.json", env_path: str = ".env") -> "Settings":
        root = Path.cwd()
        load_dotenv(root / env_path, override=False)
        raw = json.loads((root / config_path).read_text(encoding="utf-8"))

        mode = RunMode(os.getenv("TRADING_MODE", raw["execution"]["mode"]).lower())
        live = os.getenv("LIVE_TRADING", str(raw["execution"]["live_trading"])).lower() == "true"
        allow_live = os.getenv("ALLOW_LIVE_TRADING", str(raw["execution"]["allow_live_trading"])).lower() == "true"
        acknowledgement = os.getenv("LIVE_ACKNOWLEDGEMENT", "")
        if live or mode is RunMode.LIVE:
            if not (live and mode is RunMode.LIVE and allow_live):
                raise TradingError("Live mode requires TRADING_MODE=live, LIVE_TRADING=true and ALLOW_LIVE_TRADING=true.")
            if raw["execution"].get("require_live_acknowledgement", True) and acknowledgement != raw["execution"]["live_acknowledgement"]:
                raise TradingError("Live acknowledgement phrase is missing or incorrect.")
        if mode is RunMode.TESTNET and live:
            raise TradingError("LIVE_TRADING cannot be true in testnet mode.")
        if mode is not RunMode.LIVE and live:
            raise TradingError("LIVE_TRADING=true is only valid with TRADING_MODE=live.")

        db = Path(os.getenv("DATABASE_PATH", raw["app"]["database_path"]))
        log_level = os.getenv("LOG_LEVEL", raw["app"]["log_level"]).upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise TradingError("Invalid LOG_LEVEL.")
        return cls(root, raw, mode, live, db, log_level)

    @property
    def live_enabled(self) -> bool:
        return self.mode is RunMode.LIVE and self.live_trading
