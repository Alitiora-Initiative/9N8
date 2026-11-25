"""Minimal, fail-closed OKX v5 REST client for one personal account.

Only public market data, account reads, spot order placement, open-order reads,
and order cancellation are implemented. There are deliberately no withdrawal,
fund-transfer, sub-account, margin, or derivatives methods.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx


class OKXError(RuntimeError):
    """Safe, user-facing OKX API error."""


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sign_request(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    prehash = f"{timestamp}{method.upper()}{request_path}{body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), prehash, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


class OKXClient:
    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        *,
        base_url: str = "https://www.okx.com",
        demo: bool = True,
        timeout: float = 12.0,
        http_client: Any | None = None,
    ):
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.passphrase = passphrase.strip()
        self.base_url = base_url.rstrip("/")
        self.demo = bool(demo)
        self.timeout = float(timeout)
        self._http = http_client or httpx.Client(timeout=self.timeout)

        if not self.base_url.startswith("https://"):
            raise ValueError("OKX_API_BASE must use HTTPS.")

    @property
    def credentials_configured(self) -> bool:
        return bool(self.api_key and self.api_secret and self.passphrase)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        private: bool = False,
    ) -> dict[str, Any]:
        method = method.upper()
        query = urlencode(
            [(key, value) for key, value in (params or {}).items() if value is not None]
        )
        request_path = path + (f"?{query}" if query else "")
        body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else ""
        headers = {"Accept": "application/json", "Content-Type": "application/json"}

        if private:
            if not self.credentials_configured:
                raise OKXError(
                    "OKX credentials are not configured. Set OKX_API_KEY, "
                    "OKX_API_SECRET and OKX_API_PASSPHRASE in your private .env file."
                )
            timestamp = utc_timestamp()
            headers.update({
                "OK-ACCESS-KEY": self.api_key,
                "OK-ACCESS-SIGN": sign_request(
                    self.api_secret, timestamp, method, request_path, body_text
                ),
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": self.passphrase,
            })
            if self.demo:
                headers["x-simulated-trading"] = "1"

        try:
            response = self._http.request(
                method,
                self.base_url + request_path,
                headers=headers,
                content=body_text if body is not None else None,
                timeout=self.timeout,
            )
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise OKXError("OKX request timed out. Check your connection and try again.") from exc
        except httpx.HTTPError as exc:
            raise OKXError("Could not reach OKX. Check the API domain and your internet connection.") from exc
        except (ValueError, TypeError) as exc:
            raise OKXError("OKX returned an unreadable response.") from exc

        if not isinstance(payload, dict):
            raise OKXError("OKX returned an unexpected response format.")
        if response.status_code >= 400:
            code = str(payload.get("code", response.status_code))
            message = str(payload.get("msg", "Request rejected"))[:220]
            raise OKXError(f"OKX rejected the request ({code}): {message}")
        if str(payload.get("code", "0")) != "0":
            code = str(payload.get("code", "unknown"))
            message = str(payload.get("msg", "Request rejected"))[:220]
            raise OKXError(f"OKX error {code}: {message}")
        return payload

    def public_time(self) -> dict[str, Any]:
        payload = self._request("GET", "/api/v5/public/time")
        return (payload.get("data") or [{}])[0]

    def ticker(self, inst_id: str) -> dict[str, Any]:
        payload = self._request(
            "GET", "/api/v5/market/ticker", params={"instId": inst_id.upper()}
        )
        rows = payload.get("data") or []
        if not rows:
            raise OKXError(f"No market ticker was returned for {inst_id.upper()}.")
        return rows[0]

    def instrument(self, inst_id: str) -> dict[str, Any]:
        payload = self._request(
            "GET", "/api/v5/public/instruments",
            params={"instType": "SPOT", "instId": inst_id.upper()},
        )
        rows = payload.get("data") or []
        if not rows:
            raise OKXError(f"OKX does not list {inst_id.upper()} as a spot instrument.")
        item = rows[0]
        if item.get("state") != "live":
            raise OKXError(f"{inst_id.upper()} is not currently in the live-trading state.")
        return item

    def candles(self, inst_id: str, bar: str = "15m", limit: int = 100) -> list[list[str]]:
        if not 30 <= int(limit) <= 300:
            raise ValueError("Candle limit must be between 30 and 300.")
        payload = self._request(
            "GET", "/api/v5/market/candles",
            params={"instId": inst_id.upper(), "bar": bar, "limit": int(limit)},
        )
        rows = payload.get("data") or []
        if not rows:
            raise OKXError(f"No candles were returned for {inst_id.upper()}.")
        # OKX returns newest first; strategy calculations require oldest first.
        return list(reversed(rows))

    def account_config(self) -> dict[str, Any]:
        payload = self._request("GET", "/api/v5/account/config", private=True)
        rows = payload.get("data") or []
        if not rows:
            raise OKXError("OKX returned no account configuration.")
        return rows[0]

    def balance(self) -> dict[str, Any]:
        payload = self._request("GET", "/api/v5/account/balance", private=True)
        rows = payload.get("data") or []
        if not rows:
            raise OKXError("OKX returned no account balance.")
        return rows[0]

    def open_orders(self, inst_id: str | None = None) -> list[dict[str, Any]]:
        payload = self._request(
            "GET", "/api/v5/trade/orders-pending",
            params={"instType": "SPOT", "instId": inst_id.upper() if inst_id else None},
            private=True,
        )
        return payload.get("data") or []

    def place_spot_order(self, order: dict[str, Any]) -> dict[str, Any]:
        required = {"instId", "side", "ordType", "sz"}
        if not required.issubset(order):
            raise ValueError("Incomplete spot order.")
        if order["side"] not in {"buy", "sell"}:
            raise ValueError("Spot order side must be buy or sell.")
        if order["ordType"] not in {"limit", "market"}:
            raise ValueError("Only spot limit and market orders are enabled.")
        if order.get("tdMode") != "cash":
            raise ValueError("Only cash spot trading is enabled.")
        payload = self._request(
            "POST", "/api/v5/trade/order", body=order, private=True
        )
        rows = payload.get("data") or []
        if not rows:
            raise OKXError("OKX did not return an order result.")
        result = rows[0]
        if str(result.get("sCode", "0")) != "0":
            raise OKXError(
                f"OKX did not accept the order ({result.get('sCode')}): "
                f"{str(result.get('sMsg', 'Unknown order error'))[:200]}"
            )
        return result

    def cancel_spot_order(self, inst_id: str, order_id: str) -> dict[str, Any]:
        payload = self._request(
            "POST", "/api/v5/trade/cancel-order",
            body={"instId": inst_id.upper(), "ordId": str(order_id)},
            private=True,
        )
        rows = payload.get("data") or []
        if not rows:
            raise OKXError("OKX did not return a cancellation result.")
        result = rows[0]
        if str(result.get("sCode", "0")) != "0":
            raise OKXError(
                f"OKX could not cancel the order ({result.get('sCode')}): "
                f"{str(result.get('sMsg', 'Unknown cancellation error'))[:200]}"
            )
        return result

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass
