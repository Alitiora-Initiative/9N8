from __future__ import annotations

import asyncio
import logging
import re
import time
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
from .networks import NETWORKS, execution_enabled, get_network

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
_dashboard_message_ids: dict[int, int] = {}
_pending_detected_mints: dict[int, str] = {}
_wallet_reveal_locks: dict[int, asyncio.Lock] = {}


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
        [InlineKeyboardButton(text="🌐 Switch Network / Chains", callback_data="network:show")],
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
            InlineKeyboardButton(text="⚙️ Live Settings", callback_data="settings:show"),
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


async def _network_selector_text(telegram_id: int) -> str:
    svc = _svc()
    address = await svc.db.get_wallet_address(telegram_id)
    sol_balance = "Wallet not created"
    if address:
        try:
            sol_balance = f"{format_sol(await svc.wallet.balance(telegram_id))} SOL"
        except Exception:
            sol_balance = "Temporarily unavailable"

    lines = [
        "🌐 9N8 NETWORK SELECTOR",
        "",
        "Selected execution network: 🟣 SOLANA MAINNET",
        "",
        f"🟣 Solana — ACTIVE · {sol_balance}",
        "Markets: Pump.fun and Raydium",
        "Data: Helius · Swaps: Jupiter · Private routing: Jito",
        "",
    ]
    for network in NETWORKS:
        if network.code == "solana":
            continue
        lines.extend([
            f"{network.name} — NOT CONNECTED",
            f"Native asset: {network.native_symbol}",
            f"DEX targets: {network.dex_targets} (not wired)",
            "Wallet and live balance: not configured",
            "",
        ])
    lines.extend([
        "EVM network selection is currently blocked. This runtime has no EVM wallet vault, RPC listener, contract-risk adapter, DEX swap executor, or EVM position watchdog.",
        "Selecting an unavailable network will not change the active engine. Do not send ETH or BNB assets to the Solana deposit address.",
    ])
    return "\n".join(lines)


def _network_selector_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟣 Solana — ACTIVE", callback_data="network:select:solana")],
        [
            InlineKeyboardButton(text="🔵 Base — NOT CONNECTED", callback_data="network:select:base"),
            InlineKeyboardButton(text="🔷 Ethereum — NOT CONNECTED", callback_data="network:select:ethereum"),
        ],
        [InlineKeyboardButton(text="🟡 BNB Chain — NOT CONNECTED", callback_data="network:select:bnb")],
        [InlineKeyboardButton(text="💼 Manage Solana Wallet", callback_data="network:wallets")],
        [InlineKeyboardButton(text="🏠 Main Dashboard", callback_data="home:show")],
    ])


def _network_unavailable_text(code: str) -> str:
    network = get_network(code)
    if network is None:
        return "Unknown network. The active execution network remains Solana Mainnet."
    return (
        f"⛔ {network.name.upper()} IS NOT CONNECTED\n\n"
        "No chain switch was made. Solana Mainnet remains the only execution-enabled network.\n\n"
        f"Requested DEX targets: {network.dex_targets}\n"
        f"Status: {network.execution_note}\n\n"
        "No wallet was generated, no balance was queried, and no transaction can be submitted for this network in the current release."
    )


async def _solana_wallet_text(telegram_id: int) -> str:
    svc = _svc()
    address = await svc.db.get_wallet_address(telegram_id)
    if not address:
        return (
            "💼 SOLANA WALLET MANAGEMENT\n\n"
            "No Solana trading wallet is connected. Create a dedicated wallet or import an existing Solana key."
        )
    try:
        balance = f"{format_sol(await svc.wallet.balance(telegram_id))} SOL"
    except Exception:
        balance = "Temporarily unavailable"
    return (
        "💼 SOLANA WALLET MANAGEMENT\n\n"
        f"Network: Solana Mainnet\n"
        f"Address: {address}\n"
        f"Balance: {balance}\n\n"
        "Only send Solana-native assets to this address. Base, Ethereum and BNB Chain deposits are not supported here."
    )


