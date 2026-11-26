from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import aiohttp
from eth_account import Account
from web3 import AsyncHTTPProvider, AsyncWeb3

from connectors.base import ExecutionConnector
from connectors.retry import RetryableReadError, retry_read
from models import (
    AccountState, Candle, ConnectorUnavailable, MarketSnapshot, MarketType,
    OpenPosition, RiskRejected, Side, TradeIntent, TradeReceipt,
)
from security.dex_gate import DexSecurityGate

ERC20_ABI = [
    {"type":"function","name":"decimals","stateMutability":"view","inputs":[],"outputs":[{"type":"uint8"}]},
    {"type":"function","name":"balanceOf","stateMutability":"view","inputs":[{"name":"owner","type":"address"}],"outputs":[{"type":"uint256"}]},
    {"type":"function","name":"allowance","stateMutability":"view","inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"outputs":[{"type":"uint256"}]},
    {"type":"function","name":"approve","stateMutability":"nonpayable","inputs":[{"name":"spender","type":"address"},{"name":"value","type":"uint256"}],"outputs":[{"type":"bool"}]},
]
USDC_BY_CHAIN = {
    1: "0xA0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    56: "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}


class EVM0xConnector(ExecutionConnector):
    venue = "0x-evm-dex"

    def __init__(self, rpc_url: str | None = None, chain_id: int | None = None,
                 private_key: str | None = None, live_certified: bool = False):
        self.rpc_url = rpc_url or os.getenv("EVM_RPC_URL", "").strip()
        self.chain_id = chain_id or int(os.getenv("EVM_CHAIN_ID", "1"))
        self.live_certified = bool(live_certified)
        self.private_key = (private_key or os.getenv("EVM_PRIVATE_KEY", "").strip()) if self._live_execution_authorized() else ""
        self.api_key = os.getenv("ZEROX_API_KEY", "").strip()
        self.w3: AsyncWeb3 | None = None
        self.account = None
        self.http: aiohttp.ClientSession | None = None
        self.security = DexSecurityGate()
        self.venue = f"0x-chain-{self.chain_id}"

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

    @retry_read(attempts=3, timeout_seconds=10)
    async def connect(self) -> None:
        if not self.rpc_url:
            raise ConnectorUnavailable("EVM_RPC_URL is required.")
        if not self._live_execution_authorized():
            self.private_key = ""
            self.account = None

        self.w3 = AsyncWeb3(AsyncHTTPProvider(self.rpc_url, request_kwargs={"timeout":10}))
        actual = int(await self.w3.eth.chain_id)
        if actual != self.chain_id:
            raise ConnectorUnavailable(f"RPC chain ID {actual} differs from configured {self.chain_id}.")
        if self.private_key:
            try:
                self.account = Account.from_key(self.private_key)
            except Exception as exc:
                raise ConnectorUnavailable("EVM_PRIVATE_KEY is invalid.") from exc
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self) -> None:
        if self.http:
            await self.http.close()
            self.http = None
        if self.w3:
            await self.w3.provider.disconnect()
            self.w3 = None
        await self.security.close()

    @retry_read(attempts=3, timeout_seconds=10)
    async def _quote(self, params: dict) -> dict:
        if not self.http:
            raise ConnectorUnavailable("0x connector not connected.")
        if not self.api_key:
            raise ConnectorUnavailable("ZEROX_API_KEY is required.")
        async with self.http.get(
            "https://api.0x.org/swap/allowance-holder/quote",
            params=params,
            headers={"0x-api-key":self.api_key,"0x-version":"v2"},
        ) as response:
            data = await response.json(content_type=None)
            if response.status in {408, 425, 429, 500, 502, 503, 504}:
                raise RetryableReadError(f"0x quote transient HTTP {response.status}.")
            if response.status >= 400:
                raise ConnectorUnavailable(f"0x quote returned HTTP {response.status}.")
            return data

    @retry_read(attempts=3, timeout_seconds=10)
    async def fetch_snapshot(self, token_address: str) -> MarketSnapshot:
        if not self.http:
            raise ConnectorUnavailable("0x connector not connected.")
        async with self.http.get(
            "https://api.dexscreener.com/latest/dex/tokens/" + token_address
        ) as response:
            data = await response.json(content_type=None)
            if response.status in {408, 425, 429, 500, 502, 503, 504}:
                raise RetryableReadError(f"DexScreener transient HTTP {response.status}.")
            if response.status >= 400:
                raise ConnectorUnavailable("DexScreener market data unavailable.")
        chain_names = {1:"ethereum",56:"bsc",8453:"base"}
        wanted = chain_names.get(self.chain_id)
        pairs = [p for p in data.get("pairs",[]) if p.get("chainId") == wanted]
        if not pairs:
            raise ConnectorUnavailable("No DexScreener pair exists for this token on the configured chain.")
        pair = max(pairs,key=lambda p:float((p.get("liquidity") or {}).get("usd") or 0))
        price = float(pair["priceUsd"])
        return MarketSnapshot(
            self.venue, token_address, MarketType.EVM_DEX, price, price, price,
            volume_5m=float((pair.get("volume") or {}).get("m5") or 0),
            buy_volume_5m=float((pair.get("txns") or {}).get("m5",{}).get("buys") or 0),
            sell_volume_5m=float((pair.get("txns") or {}).get("m5",{}).get("sells") or 0),
        )

    @retry_read(attempts=3, timeout_seconds=10)
    async def fetch_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        raise ConnectorUnavailable(
            "EVM DEX OHLCV requires a verified pool-address/history provider. "
            "No synthetic candles are generated; configure a pool feed before enabling DEX strategies."
        )

    @retry_read(attempts=3, timeout_seconds=10)
    async def _token_balance(self, token_address: str) -> tuple[float,float]:
        if not self.w3 or not self.account:
            raise ConnectorUnavailable("EVM burner wallet is not configured.")
        token = self.w3.eth.contract(address=self.w3.to_checksum_address(token_address),abi=ERC20_ABI)
        decimals = int(await token.functions.decimals().call())
        raw = int(await token.functions.balanceOf(self.account.address).call())
        return raw / (10 ** decimals), float(decimals)

    @retry_read(attempts=3, timeout_seconds=10)
    async def get_balance(self) -> AccountState:
        if not self.w3 or not self.account:
            raise ConnectorUnavailable("EVM burner wallet is not configured.")
        stable = USDC_BY_CHAIN.get(self.chain_id)
        if not stable:
            raise ConnectorUnavailable("No USD stablecoin configuration for this EVM chain.")
        balance, _ = await self._token_balance(stable)
        return AccountState(self.venue,balance,balance,balance,datetime.now(timezone.utc))

    @retry_read(attempts=3, timeout_seconds=10)
    async def get_open_positions(self) -> list[OpenPosition]:
        # Token balances alone have no reliable cost basis; use the reconciled SQLite trade ledger.
        return []

    async def _send_transaction(self, tx: dict) -> str:
        if not self.w3 or not self.account:
            raise ConnectorUnavailable("EVM wallet is not configured.")
        tx.setdefault("from",self.account.address)
        tx.setdefault("chainId",self.chain_id)
        tx.setdefault("nonce",await self.w3.eth.get_transaction_count(self.account.address,"pending"))
        if "gas" not in tx:
            tx["gas"] = int(await self.w3.eth.estimate_gas(tx) * 1.2)
        if "gasPrice" not in tx and "maxFeePerGas" not in tx:
            tx["gasPrice"] = await self.w3.eth.gas_price
        signed = self.account.sign_transaction(tx)
        tx_hash = await asyncio.wait_for(
            self.w3.eth.send_raw_transaction(signed.raw_transaction), timeout=5.0
        )
        receipt = await self.w3.eth.wait_for_transaction_receipt(tx_hash,timeout=90)
        if int(receipt.status) != 1:
            raise ConnectorUnavailable("EVM transaction reverted on-chain.")
        return tx_hash.hex()

    async def _swap(self, intent: TradeIntent, quantity: float, client_order_id: str) -> TradeReceipt:
        if not self._live_execution_authorized():
            raise RiskRejected("EVM live execution is locked by the connector-level certification gate.")
        if not self.w3 or not self.account:
            raise ConnectorUnavailable("EVM burner wallet is not configured.")
        safety = intent.metadata.get("token_safety")
        if not isinstance(safety,dict) or not safety.get("approved") or not safety.get("buy_sell_simulation_passed"):
            raise RiskRejected("EVM token security and independent buy/sell simulation have not passed.")
        sell_token = intent.metadata.get("sell_token")
        buy_token = intent.metadata.get("buy_token")
        sell_amount = int(intent.metadata.get("sell_amount_raw",0))
        if not sell_token or not buy_token or sell_amount <= 0:
            raise RiskRejected("0x sell token, buy token and raw amount are required.")
        quote = await self._quote({
            "chainId":str(self.chain_id),"sellToken":sell_token,"buyToken":buy_token,
            "sellAmount":str(sell_amount),"taker":self.account.address,
        })
        txdata = quote.get("transaction")
        allowance = quote.get("issues",{}).get("allowance") or {}
        spender = allowance.get("spender") or quote.get("allowanceTarget")
        if not isinstance(txdata,dict) or not txdata.get("to") or not txdata.get("data"):
            raise ConnectorUnavailable("0x did not return a complete firm transaction quote.")
        allowed = {str(x).lower() for x in intent.metadata.get("allowlisted_0x_targets",[])}
        if str(txdata["to"]).lower() not in allowed:
            raise RiskRejected("0x transaction target is absent from the explicit chain/router allowlist.")
        if spender:
            if str(spender).lower() not in allowed:
                raise RiskRejected("0x allowance spender is absent from the explicit allowlist.")
            token = self.w3.eth.contract(
                address=self.w3.to_checksum_address(sell_token),abi=ERC20_ABI
            )
            current = int(await token.functions.allowance(
                self.account.address,self.w3.to_checksum_address(spender)
            ).call())
            if current < sell_amount:
                # Exact allowance only. Infinite approvals are deliberately forbidden.
                approve = token.functions.approve(self.w3.to_checksum_address(spender),sell_amount)
                await self._send_transaction(await approve.build_transaction({
                    "from":self.account.address,"chainId":self.chain_id,
                    "nonce":await self.w3.eth.get_transaction_count(self.account.address,"pending"),
                }))
        tx = {
            "to":self.w3.to_checksum_address(txdata["to"]),
            "data":txdata["data"],"value":int(txdata.get("value","0")),
            "chainId":self.chain_id,
        }
        # Simulate the exact firm quote against the current chain state before broadcast.
        await self.w3.eth.call({**tx,"from":self.account.address})
        tx_hash = await self._send_transaction(tx)
        return TradeReceipt(
            self.venue,intent.symbol,intent.side,tx_hash,quantity,intent.entry_price,0.0,0.0,
            datetime.now(timezone.utc),tx_hash=tx_hash,
            raw={"quote":quote,"client_order_id":client_order_id,"simulation":"eth_call_passed"},
        )

    async def buy(self,intent:TradeIntent,quantity:float,client_order_id:str)->TradeReceipt:
        if intent.side is not Side.BUY:
            raise ValueError("buy() requires buy intent.")
        return await self._swap(intent,quantity,client_order_id)

    async def sell(self,intent:TradeIntent,quantity:float,client_order_id:str)->TradeReceipt:
        if intent.side is not Side.SELL:
            raise ValueError("sell() requires sell intent.")
        return await self._swap(intent,quantity,client_order_id)

    async def close_position(self,position:OpenPosition,quantity:float|None=None)->TradeReceipt:
        intent=TradeIntent(
            self.venue,position.symbol,MarketType.EVM_DEX,Side.SELL,
            position.mark_price,position.stop_loss,position.take_profit,
            abs(position.mark_price-position.stop_loss),"Emergency EVM exit",position.metadata,
        )
        return await self._swap(intent,float(quantity or position.quantity),"panic-"+uuid.uuid4().hex)

    async def cancel_all_orders(self,symbol:str|None=None)->None:
        # A 0x swap is an atomic transaction, not a resting order.
        return None

    async def provider_health(self)->dict[str,Any]:
        return {"venue":self.venue,"connected":self.w3 is not None,"wallet_configured":self.account is not None}
