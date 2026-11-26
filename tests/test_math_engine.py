from decimal import Decimal

import pytest

from src.n9n8.math_engine import (
    BalanceRequirement,
    can_open_position,
    constant_product_out,
    drawdown_pct,
    from_lamports,
    minimum_out,
    moon_bag_split,
    net_profit_pct,
    should_exit,
    to_lamports,
    treasury_profit_fee,
    usd_to_lamports,
)


def test_lamport_conversion_is_integer_exact():
    assert to_lamports("0.123456789") == 123456789
    assert from_lamports(123456789) == Decimal("0.123456789")


def test_usd_to_lamports_uses_decimal_price():
    assert usd_to_lamports("1", "200") == 5_000_000


def test_minimum_out_and_constant_product():
    assert minimum_out(1_000_000, 500) == 950_000
    out = constant_product_out("1", "100", "200", 30)
    assert out > 0
    assert out < Decimal("2")


def test_exact_position_reservation():
    requirement = BalanceRequirement(50, 10, 100, 200, 5)
    assert requirement.total_lamports == 365
    assert can_open_position(365, requirement)
    assert not can_open_position(364, requirement)


def test_profit_fee_only_on_positive_net_profit():
    assert treasury_profit_fee("1") == Decimal("0.001000000")
    assert treasury_profit_fee("-1") == Decimal("0")


def test_moon_bag_split_uses_integer_floor():
    sell, retain = moon_bag_split(101, "15")
    assert sell + retain == 101
    assert retain == 15


def test_exit_conditions_have_deterministic_priority():
    assert should_exit("11", 1, "10", "15", 60) == "take_profit"
    assert should_exit("-16", 1, "10", "15", 60) == "stop_loss"
    assert should_exit("0", 60, "10", "15", 60) == "max_hold"
    assert should_exit("0", 3, "10", "15", 60) is None


def test_drawdown():
    assert drawdown_pct("85", "100") == Decimal("15")


def test_reject_non_finite_money():
    with pytest.raises(ValueError):
        to_lamports("NaN")
