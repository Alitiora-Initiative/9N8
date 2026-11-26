from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, WebAppInfo,
)
from solders.pubkey import Pubkey

from .config import Settings
from .database import Database
from .math_engine import format_sol, to_lamports, usd_to_lamports
from .oracle import PriceOracle
from .wallet import WalletError, WalletManager
from .autonomous_engine import AutonomousEngine

logger = logging.getLogger(__name__)
router = Router()


class ImportWalletFlow(StatesGroup):
    secret = State()


class ParameterFlow(StatesGroup):
    value = State()


class WithdrawFlow(StatesGroup):
    amount = State()
    destination = State()


class SnipeFlow(StatesGroup):
    mint = State()


class LiveSnipeFlow(StatesGroup):
    mint = State()
    amount = State()


class WipeFlow(StatesGroup):
    destination = State()


class AppServices:
    def __init__(
        self, settings: Settings, db: Database, wallet: WalletManager,
        oracle: PriceOracle, engine: AutonomousEngine,
    ):
        self.settings, self.db, self.wallet = settings, db, wallet
        self.oracle, self.engine = oracle, engine


services: AppServices | None = None
_pending_paper_snipes: dict[int, dict] = {}
_pending_live_snipes: dict[int, dict] = {}
_withdraw_locks: dict[int, asyncio.Lock] = {}


def configure_handlers(value: AppServices) -> None:
    global services
    services = value
    # This deployment is intentionally single-operator. Filter every message
    # and callback at the Router boundary, not only individual commands.
    if not getattr(router, "_n9n8_authorized_filter_installed", False):
        router.message.filter(F.from_user.id == value.settings.authorized_user_id)
        router.callback_query.filter(F.from_user.id == value.settings.authorized_user_id)
        router._n9n8_authorized_filter_installed = True


def _svc() -> AppServices:
    if services is None:
        raise RuntimeError("Telegram handlers have not been configured.")
    return services


def _is_private(message: Message) -> bool:
    return message.chat.type == "private"


