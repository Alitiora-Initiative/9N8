from __future__ import annotations

import asyncio
import base64
import logging
from collections import OrderedDict
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
MAX_SEEN_SIGNATURES = 20_000
MAX_EVENT_WORKERS = 8
MAX_QUEUED_EVENTS = 512
TRANSACTION_RETRY_DELAYS = (0.0, 0.25, 0.5, 1.0)


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
        # Preserve recent deduplication history without periodically forgetting
        # every processed signature at once. In-flight signatures are tracked
        # separately so a failed lookup can be retried after a stream replay.
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._in_flight: set[str] = set()
        self._stop = asyncio.Event()
        self._curve_tasks: dict[str, asyncio.Task] = {}

    def _remember_signature(self, signature: str) -> None:
        self._seen[signature] = None
        self._seen.move_to_end(signature)
        while len(self._seen) > MAX_SEEN_SIGNATURES:
            self._seen.popitem(last=False)

    async def stop(self) -> None:
        self._stop.set()
        for task in self._curve_tasks.values():
            task.cancel()
        if self._curve_tasks:
            await asyncio.gather(*self._curve_tasks.values(), return_exceptions=True)
        self._curve_tasks.clear()

    async def run(self, callback: Callable[[LaunchCandidate], Awaitable[None]]) -> None:
        """Consume launch events while parsing transactions with bounded concurrency.

        A bounded queue prevents a burst of signatures from creating unbounded
        tasks. Transaction lookups are retried briefly because a processed log
        can arrive before the RPC node exposes the corresponding transaction.
        """
        self._stop.clear()
        programs = [PUMP_FUN_PROGRAM, RAYDIUM_V4_PROGRAM, RAYDIUM_CPMM_PROGRAM, PUMPSWAP_PROGRAM]
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=MAX_QUEUED_EVENTS)

        async def process_worker() -> None:
            while True:
                event = await queue.get()
                signature = str(event.get("signature") or "")
                try:
                    completed = await self._process_event(event, callback)
                    if completed and signature:
                        self._remember_signature(signature)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug(
                        "Launch transaction %s deferred after processing error: %s",
                        signature[:16],
                        type(exc).__name__,
                    )
                finally:
                    if signature:
                        self._in_flight.discard(signature)
                    queue.task_done()

        workers = [
            asyncio.create_task(process_worker(), name=f"9n8-launch-worker-{index}")
            for index in range(MAX_EVENT_WORKERS)
        ]
        try:
            async for event in self.helius.logs(programs):
                if self._stop.is_set():
                    break
                signature = str(event.get("signature") or "")
                if (
                    not signature
                    or event.get("err") is not None
                    or signature in self._seen
                    or signature in self._in_flight
                ):
                    continue
                self._in_flight.add(signature)
                try:
                    await queue.put(event)
                except BaseException:
                    self._in_flight.discard(signature)
                    raise
            # Finite streams (e.g. tests or an upstream shutdown) drain their
            # accepted work before this scanner returns. Cancellation skips join.
            await queue.join()
        finally:
            for task in workers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            self._in_flight.clear()

    async def _process_event(
        self,
        event: dict,
        callback: Callable[[LaunchCandidate], Awaitable[None]],
    ) -> bool:
        signature = str(event.get("signature") or "")
        tx: dict | None = None
        for delay in TRANSACTION_RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                tx = await self.helius.transaction(signature)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(
                    "Helius transaction lookup %s retrying after %s",
                    signature[:16],
                    type(exc).__name__,
                )
                tx = None
            if tx is not None:
                break
        if tx is None:
            # Do not mark the signature as seen. Helius replay/reconnect or a
            # subsequent notification can make it available for another attempt.
            return False

        candidate = self._parse_transaction(
            signature, event.get("slot"), event.get("logs", []), tx
        )
        if candidate and candidate.launchpad == "pump.fun":
            try:
                curve = await self.read_bonding_curve(candidate.mint)
            except Exception as exc:
                logger.debug(
                    "Pump.fun curve lookup %s unavailable: %s",
                    candidate.mint[:12],
                    type(exc).__name__,
                )
                curve = None
            candidate = LaunchCandidate(
                candidate.mint, candidate.launchpad, candidate.signature,
                candidate.slot, candidate.created_at_slot,
                candidate.developer_buy_sol, candidate.source_logs, curve,
            )
        if candidate:
            await callback(candidate)
            if (
                candidate.launchpad == "pump.fun"
                and candidate.bonding_curve
                and not candidate.bonding_curve.complete
            ):
                old = self._curve_tasks.get(candidate.mint)
                if old is None or old.done():
                    self._curve_tasks[candidate.mint] = asyncio.create_task(
                        self._watch_curve(candidate, callback),
                        name=f"9n8-curve-{candidate.mint[:8]}",
                    )
        return True

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