async def _show_command_panel(
    message: Message, text: str, keyboard: InlineKeyboardMarkup,
) -> None:
    telegram_id = message.from_user.id
    existing_id = _dashboard_message_ids.get(telegram_id)
    if existing_id is not None:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=existing_id,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                existing_id = None
    if existing_id is None:
        sent = await message.answer(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        _dashboard_message_ids[telegram_id] = sent.message_id
    try:
        await message.delete()
    except Exception:
        pass



async def _render_quick_snipe_panel(
    message: Message, telegram_id: int, text: str, keyboard: InlineKeyboardMarkup,
) -> None:
    panel_id = _dashboard_message_ids.get(telegram_id)
    if panel_id == message.message_id:
        try:
            await message.edit_text(
                text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            return
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.warning(
                "Could not edit the current quick-snipe panel for Telegram account %s (%s)",
                telegram_id,
                type(exc).__name__,
            )
    sent = await message.answer(
        text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    _dashboard_message_ids[telegram_id] = sent.message_id
    if (
        message.chat.type == "private"
        and message.from_user is not None
        and message.from_user.id == telegram_id
        and (message.text or "").startswith("/")
    ):
        try:
            await message.delete()
        except Exception:
            pass


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
    mev = "ON" if config.get("mev_shield", True) else "OFF"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Pump.fun", callback_data="settings:launchpad:pump.fun"),
            InlineKeyboardButton(text="Raydium", callback_data="settings:launchpad:raydium"),
            InlineKeyboardButton(text="Both", callback_data="settings:launchpad:both"),
        ],
        [
            InlineKeyboardButton(text="Fresh Launch", callback_data="settings:pumpmode:fresh"),
            InlineKeyboardButton(text="Migration", callback_data="settings:pumpmode:migration"),
        ],
        [InlineKeyboardButton(text=f"MEV Shield: {mev}", callback_data="settings:toggle:mev_shield")],
        [
            InlineKeyboardButton(text=f"Buy: {config.get('buy_amount_sol','0.05')} SOL", callback_data="settings:field:buy_amount_sol"),
            InlineKeyboardButton(text=f"Buy slip: {config.get('slippage_pct','15')}%", callback_data="settings:field:slippage_pct"),
        ],
        [
            InlineKeyboardButton(text=f"Sell slip: {config.get('sell_slippage_pct','20')}%", callback_data="settings:field:sell_slippage_pct"),
            InlineKeyboardButton(text=f"Jito tip: {format_sol(int(config.get('jito_tip_lamports',2000000)))} SOL", callback_data="settings:field:jito_tip_lamports"),
        ],
        [
            InlineKeyboardButton(text=f"Priority fee: {config.get('priority_fee_lamports',10000)} μLamports", callback_data="settings:field:priority_fee_lamports"),
        ],
        [
            InlineKeyboardButton(text=f"TP1: +{config.get('take_profit_pct','100')}% / sell 50%", callback_data="settings:field:take_profit_pct"),
            InlineKeyboardButton(text=f"Trail: {config.get('trailing_stop_pct','20')}%", callback_data="settings:field:trailing_stop_pct"),
        ],
        [
            InlineKeyboardButton(text=f"Hard stop: -{config.get('stop_loss_pct','30')}%", callback_data="settings:field:stop_loss_pct"),
            InlineKeyboardButton(text=f"Max hold: {int(config.get('max_hold_seconds',14400))//3600}h", callback_data="settings:field:max_hold_seconds"),
        ],
        [
            InlineKeyboardButton(text=f"Min liquidity: {config.get('min_liquidity_sol','5')} SOL", callback_data="settings:field:min_liquidity_sol"),
            InlineKeyboardButton(text=f"LP lock: {config.get('min_lp_locked_pct','80')}%", callback_data="settings:field:min_lp_locked_pct"),
        ],
        [
            InlineKeyboardButton(text=f"Top 10 cap: {config.get('max_top10_concentration_pct','25')}%", callback_data="settings:field:max_top10_concentration_pct"),
            InlineKeyboardButton(text=f"Dev cap: {config.get('max_dev_wallet_pct','5')}%", callback_data="settings:field:max_dev_wallet_pct"),
        ],
        [
            InlineKeyboardButton(text=f"Daily trades: {config.get('daily_position_limit',5)}", callback_data="settings:field:daily_position_limit"),
            InlineKeyboardButton(text=f"Daily loss cap: {config.get('max_daily_drawdown_pct','15')}%", callback_data="settings:field:max_daily_drawdown_pct"),
        ],
        [InlineKeyboardButton(text="🛡️ Open Anti-Rug Gatekeeper", callback_data="safety:live")],
        [InlineKeyboardButton(text="Back to Dashboard", callback_data="home:show")],
    ])


def _safety_keyboard(config: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Mint authority revoked: REQUIRED", callback_data="safety:locked:mint")],
        [InlineKeyboardButton(text="✅ Freeze authority revoked: REQUIRED", callback_data="safety:locked:freeze")],
        [InlineKeyboardButton(text=f"LP burned/locked minimum: {config.get('min_lp_locked_pct','80')}%", callback_data="settings:field:min_lp_locked_pct")],
        [InlineKeyboardButton(text=f"Minimum liquidity: {config.get('min_liquidity_sol','5')} SOL", callback_data="settings:field:min_liquidity_sol")],
        [InlineKeyboardButton(text=f"Max top-10 holders: {config.get('max_top10_concentration_pct','25')}%", callback_data="settings:field:max_top10_concentration_pct")],
        [InlineKeyboardButton(text=f"Max single developer: {config.get('max_dev_wallet_pct','5')}%", callback_data="settings:field:max_dev_wallet_pct")],
        [InlineKeyboardButton(text="⚙️ All Trading Settings", callback_data="settings:show")],
        [InlineKeyboardButton(text="🏠 Main Dashboard", callback_data="home:show")],
    ])


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



