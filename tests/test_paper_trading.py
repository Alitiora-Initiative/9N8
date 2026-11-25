import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from src.paper_trading import PaperConfig, PaperTradingEngine, PaperTradingError


class PaperTradingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = PaperConfig(
            initial_balance=Decimal("1000"),
            fee_rate=Decimal("0"),
            slippage_bps=Decimal("0"),
            risk_per_trade_pct=Decimal("1"),
            max_position_pct=Decimal("20"),
            daily_loss_limit_pct=Decimal("3"),
            hard_stop_loss_pct=Decimal("5"),
            tp1_target_pct=Decimal("10"),
            tp1_close_pct=Decimal("50"),
            trailing_activation_pct=Decimal("8"),
            trailing_stop_pct=Decimal("4"),
            max_hold_minutes=240,
            stagnation_profit_pct=Decimal("1"),
            max_open_positions=3,
        )
        self.engine = PaperTradingEngine(
            str(Path(self.temp.name) / "paper.sqlite3"), self.config
        )

    def tearDown(self):
        self.temp.cleanup()

    def open_one(self, symbol="BTC-USDT", amount="100"):
        return self.engine.open_position(
            symbol, Decimal(amount), bid="100", ask="100", reason="TEST"
        )

    def test_paper_entry_uses_virtual_cash_and_does_not_need_exchange(self):
        opened = self.open_one()
        self.assertEqual(opened["entry_price"], Decimal("100"))
        self.assertEqual(opened["quantity"], Decimal("1"))
        summary = self.engine.summary()
        self.assertEqual(summary["cash_balance"], Decimal("900"))
        self.assertEqual(summary["open_positions"], 1)

    def test_per_position_allocation_cap_and_duplicate_symbol_are_enforced(self):
        with self.assertRaises(PaperTradingError):
            self.open_one(amount="201")
        self.open_one()
        with self.assertRaises(PaperTradingError):
            self.open_one()

    def test_tp1_partially_closes_then_trailing_stop_closes_remainder(self):
        opened = self.open_one()
        tp1 = self.engine.update_prices({
            "BTC-USDT": {"bidPx": "110", "askPx": "110.1", "last": "110"}
        })
        self.assertEqual(len(tp1), 1)
        self.assertEqual(tp1[0]["reason"], "TAKE_PROFIT_1")
        self.assertTrue(tp1[0]["partial"])
        remaining = self.engine.positions()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(Decimal(remaining[0]["quantity"]), Decimal("0.5"))

        trailing = self.engine.update_prices({
            "BTC-USDT": {"bidPx": "105", "askPx": "105.1", "last": "105"}
        })
        self.assertEqual(len(trailing), 1)
        self.assertEqual(trailing[0]["reason"], "TRAILING_STOP")
        self.assertFalse(trailing[0]["partial"])
        self.assertEqual(self.engine.positions(), [])
        self.assertEqual(len(self.engine.history(10)), 2)
        self.assertGreater(self.engine.summary()["realized_pnl"], Decimal("0"))

    def test_hard_stop_closes_position(self):
        self.open_one()
        events = self.engine.update_prices({
            "BTC-USDT": {"bidPx": "94", "askPx": "94.1", "last": "94"}
        })
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason"], "HARD_STOP_LOSS")
        self.assertEqual(self.engine.positions(), [])

    def test_daily_drawdown_breaker_pauses_automatic_entries(self):
        config = PaperConfig(
            initial_balance=Decimal("1000"),
            fee_rate=Decimal("0"),
            slippage_bps=Decimal("0"),
            max_position_pct=Decimal("100"),
            daily_loss_limit_pct=Decimal("1"),
            hard_stop_loss_pct=Decimal("5"),
        )
        engine = PaperTradingEngine(str(Path(self.temp.name) / "risk.sqlite3"), config)
        engine.open_position("BTC-USDT", "500", bid="100", ask="100")
        events = engine.update_prices({
            "BTC-USDT": {"bidPx": "94", "askPx": "94.1", "last": "94"}
        })
        self.assertEqual(events[0]["reason"], "HARD_STOP_LOSS")
        self.assertFalse(engine.summary()["enabled"])
        self.assertGreaterEqual(engine.summary()["daily_drawdown_pct"], Decimal("1"))
        engine.set_enabled(False)
        with self.assertRaises(PaperTradingError):
            engine.set_enabled(True)

    def test_strategy_setting_is_validated_and_persisted(self):
        self.engine.set_strategy("trend")
        self.assertEqual(self.engine.strategy(), "trend")
        with self.assertRaises(PaperTradingError):
            self.engine.set_strategy("unbounded_magic")

    def test_manual_close_records_net_pnl_and_history(self):
        opened = self.open_one()
        closed = self.engine.close_position(opened["ticket"], bid="101")
        self.assertEqual(closed["reason"], "MANUAL_PAPER_CLOSE")
        self.assertEqual(closed["net_pnl"], Decimal("1"))
        self.assertEqual(self.engine.history(1)[0]["symbol"], "BTC-USDT")


if __name__ == "__main__":
    unittest.main()