def _home_keyboard(webapp_url: str = "") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if webapp_url.startswith("https://"):
        rows.append([InlineKeyboardButton(
            text="Open 9N8 Control Panel",
            web_app=WebAppInfo(url=webapp_url),
        )])
    rows.extend([
        [
            InlineKeyboardButton(text="🧪 Paper Trading", callback_data="paper:menu"),
            InlineKeyboardButton(text="🔴 Live Solana Sniper", callback_data="live:menu"),
        ],
        [InlineKeyboardButton(text="🎯 Manual Live Snipe", callback_data="live:snipe")],
        [
            InlineKeyboardButton(text="📊 Positions", callback_data="positions:show"),
            InlineKeyboardButton(text="📜 Trade History", callback_data="history:show"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Paper Settings", callback_data="paper:settings"),
            InlineKeyboardButton(text="🛡️ Safety Filters", callback_data="safety:show"),
        ],
        [
            InlineKeyboardButton(text="Create Wallet", callback_data="wallet:create"),
            InlineKeyboardButton(text="Import Wallet", callback_data="wallet:import"),
        ],
        [
            InlineKeyboardButton(text="Deposit SOL", callback_data="deposit:show"),
            InlineKeyboardButton(text="Withdraw SOL", callback_data="wallet:exit"),
        ],
        [InlineKeyboardButton(text="Refresh Dashboard", callback_data="status:show")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _mode_keyboard(mode: str, enabled: bool) -> InlineKeyboardMarkup:
    if mode == "paper":
        rows = [
            [InlineKeyboardButton(
                text="⏸ Stop Paper Entries" if enabled else "▶ Start Paper Trading",
                callback_data="paper:stop" if enabled else "paper:start",
            )],
            [
                InlineKeyboardButton(text="Open Paper Positions", callback_data="positions:paper"),
                InlineKeyboardButton(text="Paper Settings", callback_data="paper:settings"),
            ],
            [
                InlineKeyboardButton(text="Manual Paper Snipe", callback_data="paper:snipe"),
                InlineKeyboardButton(text="Paper History", callback_data="history:paper"),
            ],
            [InlineKeyboardButton(text="Back to Dashboard", callback_data="home:show")],
        ]
    else:
        rows = [
            [InlineKeyboardButton(
                text="🔴 Stop Live Entries" if enabled else "Check Live Readiness",
                callback_data="live:stop" if enabled else "live:check",
            )],
            [
                InlineKeyboardButton(text="Live Positions", callback_data="positions:live"),
                InlineKeyboardButton(text="Live Settings", callback_data="settings:show"),
            ],
            [
                InlineKeyboardButton(text="Wallet Balance", callback_data="deposit:show"),
                InlineKeyboardButton(text="Live History", callback_data="history:live"),
            ],
            [InlineKeyboardButton(text="Back to Dashboard", callback_data="home:show")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _settings_keyboard(config: dict) -> InlineKeyboardMarkup:
    launchpad = str(config.get("launchpad", "both"))
    pump_mode = str(config.get("pump_mode", "fresh"))
    mev = "ON" if config.get("mev_shield", True) else "OFF"
    rows = [
        [
            InlineKeyboardButton(text="Pump.fun", callback_data="settings:launchpad:pump.fun"),
            InlineKeyboardButton(text="Raydium", callback_data="settings:launchpad:raydium"),
            InlineKeyboardButton(text="Both", callback_data="settings:launchpad:both"),
        ],
        [
            InlineKeyboardButton(text="Fresh Launch", callback_data="settings:pumpmode:fresh"),
            InlineKeyboardButton(text="Migration", callback_data="settings:pumpmode:migration"),
        ],
        [
            InlineKeyboardButton(text=f"MEV Shield: {mev}", callback_data="settings:toggle:mev_shield"),
        ],
        [
            InlineKeyboardButton(text=f"Buy SOL: {config['buy_amount_sol']}", callback_data="settings:field:buy_amount_sol"),
            InlineKeyboardButton(text=f"Slippage: {config['slippage_pct']}%", callback_data="settings:field:slippage_pct"),
        ],
        [
            InlineKeyboardButton(text=f"Priority μLamports: {config['priority_fee_lamports']}", callback_data="settings:field:priority_fee_lamports"),
            InlineKeyboardButton(text=f"Jito Tip: {format_sol(int(config['jito_tip_lamports']))} SOL", callback_data="settings:field:jito_tip_lamports"),
        ],
        [
            InlineKeyboardButton(text=f"Take Profit: {config['take_profit_pct']}%", callback_data="settings:field:take_profit_pct"),
            InlineKeyboardButton(text=f"Stop Loss: {config['stop_loss_pct']}%", callback_data="settings:field:stop_loss_pct"),
        ],
        [
            InlineKeyboardButton(text=f"Max Hold: {config['max_hold_seconds']}s", callback_data="settings:field:max_hold_seconds"),
            InlineKeyboardButton(text=f"Moon Bag: {config['moon_bag_pct']}%", callback_data="settings:field:moon_bag_pct"),
        ],
        [
            InlineKeyboardButton(text=f"Daily Trades: {config['daily_position_limit']}", callback_data="settings:field:daily_position_limit"),
            InlineKeyboardButton(text=f"Daily Drawdown: {config['max_daily_drawdown_pct']}%", callback_data="settings:field:max_daily_drawdown_pct"),
        ],
        [
            InlineKeyboardButton(text=f"Migration Trigger: {config.get('pump_migration_progress_pct', '80')}%", callback_data="settings:field:pump_migration_progress_pct"),
            InlineKeyboardButton(text=f"Min Liquidity USD: {config.get('min_liquidity_usd', '5000')}", callback_data="settings:field:min_liquidity_usd"),
        ],
        [
            InlineKeyboardButton(text=f"Top-10 Cap: {config['max_top10_concentration_pct']}%", callback_data="settings:field:max_top10_concentration_pct"),
        ],
        [InlineKeyboardButton(text="Back to Dashboard", callback_data="home:show")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _fees_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Change MEV / Risk Settings", callback_data="settings:risk")],
        [InlineKeyboardButton(text="Back to Dashboard", callback_data="home:show")],
    ])


async def _delete_after(bot: Bot, chat_id: int, message_id: int, delay: int = 60) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        logger.info("Sensitive Telegram message could not be auto-deleted (message id %s).", message_id)


async def _send_sensitive_key(message: Message, address: str, secret: str) -> None:
    content = (
        "NEW 9N8 SOLANA WALLET\n\n"
        f"Deposit address:\n{address}\n\n"
        f"RECOVERY PRIVATE KEY (base58):\n{secret}\n\n"
        "Save this key offline now. Anyone with it can take every asset in this wallet. "
        "9N8 will delete this Telegram message after 60 seconds, but message deletion "
        "cannot remove screenshots, notifications, backups or copies. Never share this key."
    )
    sent = await message.answer(content)
    asyncio.create_task(_delete_after(message.bot, sent.chat.id, sent.message_id, 60))


async def _dashboard_text(telegram_id: int) -> str:
    svc = _svc()
    await svc.db.ensure_user(telegram_id)
    user = await svc.db.get_user(telegram_id)
    live_config = await svc.db.get_settings(telegram_id)
    address = (user or {}).get("solana_address")
    live_balance = "Wallet not created"
    live_balance_usd = "—"
    rpc_latency = "not measured"
    balance_lamports = 0
    if address:
        try:
            started = time.perf_counter()
            balance_lamports = await svc.wallet.balance(telegram_id)
            rpc_latency = f"{(time.perf_counter() - started) * 1000:.0f} ms"
            live_balance = f"{format_sol(balance_lamports)} SOL"
            sol_usd = await svc.oracle.sol_usd()
            live_balance_usd = f"${(Decimal(format_sol(balance_lamports)) * sol_usd):.2f}"
        except Exception:
            live_balance = "Balance temporarily unavailable"

    paper_engine = svc.engine.paper_engine
    paper_account = await paper_engine.store.account(telegram_id) if paper_engine else None
    paper_on = await paper_engine.store.mode_enabled(telegram_id, "paper") if paper_engine else False
    live_on = await paper_engine.store.mode_enabled(telegram_id, "live") if paper_engine else bool(live_config.get("live_enabled",False))
    paper_days = await paper_engine.store.active_days(telegram_id) if paper_engine else 0
    paper_balance = (
        f"{format_sol(int(paper_account['cash_balance_lamports']))} virtual SOL"
        if paper_account else "Not started · $1,000 virtual value"
    )
    activation = "PAID" if (user or {}).get("activation_paid") else "NOT PAID"
    live_positions = await svc.db.active_positions(telegram_id)
    unrealized = sum(
        int(p.get("last_value_lamports") or 0) - int(p.get("initial_cost_lamports") or 0)
        for p in live_positions if int(p.get("last_value_lamports") or 0) > 0
    )
    scanner_task = getattr(svc.engine, "_scanner_task", None)
    scanner_status = "RUNNING" if scanner_task and not scanner_task.done() else "IDLE"
    return (
        "⚡ 9N8 · SOLANA TRADING CONTROL\n\n"
        "TWO SEPARATE MODES\n"
        f"🧪 Paper Trading: {'RUNNING' if paper_on else 'PAUSED'}\n"
        f"Virtual cash: {paper_balance}\n"
        f"Practice days: {paper_days}/7\n"
        f"🔴 Live Solana Sniper: {'ARMED' if live_on else 'LOCKED / OFF'}\n"
        f"Market listener: {scanner_status}\n"
        f"Live positions: {len(live_positions)}\n"
        f"Marked unrealized PnL: {format_sol(unrealized)} SOL\n\n"
        f"Live wallet: {live_balance} · {live_balance_usd}\n"
        f"Wallet RPC response: {rpc_latency}\n"
        f"Deposit address: {address or 'Create a wallet to receive SOL'}\n"
        f"Live activation: {activation}\n"
        f"Operator live switch: {'ON' if svc.settings.live_trading_enabled else 'OFF'}\n\n"
        "Paper trades use real market quotes but virtual funds only. Live mode uses your wallet "
        "and needs a separate readiness check and your confirmation."
    )


def _paper_menu_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    action = "paper:stop" if enabled else "paper:start"
    label = "⏸ Stop Paper Entries" if enabled else "▶ Start Paper Trading"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=action)],
        [InlineKeyboardButton(text="Paper Positions", callback_data="positions:paper"),
         InlineKeyboardButton(text="Paper Settings", callback_data="paper:settings")],
        [InlineKeyboardButton(text="Manual Paper Snipe", callback_data="paper:snipe"),
         InlineKeyboardButton(text="Paper History", callback_data="history:paper")],
        [InlineKeyboardButton(text="Back to Dashboard", callback_data="home:show")],
    ])


def _live_menu_keyboard(enabled: bool, ready: bool) -> InlineKeyboardMarkup:
    rows = []
    if enabled:
        rows.append([InlineKeyboardButton(text="⏸ Stop Live Entries", callback_data="live:stop")])
    elif ready:
        rows.append([InlineKeyboardButton(text="Confirm Start Live Sniper", callback_data="live:confirm")])
    rows.extend([
        [InlineKeyboardButton(text="Refresh Readiness", callback_data="live:check")],
        [InlineKeyboardButton(text="🎯 Manual Snipe", callback_data="live:snipe"),
         InlineKeyboardButton(text="📊 Live Positions", callback_data="positions:live")],
        [InlineKeyboardButton(text="⚙️ Live Settings", callback_data="settings:show"),
         InlineKeyboardButton(text="🛡️ Live Safety", callback_data="safety:live")],
        [InlineKeyboardButton(text="Back to Dashboard", callback_data="home:show")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _paper_settings_keyboard(config: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"Buy {config.get('buy_amount_sol','0.05')} SOL", callback_data="paper:field:buy_amount_sol"),
         InlineKeyboardButton(text=f"Slippage {config.get('slippage_pct','15')}%", callback_data="paper:field:slippage_pct")],
        [InlineKeyboardButton(text=f"TP1 +{config.get('take_profit_pct','100')}%", callback_data="paper:field:take_profit_pct"),
         InlineKeyboardButton(text=f"TP2 +{config.get('take_profit_2_pct','300')}%", callback_data="paper:field:take_profit_2_pct")],
        [InlineKeyboardButton(text=f"TP2 sells {config.get('take_profit_2_sell_pct','25')}%", callback_data="paper:field:take_profit_2_sell_pct"),
         InlineKeyboardButton(text=f"Trail {config.get('trailing_stop_pct','20')}%", callback_data="paper:field:trailing_stop_pct")],
        [InlineKeyboardButton(text=f"Hard stop {config.get('stop_loss_pct','30')}%", callback_data="paper:field:stop_loss_pct"),
         InlineKeyboardButton(text=f"Hold {int(config.get('max_hold_seconds',14400))//3600}h", callback_data="paper:field:max_hold_seconds")],
        [InlineKeyboardButton(text=f"Min liquidity {config.get('min_liquidity_sol','5')} SOL", callback_data="paper:field:min_liquidity_sol"),
         InlineKeyboardButton(text=f"LP locked {config.get('min_lp_locked_pct','80')}%", callback_data="paper:field:min_lp_locked_pct")],
        [InlineKeyboardButton(text=f"Top 10 ≤ {config.get('max_top10_concentration_pct','25')}%", callback_data="paper:field:max_top10_concentration_pct"),
         InlineKeyboardButton(text=f"Dev ≤ {config.get('max_dev_wallet_pct','5')}%", callback_data="paper:field:max_dev_wallet_pct")],
        [InlineKeyboardButton(text="Toggle virtual MEV cost", callback_data="paper:mev")],
        [InlineKeyboardButton(text="Back to Paper Menu", callback_data="paper:menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _paper_menu_text(telegram_id: int) -> str:
    svc = _svc()
    paper = svc.engine.paper_engine
    if paper is None:
        return "PAPER TRADING IS NOT CONFIGURED."
    account = await paper.store.account(telegram_id)
    enabled = await paper.store.mode_enabled(telegram_id, "paper")
    days = await paper.store.active_days(telegram_id)
    positions = await paper.store.positions(telegram_id)
    config = await paper.store.settings(telegram_id, "paper")
    cash = int(account["cash_balance_lamports"]) if account else 0
    realized = int(account["realized_pnl_lamports"]) if account else 0
    unrealized = sum(int(p.get("last_value_lamports") or 0) - int(p.get("initial_cost_lamports") or 0) for p in positions)
    return (
        "🧪 9N8 PAPER TRADING\n\n"
        f"Status: {'RUNNING · VIRTUAL FUNDS ONLY' if enabled else 'PAUSED'}\n"
        f"Virtual cash: {format_sol(cash)} SOL\n"
        f"Open practice positions: {len(positions)}\n"
        f"Realized practice PnL: {format_sol(realized)} SOL\n"
        f"Marked unrealized PnL: {format_sol(unrealized)} SOL\n"
        f"Practice days recorded: {days}/7\n\n"
        f"Buy amount: {config.get('buy_amount_sol','0.05')} SOL\n"
        f"Liquidity minimum: {config.get('min_liquidity_sol','5')} SOL\n"
        f"TP1 +{config.get('take_profit_pct','100')}% · TP2 +{config.get('take_profit_2_pct','300')}%\n"
        f"Trail {config.get('trailing_stop_pct','20')}% · Hard stop -{config.get('stop_loss_pct','30')}%\n\n"
        "Paper trades use real market quotes and virtual balances. No wallet signing, real Jito tip or on-chain transaction is sent."
    )


async def _live_menu_text(telegram_id: int) -> str:
    svc = _svc()
    readiness = await svc.engine.live_readiness(telegram_id)
    active = await svc.engine.paper_engine.store.mode_enabled(telegram_id, "live") if svc.engine.paper_engine else False
    lines = [
        "🔴 9N8 LIVE SOLANA SNIPER", "",
        "Status: " + ("LIVE ENTRIES ARMED" if active else "OFF"),
        "Operator live switch: " + ("ON" if readiness["operator_live_switch"] else "OFF"),
        "Wallet: " + ("connected" if readiness["wallet_connected"] else "not connected"),
        "Activation: " + ("confirmed" if readiness["activation_confirmed"] else "not confirmed"),
        f"Paper days: {readiness['paper_days']}/7",
        f"Wallet balance: {readiness['wallet_balance_sol']} SOL (approximately {readiness['wallet_balance_usd']} USD)",
        "Minimum wallet value: $10", "",
        "READINESS: " + ("PASSED" if readiness["ready"] else "BLOCKED"),
    ]
    lines.extend(["• " + str(item) for item in readiness["blockers"]])
    lines.extend(["", "Live mode uses real wallet funds. A token can lose its full value. Starting Live requires a separate confirmation."])
    return "\n".join(lines)


async def _positions_text(telegram_id: int) -> str:
    svc = _svc()
    paper_positions = await svc.engine.paper_engine.store.positions(telegram_id) if svc.engine.paper_engine else []
    live_positions = await svc.db.active_positions(telegram_id)
    lines = ["📊 POSITIONS · TWO SEPARATE BOOKS", "", "PAPER (VIRTUAL)"]
    if not paper_positions:
        lines.append("No open paper positions.")
    for p in paper_positions[:10]:
        lines.append(f"• {str(p['mint'])[:8]}… · {p.get('last_pnl_pct','0')}% · {p['token_amount_raw']} raw")
    lines.extend(["", "LIVE (REAL WALLET)"])
    if not live_positions:
        lines.append("No open live positions.")
    for p in live_positions[:10]:
        lines.append(f"• {str(p['mint'])[:8]}… · {p['token_amount_raw']} raw · {p.get('exit_reason') or 'monitoring'}")
    return "\n".join(lines)


async def _history_text(telegram_id: int, mode: str = "both") -> str:
    svc = _svc()
    lines = ["📜 9N8 TRADE HISTORY", ""]
    if mode in {"both","paper"}:
        paper_rows = await svc.engine.paper_engine.store.history(telegram_id,20) if svc.engine.paper_engine else []
        lines.append("PAPER TRADES · SIMULATED")
        if not paper_rows:
            lines.append("No paper trades recorded.")
        for row in paper_rows[:20]:
            amount = int(row.get("sol_amount_lamports") or 0)
            lines.append(
                f"• {str(row.get('side','')).upper()} {str(row.get('mint',''))[:8]}… · "
                f"{format_sol(amount)} SOL · {row.get('reason') or 'simulated'}"
            )
    if mode in {"both","live"}:
        live_rows = await svc.db.list_trade_receipts(telegram_id,50)
        lines.extend(["", "LIVE TRANSACTIONS · BLOCKCHAIN RECEIPTS"])
        if not live_rows:
            lines.append("No live blockchain receipts recorded.")

        since = datetime.now(timezone.utc) - timedelta(hours=24)
        net_24h = 0
        confirmed_exits_24h = 0
        for row in live_rows:
            created = row.get("created_at")
            try:
                created_at = created if isinstance(created, datetime) else datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            if created_at >= since and row.get("event_type") == "sell" and row.get("status") in {"confirmed", "finalized"}:
                pnl_value = metadata.get("net_realized_pnl_lamports")
                if pnl_value is not None:
                    net_24h += int(pnl_value)
                    confirmed_exits_24h += 1
        lines.append(
            f"24h confirmed realized PnL: {format_sol(net_24h)} SOL "
            f"({confirmed_exits_24h} confirmed exits)"
        )

        for row in live_rows[:20]:
            signature = str(row.get("signature") or "")
            lines.append(
                f"• {str(row.get('event_type','trade')).upper()} · {str(row.get('status','unknown')).upper()} "
                f"· {str(row.get('mint') or 'SOL')[:8]}…"
            )
            if signature:
                lines.append(f"  https://solscan.io/tx/{signature}")

        security_rows = await svc.db.list_security_events(telegram_id, 30)
        rejection_rows = [
            item for item in security_rows
            if "reject" in str(item.get("event_type", "")).lower()
            or "skip" in str(item.get("event_type", "")).lower()
        ]
        lines.extend(["", "RECENT SAFETY REJECTIONS · NO BUY WAS SENT"])
        if not rejection_rows:
            lines.append("No recorded safety rejections.")
        for item in rejection_rows[:12]:
            lines.append(
                f"• {str(item.get('event_type','rejected')).replace('_',' ')}: "
                f"{str(item.get('detail',''))[:260]}"
            )
    return "\n".join(lines)


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    await state.clear()
    existing = await svc.db.get_user(message.from_user.id)
    if not existing and await svc.db.user_count() >= svc.settings.max_users:
        await message.answer("9N8 is currently at its onboarding capacity. Please contact the operator.")
        return
    await svc.db.ensure_user(message.from_user.id)
    await message.answer(
        await _dashboard_text(message.from_user.id),
        reply_markup=_home_keyboard(svc.settings.webapp_url),
    )




async def _show_paper_snipe_preview(message: Message, telegram_id: int, mint_text: str, amount_sol: str = "0.05") -> None:
    svc = _svc()
    paper = svc.engine.paper_engine
    if paper is None:
        await message.answer("The paper practice engine is not configured.")
        return
    if not await paper.store.mode_enabled(telegram_id, "paper"):
        await message.answer("Start virtual Paper Trading first with /paper_on. No real SOL will be spent.")
        return
    try:
        mint = str(Pubkey.from_string(mint_text.strip()))
        preview = await paper.preview_mint(telegram_id, mint, amount_sol)
    except Exception as exc:
        await message.answer(f"Could not scan this mint: {type(exc).__name__}: {str(exc)[:180]}")
        return
    if not preview.get("passed"):
        reasons = "\n".join("• " + str(item) for item in preview.get("reasons", [])[:6])
        await message.answer(
            "🛡️ PAPER SNIPE REJECTED\n"
            f"Mint: {mint}\n{reasons}\n\n"
            "No practice buy was recorded."
        )
        return
    _pending_paper_snipes[telegram_id] = {"mint": mint, "preview": preview}
    risk = preview.get("risk", {})
    await message.answer(
        "🎯 MANUAL PAPER SNIPE PREVIEW\n\n"
        f"Mint: {mint}\n"
        f"Virtual buy: {preview['buy_amount_sol']} SOL\n"
        f"Minimum quoted output: {preview['minimum_output_raw']} raw units\n"
        f"Price impact: {preview.get('price_impact_pct','unknown')}\n"
        f"Mint authority revoked: {'YES' if risk.get('mint_authority_disabled') else 'NO'}\n"
        f"Freeze authority revoked: {'YES' if risk.get('freeze_authority_disabled') else 'NO'}\n"
        f"Top-10 wallet share: {risk.get('top10_concentration_pct','unknown')}%\n"
        f"Liquidity: {risk.get('liquidity_usd','unknown')} USD\n"
        f"LP locked/burned: {risk.get('lp_locked_pct','unknown')}%\n\n"
        "The safety check and price quote are repeated when you confirm. This is virtual money only.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="0.05 SOL",callback_data="paper:snipe:amount:0.05"),
             InlineKeyboardButton(text="0.10 SOL",callback_data="paper:snipe:amount:0.10")],
            [InlineKeyboardButton(text="0.25 SOL",callback_data="paper:snipe:amount:0.25"),
             InlineKeyboardButton(text="0.50 SOL",callback_data="paper:snipe:amount:0.50")],
            [InlineKeyboardButton(text="Confirm Paper Buy",callback_data="paper:snipe:confirm"),
             InlineKeyboardButton(text="Cancel",callback_data="paper:snipe:cancel")],
        ]),
    )


@router.message(Command("paper"))
async def cmd_paper(message: Message) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    enabled = await svc.engine.paper_engine.store.mode_enabled(message.from_user.id, "paper")
    await message.answer(await _paper_menu_text(message.from_user.id),reply_markup=_paper_menu_keyboard(enabled))


@router.message(Command("paper_on"))
async def cmd_paper_on(message: Message) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    try:
        account = await svc.engine.start_paper_user(message.from_user.id)
        await message.answer(
            "🧪 PAPER TRADING STARTED\n"
            "Your practice account has a $1,000 virtual starting value.\n"
            f"Virtual SOL cash: {format_sol(account['cash_balance_lamports'])} SOL\n"
            "Your real wallet is not used for paper buys or sells."
        )
        await message.answer(await _paper_menu_text(message.from_user.id),reply_markup=_paper_menu_keyboard(True))
    except Exception as exc:
        await message.answer(f"Paper mode could not start: {type(exc).__name__}: {str(exc)[:240]}")


@router.message(Command("paper_off"))
async def cmd_paper_off(message: Message) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    await svc.engine.stop_paper_user(message.from_user.id)
    await message.answer("Paper entries paused. Existing virtual positions remain monitored.")


@router.callback_query(F.data == "paper:menu")
async def cb_paper_menu(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    enabled = await svc.engine.paper_engine.store.mode_enabled(callback.from_user.id, "paper")
    await callback.message.edit_text(await _paper_menu_text(callback.from_user.id),reply_markup=_paper_menu_keyboard(enabled))


@router.callback_query(F.data == "paper:start")
async def cb_paper_start(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    try:
        await svc.engine.start_paper_user(callback.from_user.id)
        await callback.message.edit_text(await _paper_menu_text(callback.from_user.id),reply_markup=_paper_menu_keyboard(True))
    except Exception as exc:
        await callback.message.answer(f"Paper mode could not start: {type(exc).__name__}: {str(exc)[:200]}")


@router.callback_query(F.data == "paper:stop")
async def cb_paper_stop(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    await svc.engine.stop_paper_user(callback.from_user.id)
    await callback.message.edit_text(await _paper_menu_text(callback.from_user.id),reply_markup=_paper_menu_keyboard(False))


@router.message(Command("live"))
async def cmd_live(message: Message) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    readiness = await svc.engine.live_readiness(message.from_user.id)
    enabled = await svc.engine.paper_engine.store.mode_enabled(message.from_user.id, "live")
    await message.answer(await _live_menu_text(message.from_user.id),reply_markup=_live_menu_keyboard(enabled,readiness["ready"]))


@router.message(Command("live_on"))
async def cmd_live_on(message: Message) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    user = await svc.db.get_user(message.from_user.id)
    if not user or not user.get("activation_paid"):
        try:
            price = await svc.oracle.sol_usd()
            lamports = usd_to_lamports("1.00",price)
            await message.answer(
                "LIVE ACTIVATION REQUIRED\n"
                f"One-time payment: $1.00 ≈ {format_sol(lamports)} SOL.\n"
                "Nothing is transferred until you press Confirm.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Confirm $1 Activation",callback_data="activation:confirm"),
                    InlineKeyboardButton(text="Cancel",callback_data="home:show"),
                ]]),
            )
        except Exception as exc:
            await message.answer(f"Activation price unavailable: {exc}")
        return
    readiness = await svc.engine.live_readiness(message.from_user.id)
    enabled = await svc.engine.paper_engine.store.mode_enabled(message.from_user.id, "live")
    await message.answer(await _live_menu_text(message.from_user.id),reply_markup=_live_menu_keyboard(enabled,readiness["ready"]))




@router.callback_query(F.data == "live:menu")
async def cb_live_menu(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    readiness = await svc.engine.live_readiness(callback.from_user.id)
    enabled = await svc.engine.paper_engine.store.mode_enabled(callback.from_user.id, "live")
    await callback.message.edit_text(await _live_menu_text(callback.from_user.id),reply_markup=_live_menu_keyboard(enabled,readiness["ready"]))


@router.callback_query(F.data == "live:check")
async def cb_live_check(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    readiness = await svc.engine.live_readiness(callback.from_user.id)
    enabled = await svc.engine.paper_engine.store.mode_enabled(callback.from_user.id, "live")
    await callback.message.edit_text(await _live_menu_text(callback.from_user.id),reply_markup=_live_menu_keyboard(enabled,readiness["ready"]))


@router.callback_query(F.data == "live:confirm")
async def cb_live_confirm(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer("Rechecking all live safety requirements…")
    try:
        await svc.engine.start_live_user(callback.from_user.id)
        await callback.message.edit_text(
            "🔴 LIVE SNIPER ARMED\n\n"
            "The live entry engine is enabled for this Telegram account. Paper remains separate.\n\n"
            + await _live_menu_text(callback.from_user.id),
            reply_markup=_live_menu_keyboard(True,True),
        )
    except Exception as exc:
        await callback.message.edit_text(
            "LIVE MODE WAS NOT STARTED\n" + str(exc)[:800] + "\n\n" + await _live_menu_text(callback.from_user.id),
            reply_markup=_live_menu_keyboard(False,False),
        )


@router.callback_query(F.data == "live:stop")
async def cb_live_stop(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    await svc.engine.stop_live_user(callback.from_user.id)
    await callback.message.edit_text(await _live_menu_text(callback.from_user.id),reply_markup=_live_menu_keyboard(False,False))


@router.message(Command("positions"))
async def cmd_positions(message: Message) -> None:
    if not _is_private(message):
        return
    await message.answer(await _positions_text(message.from_user.id))


@router.callback_query(F.data == "positions:show")
async def cb_positions(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(await _positions_text(callback.from_user.id),reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Paper Positions",callback_data="positions:paper"),
         InlineKeyboardButton(text="Live Positions",callback_data="positions:live")],
        [InlineKeyboardButton(text="Back",callback_data="home:show")],
    ]))


@router.message(Command("paper_positions"))
async def cmd_paper_positions(message: Message) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    rows = await svc.engine.paper_engine.store.positions(message.from_user.id) if svc.engine.paper_engine else []
    if not rows:
        await message.answer("No open virtual positions. Use /paper to review your practice account.")
        return
    await _send_paper_positions(message, rows)


async def _send_paper_positions(message: Message, rows: list[dict]) -> None:
    lines = ["🧪 PAPER POSITIONS · VIRTUAL ONLY", ""]
    buttons = []
    for row in rows[:10]:
        mint = str(row["mint"])
        lines.append(
            f"{mint}\n"
            f"Entry cost: {format_sol(int(row.get('initial_cost_lamports') or 0))} SOL\n"
            f"Marked PnL: {row.get('last_pnl_pct','0')}%\n"
            f"Tokens: {row['token_amount_raw']} raw units"
        )
        buttons.append([InlineKeyboardButton(text=f"Virtual Sell 100% {mint[:7]}…", callback_data=f"paper:position:sell:{row['id']}")])
    buttons.append([InlineKeyboardButton(text="Back to Paper Menu", callback_data="paper:menu")])
    await message.answer("\n\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == "positions:paper")
async def cb_paper_positions(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    rows = await svc.engine.paper_engine.store.positions(callback.from_user.id) if svc.engine.paper_engine else []
    if not rows:
        await callback.message.edit_text("No open virtual positions.",reply_markup=_paper_menu_keyboard(False))
        return
    lines = ["PAPER POSITIONS · VIRTUAL ONLY",""]
    buttons = []
    for row in rows[:10]:
        mint = str(row["mint"])
        lines.append(
            f"Mint: {mint}\n"
            f"Entry cost: {format_sol(int(row.get('initial_cost_lamports') or 0))} SOL\n"
            f"Marked PnL: {row.get('last_pnl_pct','0')}%\n"
            f"Tokens: {row['token_amount_raw']} raw units"
        )
        buttons.append([InlineKeyboardButton(text=f"Virtual Sell 100% {mint[:7]}…", callback_data=f"paper:position:sell:{row['id']}")])
    buttons.append([InlineKeyboardButton(text="Back to Paper Menu",callback_data="paper:menu")])
    await callback.message.edit_text("\n\n".join(lines),reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("paper:position:sell:"))
async def cb_paper_position_sell(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer("Recording a virtual paper exit…")
    try:
        position_id = int(callback.data.rsplit(":", 1)[-1])
        await svc.engine.paper_engine.panic_sell(callback.from_user.id, position_id)
        await callback.message.answer("Paper position closed with a virtual quote. No real SOL was spent or received.")
    except Exception as exc:
        await callback.message.answer(f"Paper exit failed: {type(exc).__name__}: {str(exc)[:180]}")


@router.message(Command("live_positions"))
async def cmd_live_positions(message: Message) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    rows = await svc.db.active_positions(message.from_user.id)
    if not rows:
        await message.answer("No open live positions in your real Solana wallet.")
        return
    await _send_live_positions(message, rows)


async def _send_live_positions(message: Message, rows: list[dict]) -> None:
    lines = ["🔴 LIVE POSITIONS · REAL WALLET", ""]
    buttons = []
    for row in rows[:8]:
        mint = str(row["mint"])
        lines.append(
            f"{mint}\n"
            f"Entry cost: {format_sol(int(row.get('initial_cost_lamports') or 0))} SOL\n"
            f"Current PnL: {row.get('last_pnl_pct','0')}%\n"
            f"Tokens: {row['token_amount_raw']} raw units\n"
            f"Exit phase: {row.get('exit_reason') or 'monitoring'}"
        )
        buttons.append([
            InlineKeyboardButton(text=f"Sell 50% {mint[:7]}…", callback_data=f"position:sell50:{row['id']}"),
            InlineKeyboardButton(text=f"Panic 100% {mint[:7]}…", callback_data=f"position:panic:{row['id']}"),
        ])
    buttons.extend([
        [InlineKeyboardButton(text="🧨 PANIC SELL ALL LIVE POSITIONS", callback_data="positions:panic_all:ask")],
        [InlineKeyboardButton(text="Back to Live Menu", callback_data="live:menu")],
    ])
    await message.answer("\n\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == "positions:live")
async def cb_live_positions(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    rows = await svc.db.active_positions(callback.from_user.id)
    if not rows:
        await callback.message.edit_text("No open live positions.",reply_markup=_live_menu_keyboard(False,False))
        return
    lines = ["LIVE POSITIONS · REAL WALLET",""]
    buttons = []
    for row in rows[:8]:
        mint = str(row["mint"])
        lines.append(
            f"Mint: {mint}\n"
            f"Entry cost: {format_sol(int(row.get('initial_cost_lamports') or 0))} SOL\n"
            f"Current PnL: {row.get('last_pnl_pct','0')}%\n"
            f"Tokens: {row['token_amount_raw']} raw\n"
            f"Target state: {row.get('exit_reason') or 'monitoring'}"
        )
        buttons.append([
            InlineKeyboardButton(text=f"Sell 50% {mint[:7]}…", callback_data=f"position:sell50:{row['id']}"),
            InlineKeyboardButton(text=f"Panic 100% {mint[:7]}…", callback_data=f"position:panic:{row['id']}"),
        ])
    buttons.extend([
        [InlineKeyboardButton(text="🧨 PANIC SELL ALL LIVE POSITIONS", callback_data="positions:panic_all:ask")],
        [InlineKeyboardButton(text="Back to Live Menu",callback_data="live:menu")],
    ])
    await callback.message.edit_text("\n\n".join(lines),reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == "positions:panic_all:ask")
async def cb_panic_all_ask(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    rows = await svc.db.active_positions(callback.from_user.id)
    if not rows:
        await callback.message.edit_text("No open live positions to close.", reply_markup=_live_menu_keyboard(False, False))
        return
    if len(rows) > 8:
        await callback.message.answer(
            f"You have {len(rows)} live positions. This bulk action is capped at 8 per confirmation; "
            "close some first, then repeat."
        )
        return
    await callback.message.edit_text(
        "⚠️ CONFIRM PANIC SELL ALL\n\n"
        f"This will attempt to sell all {len(rows)} open real-wallet token positions, one by one, "
        "using fresh Jupiter quotes and the current live safety/execution settings.\n\n"
        "Each sell has its own transaction signature and may fail independently. "
        "Do not retry an unknown transaction outcome.\n\n"
        "This action cannot be undone.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧨 CONFIRM SELL ALL", callback_data="positions:panic_all:confirm")],
            [InlineKeyboardButton(text="Cancel", callback_data="positions:live")],
        ]),
    )


@router.callback_query(F.data == "positions:panic_all:confirm")
async def cb_panic_all_confirm(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer("Processing live position exits…")
    await svc.engine.stop_live_user(callback.from_user.id)
    rows = await svc.db.active_positions(callback.from_user.id)
    results = []
    for row in rows[:8]:
        position_id = int(row["id"])
        try:
            await svc.engine.panic_sell(callback.from_user.id, position_id)
            results.append(f"Position #{position_id}: sell submitted; verify its receipt.")
        except Exception as exc:
            results.append(f"Position #{position_id}: not confirmed — {type(exc).__name__}: {str(exc)[:120]}")
    await callback.message.edit_text(
        "PANIC SELL ALL RESULTS\n\n"
        + ("\n".join(results) if results else "No open live positions were found.")
        + "\n\nNew live entries are OFF. Check /live_positions and /live_history for receipts.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Refresh Live Positions", callback_data="positions:live")],
            [InlineKeyboardButton(text="Back to Live Menu", callback_data="live:menu")],
        ]),
    )


@router.callback_query(F.data.startswith("position:sell50:"))
async def cb_live_sell50(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer("Submitting your 50% sell…")
    try:
        position_id = int(callback.data.rsplit(":", 1)[-1])
        await svc.engine.sell_position_percent(callback.from_user.id, position_id, 50)
        await callback.message.answer("50% sell was processed by the live engine. Check the on-chain receipt before another trade.")
    except Exception as exc:
        await callback.message.answer(f"50% sell was not completed: {type(exc).__name__}: {str(exc)[:200]}")


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    if not _is_private(message):
        return
    await message.answer("Choose the settings area you want to change.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Paper Settings",callback_data="paper:settings")],
        [InlineKeyboardButton(text="Live Settings",callback_data="settings:show")],
        [InlineKeyboardButton(text="Back",callback_data="home:show")],
    ]))


