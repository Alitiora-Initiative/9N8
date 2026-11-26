from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from .helius import HeliusClient


@dataclass(frozen=True)
class TokenRisk:
    passed: bool
    reasons: tuple[str, ...]
    mint_authority_disabled: bool
    freeze_authority_disabled: bool
    top10_concentration_pct: str | None


class TokenSecurity:
    """Fail-closed checks for Mint authorities and holder concentration.

    Pool/LP lock verification is not inferred from a social API score. It remains
    a mandatory operator-configurable gate until the relevant pool and lock
    accounts are positively identified and parsed for that launchpad.
    """
    def __init__(self, helius: HeliusClient, max_top10_pct: str = "25"):
        self.helius = helius
        self.max_top10_pct = max_top10_pct

    async def inspect(self, mint: str) -> TokenRisk:
        reasons: list[str] = []
        info = await self.helius.account_info(mint, "base64")
        if not info:
            return TokenRisk(False, ("Mint account not found",), False, False, None)
        owner = info.get("owner", "")
        raw = info.get("data")
        if not isinstance(raw, list) or len(raw) < 1:
            return TokenRisk(False, ("Mint account data unavailable",), False, False, None)
        try:
            data = base64.b64decode(raw[0], validate=True)
        except Exception:
            return TokenRisk(False, ("Mint account data could not be decoded",), False, False, None)
        # SPL Mint base layout: COption<Pubkey> at offset 0, freeze authority at 46.
        if len(data) < 82:
            return TokenRisk(False, ("Mint layout is not a standard SPL mint",), False, False, None)
        mint_authority_disabled = int.from_bytes(data[0:4], "little") == 0
        freeze_authority_disabled = int.from_bytes(data[46:50], "little") == 0
        if not mint_authority_disabled:
            reasons.append("Mint authority is still enabled")
        if not freeze_authority_disabled:
            reasons.append("Freeze authority is still enabled")
        try:
            supply_result, holders = await __import__("asyncio").gather(
                self.helius.token_supply(mint),
                self.helius.largest_accounts(mint),
            )
            supply_raw = int(supply_result["value"]["amount"])
            top_raw = sum(int(h["amount"]) for h in holders[:10])
            concentration = (top_raw * 100 / supply_raw) if supply_raw else 100.0
        except Exception:
            concentration = None
            reasons.append("Holder concentration data unavailable")
        if concentration is not None and concentration > float(self.max_top10_pct):
            reasons.append(f"Top-10 holder concentration {concentration:.2f}% exceeds limit")
        if owner not in {
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        }:
            reasons.append("Mint is not owned by a recognized SPL Token program")
        return TokenRisk(
            not reasons,
            tuple(reasons),
            mint_authority_disabled,
            freeze_authority_disabled,
            None if concentration is None else f"{concentration:.4f}",
        )
