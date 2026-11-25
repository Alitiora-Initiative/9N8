from __future__ import annotations

import asyncio
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
    """Paper executions use live provider bid/ask prices plus explicit fee/slippage."""

    def __init__(self, starting_equity_usd: float, fee_rate: float = 0.001, slippage_bps: float = 5):
        if starting_equity_usd <= 0 or fee_rate < 0 or slippage_bps < 0:
            raise ValueError("Invalid paper broker parameters.")
        self.cash = float(starting_equity_usd)
        self.starting_equity = float(starting_equity_usd)
        self.fee_rate = float(fee_rate)
        self.slippage_bps = float(slippage_bps)
        self.positions: dict[str, _PaperPosition] = {}
        self._lock = asyncio.Lock()

    def _price(self, snapshot: MarketSnapshot, side: Side) -> float:
        reference = snapshot.ask if side is Side.BUY else snapshot.bid
        factor = 1 + self.slippage_bps / 10_000 if side is Side.BUY else 1 - self.slippage_bps / 10_000
        return reference * factor

    async def restore_state(self, cash_balance: float, rows: list[dict]) -> None:
        """Restore paper cash and open positions from the durable SQLite ledger."""
        import json
        async with self._lock:
            if cash_balance <= 0:
                raise ValueError("Persisted paper cash must be positive.")
            self.cash = float(cash_balance)
            self.positions.clear()
            for row in rows:
                raw = row.get("metadata_json", {})
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except json.JSONDecodeError:
                        raw = {}
                self.positions[str(row["position_key"])] = _PaperPosition(
                    str(row["venue"]), str(row["symbol"]), Side(row["side"]),
                    float(row["quantity"]), float(row["entry_price"]),
                    float(row["stop_loss"]), float(row["take_profit"]),
                    float(raw.get("paper_fees_paid", 0.0)),
                )

    async def account(self, snapshots: dict[str, MarketSnapshot]) -> AccountState:
        async with self._lock:
            equity = self.cash
            for key, pos in self.positions.items():
                snap = snapshots.get(key)
                mark = (snap.bid if pos.side is Side.BUY else snap.ask) if snap else pos.entry_price
                if pos.side is Side.BUY:
                    equity += pos.quantity * mark
                else:
                    equity -= pos.quantity * mark
            return AccountState("paper", equity, max(self.cash, 0.0), self.cash)

    async def positions_for(self, snapshots: dict[str, MarketSnapshot]) -> list[OpenPosition]:
        async with self._lock:
            result = []
            for key, p in self.positions.items():
                snap = snapshots.get(key)
                mark = snap.last if snap else p.entry_price
                direction = 1 if p.side is Side.BUY else -1
                pnl = (mark-p.entry_price)*p.quantity*direction
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
            current = self.positions.get(key)

            if current is None:
                # Opening long: pay for inventory. Opening short: receive sale proceeds,
                # while the marked liability is deducted from account equity.
                if intent.side is Side.BUY:
                    if notional + fee > self.cash:
                        raise ValueError("Insufficient virtual cash for paper buy.")
                    self.cash -= notional + fee
                else:
                    self.cash += notional - fee
                self.positions[key] = _PaperPosition(
                    intent.venue,intent.symbol,intent.side,quantity,price,
                    intent.stop_loss,intent.take_profit,fee,
                )
            elif current.side is intent.side:
                raise ValueError("A paper position already exists in the same direction.")
            else:
                if quantity > current.quantity + 1e-12:
                    raise ValueError("Paper close quantity exceeds the open position.")
                # Close an existing long with a sell, or a short with a buy.
                if current.side is Side.BUY:
                    self.cash += notional - fee
                else:
                    self.cash -= notional + fee
                current.quantity -= quantity
                if current.quantity <= 1e-12:
                    del self.positions[key]

            reference = snapshot.ask if intent.side is Side.BUY else snapshot.bid
            return TradeReceipt(
                intent.venue,intent.symbol,intent.side,"paper-"+client_order_id,
                quantity,price,fee,abs(price-reference)/max(snapshot.last,1e-12),
                datetime.now(timezone.utc),
                raw={"mode":"paper","market_bid":snapshot.bid,"market_ask":snapshot.ask,
                     "virtual_cash_after":self.cash,"client_order_id":client_order_id},
            )

    async def flatten(self, snapshots: dict[str, MarketSnapshot]) -> None:
        async with self._lock:
            for key, position in list(self.positions.items()):
                snapshot = snapshots.get(key)
                if not snapshot:
                    continue
                exit_side = Side.SELL if position.side is Side.BUY else Side.BUY
                price = self._price(snapshot, exit_side)
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
