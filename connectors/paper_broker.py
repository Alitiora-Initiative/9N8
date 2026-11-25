from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from models import AccountState, MarketSnapshot, MarketType, OpenPosition, Side, TradeIntent, TradeReceipt


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
        if (
            not all(math.isfinite(float(v)) for v in (starting_equity_usd, fee_rate, slippage_bps))
            or starting_equity_usd <= 0
            or not 0 <= fee_rate < 0.05
            or not 0 <= slippage_bps <= 500
        ):
            raise ValueError("Paper balance, fee rate or slippage configuration is outside safe bounds.")
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
            if not math.isfinite(float(cash_balance)) or cash_balance < 0:
                raise ValueError("Persisted paper cash must be finite and non-negative.")
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
            # Reserve the estimated next-entry fee so risk sizing cannot consume
            # every last cent and then fail when the simulated fee is charged.
            free_cash = max(self.cash, 0.0) / (1.0 + self.fee_rate)
            return AccountState("paper", equity, free_cash, self.cash)

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
        if not math.isfinite(float(quantity)) or quantity <= 0:
            raise ValueError("Paper order quantity must be finite and positive.")
        if not all(math.isfinite(float(v)) for v in (
            snapshot.bid, snapshot.ask, snapshot.last, intent.entry_price
        )):
            raise ValueError("Paper execution received a non-finite market or signal price.")
        if snapshot.bid <= 0 or snapshot.ask <= 0 or snapshot.last <= 0 or snapshot.ask < snapshot.bid:
            raise ValueError("Paper execution requires a valid, uncrossed positive bid/ask quote.")
        if snapshot.market_type is MarketType.STOCK:
            visible_size = snapshot.ask_size if intent.side is Side.BUY else snapshot.bid_size
            if visible_size is None or not math.isfinite(float(visible_size)) or visible_size <= 0:
                raise ValueError("Stock paper fill requires a positive displayed size at the executable quote.")
            if quantity > float(visible_size) + 1e-12:
                raise ValueError(
                    f"Stock paper quantity {quantity:.8g} exceeds the displayed top-of-book size "
                    f"{float(visible_size):.8g}; no fill was simulated."
                )
        async with self._lock:
            price = self._price(snapshot, intent.side)
            if not math.isfinite(price) or price <= 0:
                raise ValueError("Calculated paper fill price is invalid.")
            notional = price * quantity
            fee = notional * self.fee_rate
            key = intent.venue + ":" + intent.symbol
            current = self.positions.get(key)
            entry_fee_allocated = 0.0
            remaining_entry_fee = fee if current is None else 0.0

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
                    intent.venue, intent.symbol, intent.side, quantity, price,
                    intent.stop_loss, intent.take_profit, fee,
                )
            elif current.side is intent.side:
                raise ValueError("A paper position already exists in the same direction.")
            else:
                if quantity > current.quantity + 1e-12:
                    raise ValueError("Paper close quantity exceeds the open position.")
                # Allocate the remaining entry fee pro-rata across each partial close.
                entry_fee_allocated = current.fees_paid * min(1.0, quantity / current.quantity)
                remaining_entry_fee = max(0.0, current.fees_paid - entry_fee_allocated)
                # Close an existing long with a sell, or a short with a buy.
                if current.side is Side.BUY:
                    self.cash += notional - fee
                else:
                    self.cash -= notional + fee
                current.quantity -= quantity
                current.fees_paid = remaining_entry_fee
                if current.quantity <= 1e-12:
                    del self.positions[key]
                    remaining_entry_fee = 0.0

            reference = snapshot.ask if intent.side is Side.BUY else snapshot.bid
            gross_pnl = 0.0
            if current is not None:
                direction = 1.0 if current.side is Side.BUY else -1.0
                gross_pnl = (price - current.entry_price) * quantity * direction
            return TradeReceipt(
                intent.venue, intent.symbol, intent.side, "paper-" + client_order_id,
                quantity, price, fee, abs(price-reference)/max(snapshot.last,1e-12),
                datetime.now(timezone.utc),
                raw={
                    "mode": "paper",
                    "market_bid": snapshot.bid,
                    "market_ask": snapshot.ask,
                    "virtual_cash_after": self.cash,
                    "client_order_id": client_order_id,
                    "entry_fee": fee if current is None else 0.0,
                    "entry_fee_allocated": entry_fee_allocated,
                    "remaining_entry_fee": remaining_entry_fee,
                    "exit_fee": fee if current is not None else 0.0,
                    "gross_pnl": gross_pnl,
                    "net_pnl": gross_pnl - entry_fee_allocated - (fee if current is not None else 0.0),
                },
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