@router.message(Command("paper_settings"))
async def cmd_paper_settings(message: Message) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    config = await svc.engine.paper_engine.store.settings(message.from_user.id,"paper")
    await message.answer("PAPER SETTINGS · VIRTUAL ACCOUNT",reply_markup=_paper_settings_keyboard(config))


@router.callback_query(F.data == "paper:settings")
async def cb_paper_settings(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    config = await svc.engine.paper_engine.store.settings(callback.from_user.id,"paper")
    await callback.message.edit_text("PAPER SETTINGS · VIRTUAL ACCOUNT",reply_markup=_paper_settings_keyboard(config))


@router.callback_query(F.data.startswith("paper:field:"))
async def cb_paper_setting_field(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    field = callback.data.rsplit(":",1)[-1]
    await state.set_state(ParameterFlow.value)
    await state.set_data({"field":field,"mode":"paper"})
    await callback.message.answer(f"Enter the new paper value for {field}.")


@router.callback_query(F.data == "paper:mev")
async def cb_paper_mev(callback: CallbackQuery) -> None:
    svc = _svc()
    config = await svc.engine.paper_engine.store.settings(callback.from_user.id,"paper")
    await svc.engine.paper_engine.store.update_settings(
        callback.from_user.id,{"mev_shield":not bool(config.get("mev_shield",True))},"paper"
    )
    await callback.answer("Paper fee model updated.")
    config = await svc.engine.paper_engine.store.settings(callback.from_user.id,"paper")
    await callback.message.edit_text("PAPER SETTINGS",reply_markup=_paper_settings_keyboard(config))


@router.callback_query(F.data.startswith("paper:snipe:amount:"))
async def cb_paper_snipe_amount(callback: CallbackQuery) -> None:
    await callback.answer()
    pending = _pending_paper_snipes.get(callback.from_user.id)
    if not pending:
        await callback.message.answer("This preview expired. Run /snipe again.")
        return
    amount = callback.data.rsplit(":",1)[-1]
    await _show_paper_snipe_preview(callback.message,callback.from_user.id,pending["mint"],amount)


@router.callback_query(F.data == "paper:snipe:confirm")
async def cb_paper_snipe_confirm(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer("Refreshing token safety and market quote…")
    pending = _pending_paper_snipes.get(callback.from_user.id)
    if not pending:
        await callback.message.answer("This preview expired. Run /snipe again.")
        return
    try:
        position_id = await svc.engine.paper_engine.manual_buy(
            callback.from_user.id,pending["mint"],pending["preview"]
        )
        _pending_paper_snipes.pop(callback.from_user.id,None)
        await callback.message.answer(
            f"🧪 PAPER BUY RECORDED · Position #{position_id}\n"
            "Virtual cash only. No wallet signature or blockchain transaction was created."
        )
    except Exception as exc:
        await callback.message.answer(f"Manual paper buy was not recorded: {type(exc).__name__}: {str(exc)[:220]}")


@router.callback_query(F.data == "paper:snipe:cancel")
async def cb_paper_snipe_cancel(callback: CallbackQuery) -> None:
    _pending_paper_snipes.pop(callback.from_user.id,None)
    await callback.answer("Cancelled.")
    await callback.message.edit_text("Manual paper snipe cancelled.",reply_markup=_paper_menu_keyboard(False))


@router.message(Command("paper_snipe"))
async def cmd_paper_snipe(message: Message, state: FSMContext) -> None:
    if not _is_private(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        await _show_paper_snipe_preview(message, message.from_user.id, parts[1].strip())
    else:
        await state.set_state(SnipeFlow.mint)
        await message.answer("Paste a Solana mint for a PAPER preview. This uses virtual funds only.")


@router.message(Command("snipe"))
@router.message(Command("live_snipe"))
async def cmd_snipe(message: Message, state: FSMContext) -> None:
    if not _is_private(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        await _show_live_snipe_preview(message, message.from_user.id, parts[1].strip())
    else:
        await state.set_state(LiveSnipeFlow.mint)
        await message.answer("Paste a Solana mint for a LIVE trade preview. Nothing is spent until you review and confirm.")


@router.callback_query(F.data == "live:snipe")
async def cb_live_snipe(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(LiveSnipeFlow.mint)
    await callback.message.answer("Paste a Solana mint for a LIVE trade preview. Live mode must already be armed; a second confirmation is required.")


async def _show_live_snipe_preview(
    message: Message, telegram_id: int, mint_text: str, amount_sol: str = "0.05",
) -> None:
    svc = _svc()
    try:
        preview = await svc.engine.preview_live_snipe(telegram_id, mint_text, amount_sol)
    except Exception as exc:
        await message.answer(
            "🔴 LIVE SNIPE BLOCKED\n"
            f"{type(exc).__name__}: {str(exc)[:450]}\n\n"
            "No transaction was signed or submitted."
        )
        return
    _pending_live_snipes[telegram_id] = preview
    await message.answer(
        "🎯 MANUAL LIVE SNIPE PREVIEW\n\n"
        f"Mint: {preview['mint']}\n"
        f"Real SOL spend: {preview['buy_amount_sol']} SOL\n"
        f"Minimum quoted output: {preview['minimum_output_raw']} raw units\n"
        f"Price impact: {preview['price_impact_pct']}\n"
        f"Wallet required including rent/fees: {preview['estimated_required_sol']} SOL\n\n"
        "SAFETY CHECKS\n"
        f"Mint authority revoked: {'YES' if preview['mint_authority_disabled'] else 'NO'}\n"
        f"Freeze authority revoked: {'YES' if preview['freeze_authority_disabled'] else 'NO'}\n"
        f"LP locked/burned: {preview['lp_locked_pct']}%\n"
        f"Top-10 wallet share: {preview['top10_concentration_pct']}%\n"
        f"Developer share: {preview['developer_concentration_pct']}%\n"
        f"Pool liquidity (USD): {preview['liquidity_usd']}\n\n"
        "THIS USES REAL SOL. Review every number. The bot rechecks the quote, balance and safety immediately after you confirm.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="0.05 SOL", callback_data="live:snipe:amount:0.05"),
             InlineKeyboardButton(text="0.10 SOL", callback_data="live:snipe:amount:0.10")],
            [InlineKeyboardButton(text="0.25 SOL", callback_data="live:snipe:amount:0.25"),
             InlineKeyboardButton(text="0.50 SOL", callback_data="live:snipe:amount:0.50")],
            [InlineKeyboardButton(text="Custom SOL", callback_data="live:snipe:custom"),
             InlineKeyboardButton(text="Confirm LIVE Buy", callback_data="live:snipe:confirm")],
            [InlineKeyboardButton(text="Cancel", callback_data="live:snipe:cancel")],
        ]),
    )


@router.callback_query(F.data.startswith("live:snipe:amount:"))
async def cb_live_snipe_amount(callback: CallbackQuery) -> None:
    await callback.answer()
    pending = _pending_live_snipes.get(callback.from_user.id)
    if not pending:
        await callback.message.answer("This live preview expired. Run /snipe again.")
        return
    amount = callback.data.rsplit(":", 1)[-1]
    await _show_live_snipe_preview(callback.message, callback.from_user.id, pending["mint"], amount)


@router.callback_query(F.data == "live:snipe:custom")
async def cb_live_snipe_custom(callback: CallbackQuery, state: FSMContext) -> None:
    pending = _pending_live_snipes.get(callback.from_user.id)
    if not pending:
        await callback.answer("This live preview expired. Run /snipe again.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(LiveSnipeFlow.amount)
    await state.update_data(mint=pending["mint"])
    await callback.message.answer("Enter the buy amount in SOL. It must stay within your configured per-trade cap.")


@router.message(LiveSnipeFlow.mint)
async def handle_live_snipe_mint(message: Message, state: FSMContext) -> None:
    try:
        await _show_live_snipe_preview(message, message.from_user.id, (message.text or "").strip())
    finally:
        await state.clear()


@router.message(LiveSnipeFlow.amount)
async def handle_live_snipe_custom_amount(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    mint = str(data.get("mint") or "")
    try:
        amount = Decimal((message.text or "").strip())
        if not amount.is_finite() or amount <= 0:
            raise ValueError("Enter a positive SOL amount.")
        await _show_live_snipe_preview(message, message.from_user.id, mint, str(amount))
    except Exception as exc:
        await message.answer(f"Custom amount rejected: {str(exc)[:180]}")
    finally:
        await state.clear()


@router.callback_query(F.data == "live:snipe:confirm")
async def cb_live_snipe_confirm(callback: CallbackQuery) -> None:
    svc = _svc()
    pending = _pending_live_snipes.pop(callback.from_user.id, None)
    if not pending:
        await callback.answer("This live preview expired. Run /snipe again.", show_alert=True)
        return
    await callback.answer("Rechecking safety and submitting your confirmed live buy…")
    try:
        position_id = await svc.engine.manual_snipe(
            callback.from_user.id, str(pending["mint"]), str(pending["buy_amount_sol"])
        )
        position = await svc.db.get_position(position_id, callback.from_user.id)
        signature = str((position or {}).get("buy_signature") or "")
        mint = str(pending["mint"])
        buttons = [
            [InlineKeyboardButton(text="Live Chart", url="https://dexscreener.com/solana/" + mint)],
            [InlineKeyboardButton(text="Sell 50%", callback_data=f"position:sell50:{position_id}"),
             InlineKeyboardButton(text="Panic Sell 100%", callback_data=f"position:panic:{position_id}")],
            [InlineKeyboardButton(text="Live Positions", callback_data="positions:live")],
        ]
        lines = [
            "LIVE SNIPE RECORDED",
            f"Mint: {mint}",
            f"Position: #{position_id}",
            f"Real SOL spend: {pending['buy_amount_sol']} SOL",
            "Check the receipt before assuming the transaction has fully settled.",
        ]
        if signature:
            lines.append(f"Solscan: https://solscan.io/tx/{signature}")
            buttons.insert(0, [InlineKeyboardButton(text="Solscan Transaction", url="https://solscan.io/tx/" + signature)])
        await callback.message.answer("\\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as exc:
        await callback.message.answer(
            "LIVE SNIPE DID NOT COMPLETE\n"
            f"{type(exc).__name__}: {str(exc)[:450]}\n\n"
            "Check /live_positions and the wallet's Solscan history. Do not retry an order with an unknown transaction outcome."
        )


@router.callback_query(F.data == "live:snipe:cancel")
async def cb_live_snipe_cancel(callback: CallbackQuery) -> None:
    _pending_live_snipes.pop(callback.from_user.id, None)
    await callback.answer("Cancelled. No transaction was sent.")
    await callback.message.edit_text("Manual live snipe cancelled. Live mode remains unchanged.", reply_markup=_live_menu_keyboard(False, False))




@router.callback_query(F.data == "paper:snipe")
async def cb_paper_snipe(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SnipeFlow.mint)
    await callback.message.answer("Paste the Solana token mint address for a virtual paper buy preview.")


@router.message(SnipeFlow.mint)
async def handle_snipe_mint(message: Message, state: FSMContext) -> None:
    try:
        await _show_paper_snipe_preview(message,message.from_user.id,(message.text or "").strip())
    finally:
        await state.clear()


@router.message(Command("safety"))
async def cmd_safety(message: Message) -> None:
    if not _is_private(message):
        return
    await message.answer(
        "🛡️ ANTI-RUG SAFETY FILTERS\n\n"
        "These checks protect both paper and live entries. Critical checks stay ON and cannot be disabled.\n\n"
        "🔒 Mint authority revoked: REQUIRED\n"
        "🔒 Freeze authority revoked: REQUIRED\n"
        "🔒 Creator and top-holder concentration: CHECKED\n"
        "🔒 Token extensions and transfer restrictions: CHECKED\n"
        "🔒 Pool liquidity and LP lock/burn: REQUIRED\n\n"
        "You can adjust thresholds separately for the paper account and live wallet. A missing or unverifiable result blocks the entry.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Paper Safety Thresholds", callback_data="paper:settings")],
            [InlineKeyboardButton(text="Live Safety Thresholds", callback_data="safety:live")],
            [InlineKeyboardButton(text="Back", callback_data="home:show")],
        ]),
    )


@router.callback_query(F.data == "safety:show")
async def cb_safety_show(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "🛡️ ANTI-RUG SAFETY FILTERS\n\n"
        "These checks protect both paper and live entries. Critical checks stay ON and cannot be disabled.\n\n"
        "🔒 Mint authority revoked: REQUIRED\n"
        "🔒 Freeze authority revoked: REQUIRED\n"
        "🔒 Creator and top-holder concentration: CHECKED\n"
        "🔒 Token extensions and transfer restrictions: CHECKED\n"
        "🔒 Pool liquidity and LP lock/burn: REQUIRED\n\n"
        "An entry is blocked whenever a required result is missing or cannot be verified.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Paper Safety Thresholds", callback_data="paper:settings")],
            [InlineKeyboardButton(text="Live Safety Thresholds", callback_data="safety:live")],
            [InlineKeyboardButton(text="Back", callback_data="home:show")],
        ]),
    )


