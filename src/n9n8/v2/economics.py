from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Sequence

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")


def _d(value: object, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class VenueQuote:
    venue: str
    instrument: str
    bid: Decimal
    ask: Decimal
    observed_at: datetime
    taker_fee_bps: Decimal
    bid_depth: Decimal
    ask_depth: Decimal

    def __post_init__(self) -> None:
        if not self.venue.strip() or not self.instrument.strip():
            raise ValueError("venue and instrument are required")
        for name in ("bid", "ask", "taker_fee_bps", "bid_depth", "ask_depth"):
            object.__setattr__(self, name, _d(getattr(self, name), name))
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        if self.bid <= ZERO or self.ask <= ZERO or self.ask < self.bid:
            raise ValueError("bid and ask must be positive and ask cannot be below bid")
        if not ZERO <= self.taker_fee_bps <= BPS:
            raise ValueError("taker fee must be between 0 and 10000 bps")
        if self.bid_depth < ZERO or self.ask_depth < ZERO:
            raise ValueError("order-book depth cannot be negative")


@dataclass(frozen=True, slots=True)
class ArbitrageAssessment:
    approved: bool
    gross_edge_bps: Decimal
    net_edge_bps: Decimal
    gross_profit_quote: Decimal
    net_profit_quote: Decimal
    quote_age_seconds: int
    blockers: tuple[str, ...]


def assess_cross_venue_arbitrage(
    buy: VenueQuote,
    sell: VenueQuote,
    base_quantity: object,
    *,
    transfer_and_network_cost_quote: object = ZERO,
    minimum_net_profit_quote: object = ZERO,
    minimum_net_edge_bps: object = ZERO,
    now: datetime | None = None,
    max_quote_age_seconds: int = 15,
) -> ArbitrageAssessment:
    """Filter cross-venue spreads after visible depth, fees, transfer and network costs.

    This does not transfer assets, borrow, place orders or guarantee simultaneous execution.
    """
    quantity = _d(base_quantity, "base_quantity")
    transfer_cost = _d(transfer_and_network_cost_quote, "transfer_and_network_cost_quote")
    min_profit = _d(minimum_net_profit_quote, "minimum_net_profit_quote")
    min_edge = _d(minimum_net_edge_bps, "minimum_net_edge_bps")
    current = _aware(now or datetime.now(timezone.utc), "now")
    if quantity <= ZERO or transfer_cost < ZERO or min_profit < ZERO or min_edge < ZERO:
        raise ValueError("quantity must be positive and costs/thresholds non-negative")
    blockers: list[str] = []
    if buy.venue == sell.venue:
        blockers.append("venues_must_be_different")
    if buy.instrument != sell.instrument:
        blockers.append("instrument_mismatch")
    buy_age = int((current - buy.observed_at).total_seconds())
    sell_age = int((current - sell.observed_at).total_seconds())
    quote_age = max(buy_age, sell_age)
    if min(buy_age, sell_age) < -2:
        blockers.append("quote_timestamp_in_future")
    if quote_age > max_quote_age_seconds:
        blockers.append("quote_is_stale")
    if buy.ask_depth < quantity:
        blockers.append("buy_side_visible_depth_insufficient")
    if sell.bid_depth < quantity:
        blockers.append("sell_side_visible_depth_insufficient")
    entry = buy.ask * quantity
    exit_value = sell.bid * quantity
    buy_fee = entry * buy.taker_fee_bps / BPS
    sell_fee = exit_value * sell.taker_fee_bps / BPS
    gross = exit_value - entry
    net = gross - buy_fee - sell_fee - transfer_cost
    gross_edge = gross / entry * BPS
    net_edge = net / entry * BPS
    if net < min_profit:
        blockers.append("net_profit_below_minimum")
    if net_edge < min_edge:
        blockers.append("net_edge_below_minimum")
    return ArbitrageAssessment(not blockers, gross_edge, net_edge, gross, net, max(0, quote_age), tuple(blockers))


@dataclass(frozen=True, slots=True)
class InventoryPressure:
    pressure: Decimal
    quote_skew_bps: Decimal
    direction: str


def market_maker_inventory_pressure(
    current_inventory: object,
    target_inventory: object,
    max_deviation: object,
    *,
    max_quote_skew_bps: object = "25",
) -> InventoryPressure:
    """Map an account's measured inventory deviation into a bounded quote skew."""
    current = _d(current_inventory, "current_inventory")
    target = _d(target_inventory, "target_inventory")
    capacity = _d(max_deviation, "max_deviation")
    max_skew = _d(max_quote_skew_bps, "max_quote_skew_bps")
    if capacity <= ZERO or max_skew < ZERO:
        raise ValueError("max_deviation must be positive and quote skew non-negative")
    pressure = max(-ONE, min(ONE, (current - target) / capacity))
    direction = "overweight" if pressure > ZERO else "underweight" if pressure < ZERO else "balanced"
    return InventoryPressure(pressure, -pressure * max_skew, direction)


def funding_cashflow(
    side: str,
    notional_quote: object,
    funding_rate: object,
    *,
    funding_intervals: int = 1,
) -> Decimal:
    """Signed funding cash flow: positive means received and negative means paid."""
    direction = side.strip().lower()
    notional = _d(notional_quote, "notional_quote")
    rate = _d(funding_rate, "funding_rate")
    if direction not in {"long", "short"}:
        raise ValueError("side must be long or short")
    if notional < ZERO or funding_intervals < 1:
        raise ValueError("notional must be non-negative and intervals positive")
    multiplier = Decimal(-1) if direction == "long" else Decimal(1)
    return multiplier * notional * rate * Decimal(funding_intervals)


@dataclass(frozen=True, slots=True)
class EconomicEvent:
    name: str
    category: str
    impact: str
    source: str
    scheduled_at: datetime

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.source.strip() or not self.category.strip():
            raise ValueError("event name, category and source are required")
        if self.impact.lower() not in {"low", "medium", "high"}:
            raise ValueError("event impact must be low, medium or high")
        object.__setattr__(self, "scheduled_at", _aware(self.scheduled_at, "scheduled_at"))


def macro_event_blackout(
    events: Sequence[EconomicEvent],
    *,
    now: datetime,
    categories: Sequence[str] = ("CPI", "FOMC", "NFP"),
    before_minutes: int = 15,
    after_minutes: int = 15,
) -> tuple[EconomicEvent, ...]:
    """Return sourced high-impact events inside the configured no-entry window."""
    current = _aware(now, "now")
    if before_minutes < 0 or after_minutes < 0:
        raise ValueError("blackout windows cannot be negative")
    allowed = {item.strip().casefold() for item in categories}
    before = timedelta(minutes=before_minutes)
    after = timedelta(minutes=after_minutes)
    return tuple(
        event for event in events
        if event.impact.casefold() == "high"
        and event.category.casefold() in allowed
        and event.scheduled_at - before <= current <= event.scheduled_at + after
    )


def metcalfe_activity_index(active_wallets: int, baseline_active_wallets: int) -> Decimal:
    """Normalized N-squared network activity index, not a fair-value estimate."""
    if isinstance(active_wallets, bool) or isinstance(baseline_active_wallets, bool):
        raise ValueError("wallet counts must be integers")
    if active_wallets < 0 or baseline_active_wallets <= 0:
        raise ValueError("active_wallets must be non-negative and baseline positive")
    ratio = Decimal(active_wallets) / Decimal(baseline_active_wallets)
    return ratio * ratio


@dataclass(frozen=True, slots=True)
class ReflexivityAssessment:
    feedback_score: Decimal
    aligned_positive: bool
    aligned_negative: bool
    label: str


def reflexivity_feedback(
    price_return_fraction: object,
    active_wallet_growth_fraction: object,
    net_flow_fraction: object,
) -> ReflexivityAssessment:
    """Describe co-directional price, network-activity and net-flow feedback."""
    price = _d(price_return_fraction, "price_return_fraction")
    wallets = _d(active_wallet_growth_fraction, "active_wallet_growth_fraction")
    flow = _d(net_flow_fraction, "net_flow_fraction")
    if min(price, wallets, flow) <= -ONE:
        raise ValueError("fractional changes must be greater than -100%")
    positive = price > ZERO and wallets > ZERO and flow > ZERO
    negative = price < ZERO and wallets < ZERO and flow < ZERO
    score = price * wallets * flow
    label = "positive_feedback_alignment" if positive else "negative_feedback_alignment" if negative else "mixed_or_weak_feedback"
    return ReflexivityAssessment(score, positive, negative, label)


@dataclass(frozen=True, slots=True)
class TransferMechanics:
    gross_pnl_balance: Decimal
    participant_net_after_fees: Decimal
    fees_paid: Decimal
    external_capital_flow: Decimal
    label: str


def classify_trade_transfer_mechanics(
    aggregate_winner_pnl: object,
    aggregate_loser_pnl: object,
    fees_paid: object,
    *,
    external_capital_flow: object = ZERO,
    reconciliation_tolerance: object = "0.00000001",
) -> TransferMechanics:
    """Reconcile participant PnL separately from capital entering or leaving."""
    winners = _d(aggregate_winner_pnl, "aggregate_winner_pnl")
    losers = _d(aggregate_loser_pnl, "aggregate_loser_pnl")
    fees = _d(fees_paid, "fees_paid")
    flow = _d(external_capital_flow, "external_capital_flow")
    tolerance = _d(reconciliation_tolerance, "reconciliation_tolerance")
    if min(winners, fees, tolerance) < ZERO or tolerance == ZERO or losers > ZERO:
        raise ValueError("winner PnL, loser PnL, fees and tolerance are invalid")
    gross_balance = winners + losers
    net = gross_balance - fees
    if abs(gross_balance) > tolerance:
        label = "gross_pnl_reconciliation_required"
    elif fees > ZERO:
        label = "zero_sum_before_fees_negative_after_fees"
    elif flow != ZERO:
        label = "zero_sum_trading_with_external_capital_flow"
    else:
        label = "zero_sum_before_fees"
    return TransferMechanics(gross_balance, net, fees, flow, label)
