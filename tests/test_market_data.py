import unittest
from unittest.mock import patch

from src.market_data import MarketDataError, TwelveDataClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        self.response = FakeResponse({
            "meta": {"symbol": "XAU/USD"},
            "values": [
                {"datetime": "2026-11-25 10:15:00", "open": "4100", "high": "4102", "low": "4098", "close": "4101"},
                {"datetime": "2026-11-25 10:00:00", "open": "4090", "high": "4101", "low": "4089", "close": "4100"},
            ],
        })

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        return self.response


class MarketDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_api_key(self):
        with self.assertRaises(MarketDataError):
            await TwelveDataClient("").time_series("XAU/USD")

    async def test_parses_chronological_candles(self):
        with patch("src.market_data.httpx.AsyncClient", FakeAsyncClient):
            candles = await TwelveDataClient("test-key").time_series("XAU/USD", "15min", 2)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0].datetime, "2026-11-25 10:00:00")
        self.assertEqual(candles[-1].close, 4101.0)

    async def test_provider_error_is_handled(self):
        class ErrorClient(FakeAsyncClient):
            def __init__(self, *args, **kwargs):
                self.response = FakeResponse({"status": "error", "message": "bad symbol"})

        with patch("src.market_data.httpx.AsyncClient", ErrorClient):
            with self.assertRaisesRegex(MarketDataError, "bad symbol"):
                await TwelveDataClient("test-key").time_series("NOT-A-SYMBOL")


if __name__ == "__main__":
    unittest.main()