async def _onboarding_text(telegram_id: int, key_revealed: bool = False) -> str:
    svc = _svc()
    address = await svc.db.get_wallet_address(telegram_id)
    if not address:
        return "9N8 WALLET ONBOARDING\n\nNo Solana wallet is connected."
    balance_text = "Temporarily unavailable"
    rpc_latency = "not measured"
    try:
        started = time.perf_counter()
        balance = await svc.wallet.balance(telegram_id)
        rpc_latency = f"{(time.perf_counter() - started) * 1000:.0f} ms"
        balance_text = f"{format_sol(balance)} SOL"
    except Exception:
        pass
    jito_status = "configured" if bool(getattr(svc.settings, "jito_base_url", "")) else "not configured"
    key_status = (
        "Private key reveal: USED — this wallet's one-time reveal is complete. Keep your offline backup safe."
        if key_revealed
        else "Private key: ENCRYPTED AND HIDDEN. Reveal it once below, save it offline, and never share it. The Telegram message will be deleted after 60 seconds, but deletion cannot erase screenshots or notification copies."
    )
    return (
        "🔐 9N8 SECURE WALLET ONBOARDING\n\n"
        "Your dedicated Solana Mainnet trading wallet has been created. Its private key is encrypted at rest in the 9N8 wallet vault.\n\n"
        f"Wallet address:\n{address}\n\n"
        f"Balance: {balance_text}\n"
        f"Helius RPC response: {rpc_latency}\n"
        f"Jito Block Engine endpoint: {jito_status}\n\n"
        f"{key_status}\n\n"
        "EVM wallets: not generated in this release. Base, Ethereum and BNB Chain remain non-executable."
    )


