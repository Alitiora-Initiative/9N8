import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.mt5_provider import MT5Error, MT5Provider, MT5SymbolInfo
from src.paper import PaperBroker, PaperTradingError


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2
    TIMEFRAME_M15 = 15

    def __init__(self, trade_mode=0):
        self.trade_mode = trade_mode
        self.did_shutdown = False
        self.last_order_send = False

    def initialize(self, *_args):
        return True

    def terminal_info(self):
        return SimpleNamespace(connected=True)

    def account_info(self):
        return SimpleNamespace(login=123, server="Broker-Demo", currency="USD", balance=10000, equity=10000, trade_mode=self.trade_mode)

    def last_error(self):
        return (0, "ok")

    def shutdown(self):
        self.did_shutdown = True

    def symbol_info(self, symbol):
        return SimpleNamespace(
            name=symbol, digits=2, point=0.01, trade_contract_size=100,
            volume_min=0.01, volume_max=10.0, volume_step=0.01,
            trade_stops_level=1, currency_profit="USD",
        )

    def symbol_select(self, _symbol, _select):
        return True

    def symbol_info_tick(self, symbol):
        now = int(datetime.now(timezone.utc).timestamp())
        return SimpleNamespace(time=now, bid=2000.00, ask=2000.10)


class FakeProvider:
    def __init__(self):
        self.info = MT5SymbolInfo("XAUUSD", 2, 0.01, 100.0, 0.01, 10.0, 0.01, 1, "USD")
        self.bid = 2000.00
        self.ask = 2000.10

    def connect(self):
        return SimpleNamespace(server="Broker-Demo")

    def symbol_info(self, _symbol):
        return self.info

    def tick(self, symbol):
        from src.mt5_provider import MT5Tick
        return MT5Tick(symbol, self.bid, self.ask, datetime.now(timezone.utc).isoformat())


class MT5ProviderTests(unittest.TestCase):
    def test_accepts_demo_account(self):
        provider = MT5Provider(mt5_module=FakeMT5())
        account = provider.account_summary()
        self.assertEqual(account.server, "Broker-Demo")

    def test_refuses_real_account(self):
        fake = FakeMT5(trade_mode=FakeMT5.ACCOUNT_TRADE_MODE_REAL)
        provider = MT5Provider(mt5_module=fake)
        with self.assertRaises(MT5Error):
            provider.connect()
        self.assertTrue(fake.did_shutdown)

    def test_refuses_if_demo_enum_missing(self):
        fake = FakeMT5()
        fake.ACCOUNT_TRADE_MODE_DEMO = None
        provider = MT5Provider(mt5_module=fake)
        with self.assertRaises(MT5Error):
            provider.connect()


class PaperBrokerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.provider = FakeProvider()
        self.broker = PaperBroker(
            provider=self.provider,
            db_path=str(Path(self.tempdir.name) / "paper.sqlite3"),
            max_lots=0.10,
            max_open_positions=2,
            slippage_points=0,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_open_and_close_paper_trade_updates_local_ledger(self):
        position = self.broker.open_position("XAUUSD", "BUY", 0.01, 100, 200)
        self.assertAlmostEqual(position.entry_price, 2000.10)
        self.provider.bid = 2001.00
        closed = self.broker.close_position(position.ticket)
        self.assertEqual(closed.ticket, position.ticket)
        self.assertGreater(closed.pnl_quote, 0)
        self.assertEqual(len(self.broker.open_positions()), 0)
        self.assertEqual(self.broker.summary()["total_trades"], 1)

    def test_nan_lot_size_is_rejected(self):
        with self.assertRaises(PaperTradingError):
            self.broker.open_position("XAUUSD", "BUY", float("nan"), 100, 200)

    def test_lot_over_limit_is_rejected(self):
        with self.assertRaises(PaperTradingError):
            self.broker.open_position("XAUUSD", "BUY", 0.11, 100, 200)

    def test_open_position_cap_is_enforced(self):
        self.broker.open_position("XAUUSD", "BUY", 0.01, 100, 200)
        self.broker.open_position("XAUUSD", "SELL", 0.01, 100, 200)
        with self.assertRaises(PaperTradingError):
            self.broker.open_position("XAUUSD", "BUY", 0.01, 100, 200)

    def test_stale_tick_is_rejected(self):
        from src.mt5_provider import MT5Tick
        self.provider.tick = lambda symbol: MT5Tick(
            symbol, 2000.0, 2000.1, "2020-01-01T00:00:00+00:00"
        )
        with self.assertRaises(PaperTradingError):
            self.broker.open_position("XAUUSD", "BUY", 0.01, 100, 200)

    def test_background_stop_check_closes_only_local_paper_position(self):
        position = self.broker.open_position("XAUUSD", "BUY", 0.01, 100, 200)
        self.provider.bid = 1998.90
        closed = self.broker.check_stops()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].ticket, position.ticket)
        self.assertEqual(len(self.broker.open_positions()), 0)

    def test_break_even_stop_activates_after_favorable_move_then_closes_locally(self):
        position = self.broker.open_position("XAUUSD", "BUY", 0.01, 100, 300, 501, 0.10, 2, 50, 0)
        self.provider.bid = 2000.70
        self.provider.ask = 2000.80
        self.assertEqual(self.broker.check_stops(), [])
        active = self.broker.open_positions(owner_id=501)[0]
        self.assertTrue(active.be_activated)
        self.assertAlmostEqual(active.stop_loss, position.entry_price)
        self.provider.bid = 2000.05
        self.provider.ask = 2000.15
        closed = self.broker.check_stops(owner_id=501)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].reason, "BREAK_EVEN")
        self.assertEqual(len(self.broker.open_positions(owner_id=501)), 0)

    def test_paper_accounts_are_isolated_by_telegram_user(self):
        first = self.broker.open_position("XAUUSD", "BUY", 0.01, 100, 200, owner_id=1001)
        self.assertEqual(len(self.broker.open_positions(owner_id=1001)), 1)
        self.assertEqual(len(self.broker.open_positions(owner_id=1002)), 0)
        with self.assertRaises(PaperTradingError):
            self.broker.close_position(first.ticket, owner_id=1002)
        self.assertEqual(len(self.broker.open_positions(owner_id=1001)), 1)

    def test_user_limits_are_enforced_below_platform_limits(self):
        with self.assertRaises(PaperTradingError):
            self.broker.open_position("XAUUSD", "BUY", 0.06, 100, 200, owner_id=88, max_lots=0.05)

    def test_weekly_stats_include_only_selected_opt_in_user_ids(self):
        first = self.broker.open_position("XAUUSD", "BUY", 0.01, 100, 200, owner_id=71)
        self.provider.bid = 2001.00
        self.broker.close_position(first.ticket, owner_id=71)
        second = self.broker.open_position("XAUUSD", "BUY", 0.01, 100, 200, owner_id=72)
        self.provider.bid = 1999.00
        self.broker.close_position(second.ticket, owner_id=72)
        start = "2000-01-01T00:00:00+00:00"
        end = "2100-01-01T00:00:00+00:00"
        opted_in = self.broker.period_stats(start, end, [71])
        self.assertEqual(opted_in["trades"], 1)
        self.assertEqual(opted_in["wins"], 1)
        self.assertEqual(opted_in["losses"], 0)
        self.assertEqual(self.broker.period_stats(start, end, [])["trades"], 0)


if __name__ == "__main__":
    unittest.main()
