import time
import unittest

from src.okx_provider import OKXClient, OKXError, sign_request


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeHTTP:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.closed = False

    def request(self, method, url, headers=None, content=None, timeout=None):
        self.calls.append({
            "method": method, "url": url, "headers": headers or {},
            "content": content, "timeout": timeout,
        })
        return self.replies.pop(0)

    def close(self):
        self.closed = True


def server_time():
    return FakeResponse({
        "code": "0",
        "data": [{"ts": str(int(time.time() * 1000))}],
    })


class OKXProviderTests(unittest.TestCase):
    def test_signature_is_base64_hmac_sha256(self):
        signature = sign_request(
            "secret",
            "2026-11-25T08:00:00.000Z",
            "GET",
            "/api/v5/account/balance",
            "",
        )
        self.assertTrue(signature)
        self.assertEqual(len(signature), 44)

    def test_demo_private_request_uses_auth_and_simulated_header(self):
        http = FakeHTTP([
            server_time(),
            FakeResponse({"code": "0", "data": [{"totalEq": "12.3"}]}),
        ])
        client = OKXClient("key", "secret", "phrase", demo=True, http_client=http)
        data = client.balance()
        self.assertEqual(data["totalEq"], "12.3")
        self.assertIn("/api/v5/public/time", http.calls[0]["url"])
        headers = http.calls[1]["headers"]
        self.assertEqual(headers["OK-ACCESS-KEY"], "key")
        self.assertEqual(headers["OK-ACCESS-PASSPHRASE"], "phrase")
        self.assertEqual(headers["x-simulated-trading"], "1")
        self.assertIn("OK-ACCESS-SIGN", headers)
        self.assertIn("OK-ACCESS-TIMESTAMP", headers)

    def test_live_private_request_omits_demo_header(self):
        http = FakeHTTP([
            server_time(),
            FakeResponse({"code": "0", "data": [{"acctLv": "1"}]}),
        ])
        client = OKXClient("key", "secret", "phrase", demo=False, http_client=http)
        client.account_config()
        self.assertNotIn("x-simulated-trading", http.calls[1]["headers"])

    def test_private_request_refuses_missing_credentials(self):
        client = OKXClient()
        with self.assertRaises(OKXError):
            client.balance()

    def test_market_ticker_is_public_and_does_not_sign(self):
        http = FakeHTTP([FakeResponse({"code": "0", "data": [{"last": "90000"}]})])
        client = OKXClient(http_client=http)
        ticker = client.ticker("BTC-USDT")
        self.assertEqual(ticker["last"], "90000")
        self.assertNotIn("OK-ACCESS-KEY", http.calls[0]["headers"])
        self.assertIn("/api/v5/market/ticker?instId=BTC-USDT", http.calls[0]["url"])

    def test_spot_order_endpoint_receives_spot_only_confirmed_payload(self):
        http = FakeHTTP([
            server_time(),
            FakeResponse({
                "code": "0",
                "data": [{"ordId": "123456", "sCode": "0", "clOrdId": "abc"}],
            }),
        ])
        client = OKXClient("key", "secret", "phrase", demo=True, http_client=http)
        order = {
            "instId": "BTC-USDT",
            "tdMode": "cash",
            "side": "buy",
            "ordType": "limit",
            "px": "90000",
            "sz": "0.001",
            "clOrdId": "abc",
        }
        result = client.place_spot_order(order)
        self.assertEqual(result["ordId"], "123456")
        self.assertIn("/api/v5/trade/order", http.calls[1]["url"])
        self.assertIn('"tdMode":"cash"', http.calls[1]["content"])

    def test_order_requires_cash_mode(self):
        client = OKXClient("key", "secret", "phrase")
        with self.assertRaises(ValueError):
            client.place_spot_order({
                "instId": "BTC-USDT", "tdMode": "cross", "side": "buy",
                "ordType": "limit", "sz": "0.001"
            })

    def test_exchange_level_order_error_is_reported(self):
        http = FakeHTTP([
            server_time(),
            FakeResponse({
                "code": "0",
                "data": [{"ordId": "", "sCode": "51008", "sMsg": "Insufficient balance"}],
            }),
        ])
        client = OKXClient("key", "secret", "phrase", http_client=http)
        with self.assertRaises(OKXError):
            client.place_spot_order({
                "instId": "BTC-USDT", "tdMode": "cash", "side": "buy",
                "ordType": "market", "sz": "10", "tgtCcy": "quote_ccy"
            })


if __name__ == "__main__":
    unittest.main()