def _onboarding_keyboard(key_revealed: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if not key_revealed:
        rows.append([InlineKeyboardButton(text="🔑 Reveal Private Key (One-Time)", callback_data="wallet:reveal")])
    else:
        rows.append([InlineKeyboardButton(text="✅ Private Key Revealed", callback_data="wallet:reveal")])
    rows.append([InlineKeyboardButton(text="➡️ Continue to Main Dashboard", callback_data="home:show")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    pump_mode = str(live_config.get("pump_mode", "migration")).upper()
    return (
        "⚡ 9N8 · SOLANA SNIPER DASHBOARD\n\n"
        "NETWORK: 🟣 SOLANA MAINNET\n"
        "EVM networks: displayed in selector · execution not connected\n\n"
        "TWO SEPARATE BOOKS\n"
        f"🧪 Paper Trading: {'RUNNING' if paper_on else 'PAUSED'} · {paper_balance}\n"
        f"Paper practice days: {paper_days}/7\n"
        f"🔴 AUTONOMOUS LIVE MODE: {'ON' if live_on else 'OFF'}\n"
        f"Blockchain listener: {scanner_status} · Pump.fun / Raydium\n"
        f"Open live positions: {len(live_positions)}\n"
        f"Marked live PnL: {format_sol(unrealized)} SOL\n\n"
        f"Wallet: {live_balance} · {live_balance_usd}\n"
        f"Wallet RPC response: {rpc_latency}\n"
        f"Address: {address or 'Create a wallet to receive SOL'}\n"
        f"Activation: {activation} · Operator live switch: {'ON' if svc.settings.live_trading_enabled else 'OFF'}\n\n"
        "ACTIVE LIVE RULES\n"
        f"• Strategy: {live_config.get('launchpad','both')} · Pump mode: {pump_mode}\n"
        f"• Buy: {live_config.get('buy_amount_sol','0.05')} SOL · Min pool: {live_config.get('min_liquidity_sol','5')} SOL\n"
        f"• Buy/sell slippage: {live_config.get('slippage_pct','15')}% / {live_config.get('sell_slippage_pct','20')}%\n"
        f"• Jito tip: {format_sol(int(live_config.get('jito_tip_lamports',2000000)))} SOL\n"
        f"• TP1: +{live_config.get('take_profit_pct','100')}% (sell 50%) · Trail: {live_config.get('trailing_stop_pct','20')}%\n"
        f"• Hard stop: -{live_config.get('stop_loss_pct','30')}% · Max hold: {int(live_config.get('max_hold_seconds',14400))//3600}h\n\n"
        "Paper trades use real quotes and virtual funds only. Live mode uses real SOL and requires separate readiness and confirmation. "
        "This dashboard does not invent pool-scan or win-rate statistics."
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
        live_rows = await svc.db.list_trade_receipts(telegram_id,20)
        lines.extend(["", "LIVE TRANSACTIONS · BLOCKCHAIN RECEIPTS"])
        if not live_rows:
            lines.append("No live blockchain receipts recorded.")
        for row in live_rows[:20]:
            signature = str(row.get("signature") or "")
            lines.append(
                f"• {str(row.get('event_type','trade')).upper()} · {str(row.get('status','unknown')).upper()} "
                f"· {str(row.get('mint') or 'SOL')[:8]}…"
            )
            lines.append(f"  https://solscan.io/tx/{signature}")
        rejections = await svc.db.recent_rejections(telegram_id, 10)
        lines.extend(["", "RECENT SAFETY REJECTIONS"])
        if not rejections:
            lines.append("No stored token rejections.")
        for row in rejections:
            lines.append("• " + str(row.get("detail") or "Rejected token")[:240])
    return "\n".join(lines)


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    await state.clear()
    telegram_id = message.from_user.id
    existing = await svc.db.get_user(telegram_id)
    if not existing and await svc.db.user_count() >= svc.settings.max_users:
        await message.answer("9N8 is currently at its onboarding capacity. Please contact the operator.")
        return
    await svc.db.ensure_user(telegram_id)
    address = await svc.db.get_wallet_address(telegram_id)
    if not address:
        try:
            address, _secret = await svc.wallet.create_wallet(telegram_id)
            await svc.engine.watch_wallet(telegram_id)
            await svc.db.security_event(telegram_id, "wallet_created_onboarding", address)
        except Exception as exc:
            await message.answer(
                "9N8 could not create the dedicated Solana wallet. No deposit address is available.\n"
                f"{type(exc).__name__}: {str(exc)[:180]}"
            )
            return
        await _show_command_panel(
            message,
            await _onboarding_text(telegram_id),
            _onboarding_keyboard(),
        )
        return
    revealed = await svc.db.wallet_key_was_revealed(telegram_id, address)
    await _show_command_panel(
        message,
        await _dashboard_text(telegram_id) if revealed else await _onboarding_text(telegram_id),
        _home_keyboard(svc.settings.webapp_url) if revealed else _onboarding_keyboard(),
    )


@router.message(Command("chains"))
@router.message(Command("network"))
async def cmd_networks(message: Message) -> None:
    if not _is_private(message):
        return
    await _show_command_panel(
        message,
        await _network_selector_text(message.from_user.id),
        _network_selector_keyboard(),
    )


@router.callback_query(F.data == "network:show")
async def cb_network_show(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        await _network_selector_text(callback.from_user.id),
        reply_markup=_network_selector_keyboard(),
    )


@router.callback_query(F.data.startswith("network:select:"))
async def cb_network_select(callback: CallbackQuery) -> None:
    code = callback.data.rsplit(":", 1)[-1].strip().lower()
    network = get_network(code)
    if network is None:
        await callback.answer("Unknown network.", show_alert=True)
        return
    if not execution_enabled(code):
        await callback.answer(
            f"{network.name} is not connected. No chain switch was made.",
            show_alert=True,
        )
        await callback.message.edit_text(
            _network_unavailable_text(code),
            reply_markup=_network_selector_keyboard(),
        )
        return
    await callback.answer("Solana Mainnet remains selected.")
    await callback.message.edit_text(
        await _network_selector_text(callback.from_user.id),
        reply_markup=_network_selector_keyboard(),
    )


@router.callback_query(F.data == "network:wallets")
async def cb_network_wallets(callback: CallbackQuery) -> None:
    await callback.answer()
    address = await _svc().db.get_wallet_address(callback.from_user.id)
    rows = []
    if address:
        rows.append([InlineKeyboardButton(text="📥 Deposit SOL", callback_data="deposit:show")])
        revealed = await _svc().db.wallet_key_was_revealed(callback.from_user.id, address)
        if not revealed:
            rows.append([InlineKeyboardButton(text="🔑 Reveal Private Key (One-Time)", callback_data="wallet:reveal")])
    else:
        rows.extend([
            [InlineKeyboardButton(text="Create Solana Wallet", callback_data="wallet:create")],
            [InlineKeyboardButton(text="Import Solana Wallet", callback_data="wallet:import")],
        ])
    rows.extend([
        [InlineKeyboardButton(text="🌐 Back to Networks", callback_data="network:show")],
        [InlineKeyboardButton(text="🏠 Main Dashboard", callback_data="home:show")],
    ])
    await callback.message.edit_text(
        await _solana_wallet_text(callback.from_user.id),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )





async def _show_paper_snipe_preview(message: Message, telegram_id: int, mint_text: str, amount_sol: str = "0.05") -> None:
    svc = _svc()
    paper = svc.engine.paper_engine
    if paper is None:
        await _render_quick_snipe_panel(
            message, telegram_id,
            "PAPER TRADING IS NOT CONFIGURED.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Dashboard", callback_data="home:show")],
            ]),
        )
        return
    if not await paper.store.mode_enabled(telegram_id, "paper"):
        await _render_quick_snipe_panel(
            message, telegram_id,
            "PAPER TRADING IS PAUSED. Start the virtual practice account before previewing a paper buy.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="▶ Start Paper Trading", callback_data="paper:start")],
                [InlineKeyboardButton(text="🏠 Dashboard", callback_data="home:show")],
            ]),
        )
        return
    try:
        mint = str(Pubkey.from_string(mint_text.strip()))
        preview = await paper.preview_mint(telegram_id, mint, amount_sol)
    except Exception as exc:
        await _render_quick_snipe_panel(
            message, telegram_id,
            f"Could not scan this Solana mint: {type(exc).__name__}: {str(exc)[:180]}",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛡️ Safety Filters", callback_data="safety:show")],
                [InlineKeyboardButton(text="🏠 Dashboard", callback_data="home:show")],
            ]),
        )
        return
    if not preview.get("passed"):
        reasons = "\n".join("• " + str(item) for item in preview.get("reasons", [])[:6])
        await _render_quick_snipe_panel(
            message, telegram_id,
            "🛡️ PAPER SNIPE REJECTED\n"
            f"Mint: {mint}\n{reasons}\n\n"
            "No practice buy was recorded.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛡️ Safety Filters", callback_data="safety:show")],
                [InlineKeyboardButton(text="🏠 Dashboard", callback_data="home:show")],
            ]),
        )
        return
    _pending_paper_snipes[telegram_id] = {"mint": mint, "preview": preview}
    risk = preview.get("risk", {})
    await _render_quick_snipe_panel(
        message, telegram_id,
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
        InlineKeyboardMarkup(inline_keyboard=[
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
    await _show_command_panel(
        message,
        await _paper_menu_text(message.from_user.id),
        _paper_menu_keyboard(enabled),
    )


@router.message(Command("paper_on"))
async def cmd_paper_on(message: Message) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    try:
        await svc.engine.start_paper_user(message.from_user.id)
        await _show_command_panel(
            message,
            await _paper_menu_text(message.from_user.id),
            _paper_menu_keyboard(True),
        )
    except Exception as exc:
        await message.answer(f"Paper mode could not start: {type(exc).__name__}: {str(exc)[:240]}")


@router.message(Command("paper_off"))
async def cmd_paper_off(message: Message) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    await svc.engine.stop_paper_user(message.from_user.id)
    await _show_command_panel(
        message,
        await _paper_menu_text(message.from_user.id),
        _paper_menu_keyboard(False),
    )


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
    await _show_command_panel(
        message,
        await _live_menu_text(message.from_user.id),
        _live_menu_keyboard(enabled, readiness["ready"]),
    )


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
    await _show_command_panel(
        message,
        await _positions_text(message.from_user.id),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧪 Paper Positions", callback_data="positions:paper"),
             InlineKeyboardButton(text="🔴 Live Positions", callback_data="positions:live")],
            [InlineKeyboardButton(text="🏠 Main Dashboard", callback_data="home:show")],
        ]),
    )


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
        await _show_command_panel(
            message,
            "No open virtual positions. Use /paper to review your practice account.",
            _paper_menu_keyboard(False),
        )
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
    await _show_command_panel(
        message,
        "\n\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


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
    text, keyboard = await _live_positions_view(rows)
    await _show_command_panel(message, text, keyboard)


async def _live_positions_view(rows: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["📊 LIVE ACTIVE POSITIONS · SOLANA", ""]
    buttons = []
    if not rows:
        lines.append("No open live positions.")
    for row in rows[:8]:
        mint = str(row["mint"])
        entry = int(row.get("initial_cost_lamports") or 0)
        current = int(row.get("last_value_lamports") or 0)
        realized = int(row.get("realized_lamports") or 0)
        net = realized + current - entry if current > 0 else realized
        pnl_pct = Decimal(str(row.get("last_pnl_pct") or "0"))
        phase = "MOONBAG · TRAILING ACTIVE" if bool(row.get("tp1_done")) else "INITIAL · WAITING FOR TP1"
        lines.append(
            f"{mint}\n"
            f"• Initial cost: {format_sol(entry)} SOL\n"
            f"• Current quoted exit value: {format_sol(current)} SOL\n"
            f"• Net PnL estimate: {format_sol(net)} SOL ({pnl_pct:+.2f}% mark)\n"
            f"• Realized PnL ledger: {format_sol(realized)} SOL\n"
            f"• Tokens: {row['token_amount_raw']} raw units\n"
            f"• Phase: {phase}"
        )
        buttons.append([
            InlineKeyboardButton(text=f"💰 Sell 50% {mint[:7]}…", callback_data=f"position:sell50:{row['id']}"),
            InlineKeyboardButton(text=f"🚨 Sell 100% {mint[:7]}…", callback_data=f"position:panic:{row['id']}"),
        ])
        buttons.append([
            InlineKeyboardButton(text="📈 Live Chart", url="https://dexscreener.com/solana/" + mint),
        ])
    buttons.extend([
        [InlineKeyboardButton(text="🔄 Refresh Live Prices", callback_data="positions:live")],
        [InlineKeyboardButton(text="🧨 PANIC SELL ALL POSITIONS", callback_data="positions:panic_all:prompt")],
        [InlineKeyboardButton(text="🏠 Back to Dashboard", callback_data="home:show")],
    ])
    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _send_live_positions(message: Message, rows: list[dict]) -> None:
    text, keyboard = await _live_positions_view(rows)
    await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


@router.callback_query(F.data == "positions:live")
async def cb_live_positions(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer("Refreshing position marks…")
    rows = await svc.db.active_positions(callback.from_user.id)
    text, keyboard = await _live_positions_view(rows)
    await callback.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("position:sell50:"))
async def cb_live_sell50(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer("Submitting your 50% sell…")
    try:
        position_id = int(callback.data.rsplit(":", 1)[-1])
        await svc.engine.sell_position_percent(callback.from_user.id, position_id, 50)
        rows = await svc.db.active_positions(callback.from_user.id)
        text, keyboard = await _live_positions_view(rows)
        await callback.message.edit_text("✅ 50% sell submitted.\n\n" + text, reply_markup=keyboard, disable_web_page_preview=True)
    except Exception as exc:
        await callback.message.edit_text(
            f"50% sell was not completed: {type(exc).__name__}: {str(exc)[:250]}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Refresh Positions", callback_data="positions:live")],
                [InlineKeyboardButton(text="🏠 Dashboard", callback_data="home:show")],
            ]),
        )