@router.callback_query(F.data == "safety:live")
async def cb_live_safety(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    config = await svc.db.get_settings(callback.from_user.id)
    await callback.message.edit_text(
        "🔒 LIVE SAFETY FILTERS · REAL WALLET\n\n"
        "Mint authority revocation: REQUIRED · LOCKED ON\n"
        "Freeze authority revocation: REQUIRED · LOCKED ON\n"
        f"Minimum pool liquidity: {config.get('min_liquidity_sol','5')} SOL\n"
        f"Minimum LP locked/burned: {config.get('min_lp_locked_pct','80')}%\n"
        f"Maximum top-10 share: {config.get('max_top10_concentration_pct','25')}%\n"
        f"Maximum developer share: {config.get('max_dev_wallet_pct','5')}%\n\n"
        "Critical authority checks cannot be disabled. Change the numeric thresholds below; an unverified safety result still blocks every live buy.",
        reply_markup=_settings_keyboard(config),
    )


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    if not _is_private(message):
        return
    await message.answer(await _history_text(message.from_user.id,"both"))


@router.message(Command("paper_history"))
async def cmd_paper_history(message: Message) -> None:
    if not _is_private(message):
        return
    await message.answer(await _history_text(message.from_user.id, "paper"))


@router.message(Command("live_history"))
async def cmd_live_history(message: Message) -> None:
    if not _is_private(message):
        return
    await message.answer(await _history_text(message.from_user.id, "live"))


@router.message(Command("live_settings"))
async def cmd_live_settings(message: Message) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    config = await svc.db.get_settings(message.from_user.id)
    await message.answer(
        "LIVE SETTINGS · REAL WALLET\nChanges apply only to future live entries.\n",
        reply_markup=_settings_keyboard(config),
    )


@router.message(Command("stop"))
@router.message(Command("live_off"))
async def cmd_live_off(message: Message) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    await svc.engine.stop_live_user(message.from_user.id)
    await message.answer("Live entries are OFF. Paper mode is unchanged. Existing real positions remain monitored.")


@router.callback_query(F.data == "history:show")
async def cb_history_show(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(await _history_text(callback.from_user.id,"both"),reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Paper History",callback_data="history:paper"),
         InlineKeyboardButton(text="Live Receipts",callback_data="history:live")],
        [InlineKeyboardButton(text="Back",callback_data="home:show")],
    ]))


