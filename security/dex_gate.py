from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import aiohttp
from models import RiskRejected


@dataclass(frozen=True)
class TokenSafetyReport:
    approved: bool
    chain: str
    token: str
    locked_liquidity_pct: float
    top10_holder_pct: float
    buy_tax_pct: float
    sell_tax_pct: float
    simulation_passed: bool
    evidence: dict[str, Any]


class DexSecurityGate:
    """Fail-closed GoPlus adapter. Unknown security fields are never treated as safe."""

    def __init__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))

    async def close(self) -> None:
        await self.session.close()

    async def inspect(
        self, chain: str, token: str, chain_id: int | None = None,
        pool_liquidity_usd: float | None = None, min_liquidity_usd: float = 15000,
        min_locked_pct: float = 85, max_top10_pct: float = 25,
        max_tax_pct: float = 5, buy_sell_simulation_passed: bool = False,
        excluded_holder_addresses: set[str] | None = None,
    ) -> TokenSafetyReport:
        if chain not in {"solana", "evm"}:
            raise RiskRejected("Unsupported DEX chain.")
        if chain == "evm" and not chain_id:
            raise RiskRejected("EVM chain ID is required.")
        if chain == "solana":
            url = "https://api.gopluslabs.io/api/v1/solana/token_security"
            params = {"contract_addresses": token}
        else:
            url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
            params = {"contract_addresses": token}
        headers = {"accept": "application/json"}
        access = os.getenv("GOPLUS_ACCESS_TOKEN", "").strip()
        if access:
            headers["Authorization"] = "Bearer " + access
        async with self.session.get(url, params=params, headers=headers) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                raise RiskRejected(f"GoPlus token check failed with HTTP {response.status}.")
        result = body.get("result") or body.get("data")
        if isinstance(result, dict):
            record = result.get(token) or result.get(token.lower()) or next(iter(result.values()), None)
        elif isinstance(result, list) and result:
            record = result[0]
        else:
            record = None
        if not isinstance(record, dict):
            raise RiskRejected("GoPlus returned no token security record.")

        def flag(*keys: str) -> str | None:
            for key in keys:
                if key in record and record[key] not in (None, ""):
                    return str(record[key]).strip().lower()
            return None

        def pct(*keys: str) -> float:
            value = flag(*keys)
            if value is None:
                raise RiskRejected(f"Security field {keys[0]} is unknown.")
            number = float(value)
            return number * 100 if number <= 1 else number

        if chain == "solana":
            if flag("mintable", "mint_authority_active") not in {"0", "false", "none", "null"}:
                raise RiskRejected("Solana mint authority is active or unknown.")
            if flag("freezable", "freeze_authority_active") not in {"0", "false", "none", "null"}:
                raise RiskRejected("Solana freeze authority is active or unknown.")
        else:
            if flag("is_honeypot") != "0":
                raise RiskRejected("EVM honeypot status is unsafe or unknown.")
            if flag("is_mintable") != "0" or flag("is_open_source") != "1":
                raise RiskRejected("EVM mint function or source verification failed.")
            if flag("cannot_buy", "cannot_sell_all") not in {"0", "false"}:
                raise RiskRejected("EVM token buy/sell restriction present or unknown.")

        buy_tax, sell_tax = pct("buy_tax"), pct("sell_tax")
        if buy_tax > max_tax_pct or sell_tax > max_tax_pct:
            raise RiskRejected(f"Token tax exceeds {max_tax_pct:.1f}%.")

        excluded = {v.lower() for v in (excluded_holder_addresses or set())}
        holders = record.get("holders")
        if not isinstance(holders, list) or not holders:
            raise RiskRejected("Holder distribution unavailable.")
        eligible = []
        for holder in holders:
            if not isinstance(holder, dict):
                continue
            address = str(holder.get("address", "")).lower()
            if address in excluded or str(holder.get("is_contract", "0")) == "1" or str(holder.get("is_lp", "0")) == "1" or str(holder.get("is_locked", "0")) == "1":
                continue
            if holder.get("percent") is None:
                raise RiskRejected("Holder percentages incomplete.")
            value = float(holder["percent"])
            eligible.append(value * 100 if value <= 1 else value)
        if len(eligible) < 10:
            raise RiskRejected("Fewer than ten classifiable holders.")
        top10 = sum(sorted(eligible, reverse=True)[:10])
        if top10 > max_top10_pct:
            raise RiskRejected(f"Top 10 holder concentration {top10:.2f}% exceeds limit.")

        lp_holders = record.get("lp_holders")
        if not isinstance(lp_holders, list) or not lp_holders:
            raise RiskRejected("Liquidity lock report missing.")
        locked_pct = 0.0
        for lp in lp_holders:
            if not isinstance(lp, dict) or lp.get("percent") is None:
                raise RiskRejected("LP lock information incomplete.")
            value = float(lp["percent"])
            share = value * 100 if value <= 1 else value
            if str(lp.get("is_locked", "0")) == "1" or str(lp.get("is_burned", "0")) == "1":
                locked_pct += share
        if locked_pct < min_locked_pct:
            raise RiskRejected(f"Only {locked_pct:.2f}% liquidity LP supply is locked/burned.")
        if pool_liquidity_usd is None or pool_liquidity_usd < min_liquidity_usd:
            raise RiskRejected("Pool liquidity is unknown or below configured minimum.")
        if not buy_sell_simulation_passed:
            raise RiskRejected("Independent pre-trade buy and sell simulations did not pass.")
        return TokenSafetyReport(True, chain, token, locked_pct, top10, buy_tax, sell_tax,
                                 buy_sell_simulation_passed, record)
