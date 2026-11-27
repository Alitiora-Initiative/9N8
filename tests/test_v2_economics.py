from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.n9n8.v2.economics import (
    EconomicEvent, VenueQuote, assess_cross_venue_arbitrage,
    classify_trade_transfer_mechanics, funding_cashflow, macro_event_blackout,
    market_maker_inventory_pressure, metcalfe_activity_index, reflexivity_feedback,
)


NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def quote(name, bid, ask, *, at=NOW, fee="10", bid_depth="100", ask_depth="100"):
    return VenueQuote(name, "SOL/USDC", Decimal(bid), Decimal(ask), at,
                      Decimal(fee), Decimal(bid_depth), Decimal(ask_depth))


def test_cross_venue_arbitrage_accounts_for_fees_cost_and_stale_quotes():
    buy = quote("buy-venue", "99", "100", fee="10")
    sell = quote("sell-venue", "103", "104", fee="10")
    result = assess_cross_venue_arbitrage(
        buy, sell, 2, transfer_and_network_cost_quote="1",
        minimum_net_profit_quote="2", minimum_net_edge_bps="50", now=NOW,
    )
    assert result.approved
    assert result.gross_profit_quote == Decimal("6")
    assert result.net_profit_quote == Decimal("4.94")
    assert result.net_edge_bps > Decimal("200")

    stale = quote("old-venue", "99", "100", at=NOW - timedelta(minutes=1))
    rejected = assess_cross_venue_arbitrage(stale, sell, 1, now=NOW)
    assert not rejected.approved
    assert "quote_is_stale" in rejected.blockers


def test_cross_venue_arbitrage_rejects_instrument_mismatch_and_thin_depth():
    buy = quote("a", "99", "100", ask_depth="0.5")
    sell = VenueQuote("b", "BTC/USDC", Decimal("103"), Decimal("104"), NOW,
                      Decimal("10"), Decimal("100"), Decimal("100"))
    result = assess_cross_venue_arbitrage(buy, sell, 1, now=NOW)
    assert not result.approved
    assert "instrument_mismatch" in result.blockers
    assert "buy_side_visible_depth_insufficient" in result.blockers


def test_inventory_pressure_and_funding_cashflow_have_clear_signs():
    overweight = market_maker_inventory_pressure(150, 100, 100)
    underweight = market_maker_inventory_pressure(50, 100, 100)
    assert overweight.direction == "overweight"
    assert overweight.quote_skew_bps < 0
    assert underweight.quote_skew_bps > 0
    assert funding_cashflow("long", 1000, "0.001") == Decimal("-1")
    assert funding_cashflow("short", 1000, "0.001") == Decimal("1")
    assert funding_cashflow("long", 1000, "-0.001") == Decimal("1")


def test_macro_event_blackout_requires_sourced_high_impact_event():
    event = EconomicEvent("US CPI", "CPI", "high", "official calendar",
                          NOW + timedelta(minutes=10))
    low = EconomicEvent("CPI preview", "CPI", "low", "unverified blog",
                        NOW + timedelta(minutes=5))
    assert macro_event_blackout([event, low], now=NOW) == (event,)
    assert macro_event_blackout([event], now=NOW + timedelta(hours=1)) == ()


def test_metcalfe_and_reflexivity_are_descriptive_not_forecasts():
    assert metcalfe_activity_index(200, 100) == Decimal("4")
    aligned = reflexivity_feedback("0.1", "0.2", "0.3")
    assert aligned.aligned_positive
    assert aligned.feedback_score == Decimal("0.006")
    mixed = reflexivity_feedback("0.1", "-0.2", "0.3")
    assert mixed.label == "mixed_or_weak_feedback"


def test_trade_transfer_accounting_keeps_external_inflows_separate():
    result = classify_trade_transfer_mechanics(100, -100, 5, external_capital_flow=500)
    assert result.gross_pnl_balance == 0
    assert result.participant_net_after_fees == -5
    assert result.external_capital_flow == 500
    assert result.label == "zero_sum_before_fees_negative_after_fees"
    assert classify_trade_transfer_mechanics(100, -90, 2).label == "gross_pnl_reconciliation_required"
