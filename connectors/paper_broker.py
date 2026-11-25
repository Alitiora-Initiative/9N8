from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from models import AccountState, MarketSnapshot, OpenPosition, Side, TradeIntent, TradeReceipt


@dataclass
class _PaperPosition:
    venue: str
    symbol: str
    side: Side
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit: float
    fees_paid: float


class PaperBroker:
    """Deterministic paper fills using live bid/ask snapshots, not synthetic prices."""

    def __init__(self, starting_equity_usd: float, fee_rate: float = 0.001, slippage_bps: float = 5):
        if starting_equity_usd <= 0 or fee_rate < 0 or slippage_bps < 0:
            raise ValueError("Invalid paper broker parameters.")
        self.cash = float(starting_equity_usd)
        self.fee_rate = float(fee_rate)
        self.slippage_bps = float(slippage_bps)
        self.positions: dict[str, _PaperPosition] = {}
        self._lock = asyncio.Lock()

    def _price(self, snapshot: MarketSnapshot, side: Side) -> float:
        reference = snapshot.ask if side is Side.BUY else snapshot.bid
        factor = 1 + self.slippage_bps / 10_000 if side is Side.BUY else 1 - self.slippage_bps / 10_000
        return reference * factor

    async def account(self, snapshots: dict[str, MarketSnapshot]) -> AccountState:
        async with self._lock:
            equity = self.cash
            for key, pos in self.positions.items():
                snap = snapshots.get(key)
                if snap:
                    mark = snap.bid if pos.side is Side.BUY else snap.ask
                    # Cash was debited by the full buy notional at entry. Add only
                    # current liquidation value; mark-to-market PnL is then implicit.
                    equity += pos.quantity * mark if pos.side is Side.BUY else 0
            return AccountState("paper", equity, max(self.cash, 0), self.cash)

    async def positions_for(self, snapshots: dict[str, MarketSnapshot]) -> list[OpenPosition]:
        async with self._lock:
            result = []
            for key, p in self.positions.items():
                snap = snapshots.get(key)
                mark = snap.last if snap else p.entry_price
                pnl = (mark-p.entry_price)*p.quantity*(1 if p.side is Side.BUY else -1)
                result.append(OpenPosition(
                    p.venue,p.symbol,snapshot_market_type(snap),p.side,p.quantity,p.entry_price,
                    mark,p.stop_loss,p.take_profit,pnl,0,
                    metadata={"paper":True,"fees_paid":p.fees_paid},
                ))
            return result

    async def execute(self, intent: TradeIntent, quantity: float, snapshot: MarketSnapshot,
                      client_order_id: str) -> TradeReceipt:
        if quantity <= 0:
            raise ValueError("Paper order quantity must be positive.")
        async with self._lock:
            price = self._price(snapshot, intent.side)
            notional = price * quantity
            fee = notional * self.fee_rate
            key = intent.venue + ":" + intent.symbol
            if intent.side is Side.BUY:
                cost = notional + fee
                if cost > self.cash:
                    raise ValueError("Insufficient virtual cash for paper buy.")
                if key in self.positions:
                    raise ValueError("Paper position already exists for this venue/symbol.")
                self.cash -= cost
                self.positions[key] = _PaperPosition(
                    intent.venue,intent.symbol,intent.side,quantity,price,
                    intent.stop_loss,intent.take_profit,fee,
                )
            else:
                current = self.positions.get(key)
                if current is None or quantity > current.quantity + 1e-12:
                    raise ValueError("Paper sell quantity exceeds the open paper position.")
                proceeds = notional - fee
                self.cash += proceeds
                current.quantity -= quantity
                if current.quantity <= 1e-12:
                    del self.positions[key]
            return TradeReceipt(
                intent.venue,intent.symbol,intent.side,"paper-"+client_order_id,
                quantity,price,fee,abs(price-(snapshot.ask if intent.side is Side.BUY else snapshot.bid)) /
                max(snapshot.last,1e-12),datetime.now(timezone.utc),
                raw={"mode":"paper","market_bid":snapshot.bid,"market_ask":snapshot.ask,
                     "virtual_cash_after":self.cash,"client_order_id":client_order_id},
            )

    async def flatten(self, snapshots: dict[str, MarketSnapshot]) -> None:
        async with self._lock:
            for key, position in list(self.positions.items()):
                snapshot = snapshots.get(key)
                if not snapshot:
                    continue
                price = self._price(snapshot, Side.SELL if position.side is Side.BUY else Side.BUY)
                notional = price * position.quantity
                fee = notional * self.fee_rate
                if position.side is Side.BUY:
                    self.cash += notional - fee
                else:
                    self.cash -= notional + fee
                del self.positions[key]


def snapshot_market_type(snapshot):
    if snapshot is None:
        from models import MarketType
        return MarketType.CRYPTO_SPOT
    return snapshot.market_type
