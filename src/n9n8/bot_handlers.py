from __future__ import annotations

import asyncio
import logging
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
from .math_engine import format_sol, usd_to_lamports
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
    destination = State()


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


def configure_handlers(value: AppServices) -> None:
    global services
    services = value


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
            text="Open 9N8 Live Terminal",
            web_app=WebAppInfo(url=webapp_url),
        )])
    rows.extend([
        [
            InlineKeyboardButton(text="Create Wallet", callback_data="wallet:create"),
            InlineKeyboardButton(text="Import Wallet", callback_data="wallet:import"),
        ],
        [
            InlineKeyboardButton(text="Deposit Funds", callback_data="deposit:show"),
            InlineKeyboardButton(text="Sniper Setup", callback_data="settings:show"),
        ],
        [
            InlineKeyboardButton(text="MEV & Fees", callback_data="fees:show"),
            InlineKeyboardButton(text="Risk Limits", callback_data="settings:risk"),
        ],
        [
            InlineKeyboardButton(text="Execute Bot", callback_data="engine:execute"),
            InlineKeyboardButton(text="Stop Bot", callback_data="engine:stop"),
        ],
        [InlineKeyboardButton(text="Withdraw & Reset", callback_data="wallet:exit")],
        [InlineKeyboardButton(text="Refresh Status", callback_data="status:show")],
    ])
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
    config = await svc.db.get_settings(telegram_id)
    address = (user or {}).get("solana_address")
    balance_text = "Wallet not created"
    if address:
        try:
            balance_text = f"{format_sol(await svc.wallet.balance(telegram_id))} SOL"
        except Exception:
            balance_text = "Balance temporarily unavailable"
    mode = svc.settings.trading_mode.upper()
    status = "RUNNING" if config.get("enabled") else "PAUSED"
    activation = "PAID" if (user or {}).get("activation_paid") else "NOT PAID"
    return (
        "⚡ 9N8 · SOLANA AUTONOMOUS TERMINAL\n\n"
        "Launch radar: Pump.fun + Raydium\n"
        "Execution: Jupiter Swap API v2\n"
        "MEV routing: Jito private bundles\n"
        "Risk engine: mint authority · holders · liquidity · LP checks\n\n"
        f"Mode: {mode}\n"
        f"Bot state: {status}\n"
        f"Activation: {activation}\n"
        f"Wallet: {balance_text}\n"
        f"Deposit address: {address or 'Create a wallet to receive SOL'}\n\n"
        "Paper mode reports real launch events but does not fake fills. Live mode is "
        "separately locked by the server environment and requires a paid activation."
    )


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not _is_private(message):
        return
    svc = _svc()
    await state.clear()
    await svc.db.ensure_user(message.from_user.id)
    await message.answer(
        await _dashboard_text(message.from_user.id),
        reply_markup=_home_keyboard(svc.settings.webapp_url),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not _is_private(message):
        return
    await message.answer(
        "9N8 COMMANDS\n"
        "/start — dashboard\n"
        "/status — wallet and engine status\n"
        "/help — this help\n\n"
        "Use the dashboard buttons to create/import a wallet, deposit SOL, set sniper "
        "parameters, execute or pause the bot, withdraw, or rotate the wallet.\n\n"
        "The live Solana executor is disabled unless N9N8_TRADING_MODE=live and "
        "LIVE_TRADING_ENABLED=true are set by the operator. Keep paper mode enabled "
        "until you have tested the complete flow."
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
        "max_hold_seconds": (Decimal("5"), Decimal("3600")),
        "moon_bag_pct": (Decimal("0"), Decimal("90")),
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
    try:
        value = _validated_setting(field, message.text or "")
        await svc.db.update_settings(message.from_user.id, {field: value})
        await message.answer("Setting saved.")
    except (ValueError, TypeError) as exc:
        await message.answer(f"Setting was not changed: {exc}")
    finally:
        await state.clear()
    config = await svc.db.get_settings(message.from_user.id)
    await message.answer("CURRENT 9N8 SETTINGS", reply_markup=_settings_keyboard(config))


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
    svc = _svc()
    await callback.answer()
    user = await svc.db.get_user(callback.from_user.id)
    if not user or not user.get("solana_address"):
        await callback.message.answer("Create or import your Solana wallet first.")
        return
    if svc.settings.trading_mode == "live" and not user.get("activation_paid"):
        try:
            price = await svc.oracle.sol_usd(force=True)
            lamports = usd_to_lamports("1.00", price)
            await callback.message.answer(
                "LIVE ACTIVATION FEE\n\n"
                f"One-time fee: 1.00 USD ≈ {format_sol(lamports)} SOL\n"
                f"Price source: {svc.oracle.source}\n"
                "Press Confirm only if you authorize the transfer to the configured Master Treasury.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Confirm $1 Activation", callback_data="activation:confirm"),
                    InlineKeyboardButton(text="Cancel", callback_data="home:show"),
                ]]),
            )
        except Exception as exc:
            await callback.message.answer(f"Activation is locked: {exc}")
        return
    try:
        mode = await svc.engine.start_user(callback.from_user.id)
        await callback.message.answer(f"9N8 started.\nMode: {mode}")
    except Exception as exc:
        await callback.message.answer(f"Could not start 9N8: {exc}")


