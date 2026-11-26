from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any, AsyncIterator

import aiohttp
import websockets

logger = logging.getLogger(__name__)

PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RAYDIUM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM_PROGRAM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


class HeliusError(RuntimeError):
    pass


class HeliusClient:
    def __init__(self, rpc_url: str, wss_url: str, api_key: str = "", timeout: float = 12.0):
        self.rpc_url, self.wss_url, self.api_key = rpc_url, wss_url, api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._ids = itertools.count(1)

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def rpc(self, method: str, params: list[Any]) -> Any:
        await self.start()
        assert self._session is not None
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        try:
            async with self._session.post(self.rpc_url, json=payload) as response:
                data = await response.json(content_type=None)
                if response.status >= 400:
                    raise HeliusError(f"RPC HTTP {response.status}: {str(data)[:200]}")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise HeliusError(f"Helius RPC request failed: {type(exc).__name__}") from exc
        if data.get("error"):
            error = data["error"]
            raise HeliusError(f"RPC {method} failed ({error.get('code')}): {str(error.get('message'))[:180]}")
        return data.get("result")

    async def balance_lamports(self, address: str) -> int:
        result = await self.rpc("getBalance", [address, {"commitment": "confirmed"}])
        return int(result["value"])

    async def signature_status(self, signature: str) -> dict[str, Any] | None:
        result = await self.rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        rows = result.get("value", [])
        return rows[0] if rows else None

    async def transaction(self, signature: str) -> dict[str, Any] | None:
        return await self.rpc("getTransaction", [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"},
        ])

    async def account_info(self, address: str, encoding: str = "base64") -> dict[str, Any] | None:
        result = await self.rpc("getAccountInfo", [address, {"encoding": encoding, "commitment": "confirmed"}])
        return result.get("value") if result else None

    async def token_supply(self, mint: str) -> dict[str, Any]:
        return await self.rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])

    async def largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        result = await self.rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        return result.get("value", [])

    async def token_accounts(self, owner: str, mint: str) -> list[dict[str, Any]]:
        result = await self.rpc("getTokenAccountsByOwner", [
            owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"},
        ])
        return result.get("value", [])

    async def token_balance_raw(self, owner: str, mint: str) -> int:
        accounts = await self.token_accounts(owner, mint)
        total = 0
        for item in accounts:
            amount = (
                item.get("account", {}).get("data", {}).get("parsed", {})
                .get("info", {}).get("tokenAmount", {}).get("amount", "0")
            )
            total += int(amount)
        return total

    async def rent_exemption(self, size: int) -> int:
        result = await self.rpc("getMinimumBalanceForRentExemption", [size, {"commitment": "confirmed"}])
        return int(result)

    async def recent_blockhash(self) -> str:
        result = await self.rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
        return str(result["value"]["blockhash"])

    async def logs(self, programs: list[str]) -> AsyncIterator[dict[str, Any]]:
        """Subscribe separately per program; each mentions filter accepts exactly one pubkey."""
        if not programs:
            raise ValueError("At least one program ID is required.")
        delay = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.wss_url, ping_interval=20, ping_timeout=20,
                    close_timeout=5, max_size=2**22,
                ) as ws:
                    request_to_program: dict[int, str] = {}
                    for request_id, program in enumerate(dict.fromkeys(programs), start=1):
                        request_to_program[request_id] = program
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0", "id": request_id,
                            "method": "logsSubscribe",
                            "params": [{"mentions": [program]}, {"commitment": "processed"}],
                        }))
                    subscription_to_program: dict[int, str] = {}
                    pending = set(request_to_program)
                    while pending:
                        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                        request_id = reply.get("id")
                        if request_id in pending:
                            pending.remove(request_id)
                            if "error" in reply:
                                raise HeliusError(
                                    f"WebSocket logsSubscribe failed for {request_to_program[request_id]}: {str(reply['error'])[:160]}"
                                )
                            subscription_to_program[int(reply["result"])] = request_to_program[request_id]
                    delay = 1.0
                    async for raw in ws:
                        message = json.loads(raw)
                        params = message.get("params", {})
                        subscription = params.get("subscription")
                        result = params.get("result", {})
                        value = result.get("value", {})
                        if value:
                            yield {
                                "program": subscription_to_program.get(subscription),
                                "signature": value.get("signature"),
                                "err": value.get("err"),
                                "logs": value.get("logs") or [],
                                "slot": result.get("context", {}).get("slot"),
                                "subscription": subscription,
                            }
            except asyncio.CancelledError:
                raise
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError, json.JSONDecodeError, HeliusError) as exc:
                logger.warning("Helius WebSocket reconnect after %s", type(exc).__name__)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
