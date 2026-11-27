from decimal import Decimal
import pytest
from src.n9n8.v2.microstructure import BookLevel, analyze_order_book, estimate_maker_taker_fees, estimate_market_execution, impermanent_loss_fraction

def book():
    return [BookLevel(100,5),BookLevel(99,3)],[BookLevel(101,2),BookLevel(102,4)]

def test_spread_imbalance_and_market_impact():
    bids,asks=book()
    metrics=analyze_order_book(bids,asks)
    assert metrics.midpoint==Decimal("100.5")
    assert metrics.spread_bps>0 and metrics.imbalance>0
    execution=estimate_market_execution("buy",3,bids,asks)
    assert execution.average_price>metrics.midpoint
    assert execution.slippage_bps>0

def test_execution_refuses_insufficient_visible_depth():
    bids,asks=book()
    with pytest.raises(ValueError,match="insufficient visible depth"):
        estimate_market_execution("buy",99,bids,asks)

def test_maker_taker_and_impermanent_loss():
    fees=estimate_maker_taker_fees(1000,2,8)
    assert fees.maker_fee==Decimal("0.2")
    assert fees.taker_fee==Decimal("0.8")
    assert fees.maker_net_proceeds>fees.taker_net_proceeds
    assert impermanent_loss_fraction(1)==Decimal("0")
    assert impermanent_loss_fraction(4)<0