@router.callback_query(F.data.startswith("position:panic:"))
async def cb_panic_sell(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer("Submitting full position exit…")
    try:
        position_id = int(callback.data.rsplit(":", 1)[-1])
        await svc.engine.panic_sell(callback.from_user.id, position_id)
        rows = await svc.db.active_positions(callback.from_user.id)
        text, keyboard = await _live_positions_view(rows)
        await callback.message.edit_text("✅ Full-position exit submitted. Verify the on-chain receipt.\n\n" + text, reply_markup=keyboard, disable_web_page_preview=True)
    except Exception as exc:
        await callback.message.edit_text(
            f"Full-position exit was not completed: {type(exc).__name__}: {str(exc)[:250]}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Refresh Positions", callback_data="positions:live")],
                [InlineKeyboardButton(text="🏠 Dashboard", callback_data="home:show")],
            ]),
        )


@router.callback_query(F.data == "positions:panic_all:prompt")
async def cb_panic_sell_all_prompt(callback: CallbackQuery) -> None:
    rows = await _svc().db.active_positions(callback.from_user.id)
    if not rows:
        await callback.answer("There are no open live positions.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "⚠️ CONFIRM PANIC SELL ALL\n\n"
        f"This will attempt to market-sell all {len(rows)} open Solana positions sequentially. "
        "Quotes, network fees, slippage and failed transactions can affect the outcome. "
        "Check every resulting Solscan receipt.\n\n"
        "Do not press twice while exits are processing.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧨 CONFIRM SELL ALL", callback_data="positions:panic_all:confirm")],
            [InlineKeyboardButton(text="Cancel", callback_data="positions:live")],
        ]),
    )


