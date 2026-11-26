from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable

from base58 import b58decode
from solders.pubkey import Pubkey

from .helius import (
    HeliusClient, PUMP_FUN_PROGRAM, RAYDIUM_V4_PROGRAM,
    RAYDIUM_CPMM_PROGRAM, PUMPSWAP_PROGRAM,
)

logger = logging.getLogger(__name__)
PUMP_CREATE_DISCRIMINATOR = bytes([24, 30, 200, 40, 5, 28, 7, 119])
PUMP_INITIAL_REAL_TOKEN_RESERVES = 793_100_000_000_000


@dataclass(frozen=True)
class BondingCurveState:
    address: str
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete: bool
    progress_pct: Decimal


@dataclass(frozen=True)
class LaunchCandidate:
    mint: str
    launchpad: str
    signature: str
    slot: int | None
    created_at_slot: int | None
    developer_buy_sol: str | None
    source_logs: tuple[str, ...]
    bonding_curve: BondingCurveState | None = None


class LaunchpadSniper:
    """Parse actual Pump.fun creates and Raydium initializations from chain logs."""
    def __init__(self, helius: HeliusClient):
        self.helius = helius
        self._seen: set[str] = set()
        self._stop = asyncio.Event()
        self._curve_tasks: dict[str, asyncio.Task] = {}

    async def stop(self) -> None:
        self._stop.set()
        for task in self._curve_tasks.values():
            task.cancel()
        if self._curve_tasks:
            await asyncio.gather(*self._curve_tasks.values(), return_exceptions=True)
        self._curve_tasks.clear()

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
                candidate = self._parse_transaction(
                    signature, event.get("slot"), event.get("logs", []), tx
                )
                if candidate and candidate.launchpad == "pump.fun":
                    curve = await self.read_bonding_curve(candidate.mint)
                    candidate = LaunchCandidate(
                        candidate.mint, candidate.launchpad, candidate.signature,
                        candidate.slot, candidate.created_at_slot,
                        candidate.developer_buy_sol, candidate.source_logs, curve,
                    )
                if candidate:
                    await callback(candidate)
                    if candidate.launchpad == "pump.fun" and candidate.bonding_curve and not candidate.bonding_curve.complete:
                        old = self._curve_tasks.get(candidate.mint)
                        if old is None or old.done():
                            self._curve_tasks[candidate.mint] = asyncio.create_task(
                                self._watch_curve(candidate, callback),
                                name=f"9n8-curve-{candidate.mint[:8]}",
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Launch transaction %s ignored: %s", signature, type(exc).__name__)

    async def read_bonding_curve(self, mint: str) -> BondingCurveState | None:
        """Read Pump.fun's bonding-curve PDA using its published account layout."""
        mint_pk = Pubkey.from_string(mint)
        program_pk = Pubkey.from_string(PUMP_FUN_PROGRAM)
        pda, _bump = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint_pk)], program_pk
        )
        info = await self.helius.account_info(str(pda), "base64")
        if not info:
            return None
        raw = base64.b64decode(info["data"][0], validate=True)
        # Anchor discriminator (8) then five u64 fields and a boolean.
        if len(raw) < 49:
            return None
        virtual_tokens = int.from_bytes(raw[8:16], "little")
        virtual_sol = int.from_bytes(raw[16:24], "little")
        real_tokens = int.from_bytes(raw[24:32], "little")
        real_sol = int.from_bytes(raw[32:40], "little")
        total_supply = int.from_bytes(raw[40:48], "little")
        complete = bool(raw[48])
        initial_real = PUMP_INITIAL_REAL_TOKEN_RESERVES
        if initial_real <= 0:
            return None
        progress = (
            Decimal(initial_real - real_tokens) * Decimal(100) / Decimal(initial_real)
        )
        progress = max(Decimal(0), min(Decimal(100), progress))
        return BondingCurveState(
            str(pda), virtual_tokens, virtual_sol, real_tokens, real_sol,
            total_supply, complete, progress,
        )


    async def _watch_curve(
        self,
        candidate: LaunchCandidate,
        callback: Callable[[LaunchCandidate], Awaitable[None]],
    ) -> None:
        """Follow the bonding-curve account until migration or five-minute expiry."""
        try:
            if not candidate.bonding_curve:
                return
            # Avoid leaving an account subscription alive forever if a curve is
            # abandoned or the token never migrates.
            async with asyncio.timeout(600):
                async for account in self.helius.account_changes(candidate.bonding_curve.address):
                    if self._stop.is_set():
                        return
                    data = account.get("data")
                    if not isinstance(data, list) or not data:
                        continue
                    try:
                        raw = base64.b64decode(data[0], validate=True)
                        state = self._curve_from_raw(candidate.bonding_curve.address, raw)
                    except Exception:
                        continue
                    updated = LaunchCandidate(
                        candidate.mint, candidate.launchpad, candidate.signature,
                        candidate.slot, candidate.created_at_slot,
                        candidate.developer_buy_sol, candidate.source_logs, state,
                    )
                    await callback(updated)
                    if state.complete:
                        return
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.info("Bonding curve watcher expired for %s", candidate.mint)
        except Exception as exc:
            logger.debug("Bonding curve watcher %s ended: %s", candidate.mint, type(exc).__name__)
        finally:
            self._curve_tasks.pop(candidate.mint, None)

    @staticmethod
    def _curve_from_raw(address: str, raw: bytes) -> BondingCurveState:
        if len(raw) < 49:
            raise ValueError("Bonding curve account is shorter than its base layout.")
        virtual_tokens = int.from_bytes(raw[8:16], "little")
        virtual_sol = int.from_bytes(raw[16:24], "little")
        real_tokens = int.from_bytes(raw[24:32], "little")
        real_sol = int.from_bytes(raw[32:40], "little")
        total_supply = int.from_bytes(raw[40:48], "little")
        complete = bool(raw[48])
        progress = Decimal(PUMP_INITIAL_REAL_TOKEN_RESERVES - real_tokens) * Decimal(100) / Decimal(PUMP_INITIAL_REAL_TOKEN_RESERVES)
        progress = max(Decimal(0), min(Decimal(100), progress))
        return BondingCurveState(
            address, virtual_tokens, virtual_sol, real_tokens,
            real_sol, total_supply, complete, progress,
        )

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
                raw_data = b""
                if ix.get("data"):
                    try:
                        raw_data = b58decode(ix["data"])
                    except Exception:
                        pass
                is_create = (
                    parsed_type == "create"
                    or raw_data[:8] == PUMP_CREATE_DISCRIMINATOR
                    or any("Instruction: Create" in line for line in logs)
                )
                if not is_create:
                    continue
                accounts = ix.get("accounts") or []
                if accounts and isinstance(accounts[0], dict):
                    accounts = [row.get("pubkey") for row in accounts]
                if accounts:
                    try:
                        mint = str(Pubkey.from_string(str(accounts[0])))
                        return LaunchCandidate(
                            mint, "pump.fun", signature, slot, slot, None, tuple(logs[:20])
                        )
                    except Exception:
                        continue
            if program in {RAYDIUM_V4_PROGRAM, RAYDIUM_CPMM_PROGRAM, PUMPSWAP_PROGRAM}:
                parsed = ix.get("parsed") or {}
                instruction_type = str(parsed.get("type", "")).lower()
                hit = instruction_type in {
                    "initialize", "initialize2", "initialize_pool", "initialize_pool_v2"
                } or any(
                    "initialize2" in line.lower() or "initialize_pool" in line.lower()
                    for line in logs
                )
                if hit:
                    info = parsed.get("info") or {}
                    mint = info.get("baseMint") or info.get("tokenMint") or info.get("mint")
                    if mint:
                        try:
                            return LaunchCandidate(
                                str(Pubkey.from_string(str(mint))),
                                "raydium", signature, slot, slot, None, tuple(logs[:20]),
                            )
                        except Exception:
                            continue
        return None
