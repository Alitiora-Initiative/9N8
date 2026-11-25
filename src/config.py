"""Environment-backed configuration for 9N8.

MT5 integration is deliberately read-only. The paper engine uses its own local
SQLite ledger and never sends orders to the MetaTrader terminal.
"""
from dataclasses import dataclass
import os
from dotenv import load_dotenv
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

load_dotenv()


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class Config:
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    owner_id: int = _int_env("TELEGRAM_OWNER_ID", _int_env("TELEGRAM_CHAT_ID", 0, 0, 2**63 - 1), 0, 2**63 - 1)
    twelve_data_api_key: str = (os.getenv("TWELVEDATA_API_KEY", "").strip() or os.getenv("TWELVE_DATA_API_KEY", "").strip())
    mode: str = os.getenv("MODE", "paper").strip().lower()
    watchlist_raw: str = os.getenv(
        "WATCHLIST", "XAU/USD,EUR/USD,GBP/USD,USD/JPY,XAG/USD"
    )
    timeframe: str = os.getenv("TIMEFRAME", "15min").strip()
    output_size: int = _int_env("OUTPUT_SIZE", 80, 40, 500)

    # Provider: "metaapi" for cloud/mobile access; "desktop" for local Windows MT5.
    mt5_provider: str = os.getenv("MT5_PROVIDER", "metaapi").strip().lower()
    metaapi_token: str = os.getenv("METAAPI_TOKEN", "").strip()
    metaapi_account_id: str = os.getenv("METAAPI_ACCOUNT_ID", "").strip()
    metaapi_api_base: str = os.getenv(
        "METAAPI_API_BASE", "https://mt-client-api-v1.new-york.agiliumtrade.ai"
    ).strip().rstrip("/")
    metaapi_market_data_base: str = os.getenv(
        "METAAPI_MARKET_DATA_BASE", "https://mt-market-data-client-api-v1.new-york.agiliumtrade.ai"
    ).strip().rstrip("/")
    mt5_terminal_path: str = os.getenv("MT5_TERMINAL_PATH", "").strip().strip('"')
    mt5_timeframe: str = os.getenv("MT5_TIMEFRAME", "M15").strip().upper()
    mt5_bars: int = _int_env("MT5_BARS", 120, 40, 1000)
    mt5_symbols_raw: str = os.getenv(
        "MT5_SYMBOLS", "XAUUSD,EURUSD,GBPUSD,USDJPY,XAGUSD"
    )

    paper_db_path: str = os.getenv("PAPER_DB_PATH", "data/9n8_paper.sqlite3").strip()
    paper_initial_balance: float = _float_env(
        "PAPER_INITIAL_BALANCE", 10000.0, 1.0, 1_000_000_000.0
    )
    paper_currency: str = os.getenv("PAPER_CURRENCY", "USD").strip().upper()
    paper_max_lots: float = _float_env("PAPER_MAX_LOTS", 0.10, 0.01, 100.0)
    paper_max_open_positions: int = _int_env("PAPER_MAX_OPEN_POSITIONS", 3, 1, 100)
    paper_slippage_points: int = _int_env("PAPER_SLIPPAGE_POINTS", 2, 0, 10000)
    paper_max_tick_age_seconds: int = _int_env("PAPER_MAX_TICK_AGE_SECONDS", 900, 30, 86400)
    paper_default_sl_points: int = _int_env("PAPER_DEFAULT_SL_POINTS", 500, 1, 1_000_000)
    paper_default_tp_points: int = _int_env("PAPER_DEFAULT_TP_POINTS", 1000, 1, 1_000_000)
    paper_default_be_trigger_points: int = _int_env("PAPER_DEFAULT_BE_TRIGGER_POINTS", 150, 0, 1_000_000)
    paper_default_be_offset_points: int = _int_env("PAPER_DEFAULT_BE_OFFSET_POINTS", 0, 0, 100_000)
    report_day: str = os.getenv("REPORT_DAY", "MON").strip().upper()
    report_time: str = os.getenv("REPORT_TIME", "09:00").strip()
    report_timezone: str = os.getenv("REPORT_TIMEZONE", "UTC").strip()

    @property
    def watchlist(self) -> list[str]:
        symbols = [item.strip().upper() for item in self.watchlist_raw.split(",")]
        return list(dict.fromkeys(symbol for symbol in symbols if symbol))[:20]

    @property
    def mt5_symbols(self) -> list[str]:
        symbols = [item.strip().upper() for item in self.mt5_symbols_raw.split(",")]
        return list(dict.fromkeys(symbol for symbol in symbols if symbol))[:50]


config = Config()

if config.mt5_provider not in {"metaapi", "desktop"}:
    raise ValueError("MT5_PROVIDER must be 'metaapi' or 'desktop'")
if config.mode != "paper":
    raise RuntimeError("9N8 supports PAPER mode only in this release. Set MODE=paper.")
if config.mt5_timeframe not in {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}:
    raise ValueError("MT5_TIMEFRAME must be one of M1, M5, M15, M30, H1, H4, D1")
if config.report_day not in {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}:
    raise ValueError("REPORT_DAY must be MON, TUE, WED, THU, FRI, SAT, or SUN")
if len(config.report_time) != 5 or config.report_time[2] != ":":
    raise ValueError("REPORT_TIME must use HH:MM format")
try:
    _report_hour, _report_minute = (int(part) for part in config.report_time.split(":"))
except (TypeError, ValueError) as exc:
    raise ValueError("REPORT_TIME must use HH:MM format") from exc
if not (0 <= _report_hour <= 23 and 0 <= _report_minute <= 59):
    raise ValueError("REPORT_TIME must use HH:MM format")
try:
    ZoneInfo(config.report_timezone)
except ZoneInfoNotFoundError as exc:
    raise ValueError("REPORT_TIMEZONE must be a valid IANA timezone, such as UTC or Africa/Nairobi") from exc
if config.paper_default_be_trigger_points == 0 and config.paper_default_be_offset_points != 0:
    raise ValueError("PAPER_DEFAULT_BE_OFFSET_POINTS must be zero when break-even is disabled")
if config.paper_default_be_trigger_points > 0 and config.paper_default_be_offset_points >= config.paper_default_be_trigger_points:
    raise ValueError("PAPER_DEFAULT_BE_OFFSET_POINTS must be smaller than PAPER_DEFAULT_BE_TRIGGER_POINTS")
