from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, getcontext

getcontext().prec = 28

LAMPORTS_PER_SOL = Decimal("1000000000")
BPS_DENOMINATOR = Decimal("10000")
PROFIT_FEE_RATE = Decimal("0.001")


def dec(value: object, name: str = "value") -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a decimal number.") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite.")
    return result


def to_lamports(sol: object) -> int:
    amount = dec(sol, "SOL amount")
    if amount < 0:
        raise ValueError("SOL amount cannot be negative.")
    return int((amount * LAMPORTS_PER_SOL).to_integral_value(rounding=ROUND_DOWN))


def from_lamports(lamports: int) -> Decimal:
    if isinstance(lamports, bool) or int(lamports) < 0:
        raise ValueError("Lamports must be a non-negative integer.")
    return Decimal(int(lamports)) / LAMPORTS_PER_SOL


def usd_to_lamports(usd: object, sol_usd: object) -> int:
    usd_value = dec(usd, "USD amount")
    price = dec(sol_usd, "SOL/USD price")
    if usd_value < 0 or price <= 0:
        raise ValueError("USD must be non-negative and SOL/USD must be positive.")
    return to_lamports(usd_value / price)


def minimum_out(expected_out: int, slippage_bps: int) -> int:
    if expected_out < 0 or not 0 <= slippage_bps <= 9900:
        raise ValueError("Invalid expected output or slippage basis points.")
    return int((Decimal(expected_out) * (BPS_DENOMINATOR - Decimal(slippage_bps)) / BPS_DENOMINATOR).to_integral_value(rounding=ROUND_DOWN))


def constant_product_out(amount_in: object, reserve_in: object, reserve_out: object, fee_bps: int = 30) -> Decimal:
    amount = dec(amount_in, "amount_in")
    x = dec(reserve_in, "reserve_in")
    y = dec(reserve_out, "reserve_out")
    if amount <= 0 or x <= 0 or y <= 0 or not 0 <= fee_bps < 10000:
        raise ValueError("AMM inputs and reserves must be positive; fee must be in [0, 10000).")
    effective = amount * (Decimal(10000 - fee_bps) / BPS_DENOMINATOR)
    return (effective * y) / (x + effective)


@dataclass(frozen=True)
class BalanceRequirement:
    trade_lamports: int
    priority_fee_lamports: int
    jito_tip_lamports: int
    ata_rent_lamports: int
    gas_buffer_lamports: int

    @property
    def total_lamports(self) -> int:
        values = (
            self.trade_lamports,
            self.priority_fee_lamports,
            self.jito_tip_lamports,
            self.ata_rent_lamports,
            self.gas_buffer_lamports,
        )
        if any(int(x) < 0 for x in values):
            raise ValueError("Balance requirement values cannot be negative.")
        return sum(int(x) for x in values)


def can_open_position(available_lamports: int, required: BalanceRequirement) -> bool:
    return int(available_lamports) >= required.total_lamports


def net_profit_pct(current_value_sol: object, initial_cost_sol: object, total_fees_sol: object = "0") -> Decimal:
    value = dec(current_value_sol, "current value")
    cost = dec(initial_cost_sol, "initial cost")
    fees = dec(total_fees_sol, "total fees")
    if cost <= 0 or fees < 0:
        raise ValueError("Initial cost must be positive and fees cannot be negative.")
    return ((value - cost - fees) / cost) * Decimal(100)


def realized_profit_sol(received_sol: object, initial_cost_sol: object, fees_sol: object = "0") -> Decimal:
    received = dec(received_sol, "received SOL")
    cost = dec(initial_cost_sol, "initial cost")
    fees = dec(fees_sol, "fees")
    if min(received, cost, fees) < 0:
        raise ValueError("Amounts cannot be negative.")
    return received - cost - fees


def treasury_profit_fee(net_profit_sol: object) -> Decimal:
    profit = dec(net_profit_sol, "net profit")
    if profit <= 0:
        return Decimal(0)
    return (profit * PROFIT_FEE_RATE).quantize(Decimal("0.000000001"), rounding=ROUND_DOWN)


def moon_bag_split(total_raw: int, retained_pct: object) -> tuple[int, int]:
    retained = dec(retained_pct, "moon-bag percentage")
    if total_raw < 0 or not Decimal(0) <= retained <= Decimal(100):
        raise ValueError("Token amount must be non-negative and moon bag must be between 0 and 100.")
    keep = int((Decimal(total_raw) * retained / Decimal(100)).to_integral_value(rounding=ROUND_DOWN))
    return total_raw - keep, keep


def should_exit(
    profit_pct: object,
    elapsed_seconds: object,
    take_profit_pct: object,
    stop_loss_pct: object,
    max_hold_seconds: int,
) -> str | None:
    profit = dec(profit_pct, "profit percentage")
    elapsed = dec(elapsed_seconds, "elapsed seconds")
    take_profit = dec(take_profit_pct, "take-profit percentage")
    stop_loss = dec(stop_loss_pct, "stop-loss percentage")
    if min(elapsed, take_profit, stop_loss) < 0 or max_hold_seconds < 1:
        raise ValueError("Elapsed time, targets and stop-loss must be non-negative; max hold must be positive.")
    if profit >= take_profit:
        return "take_profit"
    if profit <= -stop_loss:
        return "stop_loss"
    if elapsed >= Decimal(max_hold_seconds):
        return "max_hold"
    return None


def drawdown_pct(current_equity_sol: object, starting_equity_sol: object) -> Decimal:
    current = dec(current_equity_sol, "current equity")
    start = dec(starting_equity_sol, "starting equity")
    if start <= 0 or current < 0:
        raise ValueError("Starting equity must be positive and current equity non-negative.")
    return ((start - current) / start) * Decimal(100)


def format_sol(lamports: int) -> str:
    return f"{from_lamports(lamports):.9f}".rstrip("0").rstrip(".") or "0"
