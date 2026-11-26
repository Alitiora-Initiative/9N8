from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Awaitable, Callable

from .config import Settings
from .database import Database
from .helius import HeliusClient
from .launchpad_sniper import LaunchCandidate, LaunchpadSniper
from .math_engine import (
    BalanceRequirement, dec, drawdown_pct, from_lamports, moon_bag_split,
    net_profit_pct, should_exit, to_lamports, treasury_profit_fee,
)
from .mev_executor import ExecutionError, JupiterJitoExecutor
from .oracle import PriceOracle
from .security import TokenSecurity
from .wallet import WalletError, WalletManager
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)
SOL_MINT = "So11111111111111111111111111111111111111112"
BASE_GAS_RESERVE = 10_000
Notify = Callable[[int, str], Awaitable[None]]


def utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class AutonomousEngine:
    """Per-user supervisor for real Solana launch detection and position exits.

    Paper mode reports detections only; it never manufactures fills or PnL.
    Live mode requires environment-level enablement plus a paid activation.
    """
    def __init__(
        self,
        settings: Settings,
        db: Database,
        helius: HeliusClient,
        wallet: WalletManager,
        oracle: PriceOracle,
        executor: JupiterJitoExecutor,
        sniper: LaunchpadSniper,
        security: TokenSecurity,
        notify: Notify,
        telemetry: Callable[[int, dict], Awaitable[None]] | None = None,
    ):
        self.settings = settings
        self.db, self.helius, self.wallet = db, helius, wallet
        self.oracle, self.executor = oracle, executor
        self.sniper, self.security, self.notify = sniper, security, notify
        self.telemetry = telemetry
        self._scanner_task: asyncio.Task | None = None
        self._position_tasks: dict[int, asyncio.Task] = {}
        self._wallet_watch_tasks: dict[int, asyncio.Task] = {}
        self._stopping = False
        self._quote_lock = asyncio.Lock()
        self._last_quote_at = 0.0
        self._candidate_lock = asyncio.Semaphore(4)
        self._paper_notified: set[tuple[int, str]] = set()

    @property
    def live_enabled(self) -> bool:
        return self.settings.trading_mode == "live" and self.settings.live_trading_enabled

    async def start_user(self, telegram_id: int) -> str:
        await self.db.ensure_user(telegram_id)
        user = await self.db.get_user(telegram_id)
        if not user or not user.get("solana_address"):
            raise WalletError("Create or import a Solana wallet before executing the bot.")
        if self.settings.trading_mode == "live":
            if not self.settings.live_trading_enabled:
                raise ExecutionError("Live execution is locked by LIVE_TRADING_ENABLED.")
            if not user.get("activation_paid"):
                raise ExecutionError("The $1 activation fee has not been confirmed.")
        await self.db.update_settings(telegram_id, {"enabled": True})
        balance = await self.wallet.balance(telegram_id)
        await self.db.start_daily_risk(telegram_id, utc_day(), balance)
        await self.watch_wallet(telegram_id)
        await self._ensure_scanner()
        # Resume monitoring previously stored positions after a process restart.
        for position in await self.db.active_positions(telegram_id):
            position_id = int(position["id"])
            if position_id not in self._position_tasks or self._position_tasks[position_id].done():
                self._position_tasks[position_id] = asyncio.create_task(
                    self._monitor_position(position_id), name=f"9n8-position-{position_id}"
                )
        await self.db.security_event(telegram_id, "engine_started", self.settings.trading_mode)
        return "LIVE autonomous execution" if self.live_enabled else "PAPER detection only (no simulated fills)"

    async def stop_user(self, telegram_id: int) -> None:
        await self.db.update_settings(telegram_id, {"enabled": False})
        await self.db.security_event(
            telegram_id, "engine_stopped",
            "New entries disabled; open position monitors remain active.",
        )
        await self.notify(
            telegram_id,
            "9N8 paused. New entries are disabled; existing positions remain under exit monitoring.",
        )


    async def watch_wallet(self, telegram_id: int) -> None:
        """Start one Helius accountSubscribe listener to push SOL deposit alerts."""
        address = await self.db.get_wallet_address(telegram_id)
        if not address:
            return
        running = self._wallet_watch_tasks.get(telegram_id)
        if running and not running.done():
            return
        self._wallet_watch_tasks[telegram_id] = asyncio.create_task(
            self._monitor_wallet_deposits(telegram_id, address),
            name=f"9n8-wallet-{telegram_id}",
        )

    async def reset_wallet_watcher(self, telegram_id: int) -> None:
        old = self._wallet_watch_tasks.pop(telegram_id, None)
        if old and not old.done():
            old.cancel()
            await asyncio.gather(old, return_exceptions=True)
        await self.watch_wallet(telegram_id)

    async def _monitor_wallet_deposits(self, telegram_id: int, address: str) -> None:
        previous = await self.helius.balance_lamports(address)
        try:
            async for value in self.helius.account_changes(address):
                if self._stopping:
                    return
                current = int(value.get("lamports") or 0)
                delta = current - previous
                previous = current
                if delta <= 0:
                    continue
                await self.notify(
                    telegram_id,
                    f"SOL BALANCE CREDIT DETECTED\n"
                    f"Received: {from_lamports(delta)} SOL\n"
                    f"Current wallet balance: {from_lamports(current)} SOL\n"
                    f"Wallet: {address}\n"
                    "Helius observed an incoming SOL balance change; credits can include transfers or reclaimed account rent.",
                )
                if self.telemetry:
                    await self.telemetry(telegram_id, {
                        "type": "balance_credit",
                        "asset": "SOL",
                        "amount_lamports": delta,
                        "balance_lamports": current,
                        "wallet_address": address,
                    })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Wallet deposit watcher stopped for %s (%s)", telegram_id, type(exc).__name__)
        finally:
            self._wallet_watch_tasks.pop(telegram_id, None)

    async def _ensure_scanner(self) -> None:
        if self._scanner_task is None or self._scanner_task.done():
            self._stopping = False
            self._scanner_task = asyncio.create_task(
                self.sniper.run(self._on_candidate), name="9n8-launchpad-scanner"
            )

    async def shutdown(self) -> None:
        self._stopping = True
        await self.sniper.stop()
        tasks: list[asyncio.Task] = []
        if self._scanner_task:
            self._scanner_task.cancel()
            tasks.append(self._scanner_task)
        for task in self._position_tasks.values():
            task.cancel()
            tasks.append(task)
        for task in self._wallet_watch_tasks.values():
            task.cancel()
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._position_tasks.clear()
        self._wallet_watch_tasks.clear()

    async def _on_candidate(self, candidate: LaunchCandidate) -> None:
        if self._stopping:
            return
        async with self._candidate_lock:
            users = await self.db.all_users_with_wallets()
            for user in users:
                telegram_id = int(user["telegram_id"])
                config = await self.db.get_settings(telegram_id)
                if not config.get("enabled"):
                    continue
                selected = str(config.get("launchpad", "both")).lower()
                source = candidate.launchpad.lower()
                if selected != "both" and selected != source:
                    continue
                if not self.live_enabled:
                    key = (telegram_id, candidate.mint)
                    if key not in self._paper_notified:
                        self._paper_notified.add(key)
                        if len(self._paper_notified) > 10000:
                            self._paper_notified.clear()
                            self._paper_notified.add(key)
                        await self.notify(
                            telegram_id,
                            f"LAUNCH DETECTED · {candidate.launchpad}\\n"
                            f"Mint: {candidate.mint}\\nSlot: {candidate.slot or 'unknown'}\\n"
                            "PAPER MODE: detected only. No order or simulated fill was created.",
                        )
                    if self.telemetry:
                        await self.telemetry(telegram_id, {
                            "type": "launch_detected", "launchpad": candidate.launchpad,
                            "mint": candidate.mint, "signature": candidate.signature,
                            "slot": candidate.slot, "mode": "paper",
                            "curve_progress_pct": str(candidate.bonding_curve.progress_pct)
                                if candidate.bonding_curve else None,
                            "curve_complete": candidate.bonding_curve.complete
                                if candidate.bonding_curve else None,
                        })
                    continue
                try:
                    await self._consider_entry(telegram_id, candidate, config)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Entry evaluation failed for Telegram account %s", telegram_id)
                    await self.notify(
                        telegram_id,
                        f"9N8 skipped {candidate.mint[:8]}…: {type(exc).__name__}: {str(exc)[:180]}",
                    )

    async def _consider_entry(self, telegram_id: int, candidate: LaunchCandidate, config: dict) -> None:
        user = await self.db.get_user(telegram_id)
        if not user or not user.get("activation_paid"):
            return
        if await self.db.has_open_mint(telegram_id, candidate.mint):
            return

        balance = await self.wallet.balance(telegram_id)
        today = utc_day()
        daily = await self.db.start_daily_risk(telegram_id, today, balance)
        daily_cap = int(config.get("daily_position_limit", self.settings.default_daily_trades))
        if daily["trade_count"] >= daily_cap:
            return

        max_drawdown = dec(config.get("max_daily_drawdown_pct", "15"))
        baseline = daily["starting_equity_lamports"]
        if baseline > 0:
            realized_loss = max(0, -daily["realized_pnl_lamports"])
            estimate = max(0, baseline - realized_loss)
            dd = drawdown_pct(estimate, baseline)
            if dd >= max_drawdown:
                await self.db.update_settings(telegram_id, {"enabled": False})
                await self.notify(
                    telegram_id,
                    f"DAILY CIRCUIT BREAKER\nRealized drawdown {dd:.2f}% reached the {max_drawdown}% limit. New entries paused.",
                )
                return

        if len(await self.db.active_positions(telegram_id)) >= daily_cap:
            return
        excluded_token_accounts: set[str] = set()
        if candidate.launchpad == "pump.fun":
            curve = candidate.bonding_curve
            if not curve or curve.complete:
                return
            pump_mode = str(config.get("pump_mode", "fresh")).lower()
            if pump_mode == "migration":
                threshold = dec(config.get("pump_migration_progress_pct", "80"))
                if curve.progress_pct < threshold:
                    return
            elif pump_mode == "fresh":
                if candidate.created_at_slot is None:
                    return
                current_slot = int(await self.helius.rpc("getSlot", [{"commitment": "processed"}]))
                if current_slot - int(candidate.created_at_slot) > 3:
                    return
            # Exclude Pump.fun's associated bonding-curve token vault from the
            # top-holder concentration calculation; it is protocol inventory.
            mint_info = await self.helius.account_info(candidate.mint, "base64")
            if mint_info:
                token_program = Pubkey.from_string(str(mint_info["owner"]))
                curve_pk = Pubkey.from_string(curve.address)
                mint_pk = Pubkey.from_string(candidate.mint)
                associated_program = Pubkey.from_string(
                    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
                )
                vault, _ = Pubkey.find_program_address(
                    [bytes(curve_pk), bytes(token_program), bytes(mint_pk)],
                    associated_program,
                )
                excluded_token_accounts.add(str(vault))
        risk = await self.security.inspect(
            candidate.mint,
            launchpad=candidate.launchpad,
            max_top10_pct=str(config.get("max_top10_concentration_pct", "25")),
            min_liquidity_usd=str(config.get("min_liquidity_usd", "5000")),
            excluded_token_accounts=excluded_token_accounts,
        )
        if not risk.passed:
            await self.notify(
                telegram_id,
                f"SECURITY FILTER REJECTED {candidate.mint[:8]}…\n"
                + "\n".join(f"• {reason}" for reason in risk.reasons[:5]),
            )
            return

        amount_sol = dec(config.get("buy_amount_sol", self.settings.default_buy_sol))
        if amount_sol <= 0 or amount_sol > dec(self.settings.max_position_sol):
            await self.notify(telegram_id, "Entry blocked: buy amount exceeds the hard position cap.")
            return
        trade_lamports = to_lamports(amount_sol)
        priority_micro = int(config.get("priority_fee_lamports", 10000))
        priority_reserve = priority_micro
        use_jito = bool(config.get("mev_shield", True))
        jito_tip = int(config.get("jito_tip_lamports", 1_000_000)) if use_jito else 0
        try:
            # SPL token account data currently uses the base 165-byte account
            # layout. Query the cluster instead of baking in a rent constant.
            ata_rent_lamports = 2 * await self.helius.rent_exemption(165)
        except Exception as exc:
            raise ExecutionError("Could not verify current SPL token-account rent; entry blocked.") from exc
        required = BalanceRequirement(
            trade_lamports, priority_reserve, jito_tip,
            ata_rent_lamports, BASE_GAS_RESERVE,
        )
        if balance < required.total_lamports:
            await self.notify(
                telegram_id,
                f"Entry skipped: balance {from_lamports(balance)} SOL; "
                f"required reserve {from_lamports(required.total_lamports)} SOL.",
            )
            return

        wallet_key = await self.wallet.keypair_for(telegram_id)
        slippage_bps = int(dec(config.get("slippage_pct", "5")) * Decimal(100))
        result = await self.executor.build_swap(
            wallet=wallet_key,
            input_mint=SOL_MINT,
            output_mint=candidate.mint,
            amount_raw=trade_lamports,
            slippage_bps=slippage_bps,
            priority_fee_micro_lamports=priority_micro,
            jito_tip_lamports=jito_tip,
            use_jito=use_jito,
        )
        token_amount = await self.helius.token_balance_raw(str(wallet_key.pubkey()), candidate.mint)
        if token_amount <= 0:
            raise ExecutionError("Swap confirmed but no token balance was found; position was not recorded.")
        supply = await self.helius.token_supply(candidate.mint)
        decimals = int(supply["value"]["decimals"])
        sol_usd = await self.oracle.sol_usd()
        token_units = Decimal(token_amount) / (Decimal(10) ** decimals)
        entry_price = (amount_sol * sol_usd / token_units) if token_units > 0 else Decimal(0)
        tx_data = await self.helius.transaction(result.signature)
        network_fee = int(((tx_data or {}).get("meta") or {}).get("fee") or 5000)
        total_basis = trade_lamports + network_fee + jito_tip
        position_id = await self.db.create_position(
            telegram_id, candidate.mint, token_amount, total_basis,
            str(entry_price), result.signature,
        )
        await self.db.record_daily_entry(telegram_id, today)
        self._position_tasks[position_id] = asyncio.create_task(
            self._monitor_position(position_id), name=f"9n8-position-{position_id}"
        )
        await self.notify(
            telegram_id,
            f"ENTRY CONFIRMED · {candidate.launchpad}\n"
            f"Mint: {candidate.mint}\nSpend: {from_lamports(trade_lamports)} SOL\n"
            f"Tokens: {token_amount} raw units\nEntry: {entry_price:.12f} USD\n"
            f"Signature: https://solscan.io/tx/{result.signature}\n"
            f"MEV: {'Jito bundle landed' if result.bundle_id else 'RPC confirmed'}",
        )
        if self.telemetry:
            await self.telemetry(telegram_id, {
                "type": "position_opened", "position_id": position_id,
                "mint": candidate.mint, "launchpad": candidate.launchpad,
                "token_amount_raw": token_amount, "entry_price_usd": str(entry_price),
                "signature": result.signature,
            })

    async def _rate_limited_quote(self, mint: str, amount_raw: int) -> dict:
        interval = 1.05 if self.settings.jupiter_api_key else 2.1
        async with self._quote_lock:
            wait = interval - (time.monotonic() - self._last_quote_at)
            if wait > 0:
                await asyncio.sleep(wait)
            quote = await self.executor.quote(mint, SOL_MINT, amount_raw)
            self._last_quote_at = time.monotonic()
            return quote

    async def _monitor_position(self, position_id: int) -> None:
        try:
            while not self._stopping:
                position = await self.db.get_position(position_id)
                if not position or position["status"] != "open":
                    return
                telegram_id = int(position["telegram_id"])
                config = await self.db.get_settings(telegram_id)
                mint = str(position["mint"])
                wallet_address = await self.db.get_wallet_address(telegram_id)
                if not wallet_address:
                    return
                amount = await self.helius.token_balance_raw(wallet_address, mint)
                if amount <= 0:
                    await self.db.close_position(position_id, 0, "balance_zero", 0, "", True)
                    return
                quote = await self._rate_limited_quote(mint, amount)
                current_out = int(quote.get("outAmount") or 0)
                min_out = int(quote.get("otherAmountThreshold") or current_out)
                if current_out <= 0 or min_out <= 0:
                    await asyncio.sleep(1)
                    continue
                basis_lamports = int(position["initial_cost_lamports"])
                estimated_exit_cost = (
                    int(config.get("priority_fee_lamports", 10000))
                    + (int(config.get("jito_tip_lamports", 1_000_000)) if config.get("mev_shield", True) else 0)
                    + 5000
                )
                current_value_lamports = max(0, min_out - estimated_exit_cost)
                profit = net_profit_pct(
                    from_lamports(current_value_lamports),
                    from_lamports(basis_lamports),
                )
                opened = datetime.fromisoformat(str(position["opened_at"]))
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                elapsed = Decimal(str((datetime.now(timezone.utc) - opened).total_seconds()))
                already_partial = position.get("exit_reason") == "take_profit_partial"
                reason = None
                if already_partial:
                    if elapsed >= Decimal(int(config.get("max_hold_seconds", 60))):
                        reason = "max_hold"
                else:
                    reason = should_exit(
                        profit,
                        elapsed,
                        config.get("take_profit_pct", "10"),
                        config.get("stop_loss_pct", "15"),
                        int(config.get("max_hold_seconds", 60)),
                    )
                if reason:
                    await self._execute_exit(
                        position_id, reason, amount, int(position["token_amount_raw"]), config
                    )
                    refreshed = await self.db.get_position(position_id)
                    if not refreshed or refreshed["status"] != "open":
                        return
                if self.telemetry:
                    await self.telemetry(telegram_id, {
                        "type": "position_update", "position_id": position_id,
                        "mint": mint, "pnl_pct": str(profit.quantize(Decimal("0.01"))),
                        "current_value_sol": str(from_lamports(current_value_lamports)),
                        "elapsed_seconds": int(elapsed),
                        "max_hold_seconds": int(config.get("max_hold_seconds", 60)),
                    })
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Position monitor failed: %s", position_id)
            position = await self.db.get_position(position_id)
            if position:
                await self.notify(
                    int(position["telegram_id"]),
                    f"Position monitor paused for {position['mint'][:8]}…: {type(exc).__name__}. "
                    "No fabricated exit was recorded; inspect the wallet before restarting.",
                )
        finally:
            self._position_tasks.pop(position_id, None)

    async def _execute_exit(
        self, position_id: int, reason: str, actual_amount: int,
        originally_recorded_amount: int, config: dict,
    ) -> None:
        position = await self.db.get_position(position_id)
        if not position:
            return
        telegram_id = int(position["telegram_id"])
        mint = str(position["mint"])
        wallet_key = await self.wallet.keypair_for(telegram_id)
        current_balance = await self.helius.token_balance_raw(str(wallet_key.pubkey()), mint)
        if current_balance <= 0:
            await self.db.close_position(position_id, 0, "balance_zero", 0, "", True)
            return
        partial = (
            reason == "take_profit"
            and Decimal(str(config.get("moon_bag_pct", "15"))) > 0
        )
        if partial:
            sell_amount, _retained = moon_bag_split(
                current_balance, config.get("moon_bag_pct", "15")
            )
        else:
            sell_amount = current_balance
        if sell_amount <= 0:
            return

        quote = await self._rate_limited_quote(mint, sell_amount)
        expected_out = int(quote.get("outAmount") or 0)
        min_out = int(quote.get("otherAmountThreshold") or expected_out)
        if expected_out <= 0 or min_out <= 0:
            raise ExecutionError("No reliable SOL exit quote is available.")
        old_amount = int(position["token_amount_raw"])
        total_basis = int(position["initial_cost_lamports"])
        basis_sold = int(
            (Decimal(total_basis) * Decimal(sell_amount) / Decimal(max(old_amount, 1)))
            .to_integral_value()
        )
        priority_micro = int(config.get("priority_fee_lamports", 10000))
        use_jito = bool(config.get("mev_shield", True))
        jito_tip = int(config.get("jito_tip_lamports", 1_000_000)) if use_jito else 0
        sell_fees = priority_micro + jito_tip + 5000
        net_profit = from_lamports(min_out - basis_sold - sell_fees)
        treasury_lamports = to_lamports(treasury_profit_fee(net_profit))
        result = await self.executor.build_swap(
            wallet=wallet_key,
            input_mint=mint,
            output_mint=SOL_MINT,
            amount_raw=sell_amount,
            slippage_bps=int(dec(config.get("slippage_pct", "5")) * Decimal(100)),
            priority_fee_micro_lamports=priority_micro,
            jito_tip_lamports=jito_tip,
            treasury_fee_lamports=treasury_lamports,
            use_jito=use_jito,
        )
        if treasury_lamports > 0:
            await self.db.record_fee(
                telegram_id,
                "profit",
                treasury_lamports,
                result.signature,
                f"profit:{position_id}:{result.signature}",
            )
        remaining = await self.helius.token_balance_raw(str(wallet_key.pubkey()), mint)
        fully_closed = remaining == 0
        net_realized_lamports = min_out - basis_sold - sell_fees - treasury_lamports
        await self.db.close_position(
            position_id, remaining,
            "take_profit_partial" if partial and not fully_closed else reason,
            expected_out, result.signature, fully_closed,
        )
        await self.db.record_daily_pnl(telegram_id, utc_day(), net_realized_lamports)
        rent_reclaimed = await self._reclaim_empty_accounts(telegram_id, mint, fully_closed)
        await self.notify(
            telegram_id,
            f"EXIT CONFIRMED · {reason.upper()}\n"
            f"Mint: {mint}\nSold: {sell_amount} raw units\n"
            f"Expected SOL out: {from_lamports(expected_out)}\n"
            f"Profit fee: {from_lamports(treasury_lamports)} SOL\n"
            f"Remaining tokens: {remaining}\n"
            f"ATA rent released: {from_lamports(rent_reclaimed)} SOL\n"
            f"Signature: https://solscan.io/tx/{result.signature}",
        )
        if self.telemetry:
            await self.telemetry(telegram_id, {
                "type": "position_exit", "position_id": position_id,
                "mint": mint, "reason": reason, "remaining_raw": remaining,
                "signature": result.signature, "treasury_fee_lamports": treasury_lamports,
            })

    async def _reclaim_empty_accounts(self, telegram_id: int, mint: str, fully_closed: bool) -> int:
        if not fully_closed:
            return 0
        address = await self.db.get_wallet_address(telegram_id)
        if not address:
            return 0
        reclaimed = 0
        for item in await self.helius.token_accounts(address, mint):
            token_account = str(item.get("pubkey", ""))
            token_program = str(item.get("account", {}).get("owner", ""))
            info = item.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            raw = int((info.get("tokenAmount") or {}).get("amount", "0"))
            if not token_account or token_program not in TokenSecurity.TOKEN_PROGRAMS:
                continue
            if raw > 0:
                try:
                    quote = await self._rate_limited_quote(mint, raw)
                    dust_value = int(quote.get("otherAmountThreshold") or quote.get("outAmount") or 0)
                except Exception:
                    continue
                if dust_value >= 10_000:
                    continue
            account_info = await self.helius.account_info(token_account, "base64")
            rent = int(account_info.get("lamports", 0)) if account_info else 0
            await self.wallet.close_token_account(
                telegram_id, token_account, mint, raw, token_program
            )
            reclaimed += rent
        return reclaimed

    async def panic_sell(self, telegram_id: int, position_id: int) -> None:
        position = await self.db.get_position(position_id, telegram_id)
        if not position or position.get("status") != "open":
            raise ValueError("Open position not found.")
        config = await self.db.get_settings(telegram_id)
        address = await self.db.get_wallet_address(telegram_id)
        if not address:
            raise WalletError("No active wallet.")
        balance = await self.helius.token_balance_raw(address, str(position["mint"]))
        if balance <= 0:
            await self.db.close_position(position_id, 0, "balance_zero", 0, "", True)
            return
        await self._execute_exit(
            position_id, "manual_panic_sell", balance,
            int(position["token_amount_raw"]), config,
        )
