from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .launchpad_sniper import LaunchCandidate
from .math_engine import dec, from_lamports, to_lamports, usd_to_lamports
from .paper_store import PaperStore

logger = logging.getLogger(__name__)
SOL_MINT = "So11111111111111111111111111111111111111112"


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class PaperTradingEngine:
    """Real-market paper trading. It can request quotes, but cannot sign or submit swaps."""

    def __init__(self, store: PaperStore, helius: Any, oracle: Any, executor: Any,
                 security: Any, notify: Any, telemetry: Any = None,
                 max_position_sol: str = "0.25", jupiter_api_key: str = ""):
        self.store = store
        self.helius = helius
        self.oracle = oracle
        self.executor = executor
        self.security = security
        self.notify = notify
        self.telemetry = telemetry
        self.max_position_sol = dec(max_position_sol)
        self.jupiter_api_key = jupiter_api_key
        self._tasks: dict[int, asyncio.Task] = {}
        self._quote_lock = asyncio.Lock()
        self._last_quote_at = 0.0
        self._stopping = False

    async def start_user(self, telegram_id: int) -> dict[str, Any]:
        price = await self.oracle.sol_usd()
        bankroll = usd_to_lamports("1000", price)
        account = await self.store.create_account(telegram_id, bankroll)
        await self.store.set_mode_enabled(telegram_id, "paper", True)
        await self.store.touch_day(telegram_id, datetime.now(timezone.utc).date().isoformat())
        await self._resume_positions(telegram_id)
        return account

    async def stop_user(self, telegram_id: int) -> None:
        # Stop new paper entries. Existing virtual positions remain monitored.
        await self.store.set_mode_enabled(telegram_id, "paper", False)

    async def _resume_positions(self, telegram_id: int) -> None:
        for position in await self.store.positions(telegram_id):
            self._ensure_monitor(int(position["id"]))

    def _ensure_monitor(self, position_id: int) -> None:
        task = self._tasks.get(position_id)
        if task is None or task.done():
            self._tasks[position_id] = asyncio.create_task(
                self._monitor(position_id), name=f"9n8-paper-position-{position_id}"
            )

    async def shutdown(self) -> None:
        self._stopping = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _quote(self, input_mint: str, output_mint: str, amount_raw: int) -> dict[str, Any]:
        if amount_raw <= 0:
            raise ValueError("Quote amount must be positive.")
        # Jupiter keyless access is deliberately kept below its published request rate.
        interval = 1.05 if self.jupiter_api_key else 2.1
        async with self._quote_lock:
            wait = interval - (time.monotonic() - self._last_quote_at)
            if wait > 0:
                await asyncio.sleep(wait)
            quote = await self.executor.quote(input_mint, output_mint, amount_raw)
            self._last_quote_at = time.monotonic()
        if not isinstance(quote, dict) or int(quote.get("outAmount") or 0) <= 0:
            raise RuntimeError("Jupiter did not return a usable quote.")
        return quote

    async def _minimum_liquidity_usd(self, config: dict[str, Any]) -> str:
        sol_price = await self.oracle.sol_usd()
        minimum = dec(config.get("min_liquidity_sol", "5")) * sol_price
        configured_usd = dec(config.get("min_liquidity_usd", "0"))
        return str(max(minimum, configured_usd))

    async def on_candidate(self, telegram_id: int, candidate: LaunchCandidate) -> None:
        if self._stopping or not await self.store.mode_enabled(telegram_id, "paper"):
            return
        day = datetime.now(timezone.utc).date().isoformat()
        await self.store.touch_day(telegram_id, day)
        config = await self.store.settings(telegram_id, "paper")
        selected = str(config.get("launchpad", "both")).lower()
        source = candidate.launchpad.lower()
        if selected not in {"both", source}:
            return
        if await self.store.has_open_mint(telegram_id, candidate.mint):
            return
        if await self.store.trade_count_today(telegram_id, day) >= int(config.get("daily_position_limit", 5)):
            return
        if len(await self.store.positions(telegram_id)) >= int(config.get("daily_position_limit", 5)):
            return

        # A Pump.fun curve before migration is not a verified Raydium pool. Wait
        # for a completed curve/migration rather than pretending it has locked LP.
        security_source = source
        if source == "pump.fun":
            if not candidate.bonding_curve or not candidate.bonding_curve.complete:
                return
            security_source = "raydium"

        minimum_liquidity_usd = await self._minimum_liquidity_usd(config)
        risk = await self.security.inspect(
            candidate.mint,
            launchpad=security_source,
            max_top10_pct=str(config.get("max_top10_concentration_pct", "25")),
            min_liquidity_usd=minimum_liquidity_usd,
            min_lp_locked_pct=str(config.get("min_lp_locked_pct", "80")),
            max_dev_pct=str(config.get("max_dev_wallet_pct", "5")),
            require_mint_authority_revoked=bool(config.get("require_mint_authority_revoked", True)),
            require_freeze_authority_revoked=bool(config.get("require_freeze_authority_revoked", True)),
        )
        if not risk.passed:
            await self.notify(
                telegram_id,
                "PAPER TOKEN SKIPPED · SECURITY CHECK\n"
                f"Mint: {candidate.mint}\n" +
                "\n".join("• " + reason for reason in risk.reasons[:5]),
            )
            return

        amount_sol = dec(config.get("buy_amount_sol", "0.05"))
        if amount_sol <= 0 or amount_sol > self.max_position_sol:
            await self.notify(telegram_id, "Paper entry skipped: buy amount exceeds the configured per-position cap.")
            return
        spend = to_lamports(amount_sol)
        quote = await self._quote(SOL_MINT, candidate.mint, spend)
        received = int(quote.get("otherAmountThreshold") or quote.get("outAmount") or 0)
        if received <= 0:
            return
        supply = await self.helius.token_supply(candidate.mint)
        decimals = int(supply["value"]["decimals"])
        sol_price = await self.oracle.sol_usd()
        token_units = Decimal(received) / (Decimal(10) ** decimals)
        if token_units <= 0:
            return
        entry_price = (amount_sol * sol_price) / token_units
        # These are explicit modelling costs, not real fees or Jito tips.
        swap_estimate = int((Decimal(spend) * Decimal("0.001")).to_integral_value())
        network_estimate = max(5000, int(config.get("priority_fee_lamports", 10000)))
        tip_estimate = int(config.get("jito_tip_lamports", 0)) if config.get("mev_shield", True) else 0
        fee = swap_estimate + network_estimate + tip_estimate
        position_id = await self.store.buy(
            telegram_id, candidate.mint, candidate.launchpad, received, spend, fee,
            str(entry_price), quote, candidate.signature,
        )
        self._ensure_monitor(position_id)
        await self.notify(
            telegram_id,
            "🧪 PAPER BUY · SIMULATED\n"
            f"Token: {candidate.mint}\n"
            f"Source: {candidate.launchpad}\n"
            f"Virtual spend: {from_lamports(spend)} SOL\n"
            f"Estimated costs: {from_lamports(fee)} SOL\n"
            f"Estimated tokens: {received} raw units\n"
            "Price source: Jupiter quote; no transaction was signed or sent.",
        )
        if self.telemetry:
            await self.telemetry(telegram_id, {
                "type": "paper_position_opened", "position_id": position_id,
                "mint": candidate.mint, "launchpad": candidate.launchpad,
                "token_amount_raw": str(received), "entry_price_usd": str(entry_price),
                "simulated_spend_lamports": spend, "simulated_fee_lamports": fee,
            })

    async def preview_mint(
        self, telegram_id: int, mint: str, amount_override_sol: str | None = None,
    ) -> dict[str, Any]:
        config = await self.store.settings(telegram_id, "paper")
        minimum_liquidity_usd = await self._minimum_liquidity_usd(config)
        risk = await self.security.inspect(
            mint, launchpad="raydium",
            max_top10_pct=str(config.get("max_top10_concentration_pct", "25")),
            min_liquidity_usd=minimum_liquidity_usd,
            min_lp_locked_pct=str(config.get("min_lp_locked_pct", "80")),
            max_dev_pct=str(config.get("max_dev_wallet_pct", "5")),
            require_mint_authority_revoked=bool(config.get("require_mint_authority_revoked", True)),
            require_freeze_authority_revoked=bool(config.get("require_freeze_authority_revoked", True)),
        )
        if not risk.passed:
            return {"passed": False, "reasons": list(risk.reasons), "mint": mint}
        amount_sol = dec(amount_override_sol if amount_override_sol is not None else config.get("buy_amount_sol", "0.05"))
        if amount_sol <= 0 or amount_sol > self.max_position_sol:
            return {"passed": False, "reasons": ["Buy amount exceeds the configured safety cap."], "mint": mint}
        amount = to_lamports(amount_sol)
        quote = await self._quote(SOL_MINT, mint, amount)
        return {
            "passed": True, "mint": mint, "launchpad": "raydium",
            "buy_amount_sol": str(from_lamports(amount)),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expected_output_raw": str(quote.get("outAmount")),
            "minimum_output_raw": str(quote.get("otherAmountThreshold") or quote.get("outAmount")),
            "price_impact_pct": str(quote.get("priceImpactPct") or "unknown"),
            "risk": {
                "mint_authority_disabled": risk.mint_authority_disabled,
                "freeze_authority_disabled": risk.freeze_authority_disabled,
                "top10_concentration_pct": risk.top10_concentration_pct,
                "liquidity_usd": risk.liquidity_usd,
                "lp_locked_pct": risk.lp_locked_pct,
            },
            "quote": quote,
        }

    async def manual_buy(self, telegram_id: int, mint: str, preview: dict[str, Any]) -> int:
        if not preview.get("passed") or preview.get("mint") != mint:
            raise ValueError("A passing preview for this mint is required.")
        if not await self.store.mode_enabled(telegram_id, "paper"):
            raise ValueError("Start paper mode before confirming a paper buy.")
        created_at = _timestamp(preview.get("created_at", ""))
        if (datetime.now(timezone.utc) - created_at).total_seconds() > 90:
            raise ValueError("This preview expired. Run /snipe again for a fresh safety check and quote.")
        amount_sol = str(preview["buy_amount_sol"])
        # Re-run every security rule and fetch a fresh quote immediately before
        # recording the virtual fill; do not trust stale Telegram callback state.
        fresh = await self.preview_mint(telegram_id, mint, amount_sol)
        if not fresh.get("passed"):
            raise ValueError("The token no longer passes the safety checks: " + "; ".join(fresh.get("reasons", [])[:3]))
        quote = dict(fresh["quote"])
        tokens = int(fresh.get("minimum_output_raw") or 0)
        if tokens <= 0:
            raise ValueError("Jupiter quote has no minimum output.")
        amount = to_lamports(dec(amount_sol))
        config = await self.store.settings(telegram_id, "paper")
        sol_price = await self.oracle.sol_usd()
        supply = await self.helius.token_supply(mint)
        decimals = int(supply["value"]["decimals"])
        token_units = Decimal(tokens) / (Decimal(10) ** decimals)
        entry_price = dec(amount_sol) * sol_price / token_units
        fee = max(5000, int(config.get("priority_fee_lamports", 10000))) + (
            int(config.get("jito_tip_lamports", 0)) if config.get("mev_shield", True) else 0
        ) + int(Decimal(amount) * Decimal("0.001"))
        pid = await self.store.buy(
            telegram_id, mint, "raydium", tokens, amount, fee, str(entry_price), quote,
            "manual-paper-preview",
        )
        self._ensure_monitor(pid)
        await self.notify(
            telegram_id,
            "🧪 MANUAL PAPER BUY RECORDED\n"
            f"Mint: {mint}\nVirtual spend: {from_lamports(amount)} SOL\n"
            f"Estimated tokens: {tokens} raw units\n"
            "No wallet signature or blockchain transaction was created.",
        )
        return pid

    async def panic_sell(self, telegram_id: int, position_id: int) -> None:
        position = await self.store.position(position_id, telegram_id)
        if not position or position["status"] != "open":
            raise ValueError("Open paper position not found.")
        await self._sell(position, int(position["token_amount_raw"]), "manual_paper_sell")

    async def _monitor(self, position_id: int) -> None:
        try:
            while not self._stopping:
                position = await self.store.position(position_id)
                if not position or position["status"] != "open":
                    return
                telegram_id = int(position["telegram_id"])
                config = await self.store.settings(telegram_id, "paper")
                amount = int(position["token_amount_raw"])
                quote = await self._quote(str(position["mint"]), SOL_MINT, amount)
                expected = int(quote.get("outAmount") or 0)
                minimum = int(quote.get("otherAmountThreshold") or expected)
                if expected <= 0 or minimum <= 0:
                    await asyncio.sleep(3)
                    continue
                estimated_cost = self._estimated_exit_fee(minimum, config)
                value = max(0, minimum - estimated_cost)
                basis = int(position["initial_cost_lamports"])
                if basis <= 0:
                    return
                pnl = (Decimal(value - basis) * Decimal(100)) / Decimal(basis)
                await self.store.mark(position_id, value, str(pnl.quantize(Decimal("0.01"))))
                position = await self.store.position(position_id)
                if not position:
                    return
                peak = max(int(position.get("peak_value_lamports") or 0), value)
                age = (datetime.now(timezone.utc) - _timestamp(position["opened_at"])).total_seconds()
                reason = None
                sell_amount = amount
                tp1_done = bool(position.get("tp1_done"))
                tp2_done = bool(position.get("tp2_done"))

                if pnl <= -dec(config.get("stop_loss_pct", "30")):
                    reason = "hard_stop"
                elif not tp1_done and pnl >= dec(config.get("take_profit_pct", "100")):
                    reason = "take_profit_1"
                    sell_amount = max(1, amount // 2)
                elif tp1_done and not tp2_done and pnl >= dec(config.get("take_profit_2_pct", "300")):
                    reason = "take_profit_2"
                    sell_fraction = dec(config.get("take_profit_2_sell_pct", "25")) / Decimal(100)
                    sell_amount = max(1, int(Decimal(amount) * sell_fraction))
                elif tp1_done and peak > 0 and Decimal(value) <= Decimal(peak) * (
                    Decimal(1) - dec(config.get("trailing_stop_pct", "20")) / Decimal(100)
                ):
                    reason = "trailing_stop"
                elif age >= int(config.get("max_hold_seconds", 14400)):
                    reason = "max_hold"

                if reason:
                    await self._sell(position, min(sell_amount, amount), reason, quote_override=None)
                    if reason == "take_profit_1":
                        await self.store.set_targets(position_id, tp1_done=True)
                    elif reason == "take_profit_2":
                        await self.store.set_targets(position_id, tp2_done=True)
                    refreshed = await self.store.position(position_id)
                    if not refreshed or refreshed["status"] != "open":
                        return
                if self.telemetry:
                    await self.telemetry(telegram_id, {
                        "type": "paper_position_update", "position_id": position_id,
                        "mint": position["mint"], "pnl_pct": str(pnl.quantize(Decimal("0.01"))),
                        "current_value_sol": str(from_lamports(value)),
                    })
                await asyncio.sleep(3.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Paper position monitor failed for %s", position_id)
            position = await self.store.position(position_id)
            if position:
                await self.notify(
                    int(position["telegram_id"]),
                    f"Paper monitor temporarily unavailable for {str(position['mint'])[:8]}…: {type(exc).__name__}. "
                    "No simulated exit was invented; the virtual position remains open and the monitor will retry.",
                )
            if not self._stopping:
                asyncio.get_running_loop().call_later(5, self._ensure_monitor, position_id)
        finally:
            current = self._tasks.get(position_id)
            if current is asyncio.current_task():
                self._tasks.pop(position_id, None)

    def _estimated_exit_fee(self, gross: int, config: dict[str, Any]) -> int:
        swap = int(Decimal(gross) * Decimal("0.001"))
        network = max(5000, int(config.get("priority_fee_lamports", 10000)))
        tip = int(config.get("jito_tip_lamports", 0)) if config.get("mev_shield", True) else 0
        return swap + network + tip

    async def _sell(self, position: dict[str, Any], tokens: int, reason: str,
                    quote_override: dict[str, Any] | None = None) -> None:
        telegram_id = int(position["telegram_id"])
        mint = str(position["mint"])
        config = await self.store.settings(telegram_id, "paper")
        quote = quote_override or await self._quote(mint, SOL_MINT, tokens)
        gross = int(quote.get("otherAmountThreshold") or quote.get("outAmount") or 0)
        if gross <= 0:
            raise RuntimeError("No usable Jupiter quote for paper exit.")
        fee = self._estimated_exit_fee(gross, config)
        result = await self.store.sell(
            int(position["id"]), telegram_id, tokens, gross, fee, reason, quote,
            "paper-quote",
        )
        await self.notify(
            telegram_id,
            f"🧪 PAPER EXIT · {reason.replace('_',' ').upper()}\n"
            f"Mint: {mint}\nTokens sold: {tokens} raw units\n"
            f"Estimated proceeds: {from_lamports(gross)} SOL\n"
            f"Estimated costs: {from_lamports(fee)} SOL\n"
            f"Estimated realized PnL: {from_lamports(int(result['realized_lamports']))} SOL\n"
            "This is a virtual fill from market quotes. No blockchain transaction was sent.",
        )