@router.callback_query(F.data == "positions:panic_all:confirm")
async def cb_panic_sell_all_confirm(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer("Processing open positions one by one…")
    rows = await svc.db.active_positions(callback.from_user.id)
    outcomes = []
    for row in rows:
        position_id = int(row["id"])
        mint = str(row["mint"])
        try:
            await svc.engine.panic_sell(callback.from_user.id, position_id)
            outcomes.append(f"✅ {mint[:9]}… — exit submitted")
        except Exception as exc:
            outcomes.append(f"❌ {mint[:9]}… — {type(exc).__name__}: {str(exc)[:120]}")
    refreshed = await svc.db.active_positions(callback.from_user.id)
    text, keyboard = await _live_positions_view(refreshed)
    report = "🧨 PANIC SELL ALL RESULTS\n" + ("\n".join(outcomes) if outcomes else "No open positions were found.")
    await callback.message.edit_text(report + "\n\n" + text, reply_markup=keyboard, disable_web_page_preview=True)


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    if not _is_private(message):
        return
    await _show_command_panel(
        message,
        "⚙️ SETTINGS\n\nChoose which separate settings book you want to manage.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧪 Paper Settings",callback_data="paper:settings")],
            [InlineKeyboardButton(text="🔴 Live Settings",callback_data="settings:show")],
            [InlineKeyboardButton(text="🏠 Dashboard",callback_data="home:show")],
        ]),
    )


@router.message(Command("paper_settings"))
async def cmd_paper_settings(message: Message) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    config = await svc.engine.paper_engine.store.settings(message.from_user.id,"paper")
    await _show_command_panel(
        message,
        "PAPER SETTINGS · VIRTUAL ACCOUNT",
        _paper_settings_keyboard(config),
    )


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
        await _render_quick_snipe_panel(
            message, telegram_id,
            "🔴 LIVE SNIPE BLOCKED\n"
            f"{type(exc).__name__}: {str(exc)[:450]}\n\n"
            "No transaction was signed or submitted.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛡️ Live Safety Filters", callback_data="safety:live")],
                [InlineKeyboardButton(text="🏠 Dashboard", callback_data="home:show")],
            ]),
        )
        return
    _pending_live_snipes[telegram_id] = preview
    await _render_quick_snipe_panel(
        message, telegram_id,
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
        InlineKeyboardMarkup(inline_keyboard=[
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
        await callback.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
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
    config = await _svc().db.get_settings(message.from_user.id)
    await _show_command_panel(
        message,
        "🛡️ ANTI-RUG SAFETY GATEKEEPER · SOLANA\n\n"
        "Every required check must pass. Missing or unverifiable evidence blocks the buy.\n\n"
        "✅ Mint authority disabled: REQUIRED · cannot be switched off\n"
        "✅ Freeze authority disabled: REQUIRED · cannot be switched off\n"
        f"✅ LP burned/locked: minimum {config.get('min_lp_locked_pct','80')}%\n"
        f"✅ Minimum liquidity: {config.get('min_liquidity_sol','5')} SOL\n"
        f"✅ Maximum top-10 wallet share: {config.get('max_top10_concentration_pct','25')}%\n"
        f"✅ Maximum developer share: {config.get('max_dev_wallet_pct','5')}%\n\n"
        "The two authority rules are hard safety requirements and cannot be disabled from Telegram.",
        _safety_keyboard(config),
    )


@router.callback_query(F.data == "safety:show")
async def cb_safety_show(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    config = await svc.db.get_settings(callback.from_user.id)
    await callback.message.edit_text(
        "🛡️ ANTI-RUG SAFETY GATEKEEPER · SOLANA\n\n"
        "Every required check must pass. Missing or unverifiable evidence blocks the buy.\n\n"
        "✅ Mint authority disabled: REQUIRED · locked ON\n"
        "✅ Freeze authority disabled: REQUIRED · locked ON\n"
        f"✅ LP burned/locked minimum: {config.get('min_lp_locked_pct','80')}%\n"
        f"✅ Minimum liquidity: {config.get('min_liquidity_sol','5')} SOL\n"
        f"✅ Maximum top-10 share: {config.get('max_top10_concentration_pct','25')}%\n"
        f"✅ Maximum single developer share: {config.get('max_dev_wallet_pct','5')}%\n\n"
        "Authority checks cannot be disabled. Adjust the numeric thresholds using the buttons below.",
        reply_markup=_safety_keyboard(config),
    )


@router.callback_query(F.data == "safety:live")
async def cb_live_safety(callback: CallbackQuery) -> None:
    await cb_safety_show(callback)


@router.callback_query(F.data.startswith("safety:locked:"))
async def cb_safety_locked(callback: CallbackQuery) -> None:
    which = callback.data.rsplit(":", 1)[-1]
    label = "Mint authority revocation" if which == "mint" else "Freeze authority revocation"
    await callback.answer(
        f"{label} is mandatory and cannot be disabled.",
        show_alert=True,
    )


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    if not _is_private(message):
        return
    await _show_command_panel(
        message,
        await _history_text(message.from_user.id, "both"),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Paper History",callback_data="history:paper"),
             InlineKeyboardButton(text="Live Receipts",callback_data="history:live")],
            [InlineKeyboardButton(text="🏠 Dashboard",callback_data="home:show")],
        ]),
    )


@router.message(Command("paper_history"))
async def cmd_paper_history(message: Message) -> None:
    if not _is_private(message):
        return
    await _show_command_panel(
        message,
        await _history_text(message.from_user.id, "paper"),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Back to Paper Menu",callback_data="paper:menu")],
            [InlineKeyboardButton(text="All History",callback_data="history:show")],
        ]),
    )