@router.callback_query(F.data == "history:paper")
async def cb_history_paper(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(await _history_text(callback.from_user.id,"paper"),reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Back to Paper Menu",callback_data="paper:menu")],
        [InlineKeyboardButton(text="All History",callback_data="history:show")],
    ]))


@router.callback_query(F.data == "history:live")
async def cb_history_live(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(await _history_text(callback.from_user.id,"live"),reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Back to Live Menu",callback_data="live:menu")],
        [InlineKeyboardButton(text="All History",callback_data="history:show")],
    ]))


@router.message(Command("withdraw"))
async def cmd_withdraw(message: Message) -> None:
    if not _is_private(message):
        return
    await _show_withdraw_options(message, message.from_user.id)

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not _is_private(message):
        return
    await message.answer(
        "9N8 COMMANDS · PAPER AND LIVE ARE SEPARATE\n\n"
        "/start — main dashboard\n"
        "/paper — virtual practice account\n"
        "/paper_on — start paper entries\n"
        "/paper_off — pause paper entries\n"
        "/paper_settings — edit paper rules\n"
        "/live — show live readiness and safety locks\n"
        "/live_on — request live activation after all checks pass\n"
        "/live_off — stop new live entries\n"
        "/snipe MINT — preview a real-wallet live snipe (requires confirmation)\n"
        "/live_snipe MINT — alias for /snipe\n"
        "/paper_snipe MINT — virtual paper-only preview\n"
        "/positions — choose paper or live positions\n"
        "/paper_positions — show virtual positions\n"
        "/live_positions — show real-wallet positions\n"
        "/settings — choose paper or live settings\n"
        "/live_settings — edit real-wallet execution settings\n"
        "/safety — show anti-rug requirements\n"
        "/paper_history — show simulated trades\n"
        "/live_history — show on-chain receipts\n"
        "/withdraw — transfer a selected amount of real SOL\n"
        "/stop — stop new live entries (paper stays unchanged)\n"
        "/status — dashboard and mode status\n"
        "/help — this guide\n\n"
        "Paper mode uses virtual SOL only. Live mode can use real SOL and remains locked "
        "unless the operator enables LIVE_TRADING_ENABLED and the wallet, activation, "
        "seven paper days and $10 balance requirements pass."
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not _is_private(message):
        return
    await message.answer(await _dashboard_text(message.from_user.id))


@router.callback_query(F.data == "home:show")
async def cb_home(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    await callback.message.edit_text(
        await _dashboard_text(callback.from_user.id),
        reply_markup=_home_keyboard(svc.settings.webapp_url),
    )


@router.callback_query(F.data == "status:show")
async def cb_status(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    await callback.message.edit_text(
        await _dashboard_text(callback.from_user.id),
        reply_markup=_home_keyboard(svc.settings.webapp_url),
    )


@router.callback_query(F.data == "wallet:create")
async def cb_create_wallet(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    try:
        address, secret = await svc.wallet.create_wallet(callback.from_user.id)
        await svc.engine.watch_wallet(callback.from_user.id)
        await callback.message.answer(
            f"Wallet created. Solana deposit address:\n{address}\n\n"
            "Your recovery key will arrive in a separate auto-deleting message."
        )
        await _send_sensitive_key(callback.message, address, secret)
    except WalletError as exc:
        await callback.message.answer(str(exc))


@router.callback_query(F.data == "wallet:import")
async def cb_import_wallet(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ImportWalletFlow.secret)
    await callback.message.answer(
        "Send the Solana private key as a base58 message. This message will be deleted "
        "after processing. Do not send your key into a group chat."
    )


@router.message(ImportWalletFlow.secret)
async def handle_import_wallet(message: Message, state: FSMContext) -> None:
    svc = _svc()
    try:
        if not _is_private(message):
            return
        address = await svc.wallet.import_wallet(message.from_user.id, (message.text or "").strip())
        await svc.engine.watch_wallet(message.from_user.id)
        await message.answer(f"Wallet imported successfully.\nSolana address:\n{address}")
    except WalletError as exc:
        await message.answer(f"Wallet import failed: {exc}")
    finally:
        await state.clear()
        try:
            await message.delete()
        except Exception:
            pass


@router.callback_query(F.data == "deposit:show")
async def cb_deposit(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    address = await svc.db.get_wallet_address(callback.from_user.id)
    if not address:
        await callback.message.answer("Create or import a wallet first.")
        return
    await callback.message.answer(
        "DEPOSIT FUNDS\n\n"
        f"Solana Mainnet address:\n{address}\n\n"
        "Send SOL or supported SPL tokens only to the matching Solana address. "
        "Do not send BNB, Base or Robinhood Chain assets here. EVM-to-Solana bridging "
        "is not enabled in this release; sending on the wrong network can permanently lose funds.\n\n"
        "Deposit confirmation is read from Solana RPC.",
    )


@router.callback_query(F.data == "settings:show")
async def cb_settings(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    config = await svc.db.get_settings(callback.from_user.id)
    await callback.message.edit_text(
        "SNIPER & LAUNCHPAD PARAMETERS\n"
        "Choose a value to edit. Numeric values are validated and saved to the encrypted local database.\n\n"
        f"Launchpad: {config['launchpad']} · Pump mode: {config.get('pump_mode', 'fresh')}",
        reply_markup=_settings_keyboard(config),
    )


@router.callback_query(F.data == "settings:risk")
async def cb_risk_settings(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    config = await svc.db.get_settings(callback.from_user.id)
    await callback.message.edit_text(
        "MEV & RISK CONTROL\n"
        "The Jito shield submits the swap and tip as an atomic private bundle. It does not "
        "guarantee inclusion or protect against every form of MEV.\n\n"
        f"MEV shield: {'ON' if config['mev_shield'] else 'OFF'}\n"
        f"Stop loss: {config['stop_loss_pct']}%\n"
        f"Daily drawdown cap: {config['max_daily_drawdown_pct']}%\n"
        f"Daily position cap: {config['daily_position_limit']}\n"
        f"Top-10 holder cap: {config['max_top10_concentration_pct']}%\n",
        reply_markup=_settings_keyboard(config),
    )


@router.callback_query(F.data.startswith("settings:launchpad:"))
async def cb_launchpad(callback: CallbackQuery) -> None:
    svc = _svc()
    value = callback.data.rsplit(":", 1)[-1]
    if value not in {"pump.fun", "raydium", "both"}:
        await callback.answer("Invalid launchpad", show_alert=True)
        return
    await svc.db.update_settings(callback.from_user.id, {"launchpad": value})
    await callback.answer("Launchpad saved")
    config = await svc.db.get_settings(callback.from_user.id)
    await callback.message.edit_reply_markup(reply_markup=_settings_keyboard(config))


@router.callback_query(F.data.startswith("settings:pumpmode:"))
async def cb_pump_mode(callback: CallbackQuery) -> None:
    svc = _svc()
    value = callback.data.rsplit(":", 1)[-1]
    if value not in {"fresh", "migration"}:
        await callback.answer("Invalid Pump.fun mode", show_alert=True)
        return
    await svc.db.update_settings(callback.from_user.id, {"pump_mode": value})
    await callback.answer("Pump.fun mode saved")
    config = await svc.db.get_settings(callback.from_user.id)
    await callback.message.edit_reply_markup(reply_markup=_settings_keyboard(config))


@router.callback_query(F.data == "settings:toggle:mev_shield")
async def cb_toggle_mev(callback: CallbackQuery) -> None:
    svc = _svc()
    config = await svc.db.get_settings(callback.from_user.id)
    await svc.db.update_settings(callback.from_user.id, {"mev_shield": not bool(config["mev_shield"])})
    config = await svc.db.get_settings(callback.from_user.id)
    await callback.answer(f"MEV shield {'ON' if config['mev_shield'] else 'OFF'}")
    await callback.message.edit_reply_markup(reply_markup=_settings_keyboard(config))


@router.callback_query(F.data.startswith("settings:field:"))
async def cb_setting_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":", 2)[2]
    allowed = {
        "buy_amount_sol", "slippage_pct", "priority_fee_lamports",
        "jito_tip_lamports", "take_profit_pct", "stop_loss_pct",
        "max_hold_seconds", "moon_bag_pct", "daily_position_limit",
        "max_daily_drawdown_pct", "max_top10_concentration_pct",
        "pump_migration_progress_pct", "min_liquidity_usd",
        "min_liquidity_sol", "min_lp_locked_pct", "max_dev_wallet_pct",
    }
    if field not in allowed:
        await callback.answer("Unknown setting", show_alert=True)
        return
    await callback.answer()
    await state.set_state(ParameterFlow.value)
    await state.update_data(field=field)
    labels = {
        "buy_amount_sol": "Buy amount in SOL (for example 0.05)",
        "slippage_pct": "Maximum slippage percentage (0.1 to 50)",
        "priority_fee_lamports": "Compute-unit price in micro-lamports (integer)",
        "jito_tip_lamports": "Jito tip in lamports (for example 1000000)",
        "take_profit_pct": "Take-profit percentage (for example 10)",
        "stop_loss_pct": "Stop-loss percentage (for example 15)",
        "max_hold_seconds": "Maximum hold time in seconds (5 to 3600)",
        "moon_bag_pct": "Moon-bag percentage (0 to 90)",
        "daily_position_limit": "Maximum new trades per UTC day (1 to 100)",
        "max_daily_drawdown_pct": "Daily realized drawdown stop percentage (1 to 50)",
        "max_top10_concentration_pct": "Maximum top-10 holder concentration percentage",
        "pump_migration_progress_pct": "Pump.fun curve progress trigger percentage (1 to 99)",
        "min_liquidity_usd": "Minimum indexed pool liquidity in USD",
        "min_liquidity_sol": "Minimum pool liquidity in SOL",
        "min_lp_locked_pct": "Minimum liquidity percentage that must be locked or burned",
        "max_dev_wallet_pct": "Maximum developer wallet share percentage",
    }
    await callback.message.answer(labels[field] + "\nSend the new numeric value.")


def _validated_setting(field: str, raw: str):
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, AttributeError):
        raise ValueError("Enter a valid number.")
    if not value.is_finite():
        raise ValueError("Enter a finite number.")
    ranges = {
        "buy_amount_sol": (Decimal("0.001"), Decimal("0.25")),
        "slippage_pct": (Decimal("0.1"), Decimal("50")),
        "priority_fee_lamports": (Decimal("0"), Decimal("100000000")),
        "jito_tip_lamports": (Decimal("0"), Decimal("100000000")),
        "take_profit_pct": (Decimal("0.5"), Decimal("500")),
        "stop_loss_pct": (Decimal("0.5"), Decimal("95")),
        "max_hold_seconds": (Decimal("5"), Decimal("86400")),
        "moon_bag_pct": (Decimal("0"), Decimal("90")),
        "take_profit_2_pct": (Decimal("1"), Decimal("1000")),
        "take_profit_2_sell_pct": (Decimal("1"), Decimal("90")),
        "trailing_stop_pct": (Decimal("1"), Decimal("80")),
        "min_liquidity_sol": (Decimal("0.1"), Decimal("100000")),
        "min_lp_locked_pct": (Decimal("50"), Decimal("100")),
        "max_dev_wallet_pct": (Decimal("0.1"), Decimal("100")),
        "daily_position_limit": (Decimal("1"), Decimal("100")),
        "max_daily_drawdown_pct": (Decimal("1"), Decimal("50")),
        "max_top10_concentration_pct": (Decimal("1"), Decimal("100")),
        "pump_migration_progress_pct": (Decimal("1"), Decimal("99")),
        "min_liquidity_usd": (Decimal("0"), Decimal("100000000")),
    }
    low, high = ranges[field]
    if value < low or value > high:
        raise ValueError(f"Value must be between {low} and {high}.")
    if field in {"max_hold_seconds", "daily_position_limit", "priority_fee_lamports", "jito_tip_lamports"}:
        if value != value.to_integral_value():
            raise ValueError("This setting must be a whole number.")
        return int(value)
    return str(value.normalize())


@router.message(ParameterFlow.value)
async def handle_parameter_value(message: Message, state: FSMContext) -> None:
    svc = _svc()
    data = await state.get_data()
    field = data.get("field")
    mode = str(data.get("mode", "live"))
    try:
        value = _validated_setting(field, message.text or "")
        if mode == "paper":
            if not svc.engine.paper_engine:
                raise ValueError("Paper engine is not available.")
            await svc.engine.paper_engine.store.update_settings(message.from_user.id, {field: value}, "paper")
        else:
            await svc.db.update_settings(message.from_user.id, {field: value})
        await message.answer(f"{mode.title()} setting saved.")
    except (ValueError, TypeError) as exc:
        await message.answer(f"Setting was not changed: {exc}")
    finally:
        await state.clear()
    if mode == "paper" and svc.engine.paper_engine:
        config = await svc.engine.paper_engine.store.settings(message.from_user.id, "paper")
        await message.answer("PAPER SETTINGS", reply_markup=_paper_settings_keyboard(config))
    else:
        config = await svc.db.get_settings(message.from_user.id)
        await message.answer("LIVE SETTINGS", reply_markup=_settings_keyboard(config))


@router.callback_query(F.data == "fees:show")
async def cb_fees(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    try:
        price = await svc.oracle.sol_usd()
        activation = usd_to_lamports("1", price)
        fee_conversion = f"$1.00 ≈ {format_sol(activation)} SOL at {price} USD/SOL ({svc.oracle.source})."
    except Exception:
        fee_conversion = "SOL/USD quote unavailable; fee transfers are locked until a fresh price is available."
    await callback.message.edit_text(
        "9N8 TREASURY FEES\n\n"
        "Activation: $1.00 once before first live execution\n"
        "Winning trade performance: 0.1% of conservative positive realized-profit estimate\n"
        "Selected SOL withdrawal: no platform fee; the real Solana network fee applies\n"
        "Stop & delete / key rotation: $1.00\n"
        "Losing trades: no performance fee\n\n"
        + fee_conversion
        + "\n\nEvery network fee and Jito tip is separate from the platform fee.",
        reply_markup=_fees_keyboard(),
    )


@router.callback_query(F.data == "engine:execute")
async def cb_execute(callback: CallbackQuery) -> None:
    await callback.answer()
    svc = _svc()
    readiness = await svc.engine.live_readiness(callback.from_user.id)
    enabled = await svc.engine.paper_engine.store.mode_enabled(callback.from_user.id, "live")
    await callback.message.answer(
        await _live_menu_text(callback.from_user.id),
        reply_markup=_live_menu_keyboard(enabled, readiness["ready"]),
    )


@router.callback_query(F.data == "activation:confirm")
async def cb_activation_confirm(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer("Confirming the activation payment…")
    user = await svc.db.get_user(callback.from_user.id)
    if user and user.get("activation_paid"):
        await callback.message.answer("Activation is already paid. Check /live for readiness.")
        return
    try:
        amount, signature = await svc.wallet.transfer_usd_fee(
            callback.from_user.id,
            "1.00",
            "activation",
            f"activation:{callback.from_user.id}",
        )
        await svc.db.set_activation_paid(callback.from_user.id, True)
        readiness = await svc.engine.live_readiness(callback.from_user.id)
        await callback.message.answer(
            f"Activation paid: {format_sol(amount)} SOL\n"
            f"Receipt: https://solscan.io/tx/{signature}\n\n"
            "This payment does not start live trading. Review your remaining requirements with /live.\n"
            + "\n".join("• " + str(item) for item in readiness["blockers"]),
            reply_markup=_live_menu_keyboard(False, readiness["ready"]),
        )
    except Exception as exc:
        await callback.message.answer(f"Activation was not completed: {type(exc).__name__}: {str(exc)[:220]}")


@router.callback_query(F.data == "engine:stop")
async def cb_stop(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    await svc.engine.stop_live_user(callback.from_user.id)
    await callback.message.answer("Live entries paused. Paper mode has not been changed.")


@router.callback_query(F.data == "wallet:exit")
async def cb_wallet_exit(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "REAL WALLET CONTROLS\n\n"
        "Withdraw a chosen part of your real SOL balance after closing all live positions. "
        "Paper balances are virtual and cannot be withdrawn.\n\n"
        "Stop & Delete remains a separate wallet-rotation operation.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Choose SOL Withdrawal Amount", callback_data="withdraw:menu")],
            [InlineKeyboardButton(text="Stop & Delete / Rotate Wallet", callback_data="wallet:wipe")],
            [InlineKeyboardButton(text="Back", callback_data="home:show")],
        ]),
    )


async def _show_withdraw_options(message: Message, telegram_id: int) -> None:
    svc = _svc()
    if await svc.db.active_positions(telegram_id):
        await message.answer(
            "WITHDRAWAL PAUSED\nClose all live token positions first. Paper positions do not affect real wallet withdrawals.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Open Live Positions", callback_data="positions:live")],
                [InlineKeyboardButton(text="Back", callback_data="home:show")],
            ]),
        )
        return
    balance = await svc.wallet.balance(telegram_id)
    # Keep 0.00002 SOL aside until the exact network fee is quoted for the signed transfer.
    withdrawable = max(0, balance - 20_000)
    if withdrawable <= 0:
        await message.answer("No SOL is available to withdraw after keeping a network-fee reserve.")
        return
    amount_text = format_sol(withdrawable)
    await message.answer(
        "💸 WITHDRAW LIVE SOL\n\n"
        f"Wallet balance: {format_sol(balance)} SOL\n"
        f"Maximum selectable payout: {amount_text} SOL\n"
        "A network-fee reserve is kept. No platform fee is added to these withdrawals.\n\n"
        "Paper funds are virtual and cannot be withdrawn.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Withdraw 25%", callback_data="withdraw:percent:25"),
             InlineKeyboardButton(text="Withdraw 50%", callback_data="withdraw:percent:50")],
            [InlineKeyboardButton(text="Withdraw 100%", callback_data="withdraw:percent:100"),
             InlineKeyboardButton(text="Custom SOL", callback_data="withdraw:custom")],
            [InlineKeyboardButton(text="Cancel", callback_data="home:show")],
        ]),
    )


