from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import aiohttp
from base58 import b58decode, b58encode
from solders.address_lookup_table_account import AddressLookupTable, AddressLookupTableAccount
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message, MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction, VersionedTransaction

from .config import Settings
from .helius import HeliusClient
from .math_engine import minimum_out, to_lamports

logger = logging.getLogger(__name__)
SOL_MINT = "So11111111111111111111111111111111111111112"


class ExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SwapResult:
    signature: str
    input_amount: int
    output_amount: int
    minimum_output: int
    bundle_id: str | None
    landing_status: str


class JupiterJitoExecutor:
    """Jupiter Swap API v2 custom-transaction route with optional Jito bundle.

    The Router /build endpoint returns raw instructions. This class composes
    them into a v0 transaction, adds an optional treasury profit transfer, and
    submits the swap and Jito tip transactions as one atomic Jito bundle.
    """
    def __init__(self, settings: Settings, helius: HeliusClient):
        self.settings, self.helius = settings, helius
        self._session: aiohttp.ClientSession | None = None
        self._ids = itertools.count(1)
        self._tip_cache: list[str] = []
        self._tip_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.settings.jupiter_api_key:
            headers["x-api-key"] = self.settings.jupiter_api_key
        return headers

    async def _jupiter_get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        await self.start()
        assert self._session
        url = self.settings.jupiter_base_url + endpoint
        try:
            async with self._session.get(url, params=params, headers=self._headers()) as response:
                data = await response.json(content_type=None)
                if response.status >= 400:
                    raise ExecutionError(f"Jupiter API rejected request ({response.status}): {str(data)[:220]}")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise ExecutionError(f"Jupiter request failed: {type(exc).__name__}") from exc
        if not isinstance(data, dict):
            raise ExecutionError("Jupiter returned an unexpected response.")
        return data

    async def _jito_rpc(self, method: str, params: list[Any]) -> Any:
        await self.start()
        assert self._session
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        url = self.settings.jito_base_url + "/api/v1/bundles"
        try:
            async with self._session.post(url, json=payload) as response:
                data = await response.json(content_type=None)
                if response.status >= 400:
                    raise ExecutionError(f"Jito HTTP {response.status}: {str(data)[:200]}")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise ExecutionError(f"Jito request failed: {type(exc).__name__}") from exc
        if data.get("error"):
            raise ExecutionError(f"Jito {method} failed: {str(data['error'])[:200]}")
        return data.get("result")

    async def _tip_accounts(self) -> list[Pubkey]:
        async with self._tip_lock:
            if not self._tip_cache:
                await self.start()
                assert self._session
                url = self.settings.jito_base_url + "/api/v1/getTipAccounts"
                async with self._session.post(
                    url, json={"jsonrpc": "2.0", "id": 1, "method": "getTipAccounts", "params": []}
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status >= 400 or data.get("error"):
                        raise ExecutionError("Could not retrieve official Jito tip accounts.")
                    self._tip_cache = [str(x) for x in data.get("result", [])]
                if not self._tip_cache:
                    raise ExecutionError("Jito returned no tip accounts.")
            return [Pubkey.from_string(x) for x in self._tip_cache]

    async def _instructions(self, items: list[dict[str, Any]]) -> list[Instruction]:
        output = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                program = Pubkey.from_string(item["programId"])
                data = b58decode(item["data"])
                accounts = [
                    AccountMeta(
                        Pubkey.from_string(meta["pubkey"]),
                        bool(meta.get("isSigner", False)),
                        bool(meta.get("isWritable", False)),
                    )
                    for meta in item.get("accounts", [])
                ]
            except Exception as exc:
                raise ExecutionError("Jupiter returned an invalid instruction; transaction was not signed.") from exc
            output.append(Instruction(program, data, accounts))
        return output

    async def _lookup_tables(self, addresses: list[str]) -> list[AddressLookupTableAccount]:
        tables: list[AddressLookupTableAccount] = []
        for address in addresses:
            info = await self.helius.account_info(address, "base64")
            if not info:
                raise ExecutionError(f"Address lookup table {address} is unavailable.")
            try:
                raw = base64.b64decode(info["data"][0], validate=True)
                table = AddressLookupTable.deserialize(raw)
                tables.append(AddressLookupTableAccount(Pubkey.from_string(address), list(table.addresses)))
            except Exception as exc:
                raise ExecutionError(f"Could not decode address lookup table {address}.") from exc
        return tables

    async def build_swap(
        self,
        wallet: Keypair,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int,
        priority_fee_lamports: int,
        jito_tip_lamports: int = 0,
        treasury_fee_lamports: int = 0,
        use_jito: bool = True,
    ) -> SwapResult:
        if amount_raw <= 0:
            raise ValueError("Swap amount must be positive.")
        if not 0 <= slippage_bps <= 5000:
            raise ValueError("Slippage must be between 0 and 50%.")
        if self.settings.trading_mode == "live" and not self.settings.live_trading_enabled:
            raise ExecutionError("Live trading is locked by environment configuration.")
        try:
            Pubkey.from_string(input_mint)
            Pubkey.from_string(output_mint)
        except Exception as exc:
            raise ValueError("Invalid SPL mint address.") from exc
        quote = await self._jupiter_get("/build", {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "taker": str(wallet.pubkey()),
            "slippageBps": str(slippage_bps),
            "computeUnitPriceMicroLamports": str(max(priority_fee_lamports, 0)),
        })
        if quote.get("error"):
            raise ExecutionError(f"Jupiter could not build route: {str(quote['error'])[:200]}")
        output_amount = int(quote.get("outAmount") or quote.get("otherAmountThreshold") or 0)
        if output_amount <= 0:
            raise ExecutionError("Jupiter did not return a positive expected output amount.")
        min_out = int(quote.get("otherAmountThreshold") or minimum_out(output_amount, slippage_bps))
        if min_out <= 0:
            raise ExecutionError("Jupiter minimum output is zero; refusing to trade.")
        blockhash = Hash.from_string(await self.helius.recent_blockhash())
        compute = await self._instructions(quote.get("computeBudgetInstructions", []))
        setup = await self._instructions(quote.get("setupInstructions", []))
        other = await self._instructions(quote.get("otherInstructions", []))
        swap = await self._instructions([quote["swapInstruction"]]) if quote.get("swapInstruction") else []
        cleanup = await self._instructions([quote["cleanupInstruction"]]) if quote.get("cleanupInstruction") else []
        if not swap:
            raise ExecutionError("Jupiter did not return a swap instruction.")
        # The API's compute budget instructions are authoritative. If the response
        # omits either compute instruction, add bounded defaults.
        if not any(ix.program_id == Pubkey.from_string("ComputeBudget111111111111111111111111111111") for ix in compute):
            compute = [set_compute_unit_limit(1_000_000), set_compute_unit_price(max(priority_fee_lamports, 0))] + compute
        instructions = compute + setup + other + swap + cleanup
        if treasury_fee_lamports:
            if treasury_fee_lamports < 0:
                raise ValueError("Treasury transfer cannot be negative.")
            from .config import Settings
            instructions.append(transfer(TransferParams(
                from_pubkey=wallet.pubkey(),
                to_pubkey=Pubkey.from_string(self.settings.treasury),
                lamports=treasury_fee_lamports,
            )))
        alts = await self._lookup_tables(quote.get("addressesByLookupTableAddress", []))
        try:
            message = MessageV0.try_compile(wallet.pubkey(), instructions, alts, blockhash)
            swap_tx = VersionedTransaction(message, [wallet])
        except Exception as exc:
            raise ExecutionError(f"Could not compile/sign the Jupiter transaction: {type(exc).__name__}") from exc

        if not use_jito or jito_tip_lamports <= 0:
            encoded = base64.b64encode(bytes(swap_tx)).decode("ascii")
            signature = await self.helius.rpc("sendTransaction", [
                encoded,
                {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed", "maxRetries": 3},
            ])
            await self._confirm_signature(str(signature))
            return SwapResult(str(signature), amount_raw, output_amount, min_out, None, "confirmed")

        tips = await self._tip_accounts()
        tip_destination = tips[int.from_bytes(__import__("os").urandom(2), "big") % len(tips)]
        tip_message = Message.new_with_blockhash(
            [transfer(TransferParams(
                from_pubkey=wallet.pubkey(),
                to_pubkey=tip_destination,
                lamports=jito_tip_lamports,
            ))],
            wallet.pubkey(),
            blockhash,
        )
        tip_tx = Transaction.new_signed_with_payer(
            [transfer(TransferParams(
                from_pubkey=wallet.pubkey(),
                to_pubkey=tip_destination,
                lamports=jito_tip_lamports,
            ))],
            wallet.pubkey(),
            [wallet],
            blockhash,
        )
        signed = [
            base64.b64encode(bytes(swap_tx)).decode("ascii"),
            base64.b64encode(bytes(tip_tx)).decode("ascii"),
        ]
        bundle_id = await self._jito_rpc("sendBundle", [signed, {"encoding": "base64"}])
        if not isinstance(bundle_id, str) or len(bundle_id) < 16:
            raise ExecutionError("Jito did not acknowledge the bundle with a valid bundle ID.")
        status = await self._wait_bundle(bundle_id)
        if status != "Landed":
            raise ExecutionError(
                f"Jito bundle {bundle_id} was not confirmed as landed (status={status}). "
                "Do not retry blindly; inspect the wallet and bundle status first."
            )
        signatures = await self._bundle_signatures(bundle_id)
        if not signatures:
            raise ExecutionError("Jito says the bundle landed but did not return transaction signatures.")
        return SwapResult(signatures[0], amount_raw, output_amount, min_out, bundle_id, "landed")

    async def _confirm_signature(self, signature: str, timeout: int = 60) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            status = await self.helius.signature_status(signature)
            if status:
                if status.get("err"):
                    raise ExecutionError("Swap transaction failed on-chain.")
                if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                    return
            await asyncio.sleep(1)
        raise ExecutionError("Swap submitted but confirmation timed out; do not submit a duplicate blindly.")

    async def _wait_bundle(self, bundle_id: str, timeout: int = 25) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            result = await self._jito_rpc("getInflightBundleStatuses", [[bundle_id]])
            values = (result or {}).get("value") or []
            if values:
                status = str(values[0].get("status", "Pending"))
                if status in {"Landed", "Failed", "Invalid"}:
                    return status
            await asyncio.sleep(0.75)
        result = await self._jito_rpc("getBundleStatuses", [[bundle_id]])
        values = (result or {}).get("value") or []
        if values:
            item = values[0]
            if item.get("err") and item["err"] not in ({"Ok": None}, None):
                return "Failed"
            if item.get("confirmation_status") in {"confirmed", "finalized"} or item.get("confirmationStatus") in {"confirmed", "finalized"}:
                return "Landed"
        return "Pending"

    async def _bundle_signatures(self, bundle_id: str) -> list[str]:
        result = await self._jito_rpc("getBundleStatuses", [[bundle_id]])
        values = (result or {}).get("value") or []
        return list(values[0].get("transactions") or []) if values else []
