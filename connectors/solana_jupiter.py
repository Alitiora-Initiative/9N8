from __future__ import annotations

import base64
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import aiohttp
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TokenAccountOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from connectors.base import ExecutionConnector
from models import AccountState, Candle, ConnectorUnavailable, MarketSnapshot, MarketType, OpenPosition, RiskRejected, Side, TradeIntent, TradeReceipt
from security.dex_gate import DexSecurityGate

log = logging.getLogger(__name__)
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class SolanaJupiterConnector(ExecutionConnector):
    venue = "jupiter-solana"

    def __init__(self, rpc_url: str | None = None, api_key: str | None = None,
                 cluster: str = "devnet", private_key: str | None = None,
                 live_certified: bool = False):
        self.rpc_url = rpc_url or os.getenv("SOLANA_RPC_URL", "").strip()
        self.api_key = api_key or os.getenv("JUPITER_API_KEY", "").strip()
        self.cluster = cluster
        self.live_certified = bool(live_certified)
        self.private_key = (private_key or os.getenv("SOLANA_PRIVATE_KEY", "").strip()) if self._live_execution_authorized() else ""
        self.rpc: AsyncClient | None = None
        self.http: aiohttp.ClientSession | None = None
        self.keypair: Keypair | None = None
        self.base_url = "https://api.jup.ag/swap/v2"
        self.security = DexSecurityGate()

    def _live_execution_authorized(self) -> bool:
        return bool(
            self.live_certified
            and os.getenv("TRADING_MODE", "").strip().lower() == "live"
            and os.getenv("LIVE_TRADING", "false").strip().lower() == "true"
            and os.getenv("ALLOW_LIVE_TRADING", "false").strip().lower() == "true"
            and os.getenv("LIVE_EXECUTION_CERTIFIED", "false").strip().lower() == "true"
            and os.getenv("ALLOW_LIVE_DEX_EXECUTION", "false").strip().lower() == "true"
            and os.getenv("LIVE_ACKNOWLEDGEMENT", "") == "I_UNDERSTAND_REAL_FUNDS_CAN_BE_LOST"
        )

    async def connect(self) -> None:
        if not self.rpc_url:
            raise ConnectorUnavailable("SOLANA_RPC_URL is required.")
        if not self._live_execution_authorized():
            self.private_key = ""
            log.warning("Solana signing is disabled; this connector is in market-data-only mode.")
        if self.cluster == "mainnet-beta" and not self.api_key:
            raise ConnectorUnavailable("JUPITER_API_KEY is required for mainnet usage.")
        self.rpc = AsyncClient(self.rpc_url, timeout=15)
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        if self.private_key:
            try:
                self.keypair = Keypair.from_base58_string(self.private_key)
            except Exception as exc:
                raise ConnectorUnavailable("SOLANA_PRIVATE_KEY is not a valid base58 keypair.") from exc
        if self.cluster == "devnet":
            log.warning("Jupiter Swap API routes mainnet liquidity; devnet mode is RPC/data testing only.")

    async def close(self) -> None:
        if self.rpc:
            await self.rpc.close()
            self.rpc = None
        if self.http:
            await self.http.close()
            self.http = None
        await self.security.close()

    async def _get(self, url: str, params: dict | None = None) -> dict:
        if not self.http:
            raise ConnectorUnavailable("Solana connector not connected.")
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        async with self.http.get(url, params=params, headers=headers) as r:
            body = await r.json(content_type=None)
            if r.status >= 400:
                raise ConnectorUnavailable(f"Jupiter/public token data returned HTTP {r.status}.")
            return body

    async def _rpc(self, method: str, params: list) -> dict:
        if not self.http:
            raise ConnectorUnavailable("Solana connector not connected.")
        async with self.http.post(self.rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        }) as r:
            body = await r.json(content_type=None)
            if r.status >= 400 or "error" in body:
                raise ConnectorUnavailable(f"Solana RPC {method} failed.")
            return body["result"]

    async def fetch_snapshot(self, symbol: str) -> MarketSnapshot:
        data = await self._get("https://api.jup.ag/price/v3", {"ids": symbol})
        record = data.get("data", {}).get(symbol) or data.get(symbol)
        if not isinstance(record, dict) or not record.get("usdPrice"):
            raise ConnectorUnavailable("Jupiter Price API returned no USD price.")
        price = float(record["usdPrice"])
        # Price API is indicative and has no bid/ask. Execution impact is measured
        # with a firm Jupiter order quote before signing.
        return MarketSnapshot(self.venue, symbol, MarketType.SOLANA_DEX, price, price, price)

    async def fetch_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        if not self.http:
            raise ConnectorUnavailable("Solana connector not connected.")
        api_key = os.getenv("BIRDEYE_API_KEY", "").strip()
        if not api_key:
            raise ConnectorUnavailable("BIRDEYE_API_KEY is required for Solana OHLCV history.")
        seconds = {"1m": 60, "5m": 300, "1h": 3600}.get(timeframe)
        if not seconds:
            raise ValueError("Unsupported Solana OHLCV timeframe.")
        now = int(datetime.now(timezone.utc).timestamp())
        params = {"address": symbol, "type": timeframe, "time_from": now - seconds * limit, "time_to": now}
        async with self.http.get(
            "https://public-api.birdeye.so/defi/ohlcv", params=params,
            headers={"X-API-KEY": api_key, "x-chain": "solana"},
        ) as r:
            data = await r.json(content_type=None)
            if r.status >= 400 or not data.get("success"):
                raise ConnectorUnavailable(f"Birdeye OHLCV returned HTTP {r.status}.")
        rows = (data.get("data") or {}).get("items") or []
        candles = [
            Candle(int(x["unixTime"]) * 1000, float(x["o"]), float(x["h"]),
                   float(x["l"]), float(x["c"]), float(x.get("v", 0)))
            for x in rows
        ]
        if not candles:
            raise ConnectorUnavailable("Birdeye returned no candles.")
        return candles

    async def get_balance(self) -> AccountState:
        if not self.rpc or not self.keypair:
            raise ConnectorUnavailable("Solana burner wallet is not configured.")
        accounts = await self.rpc.get_token_accounts_by_owner(
            self.keypair.pubkey(),
            TokenAccountOpts(mint=Pubkey.from_string(USDC_MINT), encoding="jsonParsed"),
        )
        usdc = 0.0
        for item in accounts.value:
            info = item.account.data.parsed["info"]
            usdc += float(info["tokenAmount"]["uiAmount"] or 0)
        return AccountState(self.venue, usdc, usdc, usdc, datetime.now(timezone.utc))

    async def get_open_positions(self) -> list[OpenPosition]:
        # On-chain token balances do not include a dependable cost basis. The engine
        # reconciles positions from confirmed transaction deltas and its SQLite ledger.
        return []

    async def _confirmed_token_delta(self, signature: str, mint: str) -> tuple[float, float]:
        for _ in range(12):
            tx = await self._rpc("getTransaction", [signature, {
                "encoding": "jsonParsed", "commitment": "finalized",
                "maxSupportedTransactionVersion": 0,
            }])
            if tx:
                meta = tx.get("meta") or {}
                if meta.get("err") is not None:
                    raise ConnectorUnavailable("Solana transaction finalized with an on-chain error.")
                owner = str(self.keypair.pubkey())
                pre = 0.0
                post = 0.0
                for item in meta.get("preTokenBalances", []):
                    if item.get("mint") == mint and item.get("owner") == owner:
                        pre += float(item["uiTokenAmount"]["uiAmountString"])
                for item in meta.get("postTokenBalances", []):
                    if item.get("mint") == mint and item.get("owner") == owner:
                        post += float(item["uiTokenAmount"]["uiAmountString"])
                fee_sol = float(meta.get("fee", 0)) / 1_000_000_000
                return post - pre, fee_sol
            await __import__("asyncio").sleep(1)
        raise ConnectorUnavailable("Solana transaction could not be reconciled; do not retry until chain status is checked.")

    async def _swap(self, intent: TradeIntent, quantity: float, client_order_id: str) -> TradeReceipt:
        if not self._live_execution_authorized():
            raise RiskRejected("Solana live execution is locked by the connector-level certification gate.")
        if self.cluster != "mainnet-beta":
            raise ConnectorUnavailable("Jupiter Swap API does not provide a Devnet trading venue.")
        if not self.keypair or not self.api_key or not self.http:
            raise ConnectorUnavailable("A dedicated burner wallet and Jupiter API key are required.")
        safety = intent.metadata.get("token_safety")
        if not isinstance(safety, dict) or not safety.get("approved") or not safety.get("buy_sell_simulation_passed"):
            raise RiskRejected("Solana token security and independent buy/sell simulation have not passed.")
        mint = str(intent.metadata.get("token_mint", ""))
        decimals = int(intent.metadata.get("token_decimals", -1))
        if not mint or not 0 <= decimals <= 18:
            raise RiskRejected("Token mint and verified decimals are required.")
        if intent.side is Side.BUY:
            input_mint, output_mint = USDC_MINT, mint
            raw_amount = int(float(intent.metadata["notional_usd"]) * 1_000_000)
            delta_mint = mint
        else:
            input_mint, output_mint = mint, USDC_MINT
            raw_amount = int(quantity * (10 ** decimals))
            delta_mint = mint
        order = await self._get(self.base_url + "/order", {
            "inputMint": input_mint, "outputMint": output_mint,
            "amount": str(raw_amount), "taker": str(self.keypair.pubkey()),
        })
        encoded = order.get("transaction")
        request_id = order.get("requestId")
        if not encoded or not request_id:
            raise ConnectorUnavailable("Jupiter did not return a signable order transaction.")
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(encoded))
        signed = VersionedTransaction(unsigned.message, [self.keypair])
        payload = {
            "signedTransaction": base64.b64encode(bytes(signed)).decode("ascii"),
            "requestId": request_id,
        }
        headers = {"x-api-key": self.api_key, "Content-Type": "application/json"}
        async with self.http.post(self.base_url + "/execute", json=payload, headers=headers) as r:
            result = await r.json(content_type=None)
            if r.status >= 400 or str(result.get("status", "")).lower() not in {"success", "completed"}:
                raise ConnectorUnavailable("Jupiter execution was not confirmed successful.")
        signature = str(result.get("signature", ""))
        if not signature:
            raise ConnectorUnavailable("Jupiter returned no transaction signature.")
        delta, fee_sol = await self._confirmed_token_delta(signature, delta_mint)
        filled = abs(delta)
        if filled <= 0:
            raise ConnectorUnavailable("Confirmed transaction has no wallet token balance delta.")
        price = intent.entry_price if intent.side is Side.BUY else intent.entry_price
        return TradeReceipt(
            self.venue, intent.symbol, intent.side, signature, filled, price, 0.0, 0.0,
            datetime.now(timezone.utc), tx_hash=signature,
            raw={"jupiter": result, "client_order_id": client_order_id,
                 "gas_fee_sol": fee_sol, "reconciliation": "finalized token balance delta"},
        )

    async def buy(self, intent: TradeIntent, quantity: float, client_order_id: str) -> TradeReceipt:
        if intent.side is not Side.BUY:
            raise ValueError("buy() requires buy intent.")
        return await self._swap(intent, quantity, client_order_id)

    async def sell(self, intent: TradeIntent, quantity: float, client_order_id: str) -> TradeReceipt:
        if intent.side is not Side.SELL:
            raise ValueError("sell() requires sell intent.")
        return await self._swap(intent, quantity, client_order_id)

    async def close_position(self, position: OpenPosition, quantity: float | None = None) -> TradeReceipt:
        intent = TradeIntent(
            self.venue, position.symbol, MarketType.SOLANA_DEX, Side.SELL,
            position.mark_price, position.stop_loss, position.take_profit,
            abs(position.mark_price-position.stop_loss), "Emergency Solana exit", position.metadata,
        )
        return await self._swap(intent, float(quantity or position.quantity), "panic-" + uuid.uuid4().hex)

    async def cancel_all_orders(self, symbol: str | None = None) -> None:
        # A completed Jupiter swap is an atomic transaction, not a resting order.
        return None

    async def provider_health(self) -> dict[str, Any]:
        return {"venue": self.venue, "cluster": self.cluster, "connected": self.rpc is not None,
                "wallet_configured": self.keypair is not None, "swap_api_mainnet_only": True}