@router.callback_query(F.data == "withdraw:menu")
@router.callback_query(F.data == "wallet:withdraw")
async def cb_withdraw_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await _show_withdraw_options(callback.message, callback.from_user.id)


@router.callback_query(F.data.startswith("withdraw:percent:"))
async def cb_withdraw_percent(callback: CallbackQuery, state: FSMContext) -> None:
    svc = _svc()
    await callback.answer()
    try:
        percent = int(callback.data.rsplit(":", 1)[-1])
        if percent not in {25, 50, 100}:
            raise ValueError("Select 25%, 50% or 100%.")
        if await svc.db.active_positions(callback.from_user.id):
            raise ValueError("Close all live positions before withdrawing.")
        balance = await svc.wallet.balance(callback.from_user.id)
        available = max(0, balance - 20_000)
        amount = (available * percent) // 100
        if amount <= 0:
            raise ValueError("There is no withdrawable SOL after the network-fee reserve.")
        await state.clear()
        await state.update_data(amount_lamports=amount, submitting=False)
        await state.set_state(WithdrawFlow.destination)
        await callback.message.answer(
            f"Selected payout: {format_sol(amount)} SOL ({percent}% of available wallet balance).\n"
            "Now paste your destination Solana wallet address."
        )
    except Exception as exc:
        await callback.message.answer(f"Withdrawal amount not available: {str(exc)[:200]}")


