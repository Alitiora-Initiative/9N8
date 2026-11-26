from __future__ import annotations

import asyncio
import base64
import itertools
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp
from solders.address_lookup_table_account import AddressLookupTableAccount
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
from .math_engine import minimum_out

logger = logging.getLogger(__name__)


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
    tip_signature: str | None = None


class JupiterJitoExecutor:
    """Jupiter Swap API v2 Router builder with optional atomic Jito submission."""
    REGIONAL_ENDPOINTS = (
        "https://amsterdam.mainnet.block-engine.jito.wtf",
        "https://frankfurt.mainnet.block-engine.jito.wtf",
        "https://ny.mainnet.block-engine.jito.wtf",
        "https://tokyo.mainnet.block-engine.jito.wtf",
    )

    def __init__(self, settings: Settings, helius: HeliusClient):
        self.settings, self.helius = settings, helius
        self._session: aiohttp.ClientSession | None = None
        self._ids = itertools.count(1)
        self._tip_accounts: list[str] = []
        self._tip_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _headers(self) -> dict[str, str]:
        result = {"Accept": "application/json"}
        if self.settings.jupiter_api_key:
            result["x-api-key"] = self.settings.jupiter_api_key
        return result

    async def _jupiter_get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        await self.start()
        assert self._session
        try:
            async with self._session.get(
                self.settings.jupiter_base_url + endpoint,
                params=params,
                headers=self._headers(),
            ) as response:
                data = await response.json(content_type=None)
                if response.status >= 400:
                    raise ExecutionError(f"Jupiter HTTP {response.status}: {str(data)[:220]}")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise ExecutionError(f"Jupiter request failed: {type(exc).__name__}") from exc
        if not isinstance(data, dict):
            raise ExecutionError("Jupiter returned an unexpected response.")
        if data.get("error"):
            raise ExecutionError(f"Jupiter route error: {str(data['error'])[:180]}")
        return data

    async def quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int | None = None,
    ) -> dict[str, Any]:
        if amount_raw <= 0:
            raise ValueError("Quote amount must be positive.")
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
        }
        if slippage_bps is not None:
            if not 1 <= int(slippage_bps) <= 5000:
                raise ValueError("Quote slippage must be between 1 and 5000 basis points.")
            params["slippageBps"] = str(int(slippage_bps))
        return await self._jupiter_get("/order", params)

    async def _jito_rpc(
        self, method: str, params: list[Any], base_url: str | None = None
    ) -> Any:
        await self.start()
        assert self._session
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        paths = {
            "sendBundle": "/api/v1/bundles",
            "getInflightBundleStatuses": "/api/v1/getInflightBundleStatuses",
            "getBundleStatuses": "/api/v1/getBundleStatuses",
        }
        if method not in paths:
            raise ExecutionError(f"Unsupported Jito method: {method}")
        url = (base_url or self.settings.jito_base_url) + paths[method]
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

    async def _official_tip_accounts(self) -> list[Pubkey]:
        async with self._tip_lock:
            if not self._tip_accounts:
                await self.start()
                assert self._session
                url = self.settings.jito_base_url + "/api/v1/getTipAccounts"
                async with self._session.post(
                    url, json={"jsonrpc": "2.0", "id": 1, "method": "getTipAccounts", "params": []}
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status >= 400 or data.get("error"):
                        raise ExecutionError("Could not retrieve Jito tip accounts.")
                self._tip_accounts = [str(x) for x in (data.get("result") or [])]
                if not self._tip_accounts:
                    raise ExecutionError("Jito returned no tip accounts.")
            return [Pubkey.from_string(x) for x in self._tip_accounts]

    async def _instructions(self, items: list[dict[str, Any]]) -> list[Instruction]:
        result: list[Instruction] = []
        for item in items:
            try:
                program = Pubkey.from_string(item["programId"])
                data = base64.b64decode(item["data"], validate=True)
                accounts = [
                    AccountMeta(
                        Pubkey.from_string(x["pubkey"]),
                        bool(x.get("isSigner", False)),
                        bool(x.get("isWritable", False)),
                    )
                    for x in item.get("accounts", [])
                ]
            except Exception as exc:
                raise ExecutionError("Jupiter returned an invalid instruction; refusing to sign.") from exc
            result.append(Instruction(program, data, accounts))
        return result

    async def _lookup_tables(
        self, tables_by_address: dict[str, list[str]] | None
    ) -> list[AddressLookupTableAccount]:
        # Jupiter v2 /build returns the loaded lookup-table address lists. Use
        # that response directly rather than fetching/deserializing a different
        # on-chain snapshot after the quote was assembled.
        tables: list[AddressLookupTableAccount] = []
        for address, addresses in (tables_by_address or {}).items():
            try:
                tables.append(AddressLookupTableAccount(
                    Pubkey.from_string(address),
                    [Pubkey.from_string(item) for item in addresses],
                ))
            except Exception as exc:
                raise ExecutionError(f"Jupiter returned an invalid lookup table {address}.") from exc
        return tables

    async def _submit_bundle(self, signed_transactions: list[str]) -> str:
        endpoints = [self.settings.jito_base_url, *self.REGIONAL_ENDPOINTS]
        last_error: Exception | None = None
        for endpoint in dict.fromkeys(endpoints):
            try:
                result = await self._jito_rpc(
                    "sendBundle", [signed_transactions, {"encoding": "base64"}], endpoint
                )
                if isinstance(result, str) and len(result) >= 16:
                    return result
                raise ExecutionError("Jito returned an invalid bundle identifier.")
            except (ExecutionError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                logger.warning("Jito submission failed at %s: %s", endpoint, type(exc).__name__)
        raise ExecutionError(
            f"All Jito regions failed ({type(last_error).__name__ if last_error else 'unknown'})."
        )

    async def build_swap(
        self,
        wallet: Keypair,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int,
        priority_fee_micro_lamports: int,
        jito_tip_lamports: int = 0,
        treasury_fee_lamports: int = 0,
        use_jito: bool = True,
    ) -> SwapResult:
        if amount_raw <= 0:
            raise ValueError("Swap amount must be positive.")
        if not 0 <= slippage_bps <= 5000:
            raise ValueError("Slippage must be between 0 and 50%.")
        if priority_fee_micro_lamports < 0 or jito_tip_lamports < 0 or treasury_fee_lamports < 0:
            raise ValueError("Fee amounts cannot be negative.")
        if self.settings.trading_mode == "live" and not self.settings.live_trading_enabled:
            raise ExecutionError("Live trading is locked in the environment.")
        if use_jito and jito_tip_lamports <= 0:
            raise ExecutionError("MEV shield requires a positive Jito tip.")
        quote = await self._jupiter_get("/build", {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "taker": str(wallet.pubkey()),
            "slippageBps": str(slippage_bps),
            "forJitoBundle": "true" if use_jito else "false",
        })
        expected_out = int(quote.get("outAmount") or 0)
        if expected_out <= 0:
            raise ExecutionError("Jupiter returned no positive output quote.")
        min_out = int(quote.get("otherAmountThreshold") or minimum_out(expected_out, slippage_bps))
        if min_out <= 0:
            raise ExecutionError("Jupiter minimum output is zero; refusing trade.")

        blockhash = Hash.from_string(await self.helius.recent_blockhash())
        # The configured priority fee is expressed in micro-lamports per CU.
        compute = [
            set_compute_unit_limit(1_000_000),
            set_compute_unit_price(priority_fee_micro_lamports),
        ]
        setup = await self._instructions(quote.get("setupInstructions") or [])
        other = await self._instructions(quote.get("otherInstructions") or [])
        swap_item = quote.get("swapInstruction")
        if not swap_item:
            raise ExecutionError("Jupiter did not return a swap instruction.")
        swap = await self._instructions([swap_item])
        cleanup_item = quote.get("cleanupInstruction")
        cleanup = await self._instructions([cleanup_item]) if cleanup_item else []
        instructions = compute + setup + swap
        if treasury_fee_lamports:
            instructions.append(transfer(TransferParams(
                from_pubkey=wallet.pubkey(),
                to_pubkey=Pubkey.from_string(self.settings.treasury),
                lamports=treasury_fee_lamports,
            )))
        instructions += cleanup + other
        alts = await self._lookup_tables(quote.get("addressesByLookupTableAddress") or {})
        try:
            message = MessageV0.try_compile(wallet.pubkey(), instructions, alts, blockhash)
            swap_tx = VersionedTransaction(message, [wallet])
        except Exception as exc:
            raise ExecutionError(f"Transaction build/signing failed: {type(exc).__name__}") from exc
        if len(bytes(swap_tx)) > 1232:
            raise ExecutionError("Swap transaction exceeds Solana's 1232-byte packet limit.")

        if not use_jito:
            signature = await self.helius.rpc("sendTransaction", [
                base64.b64encode(bytes(swap_tx)).decode("ascii"),
                {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed", "maxRetries": 3},
            ])
            await self._confirm_signature(str(signature))
            return SwapResult(str(signature), amount_raw, expected_out, min_out, None, "confirmed", None)

        tip_accounts = await self._official_tip_accounts()
        # Select from the official list using a cryptographically secure index.
        import secrets
        tip_destination = tip_accounts[secrets.randbelow(len(tip_accounts))]
        tip_ix = transfer(TransferParams(
            from_pubkey=wallet.pubkey(),
            to_pubkey=tip_destination,
            lamports=jito_tip_lamports,
        ))
        tip_tx = Transaction.new_signed_with_payer(
            [tip_ix], wallet.pubkey(), [wallet], blockhash,
        )
        if len(bytes(tip_tx)) > 1232:
            raise ExecutionError("Jito tip transaction exceeds Solana's packet limit.")
        signed = [
            base64.b64encode(bytes(swap_tx)).decode("ascii"),
            base64.b64encode(bytes(tip_tx)).decode("ascii"),
        ]
        bundle_id = await self._submit_bundle(signed)
        status = await self._wait_bundle(bundle_id)
        if status != "Landed":
            raise ExecutionError(
                f"Jito bundle was not confirmed as landed (status={status}, bundle={bundle_id}). "
                "Do not submit a duplicate without checking wallet and bundle history."
            )
        signatures = await self._bundle_signatures(bundle_id)
        if len(signatures) < 2:
            raise ExecutionError("Jito reports a landed bundle but did not return both swap and tip signatures.")
        return SwapResult(
            signatures[0], amount_raw, expected_out, min_out, bundle_id, "landed", signatures[1]
        )

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
        raise ExecutionError("Swap submitted but confirmation timed out; inspect before retrying.")

    async def _wait_bundle(self, bundle_id: str, timeout: int = 25) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            result = await self._jito_rpc("getInflightBundleStatuses", [[bundle_id]])
            values = (result or {}).get("value") or []
            if values:
                status = str(values[0].get("status", "Pending"))
                if status in {"Landed", "Failed", "Invalid"}:
                    return status
            await asyncio.sleep(1.05)
        result = await self._jito_rpc("getBundleStatuses", [[bundle_id]])
        values = (result or {}).get("value") or []
        if values:
            item = values[0]
            err = item.get("err")
            if err and err not in ({"Ok": None}, None):
                return "Failed"
            state = item.get("confirmation_status") or item.get("confirmationStatus")
            if state in {"confirmed", "finalized"}:
                return "Landed"
        return "Pending"

    async def _bundle_signatures(self, bundle_id: str) -> list[str]:
        result = await self._jito_rpc("getBundleStatuses", [[bundle_id]])
        values = (result or {}).get("value") or []
        return list(values[0].get("transactions") or []) if values else []
