from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


def decimal_value(value: object, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal number") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def utc_datetime(value: datetime, field: str = "timestamp") -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Candle:
    """Normalized OHLCV bar. All values are Decimal and time is UTC."""

    symbol: str
    source: str
    interval: str
    opened_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool

    def __post_init__(self) -> None:
        if not self.symbol.strip() or not self.source.strip() or not self.interval.strip():
            raise ValueError("symbol, source and interval are required")
        object.__setattr__(self, "opened_at", utc_datetime(self.opened_at, "opened_at"))
        for name in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, name, decimal_value(getattr(self, name), name))
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive")
        if self.volume < 0:
            raise ValueError("volume cannot be negative")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be at least open, close and low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be at most open, close and high")


@dataclass(frozen=True, slots=True)
class TradeIntent:
    """Immutable request shared by scanning, risk, approvals and execution."""

    intent_id: str
    telegram_user_id: int
    mode: TradingMode
    mint: str
    side: TradeSide
    amount_raw: int
    created_at: datetime
    approval_id: str | None = None

    def __post_init__(self) -> None:
        if not self.intent_id.strip():
            raise ValueError("intent_id is required")
        if isinstance(self.telegram_user_id, bool) or self.telegram_user_id <= 0:
            raise ValueError("telegram_user_id must be a positive integer")
        if not self.mint.strip():
            raise ValueError("mint is required")
        if isinstance(self.amount_raw, bool) or self.amount_raw <= 0:
            raise ValueError("amount_raw must be a positive integer")
        object.__setattr__(self, "created_at", utc_datetime(self.created_at, "created_at"))