@router.callback_query(F.data == "withdraw:custom")
async def cb_withdraw_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await state.set_state(WithdrawFlow.amount)
    await callback.message.answer("Enter the exact SOL amount to withdraw, for example 0.025. The bot will check the current balance and fee reserve.")


@router.message(WithdrawFlow.amount)
async def handle_withdraw_custom_amount(message: Message, state: FSMContext) -> None:
    svc = _svc()
    try:
        amount_value = Decimal((message.text or "").strip())
        if not amount_value.is_finite() or amount_value <= 0:
            raise ValueError("Enter a positive SOL amount.")
        amount = to_lamports(str(amount_value))
        if await svc.db.active_positions(message.from_user.id):
            raise ValueError("Close all live positions before withdrawing.")
        balance = await svc.wallet.balance(message.from_user.id)
        maximum = max(0, balance - 20_000)
        if amount > maximum:
            raise ValueError(f"Maximum available right now is {format_sol(maximum)} SOL.")
        await state.update_data(amount_lamports=amount, submitting=False)
        await state.set_state(WithdrawFlow.destination)
        await message.answer(
            f"Selected payout: {format_sol(amount)} SOL.\n"
            "Paste your destination Solana wallet address."
        )
    except Exception as exc:
        await message.answer(f"Custom withdrawal amount rejected: {str(exc)[:200]}")


