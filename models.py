from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class MarketType(StrEnum):
    CRYPTO_SPOT = "crypto_spot"
    CRYPTO_PERP = "crypto_perpetual"
    SOLANA_DEX = "solana_dex"
    EVM_DEX = "evm_dex"
    FOREX = "forex"
    COMMODITY = "commodity"
    STOCK = "stock"


class RunMode(StrEnum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class MarketSnapshot:
    venue: str
    symbol: str
    market_type: MarketType
    bid: float
    ask: float
    last: float
    timestamp: datetime = field(default_factory=utc_now)
    volume_5m: float | None = None
    buy_volume_5m: float | None = None
    sell_volume_5m: float | None = None
    quote_volume_24h_usd: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None

    @property
    def spread_pct(self) -> float:
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid if mid > 0 else float("inf")


@dataclass(frozen=True)
class TradeIntent:
    venue: str
    symbol: str
    market_type: MarketType
    side: Side
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_distance: float
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TradeSize:
    quantity: float
    notional_usd: float
    risk_usd: float


@dataclass
class TradeReceipt:
    venue: str
    symbol: str
    side: Side
    order_id: str
    quantity: float
    average_price: float
    fee_usd: float
    slippage_pct: float
    timestamp: datetime = field(default_factory=utc_now)
    stop_order_id: str | None = None
    take_profit_order_id: str | None = None
    tx_hash: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OpenPosition:
    venue: str
    symbol: str
    market_type: MarketType
    side: Side
    quantity: float
    entry_price: float
    mark_price: float
    stop_loss: float
    take_profit: float
    unrealized_pnl_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    opened_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccountState:
    venue: str
    equity_usd: float
    free_usd: float
    balance_usd: float
    timestamp: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    size: TradeSize | None = None


class TradingError(RuntimeError):
    pass


class RiskRejected(TradingError):
    pass


class ConnectorUnavailable(TradingError):
    pass