@router.message(Command("live_history"))
async def cmd_live_history(message: Message) -> None:
    if not _is_private(message):
        return
    await _show_command_panel(
        message,
        await _history_text(message.from_user.id, "live"),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Back to Live Menu",callback_data="live:menu")],
            [InlineKeyboardButton(text="All History",callback_data="history:show")],
        ]),
    )


@router.message(Command("live_settings"))
async def cmd_live_settings(message: Message) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    config = await svc.db.get_settings(message.from_user.id)
    await _show_command_panel(
        message,
        "LIVE SETTINGS · REAL WALLET\nChanges apply only to future live entries.",
        _settings_keyboard(config),
    )


@router.message(Command("stop"))
@router.message(Command("live_off"))
async def cmd_live_off(message: Message) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    await svc.engine.stop_live_user(message.from_user.id)
    await _show_command_panel(
        message,
        "LIVE ENTRIES ARE OFF. Paper mode is unchanged. Existing real positions remain monitored.\n\n"
        + await _live_menu_text(message.from_user.id),
        _live_menu_keyboard(False, False),
    )


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
        "/chains — network selector and connection status\n"
        "/network — alias for /chains\n"
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
    await _show_command_panel(
        message,
        await _dashboard_text(message.from_user.id),
        _home_keyboard(_svc().settings.webapp_url),
    )


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
        await svc.wallet.create_wallet(callback.from_user.id)
        await svc.engine.watch_wallet(callback.from_user.id)
        address = await svc.db.get_wallet_address(callback.from_user.id)
        if address:
            await svc.db.security_event(callback.from_user.id, "wallet_created_onboarding", address)
        await callback.message.edit_text(
            await _onboarding_text(callback.from_user.id),
            reply_markup=_onboarding_keyboard(),
        )
    except WalletError as exc:
        await callback.message.answer(str(exc))