@router.message(WithdrawFlow.destination)
async def handle_withdraw_destination(message: Message, state: FSMContext) -> None:
    svc = _svc()
    try:
        destination = str(Pubkey.from_string((message.text or "").strip()))
    except Exception:
        await message.answer("Invalid Solana address. No transfer was made. Send a valid base58 address.")
        return
    data = await state.get_data()
    amount = int(data.get("amount_lamports") or 0)
    if amount <= 0:
        await message.answer("Withdrawal amount expired. Start again with /withdraw.")
        await state.clear()
        return
    if await svc.db.active_positions(message.from_user.id):
        await message.answer("Close all live positions before withdrawing. No transfer was made.")
        await state.clear()
        return
    balance = await svc.wallet.balance(message.from_user.id)
    maximum = max(0, balance - 20_000)
    if amount > maximum:
        await message.answer("Your wallet balance changed. Start /withdraw again to refresh the available amount.")
        await state.clear()
        return
    await state.update_data(destination=destination, submitting=False)
    await state.set_state(None)
    await message.answer(
        "CONFIRM REAL SOL WITHDRAWAL\n\n"
        f"Destination:\n{destination}\n\n"
        f"Payout amount: {format_sol(amount)} SOL\n"
        "The network fee is quoted again before signing. Paper funds are never used.\n"
        "This transaction is submitted only after you press Confirm.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Confirm SOL Withdrawal", callback_data="withdraw:confirm"),
            InlineKeyboardButton(text="Cancel", callback_data="home:show"),
        ]]),
    )


@router.callback_query(F.data == "withdraw:confirm")
async def cb_withdraw_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    svc = _svc()
    telegram_id = callback.from_user.id
    lock = _withdraw_locks.setdefault(telegram_id, asyncio.Lock())
    await callback.answer("Checking balance and network fee…")
    async with lock:
        data = await state.get_data()
        amount = int(data.get("amount_lamports") or 0)
        destination = str(data.get("destination") or "")
        if not amount or not destination or data.get("submitting"):
            await callback.message.answer("This withdrawal confirmation is expired or already being processed. Use /withdraw to start again.")
            return
        await state.update_data(submitting=True)
        try:
            if await svc.db.active_positions(telegram_id):
                raise ValueError("Close all live positions before withdrawing.")
            balance = await svc.wallet.balance(telegram_id)
            if amount > max(0, balance - 10_000):
                raise ValueError("The real wallet balance changed. No transfer was made; refresh /withdraw.")
            # Stop new live entries but leave existing exits monitored.
            await svc.engine.stop_live_user(telegram_id)
            sent, network_fee, signature = await svc.wallet.withdraw_amount(telegram_id, destination, amount)
            await callback.message.answer(
                "✅ WITHDRAWAL CONFIRMED\n"
                f"Sent: {format_sol(sent)} SOL\n"
                f"Network fee: {format_sol(network_fee)} SOL\n"
                f"Destination: {destination}\n"
                f"Solscan: https://solscan.io/tx/{signature}"
            )
        except Exception as exc:
            await callback.message.answer(
                "WITHDRAWAL NOT COMPLETED / CHECK STATUS\n"
                f"{type(exc).__name__}: {str(exc)[:500]}\n\n"
                "Do not repeat a withdrawal when a transaction signature is shown with an unknown outcome."
            )
        finally:
            await state.clear()


@router.callback_query(F.data == "wallet:withdraw:confirm")
async def cb_legacy_withdraw_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("This older withdrawal preview has expired. Use /withdraw to choose an amount.", show_alert=True)
    await state.clear()


@router.callback_query(F.data == "wallet:wipe")
async def cb_wipe(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(WipeFlow.destination)
    await callback.message.answer(
        "Enter the external Solana address that should receive the old wallet's remaining SOL. "
        "The new bot wallet will be generated only after the payout transaction is confirmed."
    )


@router.message(WipeFlow.destination)
async def handle_wipe_destination(message: Message, state: FSMContext) -> None:
    try:
        destination = str(Pubkey.from_string((message.text or "").strip()))
    except Exception:
        await message.answer("Invalid Solana address. No transfer was made.")
        return
    await state.update_data(destination=destination)
    await state.set_state(None)
    await message.answer(
        "WARNING: Stop & Delete charges a $1 termination fee, transfers the old wallet's remaining "
        "SOL to your payout address, disables the old key and creates a new inactive wallet.\n\n"
        f"Payout address:\n{destination}\n\n"
        "This cannot undo transactions already signed or remove copies of previously exported keys.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Confirm Wipe & Rotate", callback_data="wallet:wipe:confirm"),
            InlineKeyboardButton(text="Cancel", callback_data="home:show"),
        ]]),
    )


@router.callback_query(F.data == "wallet:wipe:confirm")
async def cb_wipe_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    svc = _svc()
    await callback.answer()
    data = await state.get_data()
    destination = data.get("destination")
    if not destination:
        await callback.message.answer("Payout address expired. Start again.")
        return
    try:
        await svc.engine.stop_user(callback.from_user.id)
        address, secret, payout, signature = await svc.wallet.rotate_wallet(
            callback.from_user.id, destination
        )
        await svc.engine.reset_wallet_watcher(callback.from_user.id)
        await callback.message.answer(
            f"Old wallet payout: {format_sol(payout)} SOL\n"
            f"Receipt: https://solscan.io/tx/{signature}\n"
            f"New paused wallet address:\n{address}"
        )
        await _send_sensitive_key(callback.message, address, secret)
    except Exception as exc:
        await callback.message.answer(f"Wallet rotation was not completed: {exc}")
    finally:
        await state.clear()


@router.callback_query(F.data.startswith("paste:paper:"))
async def cb_pasted_mint_paper(callback: CallbackQuery) -> None:
    await callback.answer()
    mint = callback.data.rsplit(":", 1)[-1]
    await _show_paper_snipe_preview(callback.message, callback.from_user.id, mint)


@router.callback_query(F.data.startswith("paste:live:"))
async def cb_pasted_mint_live(callback: CallbackQuery) -> None:
    await callback.answer()
    mint = callback.data.rsplit(":", 1)[-1]
    await _show_live_snipe_preview(callback.message, callback.from_user.id, mint)


@router.message()
async def detect_pasted_solana_mint(message: Message) -> None:
    """Offer separate paper/live previews when a public token mint is pasted."""
    if not _is_private(message) or not message.text:
        return
    raw = message.text.strip()
    if raw.startswith("/") or " " in raw or not 32 <= len(raw) <= 44:
        return
    try:
        mint = str(Pubkey.from_string(raw))
    except Exception:
        return
    if mint != raw:
        return
    await message.answer(
        "🎯 SOLANA TOKEN ADDRESS DETECTED\n\n"
        f"Mint: {mint}\n\n"
        "Choose what you want to do. Paper uses virtual funds. Live only shows a preview first and requires a second confirmation before any real SOL is spent.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧪 Paper Preview", callback_data=f"paste:paper:{mint}")],
            [InlineKeyboardButton(text="🔴 Live Preview", callback_data=f"paste:live:{mint}")],
            [InlineKeyboardButton(text="Cancel", callback_data="home:show")],
        ]),
    )


@router.callback_query(F.data.startswith("position:panic:"))
async def cb_panic_sell(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    try:
        position_id = int(callback.data.rsplit(":", 1)[-1])
        await svc.engine.panic_sell(callback.from_user.id, position_id)
        await callback.message.answer("Panic sell submitted. Check the trade alert for confirmation.")
    except Exception as exc:
        await callback.message.answer(f"Panic sell failed: {exc}")
