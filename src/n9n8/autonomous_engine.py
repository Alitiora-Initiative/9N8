from __future__ import annotations

import asyncio
import base64
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
        paper_engine: Any | None = None,
    ):
        self.settings = settings
        self.db, self.helius, self.wallet = db, helius, wallet
        self.oracle, self.executor = oracle, executor
        self.sniper, self.security, self.notify = sniper, security, notify
        self.telemetry = telemetry
        self.paper_engine = paper_engine
        self._scanner_task: asyncio.Task | None = None
        self._position_tasks: dict[int, asyncio.Task] = {}
        self._exit_locks: dict[int, asyncio.Lock] = {}
        self._wallet_watch_tasks: dict[int, asyncio.Task] = {}
        self._stopping = False
        self._quote_lock = asyncio.Lock()
        self._last_quote_at = 0.0
        self._candidate_lock = asyncio.Semaphore(4)
        self._paper_notified: set[tuple[int, str]] = set()
        self._enabled_users: set[int] = set()
        self._paper_users: set[int] = set()
        self._live_users: set[int] = set()

    @property
    def live_enabled(self) -> bool:
        # This is the operator-wide kill switch. Per-user live consent is stored
        # separately and checked for every candidate.
        return bool(self.settings.live_trading_enabled)

    async def start_paper_user(self, telegram_id: int) -> dict[str, Any]:
        if self.paper_engine is None:
            raise ExecutionError("Paper trading engine is not configured.")
        account = await self.paper_engine.start_user(telegram_id)
        self._paper_users.add(int(telegram_id))
        self._enabled_users.add(int(telegram_id))
        await self._ensure_scanner()
        await self.db.security_event(telegram_id, "paper_engine_started", "Independent virtual account started; no wallet signer is used.")
        return account

    async def stop_paper_user(self, telegram_id: int) -> None:
        if self.paper_engine is None:
            raise ExecutionError("Paper trading engine is not configured.")
        await self.paper_engine.stop_user(telegram_id)
        self._paper_users.discard(int(telegram_id))
        if int(telegram_id) not in self._live_users:
            self._enabled_users.discard(int(telegram_id))
        await self.db.security_event(telegram_id, "paper_engine_stopped", "New virtual entries disabled; virtual positions remain monitored.")
        await self._stop_scanner_if_idle()

    async def live_readiness(self, telegram_id: int) -> dict[str, Any]:
        user = await self.db.get_user(telegram_id)
        checks: dict[str, Any] = {
            "operator_live_switch": self.live_enabled,
            "wallet_connected": bool(user and user.get("solana_address")),
            "activation_confirmed": bool(user and user.get("activation_paid")),
            "paper_days": 0,
            "wallet_balance_sol": "0",
            "wallet_balance_usd": "0",
            "minimum_wallet_usd": "10",
            "ready": False,
            "blockers": [],
        }
        if not self.live_enabled:
            checks["blockers"].append("The operator has not enabled LIVE_TRADING_ENABLED.")
        if not checks["wallet_connected"]:
            checks["blockers"].append("Create or import a Solana trading wallet.")
        if not checks["activation_confirmed"]:
            checks["blockers"].append("Complete the account activation check.")
        if self.paper_engine is None:
            checks["blockers"].append("The separate paper engine is unavailable.")
        else:
            checks["paper_days"] = await self.paper_engine.store.active_days(telegram_id)
            if checks["paper_days"] < 7:
                checks["blockers"].append(
                    f"Complete paper practice on at least 7 different UTC days ({checks['paper_days']}/7 recorded)."
                )
        if checks["wallet_connected"]:
            try:
                lamports = await self.wallet.balance(telegram_id)
                price = await self.oracle.sol_usd()
                checks["wallet_balance_sol"] = str(from_lamports(lamports))
                value = from_lamports(lamports) * price
                checks["wallet_balance_usd"] = str(value.quantize(Decimal("0.01")))
                if value < Decimal("10"):
                    checks["blockers"].append("The connected Solana wallet must hold at least $10 of SOL.")
            except Exception as exc:
                checks["blockers"].append("Wallet balance could not be verified: " + type(exc).__name__)
        checks["ready"] = not checks["blockers"]
        return checks

    async def preview_live_snipe(
        self, telegram_id: int, mint: str, amount_sol: str,
    ) -> dict[str, Any]:
        """Validate a manual live buy without signing or submitting a transaction."""
        try:
            canonical_mint = str(Pubkey.from_string(mint.strip()))
        except Exception as exc:
            raise ExecutionError("Enter a valid Solana token mint address.") from exc
        if canonical_mint == SOL_MINT:
            raise ExecutionError("Select a token mint other than wrapped SOL.")
        if not self.live_enabled:
            raise ExecutionError("The operator has not enabled the global live-trading switch.")
        config = await self.db.get_settings(telegram_id)
        if not bool(config.get("live_enabled", False)):
            raise ExecutionError("Live mode is OFF for this Telegram account. Open /live and arm it first.")
        readiness = await self.live_readiness(telegram_id)
        if not readiness["ready"]:
            raise ExecutionError("Live readiness is blocked: " + " ".join(readiness["blockers"][:4]))
        if await self.db.has_open_mint(telegram_id, canonical_mint):
            raise ExecutionError("This wallet already has an open position in this mint.")
        daily = await self.db.start_daily_risk(telegram_id, utc_day(), await self.wallet.balance(telegram_id))
        daily_cap = int(config.get("daily_position_limit", self.settings.default_daily_trades))
        if daily["trade_count"] >= daily_cap:
            raise ExecutionError("Your daily live trade limit has been reached.")
        if len(await self.db.active_positions(telegram_id)) >= daily_cap:
            raise ExecutionError("Your maximum number of open live positions has been reached.")

        buy_amount = dec(amount_sol)
        hard_cap = dec(self.settings.max_position_sol)
        if buy_amount <= 0 or buy_amount > hard_cap:
            raise ExecutionError(f"Buy size must be greater than zero and no more than {hard_cap} SOL.")
        amount_lamports = to_lamports(buy_amount)
        balance = await self.wallet.balance(telegram_id)
        priority = max(0, int(config.get("priority_fee_lamports", 10_000)))
        use_jito = bool(config.get("mev_shield", True))
        tip = max(0, int(config.get("jito_tip_lamports", 1_000_000))) if use_jito else 0
        try:
            token_rent = 2 * int(await self.helius.rent_exemption(165))
        except Exception as exc:
            raise ExecutionError("Current token-account rent could not be checked; live buy blocked.") from exc
        required = amount_lamports + priority + tip + token_rent + BASE_GAS_RESERVE
        if balance < required:
            raise ExecutionError(
                f"Wallet balance is {from_lamports(balance)} SOL; this buy needs at least "
                f"{from_lamports(required)} SOL including fees and rent reserves."
            )

        try:
            sol_price = await self.oracle.sol_usd()
            minimum_liquidity_usd = max(
                dec(config.get("min_liquidity_sol", "5")) * sol_price,
                dec(config.get("min_liquidity_usd", "0")),
            )
            risk = await self.security.inspect(
                canonical_mint,
                launchpad="raydium",
                max_top10_pct=str(config.get("max_top10_concentration_pct", "25")),
                min_liquidity_usd=str(minimum_liquidity_usd),
                min_lp_locked_pct=str(config.get("min_lp_locked_pct", "80")),
                max_dev_pct=str(config.get("max_dev_wallet_pct", "5")),
                require_mint_authority_revoked=bool(config.get("require_mint_authority_revoked", True)),
                require_freeze_authority_revoked=bool(config.get("require_freeze_authority_revoked", True)),
            )
        except Exception as exc:
            raise ExecutionError(f"Live token safety checks could not be completed: {type(exc).__name__}.") from exc
        if not risk.passed:
            raise ExecutionError("Token rejected by the safety gate: " + "; ".join(risk.reasons[:5]))

        async with self._quote_lock:
            interval = 1.05 if self.settings.jupiter_api_key else 2.1
            wait = interval - (time.monotonic() - self._last_quote_at)
            if wait > 0:
                await asyncio.sleep(wait)
            quote = await self.executor.quote(SOL_MINT, canonical_mint, amount_lamports)
            self._last_quote_at = time.monotonic()
        expected = int(quote.get("outAmount") or 0)
        minimum = int(quote.get("otherAmountThreshold") or expected)
        if expected <= 0 or minimum <= 0:
            raise ExecutionError("Jupiter did not return a usable buy quote.")
        supply = await self.helius.token_supply(canonical_mint)
        decimals = int(supply["value"]["decimals"])
        units = Decimal(minimum) / (Decimal(10) ** decimals)
        if units <= 0:
            raise ExecutionError("Token quote has no usable minimum output.")
        entry_price_usd = buy_amount * sol_price / units
        return {
            "mint": canonical_mint,
            "buy_amount_sol": str(buy_amount),
            "expected_output_raw": str(expected),
            "minimum_output_raw": str(minimum),
            "price_impact_pct": str(quote.get("priceImpactPct") or "unknown"),
            "entry_price_usd": str(entry_price_usd),
            "liquidity_usd": risk.liquidity_usd,
            "lp_locked_pct": risk.lp_locked_pct,
            "top10_concentration_pct": risk.top10_concentration_pct,
            "developer_concentration_pct": risk.developer_concentration_pct,
            "mint_authority_disabled": risk.mint_authority_disabled,
            "freeze_authority_disabled": risk.freeze_authority_disabled,
            "estimated_required_sol": str(from_lamports(required)),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    async def manual_snipe(self, telegram_id: int, mint: str, amount_sol: str) -> int:
        """Execute a manual live snipe only after a fresh second-stage validation."""
        preview = await self.preview_live_snipe(telegram_id, mint, amount_sol)
        if (datetime.now(timezone.utc) - datetime.fromisoformat(preview["created_at"])).total_seconds() > 30:
            raise ExecutionError("The live snipe preview expired. Run /snipe again.")
        config = await self.db.get_settings(telegram_id)
        config["buy_amount_sol"] = str(preview["buy_amount_sol"])
        candidate = LaunchCandidate(
            mint=preview["mint"],
            launchpad="raydium",
            signature="manual-telegram-snipe",
            slot=None,
            created_at_slot=None,
            developer_buy_sol=None,
            source_logs=("manual-telegram-snipe",),
            bonding_curve=None,
        )
        await self._consider_entry(telegram_id, candidate, config, buy_amount_override_sol=str(preview["buy_amount_sol"]))
        positions = await self.db.active_positions(telegram_id)
        match = next((p for p in positions if str(p.get("mint")) == preview["mint"]), None)
        if not match:
            raise ExecutionError(
                "The live buy was not recorded as an open position. Check the live trade receipt and wallet before retrying."
            )
        return int(match["id"])

    async def start_live_user(self, telegram_id: int) -> dict[str, Any]:
        await self.db.ensure_user(telegram_id)
        readiness = await self.live_readiness(telegram_id)
        if not readiness["ready"]:
            raise ExecutionError("LIVE MODE NOT READY: " + " ".join(readiness["blockers"]))
        await self.db.update_settings(telegram_id, {
            "live_enabled": True,
            "enabled": True,
        })
        self._live_users.add(int(telegram_id))
        self._enabled_users.add(int(telegram_id))
        balance = await self.wallet.balance(telegram_id)
        await self.db.start_daily_risk(telegram_id, utc_day(), balance)
        await self.watch_wallet(telegram_id)
        await self._ensure_scanner()
        for position in await self.db.active_positions(telegram_id):
            position_id = int(position["id"])
            if position_id not in self._position_tasks or self._position_tasks[position_id].done():
                self._position_tasks[position_id] = asyncio.create_task(
                    self._monitor_position(position_id), name=f"9n8-live-position-{position_id}"
                )
        await self.db.security_event(
            telegram_id, "live_engine_armed",
            "User confirmed live mode after global kill switch, wallet, activation, $10 balance and seven paper days passed.",
        )
        return readiness

    async def stop_live_user(self, telegram_id: int) -> None:
        await self.db.update_settings(telegram_id, {"live_enabled": False, "enabled": False})
        self._live_users.discard(int(telegram_id))
        if int(telegram_id) not in self._paper_users:
            self._enabled_users.discard(int(telegram_id))
        await self.db.security_event(
            telegram_id, "live_engine_stopped",
            "New live entries disabled; confirmed on-chain positions remain under exit monitoring.",
        )
        await self.notify(
            telegram_id,
            "LIVE SNIPER PAUSED. No new live entries will be opened. Existing on-chain positions remain under exit monitoring.",
        )
        await self._stop_scanner_if_idle()

    async def start_user(self, telegram_id: int) -> str:
        # Backwards-compatible alias: legacy Execute Bot now means the explicit
        # live path, never the paper practice engine.
        await self.start_live_user(telegram_id)
        return "LIVE Solana sniper armed. Paper account remains separate."

    async def stop_user(self, telegram_id: int) -> None:
        await self.stop_live_user(telegram_id)

    async def _stop_scanner_if_idle(self) -> None:
        if self._enabled_users or self._scanner_task is None:
            return
        await self.sniper.stop()
        scanner = self._scanner_task
        self._scanner_task = None
        if not scanner.done():
            scanner.cancel()
            await asyncio.gather(scanner, return_exceptions=True)

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
        if not self._enabled_users:
            return
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
        self._enabled_users.clear()
        if self.paper_engine is not None:
            await self.paper_engine.shutdown()

    async def _on_candidate(self, candidate: LaunchCandidate) -> None:
        if self._stopping or not self._enabled_users:
            return
        async with self._candidate_lock:
            users = await self.paper_engine.store.users() if self.paper_engine is not None else await self.db.all_users_with_wallets()
            for user in users:
                telegram_id = int(user["telegram_id"])
                source = candidate.launchpad.lower()
                try:
                    # Paper and live are separate account modes with separate settings,
                    # balances, positions and entry gates. They can both observe one event.
                    if self.paper_engine is not None and await self.paper_engine.store.mode_enabled(telegram_id, "paper"):
                        paper_config = await self.paper_engine.store.settings(telegram_id, "paper")
                        paper_source = str(paper_config.get("launchpad", "both")).lower()
                        if paper_source in {"both", source}:
                            await self.paper_engine.on_candidate(telegram_id, candidate)

                    live_mode_on = (
                        await self.paper_engine.store.mode_enabled(telegram_id, "live")
                        if self.paper_engine is not None
                        else bool((await self.db.get_settings(telegram_id)).get("live_enabled", False))
                    )
                    if not self.live_enabled or not live_mode_on:
                        continue
                    user_row = await self.db.get_user(telegram_id)
                    if not user_row or not user_row.get("solana_address"):
                        continue
                    live_config = await self.db.get_settings(telegram_id)
                    live_source = str(live_config.get("launchpad", "both")).lower()
                    if live_source not in {"both", source}:
                        continue
                    await self._consider_entry(telegram_id, candidate, live_config)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Candidate evaluation failed for Telegram account %s", telegram_id)
                    await self.notify(
                        telegram_id,
                        f"9N8 skipped {candidate.mint[:8]}…: {type(exc).__name__}: {str(exc)[:180]}",
                    )


    async def _confirmed_transaction(self, signature: str) -> dict | None:
        """Wait briefly for confirmed RPC metadata; do not substitute quote estimates."""
        for attempt in range(10):
            try:
                transaction = await self.helius.transaction(signature)
                meta = (transaction or {}).get("meta") or {}
                if transaction and meta and meta.get("err") is None:
                    return transaction
            except Exception as exc:
                logger.warning(
                    "Transaction metadata unavailable for %s (attempt %s: %s)",
                    signature, attempt + 1, type(exc).__name__,
                )
            await asyncio.sleep(0.5)
        return None

    @staticmethod
    def _wallet_transaction_delta(transaction: dict, wallet_address: str) -> int | None:
        """Return confirmed wallet lamport delta, excluding rent returned by CloseAccount."""
        meta = transaction.get("meta") or {}
        message = ((transaction.get("transaction") or {}).get("message") or {})
        keys = message.get("accountKeys") or []
        pre = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        wallet_index = None
        key_values: list[str] = []
        for index, item in enumerate(keys):
            value = item.get("pubkey") if isinstance(item, dict) else item
            value = str(value)
            key_values.append(value)
            if value == wallet_address:
                wallet_index = index
        if wallet_index is None or wallet_index >= len(pre) or wallet_index >= len(post):
            return None
        delta = int(post[wallet_index]) - int(pre[wallet_index])
        rent_refunds = 0
        seen_closed_accounts: set[str] = set()
        instructions = list(message.get("instructions") or [])
        for group in meta.get("innerInstructions") or []:
            instructions.extend(group.get("instructions") or [])
        for instruction in instructions:
            parsed = instruction.get("parsed") or {}
            if str(parsed.get("type") or "").lower() not in {"closeaccount", "close_account"}:
                continue
            info = parsed.get("info") or {}
            if str(info.get("destination") or "") != wallet_address:
                continue
            account = str(info.get("account") or "")
            if not account or account in seen_closed_accounts:
                continue
            seen_closed_accounts.add(account)
            try:
                account_index = key_values.index(account)
            except ValueError:
                continue
            if account_index < len(pre) and account_index < len(post):
                account_rent = max(0, int(pre[account_index]) - int(post[account_index]))
                token_row = next(
                    (
                        row for row in (meta.get("preTokenBalances") or [])
                        if int(row.get("accountIndex", -1)) == account_index
                    ),
                    None,
                )
                token_info = (token_row or {}).get("uiTokenAmount") or {}
                if (token_row or {}).get("mint") == SOL_MINT:
                    # Closing a wrapped-SOL account returns both its rent and its
                    # wrapped SOL. Exclude only the rent reserve from the trade delta.
                    wrapped_lamports = int(token_info.get("amount") or 0)
                    rent_refunds += max(0, account_rent - wrapped_lamports)
                else:
                    # A non-WSOL token account contains rent, not spendable SOL.
                    rent_refunds += account_rent
        return delta - rent_refunds

    async def _record_trade_receipt(
        self,
        telegram_id: int,
        event_type: str,
        signature: str,
        status: str,
        *,
        mint: str | None = None,
        position_id: int | None = None,
        amount_lamports: int | None = None,
        token_amount_raw: int | None = None,
        metadata: dict | None = None,
    ) -> None:
        recorder = getattr(self.db, "record_trade_receipt", None)
        if recorder is None:
            return
        await recorder(
            telegram_id,
            event_type,
            signature,
            status,
            mint=mint,
            position_id=position_id,
            amount_lamports=amount_lamports,
            token_amount_raw=token_amount_raw,
            metadata=metadata or {},
        )

    async def _consider_entry(
        self, telegram_id: int, candidate: LaunchCandidate, config: dict,
        buy_amount_override_sol: str | None = None,
    ) -> None:
        if not self.live_enabled or not bool(config.get("live_enabled", config.get("enabled", False))):
            return
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
        security_launchpad = candidate.launchpad.lower()
        if candidate.launchpad.lower() == "pump.fun":
            curve = candidate.bonding_curve
            # Live trades wait for Pump.fun migration. The pre-migration bonding
            # curve is not treated as a locked Raydium liquidity pool.
            if not curve or not curve.complete:
                return
            threshold = dec(config.get("pump_migration_progress_pct", "80"))
            if curve.progress_pct < threshold:
                return
            security_launchpad = "raydium"
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
        sol_price = await self.oracle.sol_usd()
        min_liquidity_usd = max(
            dec(config.get("min_liquidity_sol", "5")) * sol_price,
            dec(config.get("min_liquidity_usd", "0")),
        )
        risk = await self.security.inspect(
            candidate.mint,
            launchpad=security_launchpad,
            max_top10_pct=str(config.get("max_top10_concentration_pct", "25")),
            min_liquidity_usd=str(min_liquidity_usd),
            min_lp_locked_pct=str(config.get("min_lp_locked_pct", "80")),
            max_dev_pct=str(config.get("max_dev_wallet_pct", "5")),
            require_mint_authority_revoked=bool(config.get("require_mint_authority_revoked", True)),
            require_freeze_authority_revoked=bool(config.get("require_freeze_authority_revoked", True)),
            excluded_token_accounts=excluded_token_accounts,
        )
        if not risk.passed:
            await self.notify(
                telegram_id,
                f"SECURITY FILTER REJECTED {candidate.mint[:8]}…\n"
                + "\n".join(f"• {reason}" for reason in risk.reasons[:5]),
            )
            return

        amount_sol = dec(
            buy_amount_override_sol
            if buy_amount_override_sol is not None
            else config.get("buy_amount_sol", self.settings.default_buy_sol)
        )
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

        # Re-read the operator/user switches immediately before building the
        # signed transaction. The scanner and manual Telegram paths share this gate.
        latest_config = await self.db.get_settings(telegram_id)
        if not self.live_enabled or not bool(latest_config.get("live_enabled", latest_config.get("enabled", False))):
            raise ExecutionError("Live mode was switched off before the buy could be signed.")
        config = latest_config
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
        # Reconcile both transactions in a Jito bundle. The tip is a separate
        # transaction and its network fee is part of the user's actual cost basis.
        tx_data = await self._confirmed_transaction(result.signature)
        tip_tx_data = (
            await self._confirmed_transaction(result.tip_signature)
            if result.tip_signature else None
        )
        tx_meta = (tx_data or {}).get("meta") or {}
        network_fee = int(tx_meta.get("fee") or 5000)
        tip_network_fee = int(((tip_tx_data or {}).get("meta") or {}).get("fee") or 0)
        transaction_receipts_available = bool(
            tx_data and (not result.tip_signature or tip_tx_data)
        )
        wallet_address = str(wallet_key.pubkey())
        token_amount = 0
        token_decimals: int | None = None
        try:
            token_amount = await self.helius.token_balance_raw(wallet_address, candidate.mint)
        except Exception:
            logger.warning("Post-swap token balance query failed for %s", candidate.mint)
        if token_amount <= 0:
            for balance_row in tx_meta.get("postTokenBalances") or []:
                if balance_row.get("mint") != candidate.mint:
                    continue
                if balance_row.get("owner") not in {None, wallet_address}:
                    continue
                amount_info = balance_row.get("uiTokenAmount") or {}
                token_amount += int(amount_info.get("amount") or 0)
                if amount_info.get("decimals") is not None:
                    token_decimals = int(amount_info["decimals"])
        if token_amount <= 0:
            for _attempt in range(6):
                await asyncio.sleep(0.5)
                try:
                    token_amount = await self.helius.token_balance_raw(wallet_address, candidate.mint)
                except Exception:
                    token_amount = 0
                if token_amount > 0:
                    break
        token_amount_provisional = False
        if token_amount <= 0:
            # Keep a provisional record so the confirmed swap is not forgotten;
            # mark the receipt unknown and pause further entries until reconciled.
            token_amount = int(result.output_amount)
            token_amount_provisional = True
            await self.notify(
                telegram_id,
                f"WARNING: swap {result.signature} is confirmed, but Helius has not indexed the token account yet. "
                "9N8 is recording the route output provisionally and will keep checking the wallet. "
                "Verify the fill on Solscan before submitting another trade.",
            )
        receipt_status = (
            "confirmed"
            if transaction_receipts_available and not token_amount_provisional
            else "unknown"
        )
        if token_decimals is None:
            try:
                supply = await self.helius.token_supply(candidate.mint)
                token_decimals = int(supply["value"]["decimals"])
            except Exception:
                try:
                    mint_info = await self.helius.account_info(candidate.mint, "base64")
                    mint_raw = base64.b64decode(mint_info["data"][0], validate=True)
                    token_decimals = int(mint_raw[44])
                except Exception:
                    token_decimals = None
        entry_price = Decimal(0)
        if token_decimals is not None and token_decimals >= 0:
            try:
                sol_usd = await self.oracle.sol_usd()
                token_units = Decimal(token_amount) / (Decimal(10) ** token_decimals)
                if token_units > 0:
                    entry_price = amount_sol * sol_usd / token_units
            except Exception:
                logger.warning("Entry valuation unavailable for confirmed swap %s", result.signature)
        total_basis = trade_lamports + network_fee + jito_tip + tip_network_fee
        position_id = await self.db.create_position(
            telegram_id, candidate.mint, token_amount, total_basis,
            str(entry_price), result.signature,
        )
        await self.db.record_daily_entry(telegram_id, today)
        await self._record_trade_receipt(
            telegram_id, "buy", result.signature, receipt_status,
            mint=candidate.mint,
            position_id=position_id,
            amount_lamports=total_basis,
            token_amount_raw=token_amount,
            metadata={
                "launchpad": candidate.launchpad,
                "expected_output_raw": str(result.output_amount),
                "minimum_output_raw": str(result.minimum_output),
                "token_amount_source": "provisional_jupiter_quote" if token_amount_provisional else "helius_or_transaction",
                "network_fee_lamports": network_fee,
                "jito_tip_lamports": jito_tip,
                "tip_network_fee_lamports": tip_network_fee,
                "bundle_id": result.bundle_id,
                "tip_signature": result.tip_signature,
                "receipt_reconciled": receipt_status == "confirmed",
            },
        )
        if result.tip_signature:
            await self._record_trade_receipt(
                telegram_id, "fee", result.tip_signature, receipt_status,
                mint=candidate.mint,
                position_id=position_id,
                amount_lamports=jito_tip + tip_network_fee,
                metadata={"kind": "jito_tip", "bundle_id": result.bundle_id},
            )
        if receipt_status != "confirmed":
            await self.db.update_settings(telegram_id, {"enabled": False})
            await self.notify(
                telegram_id,
                "WARNING: the buy was confirmed but 9N8 could not reconcile all transaction receipts. "
                "The position is being monitored, new entries are paused, and you must verify the Solscan receipt before resuming.",
            )
        self._position_tasks[position_id] = asyncio.create_task(
            self._monitor_position(position_id), name=f"9n8-position-{position_id}"
        )
        await self.notify(
            telegram_id,
            f"ENTRY CONFIRMED · {candidate.launchpad}\n"
            f"Mint: {candidate.mint}\n"
            f"Position: #{position_id}\n"
            f"Spend: {from_lamports(trade_lamports)} SOL\n"
            f"Tokens: {token_amount} raw units\nEntry: {entry_price:.12f} USD\n"
            f"Safety: mint revoked={risk.mint_authority_disabled}, freeze revoked={risk.freeze_authority_disabled}, "
            f"LP locked={risk.lp_locked_pct}%, top-10={risk.top10_concentration_pct}%, dev={risk.developer_concentration_pct}%\n"
            f"Exit rules: TP1 +{config.get('take_profit_pct','100')}% sell 50%; "
            f"trailing {config.get('trailing_stop_pct','20')}%; hard stop -{config.get('stop_loss_pct','30')}%\n"
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
                try:
                    position = await self.db.get_position(position_id)
                    if not position or position["status"] != "open":
                        return
                    telegram_id = int(position["telegram_id"])
                    config = await self.db.get_settings(telegram_id)
                    mint = str(position["mint"])
                    wallet_address = await self.db.get_wallet_address(telegram_id)
                    if not wallet_address:
                        await self.notify(telegram_id, "Live position monitoring paused: wallet address is unavailable. No exit was fabricated.")
                        return
                    amount = await self.helius.token_balance_raw(wallet_address, mint)
                    if amount <= 0:
                        # Confirm the zero balance on repeated indexer reads before
                        # closing the database position.
                        for _attempt in range(5):
                            await asyncio.sleep(1)
                            amount = await self.helius.token_balance_raw(wallet_address, mint)
                            if amount > 0:
                                break
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
                    if basis_lamports <= 0:
                        await asyncio.sleep(1)
                        continue
                    estimated_exit_cost = (
                        int(config.get("priority_fee_lamports", 10000))
                        + (int(config.get("jito_tip_lamports", 2_000_000)) if config.get("mev_shield", True) else 0)
                        + 5000
                    )
                    current_value_lamports = max(0, min_out - estimated_exit_cost)
                    profit = net_profit_pct(
                        from_lamports(current_value_lamports),
                        from_lamports(basis_lamports),
                    )
                    peak_value = max(
                        int(position.get("peak_value_lamports") or 0),
                        current_value_lamports,
                    )
                    await self.db.update_position_metrics(
                        position_id, current_value_lamports, peak_value,
                        str(profit.quantize(Decimal("0.01"))),
                    )

                    opened = datetime.fromisoformat(str(position["opened_at"]))
                    if opened.tzinfo is None:
                        opened = opened.replace(tzinfo=timezone.utc)
                    elapsed = Decimal(str((datetime.now(timezone.utc) - opened).total_seconds()))
                    tp1_done = bool(position.get("tp1_done")) or position.get("exit_reason") in {
                        "take_profit_partial", "take_profit_1"
                    }
                    tp2_done = bool(position.get("tp2_done"))
                    reason = None

                    # The hard stop is always evaluated before profit targets.
                    if profit <= -dec(config.get("stop_loss_pct", "30")):
                        reason = "hard_stop"
                    elif not tp1_done and profit >= dec(config.get("take_profit_pct", "100")):
                        reason = "take_profit_1"
                    elif tp1_done and not tp2_done and profit >= dec(config.get("take_profit_2_pct", "300")):
                        reason = "take_profit_2"
                    elif tp1_done and peak_value > 0 and Decimal(current_value_lamports) <= (
                        Decimal(peak_value)
                        * (Decimal(1) - dec(config.get("trailing_stop_pct", "20")) / Decimal(100))
                    ):
                        reason = "trailing_stop"
                    elif elapsed >= Decimal(int(config.get("max_hold_seconds", 14400))):
                        reason = "max_hold"

                    if reason:
                        await self._execute_exit(
                            position_id, reason, amount, int(position["token_amount_raw"]), config
                        )
                        refreshed = await self.db.get_position(position_id)
                        if not refreshed or refreshed["status"] != "open":
                            return

                    if self.telemetry:
                        await self.telemetry(telegram_id, {
                            "type": "live_position_update", "position_id": position_id,
                            "mint": mint, "pnl_pct": str(profit.quantize(Decimal("0.01"))),
                            "current_value_sol": str(from_lamports(current_value_lamports)),
                            "peak_value_sol": str(from_lamports(peak_value)),
                            "tp1_done": tp1_done or reason == "take_profit_1",
                            "tp2_done": tp2_done or reason == "take_profit_2",
                            "elapsed_seconds": int(elapsed),
                            "max_hold_seconds": int(config.get("max_hold_seconds", 14400)),
                        })
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Live position monitor iteration failed: %s", position_id)
                    position = await self.db.get_position(position_id)
                    if position:
                        await self.notify(
                            int(position["telegram_id"]),
                            f"Live position monitor temporarily unavailable for {position['mint'][:8]}…: "
                            f"{type(exc).__name__}. The position remains open; verify wallet and prices. "
                            "The monitor will retry.",
                        )
                    await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        finally:
            self._position_tasks.pop(position_id, None)

    async def _execute_exit(
        self, position_id: int, reason: str, actual_amount: int,
        originally_recorded_amount: int, config: dict,
    ) -> None:
        lock = self._exit_locks.setdefault(int(position_id), asyncio.Lock())
        async with lock:
            position = await self.db.get_position(position_id)
            if not position or position.get("status") != "open":
                return
            await self._execute_exit_once(
                position_id, reason, actual_amount, originally_recorded_amount, config
            )

    async def _execute_exit_once(
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
        partial = reason in {"take_profit_1", "take_profit_2", "manual_sell_50"}
        if reason == "take_profit_1":
            # First target: return half the position and keep the other half running.
            sell_amount, _retained = moon_bag_split(
                current_balance, config.get("moon_bag_pct", "50")
            )
        elif reason == "take_profit_2":
            # Second target: sell 25% of the remaining bag.
            sell_fraction = dec(config.get("take_profit_2_sell_pct", "25")) / Decimal(100)
            sell_amount = int((Decimal(current_balance) * sell_fraction).to_integral_value(rounding="ROUND_DOWN"))
        elif reason == "manual_sell_50":
            sell_amount = max(1, current_balance // 2)
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
            slippage_bps=int(dec(config.get("sell_slippage_pct", config.get("slippage_pct", "5"))) * Decimal(100)),
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
        # The actual SOL change is taken from confirmed transaction metadata,
        # including the separate Jito tip transaction. Wallet deltas already
        # include network fees and the treasury transfer; do not subtract them twice.
        swap_tx_data = await self._confirmed_transaction(result.signature)
        tip_tx_data = (
            await self._confirmed_transaction(result.tip_signature)
            if result.tip_signature else None
        )
        swap_delta = (
            self._wallet_transaction_delta(swap_tx_data, str(wallet_key.pubkey()))
            if swap_tx_data else None
        )
        tip_delta = (
            self._wallet_transaction_delta(tip_tx_data, str(wallet_key.pubkey()))
            if tip_tx_data else (0 if not result.tip_signature else None)
        )
        remaining = await self.helius.token_balance_raw(str(wallet_key.pubkey()), mint)
        if remaining <= 0:
            for _attempt in range(5):
                await asyncio.sleep(0.5)
                remaining = await self.helius.token_balance_raw(str(wallet_key.pubkey()), mint)
                if remaining > 0:
                    break
        fully_closed = remaining == 0
        if swap_delta is None or tip_delta is None:
            await self._record_trade_receipt(
                telegram_id, "sell", result.signature, "unknown",
                mint=mint, position_id=position_id,
                token_amount_raw=sell_amount,
                metadata={
                    "reason": reason,
                    "expected_output_raw": str(expected_out),
                    "minimum_output_raw": str(min_out),
                    "basis_sold_lamports": basis_sold,
                    "treasury_fee_lamports": treasury_lamports,
                    "tip_signature": result.tip_signature,
                    "bundle_id": result.bundle_id,
                    "remaining_token_amount_raw": remaining,
                    "error": "Confirmed transaction metadata unavailable; wallet delta not reconciled.",
                },
            )
            await self.db.close_position(
                position_id, remaining,
                "exit_pnl_unknown",
                0, result.signature, fully_closed,
            )
            await self.db.update_settings(telegram_id, {"enabled": False})
            await self.notify(
                telegram_id,
                f"EXIT SUBMITTED · {reason.upper()}\\n"
                f"Mint: {mint}\\nSignature: https://solscan.io/tx/{result.signature}\\n"
                "The transaction is reported landed, but exact wallet-delta metadata is unavailable. "
                "The exit PnL is marked unknown, new entries are paused, and the position requires reconciliation before trading resumes.",
            )
            return

        actual_wallet_delta = int(swap_delta) + int(tip_delta)
        net_realized_lamports = actual_wallet_delta - basis_sold
        await self._record_trade_receipt(
            telegram_id, "sell", result.signature, "confirmed",
            mint=mint, position_id=position_id,
            amount_lamports=actual_wallet_delta,
            token_amount_raw=sell_amount,
            metadata={
                "reason": reason,
                "expected_output_raw": str(expected_out),
                "minimum_output_raw": str(min_out),
                "actual_wallet_delta_lamports": actual_wallet_delta,
                "basis_sold_lamports": basis_sold,
                "net_realized_pnl_lamports": net_realized_lamports,
                "treasury_fee_lamports": treasury_lamports,
                "tip_signature": result.tip_signature,
                "bundle_id": result.bundle_id,
                "remaining_token_amount_raw": remaining,
            },
        )
        if result.tip_signature:
            await self._record_trade_receipt(
                telegram_id, "fee", result.tip_signature, "confirmed",
                mint=mint, position_id=position_id,
                amount_lamports=jito_tip,
                metadata={"kind": "jito_tip", "bundle_id": result.bundle_id},
            )
        await self.db.close_position(
            position_id, remaining,
            reason,
            net_realized_lamports, result.signature, fully_closed,
        )
        await self.db.record_daily_pnl(telegram_id, utc_day(), net_realized_lamports)
        rent_reclaimed = await self._reclaim_empty_accounts(telegram_id, mint, fully_closed)
        await self.notify(
            telegram_id,
            f"EXIT CONFIRMED · {reason.upper()}\n"
            f"Mint: {mint}\nSold: {sell_amount} raw units\n"
            f"Expected SOL out: {from_lamports(expected_out)}\n"
            f"Observed wallet delta: {from_lamports(actual_wallet_delta)} SOL\n"
            f"Net realized PnL: {from_lamports(net_realized_lamports)} SOL\n"
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
    async def sell_position_percent(self, telegram_id: int, position_id: int, percent: int) -> None:
        if percent not in {50, 100}:
            raise ValueError("Supported manual sell percentages are 50% and 100%.")
        if percent == 100:
            await self.panic_sell(telegram_id, position_id)
            return
        position = await self.db.get_position(position_id, telegram_id)
        if not position or position.get("status") != "open":
            raise ValueError("Open live position not found.")
        config = await self.db.get_settings(telegram_id)
        address = await self.db.get_wallet_address(telegram_id)
        if not address:
            raise WalletError("No active wallet.")
        actual_balance = await self.helius.token_balance_raw(address, str(position["mint"]))
        if actual_balance <= 1:
            raise ValueError("There is not enough token balance for a partial sell.")
        await self._execute_exit(
            position_id, "manual_sell_50", actual_balance,
            int(position["token_amount_raw"]), config,
        )
