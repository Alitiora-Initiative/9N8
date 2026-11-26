from __future__ import annotations

import asyncio
import base64
from collections import defaultdict
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
    developer_concentration_pct: str | None = None
    token_program: str | None = None
    extensions_checked: bool = False


class TokenSecurity:
    """Fail-closed mint, Token-2022 extension, holder, creator, liquidity and LP checks."""

    TOKEN_PROGRAMS = {
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    }
    TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    SAFE_TOKEN_2022_EXTENSIONS = {
        "metadatapointer", "tokenmetadata", "immutableowner",
    }
    UNSAFE_TOKEN_2022_EXTENSIONS = {
        "permanentdelegate", "transferhook", "defaultaccountstate",
        "pausable", "nontransferable", "transferfeeconfig",
        "confidentialtransfermint", "confidentialtransferfeeconfig",
        "mintcloseauthority",
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
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=7))

    async def _risk_report(self, mint: str) -> dict[str, Any]:
        await self._start()
        assert self._session
        async with self._session.get(f"{self.rugcheck_url}/v1/tokens/{mint}/report") as response:
            if response.status != 200:
                raise RuntimeError(f"RugCheck HTTP {response.status}")
            payload = await response.json()
        if not isinstance(payload, dict) or str(payload.get("mint") or mint) != mint:
            raise RuntimeError("RugCheck returned an invalid or mismatched report")
        return payload

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

    @staticmethod
    def _top_holder_shares(report: dict[str, Any]) -> dict[str, Decimal]:
        rows = report.get("topHolders")
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("RugCheck did not return top holder wallet data")
        shares: dict[str, Decimal] = defaultdict(Decimal)
        for row in rows:
            if not isinstance(row, dict):
                continue
            owner = str(row.get("owner") or "").strip()
            pct = row.get("pct")
            if not owner or pct is None:
                continue
            value = dec(pct, "RugCheck holder percentage")
            if value < 0 or value > 100:
                raise RuntimeError("RugCheck returned an invalid holder percentage")
            shares[owner] += value
        if not shares:
            raise RuntimeError("RugCheck top holder wallet owners could not be verified")
        return shares

    @classmethod
    def _token_2022_extensions(cls, report: dict[str, Any]) -> tuple[bool, list[str]]:
        extension_data = report.get("token_extensions", report.get("tokenExtensions"))
        if extension_data is None:
            return False, []
        names: set[str] = set()
        if isinstance(extension_data, list):
            for item in extension_data:
                if isinstance(item, str):
                    names.add(item.replace("_", "").replace("-", "").lower())
                elif isinstance(item, dict):
                    for key in ("type", "name", "extension"):
                        value = item.get(key)
                        if isinstance(value, str) and value:
                            names.add(value.replace("_", "").replace("-", "").lower())
        elif isinstance(extension_data, dict):
            for key, value in extension_data.items():
                normalized = str(key).replace("_", "").replace("-", "").lower()
                if value not in (None, False, "", [], {}):
                    names.add(normalized)
                if isinstance(value, str):
                    names.add(value.replace("_", "").replace("-", "").lower())
        else:
            raise RuntimeError("RugCheck Token-2022 extension response has an unknown format")
        unsafe = sorted(name for name in names if name in cls.UNSAFE_TOKEN_2022_EXTENSIONS)
        unknown = sorted(
            name for name in names
            if name not in cls.SAFE_TOKEN_2022_EXTENSIONS
            and name not in cls.UNSAFE_TOKEN_2022_EXTENSIONS
        )
        return True, unsafe + unknown

    async def inspect(
        self,
        mint: str,
        *,
        launchpad: str,
        max_top10_pct: str = "25",
        min_liquidity_usd: str = "0",
        min_lp_locked_pct: str = "80",
        max_dev_pct: str = "5",
        require_mint_authority_revoked: bool = True,
        require_freeze_authority_revoked: bool = True,
        excluded_token_accounts: set[str] | None = None,
    ) -> TokenRisk:
        reasons: list[str] = []
        mint_authority_disabled = False
        freeze_authority_disabled = False
        concentration: Decimal | None = None
        developer_concentration: Decimal | None = None
        liquidity: Decimal | None = None
        lp_locked: Decimal | None = None
        token_program: str | None = None
        extensions_checked = False

        info = await self.helius.account_info(mint, "base64")
        if not info:
            return TokenRisk(False, ("Mint account does not exist",), False, False, None, None, None)
        token_program = str(info.get("owner") or "")
        if token_program not in self.TOKEN_PROGRAMS:
            reasons.append("Mint is not owned by a recognized SPL Token program")
        data_field = info.get("data")
        if not isinstance(data_field, list) or not data_field:
            return TokenRisk(False, ("Mint account data unavailable",), False, False, None, None, None)
        try:
            raw = base64.b64decode(data_field[0], validate=True)
        except Exception:
            return TokenRisk(False, ("Mint account data cannot be decoded",), False, False, None, None, None)
        if len(raw) < 82:
            return TokenRisk(False, ("Mint account layout is not a valid SPL Mint"), False, False, None, None, None)

        mint_authority_disabled = int.from_bytes(raw[0:4], "little") == 0
        freeze_authority_disabled = int.from_bytes(raw[46:50], "little") == 0
        if require_mint_authority_revoked and not mint_authority_disabled:
            reasons.append("Mint authority is active")
        if require_freeze_authority_revoked and not freeze_authority_disabled:
            reasons.append("Freeze authority is active")

        try:
            report = await self._risk_report(mint)
        except Exception:
            report = {}
            reasons.append("RugCheck token, holder and creator report could not be verified")

        # Use holder owner wallets, not token-account addresses, so multiple ATAs
        # belonging to one wallet are combined before applying concentration limits.
        holder_shares: dict[str, Decimal] = {}
        if report:
            try:
                holder_shares = self._top_holder_shares(report)
                largest = sorted(holder_shares.values(), reverse=True)
                concentration = sum(largest[:10], Decimal(0))
                if concentration > dec(max_top10_pct):
                    reasons.append(
                        f"Top-10 wallet concentration {concentration:.2f}% exceeds {max_top10_pct}%"
                    )
            except Exception:
                reasons.append("Top-10 holder wallet concentration could not be verified")

            creator = report.get("creator") or report.get("creatorAddress") or report.get("creator_address")
            creator_address = ""
            if isinstance(creator, dict):
                creator_address = str(creator.get("address") or creator.get("owner") or "")
            elif isinstance(creator, str):
                creator_address = creator
            if creator_address and creator_address in holder_shares:
                developer_concentration = holder_shares[creator_address]
            else:
                creator_balance = report.get("creatorBalance")
                token_info = report.get("token") or {}
                supply_raw = token_info.get("supply") if isinstance(token_info, dict) else None
                try:
                    if creator_balance is not None and supply_raw is not None and int(supply_raw) > 0:
                        developer_concentration = (
                            Decimal(str(creator_balance)) * Decimal(100) / Decimal(str(supply_raw))
                        )
                except Exception:
                    developer_concentration = None
            if developer_concentration is None:
                reasons.append("Creator/developer wallet concentration could not be verified")
            elif developer_concentration > dec(max_dev_pct):
                reasons.append(
                    f"Creator/developer wallet holds {developer_concentration:.2f}%; limit is {max_dev_pct}%"
                )

        if token_program == self.TOKEN_2022_PROGRAM:
            # Token-2022 has optional mint extensions that can alter transfers,
            # freeze new accounts or delegate control. Do not accept extensions
            # merely because the legacy SPL mint authority is revoked.
            has_extension_bytes = len(raw) > 165
            try:
                extensions_checked, extensions = self._token_2022_extensions(report)
                if has_extension_bytes and not extensions_checked:
                    raise RuntimeError("Mint has extension bytes but report does not describe them")
                if extensions:
                    reasons.append("Token-2022 extensions are not accepted: " + ", ".join(extensions[:5]))
                if has_extension_bytes and not extensions:
                    reasons.append("Token-2022 extension bytes were found but no recognized extension list was returned")
            except Exception:
                reasons.append("Token-2022 extensions could not be safely decoded")
        else:
            extensions_checked = True

        try:
            liquidity = await self._dex_liquidity(mint)
            if liquidity < dec(min_liquidity_usd):
                reasons.append(
                    f"Pool liquidity {liquidity:.2f} USD is below the configured minimum {min_liquidity_usd} USD"
                )
        except Exception:
            reasons.append("Live Solana pool liquidity could not be verified")

        if not report:
            # Holder and LP requirements must not silently disappear when the
            # third-party risk report is unavailable.
            reasons.append("Independent creator and liquidity-lock report is required")
        else:
            lp_values = self._find_values(
                report, {"lplockedpct", "lp_locked_pct", "lpburnedpct", "lp_burned_pct"}
            )
            try:
                parsed = [dec(value, "LP lock/burn percentage") for value in lp_values if value is not None]
                if not parsed:
                    raise RuntimeError("RugCheck report has no LP lock/burn percentage")
                # Lock and burn can overlap; take the strongest single reported
                # percentage rather than adding overlapping liquidity shares.
                lp_locked = max(parsed)
                if lp_locked < dec(min_lp_locked_pct):
                    reasons.append(
                        f"Only {lp_locked:.2f}% of LP is reported locked or burned; minimum is {min_lp_locked_pct}%"
                    )
            except Exception:
                reasons.append("LP lock/burn percentage could not be verified")

            danger_risks = []
            risks = report.get("risks", [])
            if isinstance(risks, list):
                for risk in risks:
                    if isinstance(risk, dict) and str(risk.get("level", "")).lower() in {"danger", "critical"}:
                        danger_risks.append(str(risk.get("name") or risk.get("description") or "critical risk"))
            if danger_risks:
                reasons.append("RugCheck critical risks: " + ", ".join(danger_risks[:5]))

        return TokenRisk(
            not reasons,
            tuple(dict.fromkeys(reasons)),
            mint_authority_disabled,
            freeze_authority_disabled,
            None if concentration is None else str(concentration.quantize(Decimal("0.0001"))),
            None if liquidity is None else str(liquidity.quantize(Decimal("0.01"))),
            None if lp_locked is None else str(lp_locked.quantize(Decimal("0.01"))),
            None if developer_concentration is None else str(developer_concentration.quantize(Decimal("0.0001"))),
            token_program,
            extensions_checked,
        )

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