@router.callback_query(F.data == "wallet:reveal")
async def cb_wallet_reveal(callback: CallbackQuery) -> None:
    svc = _svc()
    telegram_id = callback.from_user.id
    lock = _wallet_reveal_locks.setdefault(telegram_id, asyncio.Lock())
    async with lock:
        address = await svc.db.get_wallet_address(telegram_id)
        if not address:
            await callback.answer("No Solana wallet is connected.", show_alert=True)
            return
        if await svc.db.wallet_key_was_revealed(telegram_id, address):
            await callback.answer(
                "This wallet's one-time key reveal has already been used. If the backup is lost, use the wallet-rotation flow.",
                show_alert=True,
            )
            await callback.message.edit_text(
                await _onboarding_text(telegram_id, key_revealed=True),
                reply_markup=_onboarding_keyboard(key_revealed=True),
            )
            return
        try:
            secret = await svc.db.load_secret(telegram_id)
            await svc.db.security_event(telegram_id, "wallet_key_revealed", address)
            await callback.answer("Sending the one-time recovery key in a 60-second auto-deleting message.")
            await _send_sensitive_key(callback.message, address, secret)
            await callback.message.edit_text(
                await _onboarding_text(telegram_id, key_revealed=True),
                reply_markup=_onboarding_keyboard(key_revealed=True),
            )
        except Exception as exc:
            logger.exception("One-time wallet reveal failed for Telegram account %s", telegram_id)
            await callback.answer(
                f"Private key could not be revealed ({type(exc).__name__}).",
                show_alert=True,
            )


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
        await message.answer(
            "Solana wallet imported and encrypted at rest.\n"
            f"Address: {address}\n"
            "You can reveal the stored recovery key once from the wallet onboarding screen."
        )
        await message.answer(
            await _onboarding_text(message.from_user.id),
            reply_markup=_onboarding_keyboard(),
        )
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
        "buy_amount_sol", "slippage_pct", "sell_slippage_pct", "priority_fee_lamports",
        "jito_tip_lamports", "take_profit_pct", "stop_loss_pct", "trailing_stop_pct",
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
        "slippage_pct": "Maximum BUY slippage percentage (0.1 to 50)",
        "sell_slippage_pct": "Maximum SELL slippage percentage (0.1 to 50)",
        "trailing_stop_pct": "Trailing stop drop from peak after TP1 (1 to 80)",
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
        "sell_slippage_pct": (Decimal("0.1"), Decimal("50")),
        "priority_fee_lamports": (Decimal("0"), Decimal("100000000")),
        "jito_tip_lamports": (Decimal("0"), Decimal("100000000")),
        "take_profit_pct": (Decimal("0.5"), Decimal("500")),
        "trailing_stop_pct": (Decimal("1"), Decimal("80")),
        "stop_loss_pct": (Decimal("0.5"), Decimal("95")),
        "max_hold_seconds": (Decimal("5"), Decimal("86400")),
        "moon_bag_pct": (Decimal("0"), Decimal("90")),
        "take_profit_2_pct": (Decimal("1"), Decimal("1000")),
        "take_profit_2_sell_pct": (Decimal("1"), Decimal("90")),
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
    return format(value.normalize(), "f")


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
        "Full SOL withdrawal: $1.00\n"
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
        await svc.db.security_event(callback.from_user.id, "wallet_created_onboarding", address)
        await callback.message.edit_text(
            "✅ WALLET ROTATED — NEW SOLANA WALLET PAUSED\n\n"
            f"Old wallet payout: {format_sol(payout)} SOL\n"
            f"Receipt: https://solscan.io/tx/{signature}\n"
            f"New wallet address:\n{address}\n\n"
            "Reveal the new recovery key once below and store it offline before depositing.",
            reply_markup=_onboarding_keyboard(),
        )
    except Exception as exc:
        await callback.message.answer(f"Wallet rotation was not completed: {exc}")
    finally:
        await state.clear()


@router.callback_query(F.data == "detected:paper")
async def cb_detected_paper(callback: CallbackQuery) -> None:
    mint = _pending_detected_mints.pop(callback.from_user.id, None)
    if not mint:
        await callback.answer("Token detection expired. Paste the address again.", show_alert=True)
        return
    await callback.answer("Preparing a paper-only quote…")
    await _show_paper_snipe_preview(callback.message, callback.from_user.id, mint)


@router.callback_query(F.data == "detected:live")
async def cb_detected_live(callback: CallbackQuery) -> None:
    mint = _pending_detected_mints.pop(callback.from_user.id, None)
    if not mint:
        await callback.answer("Token detection expired. Paste the address again.", show_alert=True)
        return
    await callback.answer("Preparing a live safety preview. No order is sent.")
    await _show_live_snipe_preview(callback.message, callback.from_user.id, mint)


@router.callback_query(F.data == "detected:cancel")
async def cb_detected_cancel(callback: CallbackQuery) -> None:
    _pending_detected_mints.pop(callback.from_user.id, None)
    await callback.answer("Cancelled. No trade was submitted.")
    await callback.message.edit_text(
        "Token-address detection cancelled. No trade was submitted.",
        reply_markup=_home_keyboard(_svc().settings.webapp_url),
    )


@router.message(F.text)
async def auto_detect_pasted_address(message: Message, state: FSMContext) -> None:
    if not _is_private(message):
        return
    if await state.get_state():
        return
    candidate = (message.text or "").strip()
    if not candidate:
        return

    evm_match = re.fullmatch(r"0x[a-fA-F0-9]{40}", candidate)
    if evm_match:
        await message.answer(
            "🔷 EVM CONTRACT ADDRESS DETECTED\n\n"
            "This address format belongs to an EVM network, but Base, Ethereum and BNB Chain are not connected to 9N8's execution runtime yet. "
            "No token scan, wallet lookup, quote or transaction was attempted.\n\n"
            "Which EVM network was this address created on? Choose Base, Ethereum or BNB Chain below. These choices currently show their connection status only; no token scan or transaction will run.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔵 Base", callback_data="network:select:base"),
                    InlineKeyboardButton(text="🔷 Ethereum", callback_data="network:select:ethereum"),
                ],
                [InlineKeyboardButton(text="🟡 BNB Chain", callback_data="network:select:bnb")],
                [InlineKeyboardButton(text="🌐 Network Selector", callback_data="network:show")],
                [InlineKeyboardButton(text="🏠 Main Dashboard", callback_data="home:show")],
            ]),
        )
        return

    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", candidate):
        return
    try:
        mint = str(Pubkey.from_string(candidate))
    except Exception:
        return
    if mint == "So11111111111111111111111111111111111111112":
        return

    _pending_detected_mints[message.from_user.id] = mint
    await message.answer(
        "🎯 SOLANA TOKEN ADDRESS DETECTED\n\n"
        f"Mint: {mint}\n\n"
        "Choose which isolated account should preview this token. A Live choice only prepares a quote and safety report; it does not buy until you review and confirm.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧪 Paper Preview", callback_data="detected:paper")],
            [InlineKeyboardButton(text="🔴 Live Safety Preview", callback_data="detected:live")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="detected:cancel")],
        ]),
    )
