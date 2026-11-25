from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from models import (
    AccountState, MarketSnapshot, RiskDecision, RiskRejected, Side,
    TradeIntent, TradeSize,
)
from storage import TradeStore

log = logging.getLogger(__name__)

# These are hard ceilings. JSON settings can tighten but cannot loosen them.
HARD_MAX_RISK_PER_TRADE = 0.02
HARD_MAX_DAILY_DRAWDOWN = 0.05
HARD_MAX_LEVERAGE = 3.0
HARD_MAX_CEX_SLIPPAGE = 0.005
HARD_MAX_DEX_SLIPPAGE = 0.05
HARD_MAX_FOREX_SPREAD_PIPS = 2.0
HARD_MAX_PORTFOLIO_EXPOSURE = 0.25


class RiskManager:
    def __init__(self, store: TradeStore, settings: dict):
        self.store = store
        self.settings = settings
        self.risk_cfg = settings["risk"]
        self._flatten: Callable[[], Awaitable[None]] | None = None
        self._halt_until: datetime | None = None
        self._lock_reason = ""

    def register_flatten_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._flatten = callback

    async def is_paused(self) -> tuple[bool, str]:
        paused = await self.store.get_control("paused", "false") == "true"
        until_text = await self.store.get_control("halt_until", "")
        if until_text:
            try:
                until = datetime.fromisoformat(until_text)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < until:
                    return True, "Risk halt active until " + until.isoformat()
                await self.store.set_control("halt_until", "")
                await self.store.set_control("kill_switch", "false")
            except ValueError:
                return True, "Invalid halt state: manual review required"
        return paused, "Trading paused by operator" if paused else ""

    async def assert_entry_allowed(self) -> None:
        paused, reason = await self.is_paused()
        if paused:
            raise RiskRejected(reason)
        if await self.store.get_control("kill_switch", "false") == "true":
            raise RiskRejected("Kill switch is active.")
        if await self.store.get_control("equity_monitor_failed", "false") == "true":
            raise RiskRejected("Portfolio equity monitor has failed; entries remain blocked.")

    def _bounded(self, name: str, hard_max: float) -> float:
        return min(max(float(self.risk_cfg.get(name, hard_max)), 0.0), hard_max)

    async def evaluate(
        self,
        intent: TradeIntent,
        account: AccountState,
        snapshot: MarketSnapshot,
        current_exposure_usd: float,
        estimated_slippage_pct: float,
        contract_multiplier: float = 1.0,
        leverage: float = 1.0,
        forex_spread_pips: float | None = None,
    ) -> RiskDecision:
        await self.assert_entry_allowed()
        if account.equity_usd <= 0 or account.free_usd <= 0:
            return RiskDecision(False, "Account equity or free collateral is not positive.")
        if not all(map(lambda x: x > 0, (intent.entry_price, intent.risk_distance, contract_multiplier))):
            return RiskDecision(False, "Invalid entry, stop distance or contract multiplier.")
        if intent.side is Side.BUY and intent.stop_loss >= intent.entry_price:
            return RiskDecision(False, "Long stop-loss must be below entry.")
        if intent.side is Side.SELL and intent.stop_loss <= intent.entry_price:
            return RiskDecision(False, "Short stop-loss must be above entry.")
        if intent.side is Side.BUY and intent.take_profit <= intent.entry_price:
            return RiskDecision(False, "Long take-profit must be above entry.")
        if intent.side is Side.SELL and intent.take_profit >= intent.entry_price:
            return RiskDecision(False, "Short take-profit must be below entry.")

        market = intent.market_type.value
        max_slippage = (
            HARD_MAX_DEX_SLIPPAGE if market in {"solana_dex", "evm_dex"}
            else HARD_MAX_CEX_SLIPPAGE
        )
        configured_slippage = self._bounded(
            "maximum_dex_slippage_pct" if market in {"solana_dex","evm_dex"} else "maximum_cex_slippage_pct",
            max_slippage,
        )
        if estimated_slippage_pct < 0 or estimated_slippage_pct > configured_slippage:
            return RiskDecision(False, f"Slippage {estimated_slippage_pct:.4%} exceeds limit {configured_slippage:.4%}.")
        if forex_spread_pips is not None:
            max_spread = min(float(self.risk_cfg.get("maximum_forex_spread_pips", 2.0)), HARD_MAX_FOREX_SPREAD_PIPS)
            if forex_spread_pips < 0 or forex_spread_pips > max_spread:
                return RiskDecision(False, f"Forex spread {forex_spread_pips:.2f} pips exceeds {max_spread:.2f}.")

        if leverage < 1 or leverage > min(float(self.risk_cfg.get("maximum_perpetual_leverage", 1)), HARD_MAX_LEVERAGE):
            return RiskDecision(False, "Requested leverage exceeds the hard leverage ceiling.")
        risk_pct = self._bounded("risk_per_trade_pct", HARD_MAX_RISK_PER_TRADE)
        risk_budget = account.equity_usd * risk_pct
        quantity = risk_budget / (intent.risk_distance * contract_multiplier)
        notional = quantity * intent.entry_price * contract_multiplier
        max_notional = min(
            float(self.risk_cfg.get("max_position_notional_usd", 100)),
            account.equity_usd * min(float(self.risk_cfg.get("maximum_portfolio_exposure_pct", .25)), HARD_MAX_PORTFOLIO_EXPOSURE),
        )
        remaining_exposure = max(
            0.0,
            account.equity_usd * HARD_MAX_PORTFOLIO_EXPOSURE - current_exposure_usd,
        )
        notional = min(notional, max_notional, remaining_exposure, account.free_usd * leverage)
        if notional <= 0:
            return RiskDecision(False, "No risk budget or portfolio exposure remains.")
        quantity = notional / (intent.entry_price * contract_multiplier)
        actual_risk_usd = quantity * intent.risk_distance * contract_multiplier
        if quantity <= 0:
            return RiskDecision(False, "Calculated quantity is below zero.")
        if market in {"solana_dex", "evm_dex"} and not intent.metadata.get("security_approved", False):
            return RiskDecision(False, "DEX token has not passed the independent security gate.")
        if market in {"solana_dex", "evm_dex"} and not intent.metadata.get("buy_sell_simulation_passed", False):
            return RiskDecision(False, "DEX entry requires successful pre-trade buy and sell simulations.")
        if market in {"crypto_perpetual", "crypto_perp"} and leverage > 1 and not intent.metadata.get("isolated_margin_confirmed", False):
            return RiskDecision(False, "Isolated margin was not confirmed by the venue.")
        return RiskDecision(True, "Risk checks passed.", TradeSize(quantity, notional, actual_risk_usd))

    async def observe_equity(self, account: AccountState) -> bool:
        """Persist a UTC-day equity anchor and trip the 24-hour kill switch at 5% DD."""
        now = datetime.now(timezone.utc)
        day = now.date().isoformat()
        starting = await self.store.set_daily_equity(day, account.equity_usd)
        if starting <= 0:
            return False
        drawdown = (starting - account.equity_usd) / starting
        if drawdown >= HARD_MAX_DAILY_DRAWDOWN:
            await self.trip_kill_switch(
                f"Daily equity drawdown {drawdown:.2%} reached the hard 5% limit.",
                halt_hours=24,
            )
            return True
        return False

    async def trip_kill_switch(self, reason: str, halt_hours: int = 24) -> None:
        await self.store.set_control("kill_switch", "true")
        await self.store.set_control("paused", "true")
        until = datetime.now(timezone.utc) + timedelta(hours=halt_hours)
        await self.store.set_control("halt_until", until.isoformat())
        await self.store.set_control("kill_reason", reason)
        self._lock_reason = reason
        await self.store.event("CRITICAL", "kill_switch", reason)
        log.critical("KILL SWITCH: %s", reason)
        if self._flatten:
            try:
                await self._flatten()
            except Exception:
                # Keep the process alive and paused so open positions remain monitored.
                log.exception("Emergency flatten failed; continuing in fail-closed monitoring state.")

    async def pause(self) -> None:
        await self.store.set_control("paused", "true")
        await self.store.event("WARNING", "operator_pause", "New entries paused.")

    async def resume(self) -> None:
        paused, reason = await self.is_paused()
        if await self.store.get_control("kill_switch", "false") == "true" and await self.store.get_control("halt_until", ""):
            raise RiskRejected("Cannot resume before the mandatory 24-hour drawdown halt expires.")
        await self.store.set_control("paused", "false")
        await self.store.set_control("kill_switch", "false")
        await self.store.event("INFO", "operator_resume", "New entries resumed by operator.")
