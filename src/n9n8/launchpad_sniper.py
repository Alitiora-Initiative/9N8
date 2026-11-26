from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from base58 import b58decode
from solders.pubkey import Pubkey

from .helius import (
    HeliusClient, PUMP_FUN_PROGRAM, RAYDIUM_V4_PROGRAM,
    RAYDIUM_CPMM_PROGRAM, PUMPSWAP_PROGRAM,
)

logger = logging.getLogger(__name__)
PUMP_CREATE_DISCRIMINATOR = bytes([24, 30, 200, 40, 5, 28, 7, 119])
PUMP_CREATE_V2_DISCRIMINATOR = bytes([214, 144, 76, 236, 95, 139, 49, 180])


@dataclass(frozen=True)
class LaunchCandidate:
    mint: str
    launchpad: str
    signature: str
    slot: int | None
    created_at_slot: int | None
    developer_buy_sol: str | None
    source_logs: tuple[str, ...]


class LaunchpadSniper:
    """Low-latency launch detection; all buy decisions live in the risk engine.

    A log notification is only a candidate. The transaction is fetched and its
    program instruction data is inspected before emitting a launch event.
    """
    def __init__(self, helius: HeliusClient):
        self.helius = helius
        self._seen: set[str] = set()
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    async def run(self, callback: Callable[[LaunchCandidate], Awaitable[None]]) -> None:
        self._stop.clear()
        programs = [PUMP_FUN_PROGRAM, RAYDIUM_V4_PROGRAM, RAYDIUM_CPMM_PROGRAM, PUMPSWAP_PROGRAM]
        async for event in self.helius.logs(programs):
            if self._stop.is_set():
                break
            signature = event.get("signature")
            if not signature or event.get("err") is not None or signature in self._seen:
                continue
            self._seen.add(signature)
            if len(self._seen) > 20000:
                self._seen.clear()
            try:
                tx = await self.helius.transaction(signature)
                if not tx:
                    continue
                candidate = self._parse_transaction(signature, event.get("slot"), event.get("logs", []), tx)
                if candidate:
                    await callback(candidate)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Launch transaction %s ignored: %s", signature, type(exc).__name__)

    def _parse_transaction(
        self, signature: str, slot: int | None, logs: list[str], tx: dict
    ) -> LaunchCandidate | None:
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            return None
        message = ((tx.get("transaction") or {}).get("message") or {})
        instructions = list(message.get("instructions") or [])
        for group in meta.get("innerInstructions") or []:
            instructions.extend(group.get("instructions") or [])
        for ix in instructions:
            program = ix.get("programId")
            if isinstance(program, dict):
                program = program.get("pubkey")
            program = str(program or "")
            if program == PUMP_FUN_PROGRAM:
                parsed = ix.get("parsed") or {}
                parsed_type = str(parsed.get("type", "")).lower()
                raw = ix.get("data")
                discriminator = b""
                if raw:
                    try:
                        discriminator = b58decode(raw)[:8]
                    except Exception:
                        pass
                is_create = (
                    parsed_type in {"create", "create_v2"}
                    or discriminator in {PUMP_CREATE_DISCRIMINATOR, PUMP_CREATE_V2_DISCRIMINATOR}
                    or any("Instruction: Create" in line for line in logs)
                )
                if not is_create:
                    continue
                accounts = ix.get("accounts") or []
                if accounts and isinstance(accounts[0], dict):
                    accounts = [x.get("pubkey") for x in accounts]
                if accounts:
                    try:
                        mint = str(Pubkey.from_string(str(accounts[0])))
                        return LaunchCandidate(mint, "pump.fun", signature, slot, slot, None, tuple(logs[:15]))
                    except Exception:
                        continue
            if program in {RAYDIUM_V4_PROGRAM, RAYDIUM_CPMM_PROGRAM, PUMPSWAP_PROGRAM}:
                parsed = ix.get("parsed") or {}
                kind = str(parsed.get("type", "")).lower()
                data = ix.get("data")
                raw_data = b""
                if data:
                    try:
                        raw_data = b58decode(data)
                    except Exception:
                        pass
                label_hit = kind in {"initialize", "initialize2", "initialize_pool", "initialize_pool_v2"}
                log_hit = any("initialize2" in line.lower() or "initialize_pool" in line.lower() for line in logs)
                if label_hit or log_hit:
                    # The V4/CPMM instruction has many account layouts and both
                    # token mints must be identified. Never guess the mint index.
                    # Use parsed account objects only when the RPC parser labels
                    # mint accounts explicitly; otherwise reject as unclassified.
                    info = parsed.get("info") or {}
                    mint = info.get("baseMint") or info.get("tokenMint") or info.get("mint")
                    if mint:
                        try:
                            return LaunchCandidate(str(Pubkey.from_string(str(mint))), "raydium", signature, slot, slot, None, tuple(logs[:15]))
                        except Exception:
                            pass
        return None
