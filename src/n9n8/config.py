from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote_plus

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from solders.pubkey import Pubkey

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    helius_api_key: str
    solana_rpc_url: str
    solana_wss_url: str
    treasury: str
    encryption_key: bytes
    database_path: str
    database_url: str
    require_postgres: bool
    vercel_queue_region: str
    vercel_queue_topic: str
    vercel_queue_consumer: str
    jupiter_base_url: str
    jupiter_api_key: str
    jito_base_url: str
    live_trading_enabled: bool
    trading_mode: str
    webapp_url: str
    webapp_host: str
    webapp_port: int
    pyth_api_key: str
    max_users: int
    max_position_sol: str
    default_buy_sol: str
    default_daily_trades: int

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        helius_key = os.getenv("HELIUS_API_KEY", "").strip()
        supplied_rpc = os.getenv("SOLANA_RPC_URL", "").strip()
        supplied_wss = os.getenv("SOLANA_WSS_URL", "").strip()
        rpc = supplied_rpc or (
            f"https://mainnet.helius-rpc.com/?api-key={quote_plus(helius_key)}"
            if helius_key else ""
        )
        wss = supplied_wss or (
            f"wss://mainnet.helius-rpc.com/?api-key={quote_plus(helius_key)}"
            if helius_key else ""
        )
        treasury = os.getenv("MASTER_TREASURY_ADDRESS", "").strip()
        encryption_raw = os.getenv("ENCRYPTION_MASTER_KEY", "").strip()
        mode = os.getenv("N9N8_TRADING_MODE", "paper").strip().lower()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required.")
        if not helius_key and not (supplied_rpc and supplied_wss):
            raise RuntimeError(
                "Set HELIUS_API_KEY in the runtime environment, or provide both SOLANA_RPC_URL and SOLANA_WSS_URL."
            )
        if not treasury:
            raise RuntimeError("MASTER_TREASURY_ADDRESS is required.")
        try:
            Pubkey.from_string(treasury)
        except Exception as exc:
            raise RuntimeError("MASTER_TREASURY_ADDRESS is not a valid Solana public key.") from exc
        if not encryption_raw:
            raise RuntimeError("ENCRYPTION_MASTER_KEY is required; refusing to store unencrypted wallet keys.")
        try:
            Fernet(encryption_raw.encode("ascii"))
        except Exception as exc:
            raise RuntimeError("ENCRYPTION_MASTER_KEY must be a valid Fernet key.") from exc
        if mode not in {"paper", "live"}:
            raise RuntimeError("N9N8_TRADING_MODE must be paper or live.")
        live_enabled = _bool("LIVE_TRADING_ENABLED", False)
        database_url = os.getenv("DATABASE_URL", "").strip()
        require_postgres = _bool("N9N8_REQUIRE_POSTGRES", False)
        if require_postgres and not database_url:
            raise RuntimeError("DATABASE_URL is required for this deployed 9N8 runtime.")
        if mode == "live" and not live_enabled:
            raise RuntimeError("Live mode requires LIVE_TRADING_ENABLED=true as a second lock.")
        return cls(
            telegram_bot_token=token,
            helius_api_key=helius_key,
            solana_rpc_url=rpc,
            solana_wss_url=wss,
            treasury=treasury,
            encryption_key=encryption_raw.encode("ascii"),
            database_path=os.getenv("N9N8_DATABASE_PATH", "data/9n8-solana.sqlite3"),
            database_url=database_url,
            require_postgres=require_postgres,
            vercel_queue_region=os.getenv("VERCEL_QUEUE_REGION", "iad1").strip().lower(),
            vercel_queue_topic=os.getenv("VERCEL_QUEUE_TOPIC", "9n8-worker").strip(),
            vercel_queue_consumer=os.getenv("VERCEL_QUEUE_CONSUMER", "n9n8-daemon").strip(),
            jupiter_base_url=os.getenv("JUPITER_API_BASE_URL", "https://api.jup.ag/swap/v2").rstrip("/"),
            jupiter_api_key=os.getenv("JUPITER_API_KEY", "").strip(),
            jito_base_url=os.getenv("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf").rstrip("/"),
            live_trading_enabled=live_enabled,
            trading_mode=mode,
            webapp_url=os.getenv("WEBAPP_URL", "").strip(),
            webapp_host=os.getenv("WEBAPP_HOST", "0.0.0.0").strip(),
            webapp_port=int(os.getenv("WEBAPP_PORT", os.getenv("PORT", "8080"))),
            pyth_api_key=os.getenv("PYTH_API_KEY", "").strip(),
            max_users=int(os.getenv("N9N8_MAX_USERS", "100")),
            max_position_sol=os.getenv("N9N8_MAX_POSITION_SOL", "0.25").strip(),
            default_buy_sol=os.getenv("N9N8_DEFAULT_BUY_SOL", "0.05").strip(),
            default_daily_trades=int(os.getenv("N9N8_DEFAULT_DAILY_TRADES", "5")),
        )
