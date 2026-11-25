import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from src.metaapi_provider import MetaApiProvider
from src.mt5_provider import MT5Error


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            request = SimpleNamespace()
            response = SimpleNamespace(status_code=self.status_code, request=request)
            import httpx
            raise httpx.HTTPStatusError("error", request=request, response=response)

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.closed = False

    def get(self, url, headers=None, params=None):
        self.calls.append((url, headers, params))
        return self.replies.pop(0)

    def close(self):
        self.closed = True


class MetaApiProviderTests(unittest.TestCase):
    def provider(self, payloads):
        client = FakeClient([FakeResponse(p) for p in payloads])
        provider = MetaApiProvider("test-token", "demo-account-id", client=client)
        return provider, client

    def test_demo_account_is_accepted(self):
        provider, client = self.provider([{
            "type": "ACCOUNT_TRADE_MODE_DEMO", "login": 1234,
            "server": "Broker-Demo", "currency": "USD", "balance": 10000,
            "equity": 10020,
        }])
        account = provider.connect()
        self.assertEqual(account.server, "Broker-Demo")
        self.assertEqual(client.calls[0][1]["auth-token"], "test-token")

    def test_real_account_is_rejected(self):
        provider, _ = self.provider([{
            "type": "ACCOUNT_TRADE_MODE_REAL", "login": 1234,
            "server": "Broker-Live", "currency": "USD", "balance": 10000,
            "equity": 10000,
        }])
        with self.assertRaises(MT5Error):
            provider.connect()

    def test_missing_token_is_rejected_before_network(self):
        provider = MetaApiProvider("", "demo-account-id", client=FakeClient([]))
        with self.assertRaises(MT5Error):
            provider.connect()

    def test_tick_and_symbol_spec_parsing(self):
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        provider, _ = self.provider([
            {"type": "ACCOUNT_TRADE_MODE_DEMO", "login": 1, "server": "D", "currency": "USD", "balance": 100, "equity": 100},
            {"symbol": "XAUUSD", "bid": 2000.0, "ask": 2000.1, "time": stamp},
            {"type": "ACCOUNT_TRADE_MODE_DEMO", "login": 1, "server": "D", "currency": "USD", "balance": 100, "equity": 100},
            {"symbol": "XAUUSD", "tickSize": 0.01, "digits": 2, "contractSize": 100, "minVolume": 0.01, "maxVolume": 10, "volumeStep": 0.01, "profitCurrency": "USD"},
        ])
        tick = provider.tick("XAUUSD")
        info = provider.symbol_info("XAUUSD")
        self.assertAlmostEqual(tick.ask, 2000.1)
        self.assertEqual(info.point, 0.01)
        self.assertEqual(info.currency_profit, "USD")

    def test_candle_history_maps_metaapi_timeframe_and_excludes_forming_bar(self):
        rows = [
            {"time": "2026-11-25T09:00:00.000Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "tickVolume": 10},
            {"time": "2026-11-25T09:15:00.000Z", "open": 1.5, "high": 2.5, "low": 1, "close": 2, "tickVolume": 11},
        ]
        provider, client = self.provider([rows])
        candles = provider.candles("EURUSD", "M15", 80)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].close, 1.5)
        self.assertIn("timeframes/15m/candles", client.calls[0][0])


if __name__ == "__main__":
    unittest.main()
