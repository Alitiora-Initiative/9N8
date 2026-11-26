from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import aiohttp

from .helius import HeliusClient
from .math_engine import dec


@dataclass(frozen=True)
class TokenRisk:
    passed: bool
    reasons: tuple[str, ...]
    mint_authority_disabled: bool
    freeze_authority_disabled: bool
    top10_concentration_pct: str | None
    liquidity_usd: str | None
    lp_locked_pct: str | None


class TokenSecurity:
    """Fail-closed mint, concentration, liquidity and LP-lock pre-trade screen."""
    TOKEN_PROGRAMS = {
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    }

    def __init__(self, helius: HeliusClient, rugcheck_url: str = "https://api.rugcheck.xyz"):
        self.helius = helius
        self.rugcheck_url = rugcheck_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))

    async def _risk_report(self, mint: str) -> dict[str, Any]:
        await self._start()
        assert self._session
        async with self._session.get(f"{self.rugcheck_url}/v1/tokens/{mint}/report") as response:
            if response.status != 200:
                raise RuntimeError(f"RugCheck HTTP {response.status}")
            payload = await response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("RugCheck returned an invalid report")
        return payload

    @staticmethod
    def _find_values(node: Any, names: set[str]) -> list[Any]:
        found: list[Any] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in names:
                    found.append(value)
                found.extend(TokenSecurity._find_values(value, names))
        elif isinstance(node, list):
            for value in node:
                found.extend(TokenSecurity._find_values(value, names))
        return found

    async def _dex_liquidity(self, mint: str) -> Decimal:
        await self._start()
        assert self._session
        async with self._session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"DexScreener HTTP {response.status}")
            data = await response.json()
        pairs = [
            p for p in (data.get("pairs") or [])
            if p.get("chainId") == "solana"
            and (p.get("liquidity") or {}).get("usd") is not None
        ]
        if not pairs:
            raise RuntimeError("No indexed Solana liquidity pool is available")
        return max(dec(p["liquidity"]["usd"], "pool liquidity") for p in pairs)

    async def inspect(
        self,
        mint: str,
        *,
        launchpad: str,
        max_top10_pct: str = "25",
        min_liquidity_usd: str = "5000",
        min_lp_locked_pct: str = "95",
        excluded_token_accounts: set[str] | None = None,
    ) -> TokenRisk:
        reasons: list[str] = []
        mint_authority_disabled = False
        freeze_authority_disabled = False
        concentration: Decimal | None = None
        liquidity: Decimal | None = None
        lp_locked: Decimal | None = None

        info = await self.helius.account_info(mint, "base64")
        if not info:
            return TokenRisk(False, ("Mint account does not exist",), False, False, None, None, None)
        if str(info.get("owner")) not in self.TOKEN_PROGRAMS:
            reasons.append("Mint is not owned by a recognized SPL Token program")
        data_field = info.get("data")
        if not isinstance(data_field, list) or not data_field:
            return TokenRisk(False, ("Mint account data unavailable",), False, False, None, None, None)
        try:
            raw = base64.b64decode(data_field[0], validate=True)
        except Exception:
            return TokenRisk(False, ("Mint account data cannot be decoded",), False, False, None, None, None)
        if len(raw) < 82:
            return TokenRisk(False, ("Mint account layout is not a valid SPL Mint",), False, False, None, None, None)

        mint_authority_disabled = int.from_bytes(raw[0:4], "little") == 0
        freeze_authority_disabled = int.from_bytes(raw[46:50], "little") == 0
        if not mint_authority_disabled:
            reasons.append("Mint authority is active")
        if not freeze_authority_disabled:
            reasons.append("Freeze authority is active")

        try:
            supply, holders = await asyncio.gather(
                self.helius.token_supply(mint),
                self.helius.largest_accounts(mint),
            )
            supply_raw = int(supply["value"]["amount"])
            excluded = excluded_token_accounts or set()
            ranked = [row for row in holders if str(row.get("address", "")) not in excluded]
            top_raw = sum(int(row["amount"]) for row in ranked[:10])
            concentration = (
                Decimal(top_raw) * Decimal(100) / Decimal(supply_raw)
                if supply_raw else Decimal(100)
            )
            if concentration > dec(max_top10_pct):
                reasons.append(f"Top-10 holder concentration {concentration:.2f}% exceeds configured cap")
        except Exception:
            reasons.append("Holder concentration could not be verified")

        if launchpad.lower() != "pump.fun":
            try:
                liquidity = await self._dex_liquidity(mint)
                if liquidity < dec(min_liquidity_usd):
                    reasons.append(f"Pool liquidity {liquidity:.2f} USD is below configured minimum")
            except Exception:
                reasons.append("Live pool liquidity could not be verified")

        if launchpad.lower() == "raydium":
            try:
                report = await self._risk_report(mint)
                values = self._find_values(report, {"lplockedpct", "lp_locked_pct"})
                if values:
                    lp_locked = max(dec(v, "LP locked percentage") for v in values if v is not None)
                if lp_locked is None:
                    raise RuntimeError("RugCheck report has no recognized LP lock percentage")
                if lp_locked < dec(min_lp_locked_pct):
                    reasons.append(
                        f"Only {lp_locked:.2f}% of LP is reported locked; minimum is {min_lp_locked_pct}%"
                    )
                danger_risks = []
                risks = report.get("risks", [])
                if isinstance(risks, list):
                    for risk in risks:
                        if str(risk.get("level", "")).lower() in {"danger", "critical"}:
                            danger_risks.append(str(risk.get("name") or risk.get("description") or "critical risk"))
                if danger_risks:
                    reasons.append("RugCheck critical risks: " + ", ".join(danger_risks[:3]))
            except Exception:
                reasons.append("Raydium LP lock/burn could not be independently verified")

        return TokenRisk(
            not reasons,
            tuple(reasons),
            mint_authority_disabled,
            freeze_authority_disabled,
            None if concentration is None else str(concentration.quantize(Decimal("0.0001"))),
            None if liquidity is None else str(liquidity.quantize(Decimal("0.01"))),
            None if lp_locked is None else str(lp_locked.quantize(Decimal("0.01"))),
        )
