import unittest
from unittest.mock import patch
from decimal import Decimal

from src.bot import _assert_credentials, _build_order, _step_ok, _verify_safe_key
from src.okx_provider import OKXError


class BotLogicTests(unittest.TestCase):
    def test_limit_order_parser_builds_cash_spot_order(self):
        order, notional, details = _build_order(
            ["BTC-USDT", "buy", "limit", "0.001", "90000"]
        )
        self.assertEqual(order["instId"], "BTC-USDT")
        self.assertEqual(order["tdMode"], "cash")
        self.assertEqual(order["ordType"], "limit")
        self.assertEqual(order["side"], "buy")
        self.assertEqual(order["sz"], "0.001")
        self.assertEqual(order["px"], "90000")
        self.assertEqual(notional, Decimal("90"))
        self.assertIn("Base size", details)

    def test_market_buy_uses_quote_currency_size(self):
        order, notional, details = _build_order(
            ["BTC-USDT", "buy", "market", "10"]
        )
        self.assertEqual(order["tgtCcy"], "quote_ccy")
        self.assertEqual(order["sz"], "10")
        self.assertEqual(notional, Decimal("0"))
        self.assertIn("Quote spend", details)

    def test_market_sell_uses_base_currency_size(self):
        order, _, details = _build_order(
            ["BTC-USDT", "sell", "market", "0.001"]
        )
        self.assertEqual(order["tgtCcy"], "base_ccy")
        self.assertEqual(order["sz"], "0.001")
        self.assertIn("Base quantity", details)

    def test_order_parser_rejects_missing_limit_price(self):
        with self.assertRaises(ValueError):
            _build_order(["BTC-USDT", "buy", "limit", "0.001"])

    def test_order_parser_rejects_unknown_side(self):
        with self.assertRaises(ValueError):
            _build_order(["BTC-USDT", "short", "market", "10"])

    def test_lot_step_validation(self):
        self.assertTrue(_step_ok(Decimal("0.002"), "0.001"))
        self.assertFalse(_step_ok(Decimal("0.0025"), "0.001"))

    def test_credentials_guard_rejects_unconfigured_keys(self):
        fake = type("Fake", (), {"credentials_configured": False})()
        with patch("src.bot.okx", fake):
            with self.assertRaises(OKXError):
                _assert_credentials()

    def test_credentials_guard_accepts_configured_keys(self):
        fake = type("Fake", (), {"credentials_configured": True})()
        with patch("src.bot.okx", fake):
            _assert_credentials()

    def test_withdraw_permission_blocks_order_staging(self):
        fake = type("Fake", (), {"account_config": lambda self: {"perm": "read_only,trade,withdraw"}})()
        with patch("src.bot.okx", fake):
            with self.assertRaises(OKXError):
                _verify_safe_key()

    def test_read_only_key_cannot_stage_an_order(self):
        fake = type("Fake", (), {"account_config": lambda self: {"perm": "read_only"}})()
        with patch("src.bot.okx", fake):
            with self.assertRaises(OKXError):
                _verify_safe_key()


if __name__ == "__main__":
    unittest.main()
