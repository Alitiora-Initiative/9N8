from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from models import AccountState, Candle, MarketSnapshot, OpenPosition, Side, TradeIntent, TradeReceipt


class ExecutionConnector(ABC):
    venue: str

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def fetch_snapshot(self, symbol: str) -> MarketSnapshot: ...

    @abstractmethod
    async def fetch_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]: ...

    @abstractmethod
    async def get_balance(self) -> AccountState: ...

    @abstractmethod
    async def get_open_positions(self) -> list[OpenPosition]: ...

    @abstractmethod
    async def buy(self, intent: TradeIntent, quantity: float, client_order_id: str) -> TradeReceipt: ...

    @abstractmethod
    async def sell(self, intent: TradeIntent, quantity: float, client_order_id: str) -> TradeReceipt: ...

    @abstractmethod
    async def close_position(self, position: OpenPosition, quantity: float | None = None) -> TradeReceipt: ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: str | None = None) -> None: ...

    async def set_isolated_leverage(self, symbol: str, leverage: int) -> None:
        raise NotImplementedError(f"{self.venue} does not expose isolated leverage.")

    async def provider_health(self) -> dict[str, Any]:
        return {"venue": self.venue, "healthy": True}
