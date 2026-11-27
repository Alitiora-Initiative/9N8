from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Sequence

ZERO = Decimal("0")
BPS = Decimal("10000")

def _d(value: object, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result

@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    quantity: Decimal
    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _d(self.price, "price"))
        object.__setattr__(self, "quantity", _d(self.quantity, "quantity"))
        if self.price <= ZERO or self.quantity <= ZERO:
            raise ValueError("book level price and quantity must be positive")

@dataclass(frozen=True, slots=True)
class BookMetrics:
    best_bid: Decimal
    best_ask: Decimal
    midpoint: Decimal
    spread_bps: Decimal
    bid_depth: Decimal
    ask_depth: Decimal
    imbalance: Decimal

@dataclass(frozen=True, slots=True)
class ExecutionEstimate:
    side: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_price: Decimal
    reference_midpoint: Decimal
    slippage_bps: Decimal
    notional: Decimal

@dataclass(frozen=True, slots=True)
class FeeEstimate:
    maker_fee: Decimal
    taker_fee: Decimal
    maker_net_proceeds: Decimal
    taker_net_proceeds: Decimal

def analyze_order_book(bids: Sequence[BookLevel], asks: Sequence[BookLevel], *, levels: int = 10) -> BookMetrics:
    if not bids or not asks or levels < 1:
        raise ValueError("both book sides and a positive depth level count are required")
    best_bid = max(bids, key=lambda item: item.price).price
    best_ask = min(asks, key=lambda item: item.price).price
    if best_ask <= best_bid:
        raise ValueError("crossed or locked order book")
    midpoint = (best_bid + best_ask) / Decimal(2)
    bid_depth = sum((item.quantity for item in sorted(bids, key=lambda x: x.price, reverse=True)[:levels]), ZERO)
    ask_depth = sum((item.quantity for item in sorted(asks, key=lambda x: x.price)[:levels]), ZERO)
    total = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / total if total else ZERO
    spread_bps = (best_ask - best_bid) / midpoint * BPS
    return BookMetrics(best_bid, best_ask, midpoint, spread_bps, bid_depth, ask_depth, imbalance)

def estimate_market_execution(side: str, quantity: object, bids: Sequence[BookLevel], asks: Sequence[BookLevel], *, reference_midpoint: object | None = None) -> ExecutionEstimate:
    direction = side.strip().lower()
    if direction not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    requested = _d(quantity, "quantity")
    if requested <= ZERO:
        raise ValueError("quantity must be positive")
    book = analyze_order_book(bids, asks)
    levels = sorted(asks if direction == "buy" else bids, key=lambda item: item.price, reverse=(direction == "sell"))
    remaining, filled, notional = requested, ZERO, ZERO
    for level in levels:
        take = min(remaining, level.quantity)
        filled += take
        notional += take * level.price
        remaining -= take
        if remaining == ZERO:
            break
    if remaining > ZERO:
        raise ValueError("insufficient visible depth; refusing to extrapolate execution price")
    average = notional / filled
    mid = _d(reference_midpoint, "reference_midpoint") if reference_midpoint is not None else book.midpoint
    if mid <= ZERO:
        raise ValueError("reference midpoint must be positive")
    adverse = average - mid if direction == "buy" else mid - average
    slippage = max(ZERO, adverse / mid * BPS)
    return ExecutionEstimate(direction, requested, filled, average, mid, slippage, notional)

def estimate_maker_taker_fees(notional: object, maker_fee_bps: object, taker_fee_bps: object, *, is_sell: bool = True) -> FeeEstimate:
    amount = _d(notional, "notional")
    maker_rate = _d(maker_fee_bps, "maker_fee_bps")
    taker_rate = _d(taker_fee_bps, "taker_fee_bps")
    if amount < ZERO or min(maker_rate, taker_rate) < ZERO or max(maker_rate, taker_rate) > BPS:
        raise ValueError("notional and fee rates are outside valid ranges")
    maker_fee, taker_fee = amount * maker_rate / BPS, amount * taker_rate / BPS
    return FeeEstimate(
        maker_fee, taker_fee,
        amount - maker_fee if is_sell else amount + maker_fee,
        amount - taker_fee if is_sell else amount + taker_fee,
    )

def impermanent_loss_fraction(price_ratio: object) -> Decimal:
    """AMM impermanent-loss fraction relative to simply holding both assets."""
    ratio = _d(price_ratio, "price_ratio")
    if ratio <= ZERO:
        raise ValueError("price_ratio must be positive")
    return (Decimal(2) * ratio.sqrt() / (Decimal(1) + ratio)) - Decimal(1)