@router.callback_query(F.data == "activation:confirm")
async def cb_activation_confirm(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    try:
        amount, signature = await svc.wallet.transfer_usd_fee(
            callback.from_user.id,
            "1.00",
            "activation",
            f"activation:{callback.from_user.id}",
        )
        await svc.db.set_activation_paid(callback.from_user.id, True)
        mode = await svc.engine.start_user(callback.from_user.id)
        await callback.message.answer(
            f"Activation paid: {format_sol(amount)} SOL\n"
            f"Receipt: https://solscan.io/tx/{signature}\n"
            f"Engine: {mode}"
        )
    except Exception as exc:
        await callback.message.answer(f"Activation was not completed: {exc}")


@router.callback_query(F.data == "engine:stop")
async def cb_stop(callback: CallbackQuery) -> None:
    svc = _svc()
    await callback.answer()
    await svc.engine.stop_user(callback.from_user.id)


@router.callback_query(F.data == "wallet:exit")
async def cb_wallet_exit(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(
        "WALLET EXIT\n\n"
        "Withdraw All sends the remaining SOL, after the $1 platform fee and actual network fee, "
        "to your external Solana address. Open SPL-token positions must be closed first.\n\n"
        "Stop & Delete charges a $1 termination fee, pays the remaining SOL to your external "
        "address, retires the old key and creates a new paused wallet.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Withdraw All", callback_data="wallet:withdraw")],
            [InlineKeyboardButton(text="Stop & Delete / Rotate Key", callback_data="wallet:wipe")],
            [InlineKeyboardButton(text="Back", callback_data="home:show")],
        ]),
    )


@router.callback_query(F.data == "wallet:withdraw")
async def cb_withdraw(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(WithdrawFlow.destination)
    await callback.message.answer("Enter your external Solana payout address. Check it carefully.")


@router.message(WithdrawFlow.destination)
async def handle_withdraw_destination(message: Message, state: FSMContext) -> None:
    try:
        destination = str(Pubkey.from_string((message.text or "").strip()))
    except Exception:
        await message.answer("Invalid Solana address. No transfer was made. Send a valid base58 address.")
        return
    await state.update_data(destination=destination)
    await state.set_state(None)
    await message.answer(
        f"Confirm full SOL withdrawal to:\n{destination}\n\n"
        "This charges the $1 platform fee plus the actual Solana transaction fee.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Confirm Withdraw All", callback_data="wallet:withdraw:confirm"),
            InlineKeyboardButton(text="Cancel", callback_data="home:show"),
        ]]),
    )


@router.callback_query(F.data == "wallet:withdraw:confirm")
async def cb_withdraw_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    svc = _svc()
    await callback.answer()
    data = await state.get_data()
    destination = data.get("destination")
    if not destination:
        await callback.message.answer("Withdrawal address expired. Start again.")
        return
    if await svc.db.active_positions(callback.from_user.id):
        await callback.message.answer("Close all open positions before withdrawing the wallet balance.")
        return
    try:
        await svc.engine.stop_user(callback.from_user.id)
        amount, signature = await svc.wallet.withdraw_all(callback.from_user.id, destination)
        await svc.db.set_payout_address(callback.from_user.id, destination)
        await callback.message.answer(
            f"Withdrawal confirmed.\nSent: {format_sol(amount)} SOL\n"
            f"Solscan: https://solscan.io/tx/{signature}"
        )
    except Exception as exc:
        await callback.message.answer(f"Withdrawal was not completed: {exc}")
    finally:
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
