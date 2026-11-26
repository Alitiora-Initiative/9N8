from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from aiogram import Bot
from fastapi import Body, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from solders.pubkey import Pubkey

from .autonomous_engine import AutonomousEngine, utc_day
from .config import Settings
from .database import Database
from .math_engine import format_sol, from_lamports
from .oracle import PriceOracle
from .telemetry import TelemetryHub
from .wallet import WalletManager

logger = logging.getLogger(__name__)
WEBAPP_DIR = Path(__file__).resolve().parents[2] / "webapp"


def verify_telegram_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> dict[str, Any]:
    if not init_data or not bot_token:
        raise ValueError("Telegram WebApp authentication data is missing.")
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    values = dict(pairs)
    received_hash = values.pop("hash", "")
    values.pop("signature", None)
    if not received_hash or len(received_hash) != 64:
        raise ValueError("Telegram WebApp signature is missing or malformed.")
    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise ValueError("Telegram WebApp signature verification failed.")
    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        telegram_id = int(user["id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Telegram WebApp user payload is invalid.") from exc
    now = int(time.time())
    if auth_date > now + 60 or now - auth_date > max_age_seconds:
        raise ValueError("Telegram WebApp session has expired. Reopen the Mini App from Telegram.")
    return {"id": telegram_id, "user": user, "auth_date": auth_date}


class SettingsUpdate(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class ActionRequest(BaseModel):
    confirm: bool = False
    destination: str | None = None


def _validate_settings(values: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "launchpad", "pump_mode", "pump_migration_progress_pct",
        "buy_amount_sol", "slippage_pct", "priority_fee_lamports",
        "jito_tip_lamports", "take_profit_pct", "stop_loss_pct",
        "max_hold_seconds", "moon_bag_pct", "daily_position_limit",
        "max_daily_drawdown_pct", "max_top10_concentration_pct",
        "min_liquidity_usd", "mev_shield",
    }
    if not values or set(values) - allowed:
        raise ValueError("Settings payload contains no values or unsupported fields.")
    result: dict[str, Any] = {}
    for key, value in values.items():
        if key in {"launchpad"}:
            if value not in {"pump.fun", "raydium", "both"}:
                raise ValueError("launchpad must be pump.fun, raydium or both.")
            result[key] = value
        elif key == "pump_mode":
            if value not in {"fresh", "migration"}:
                raise ValueError("pump_mode must be fresh or migration.")
            result[key] = value
        elif key == "mev_shield":
            if not isinstance(value, bool):
                raise ValueError("mev_shield must be a boolean.")
            result[key] = value
        else:
            from .bot_handlers import _validated_setting
            result[key] = _validated_setting(key, str(value))
    return result


def create_webapp(
    settings: Settings,
    db: Database,
    engine: AutonomousEngine,
    wallet: WalletManager,
    oracle: PriceOracle,
    bot: Bot,
    telemetry: TelemetryHub,
) -> FastAPI:
    app = FastAPI(title="9N8 Telegram Mini App", docs_url=None, redoc_url=None)
    app.state.db = db
    app.state.engine = engine
    app.state.wallet = wallet
    app.state.oracle = oracle
    app.state.bot = bot
    app.state.telemetry = telemetry
    if WEBAPP_DIR.exists():
        app.mount("/assets", StaticFiles(directory=str(WEBAPP_DIR)), name="assets")

    async def authorize_header(init_data: str | None) -> int:
        try:
            parsed = verify_telegram_init_data(init_data or "", settings.telegram_bot_token)
            telegram_id = int(parsed["id"])
            existing = await db.get_user(telegram_id)
            if not existing and await db.user_count() >= settings.max_users:
                raise HTTPException(status_code=429, detail="9N8 onboarding capacity is currently full.")
            await db.ensure_user(telegram_id)
            return telegram_id
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.get("/")
    async def index():
        index_path = WEBAPP_DIR / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=404, detail="Mini App frontend is not installed.")
        return FileResponse(index_path)

    @app.get("/health")
    async def health():
        return {"ok": True, "service": "9n8-solana-telegram", "mode": settings.trading_mode}

    @app.get("/api/state")
    async def state(x_telegram_init_data: str | None = Header(default=None)):
        telegram_id = await authorize_header(x_telegram_init_data)
        user = await db.get_user(telegram_id)
        config = await db.get_settings(telegram_id)
        balance_lamports = await wallet.balance(telegram_id)
        try:
            sol_usd = str(await oracle.sol_usd())
            price_source = oracle.source
        except Exception:
            sol_usd, price_source = None, "unavailable"
        risk = await db.daily_risk_snapshot(telegram_id, utc_day())
        positions = await db.active_positions(telegram_id)
        return {
            "mode": settings.trading_mode,
            "live_enabled": settings.live_trading_enabled,
            "enabled": bool(config.get("enabled")),
            "activation_paid": bool(user and user.get("activation_paid")),
            "wallet_address": user.get("solana_address") if user else None,
            "balance_sol": format_sol(balance_lamports),
            "sol_usd": sol_usd,
            "price_source": price_source,
            "daily_realized_pnl_sol": str(from_lamports((risk or {}).get("realized_pnl_lamports", 0))),
            "daily_trade_count": (risk or {}).get("trade_count", 0),
            "settings": config,
            "positions": positions,
        }

    @app.get("/api/settings")
    async def get_settings(x_telegram_init_data: str | None = Header(default=None)):
        telegram_id = await authorize_header(x_telegram_init_data)
        return await db.get_settings(telegram_id)

    @app.post("/api/settings")
    async def post_settings(
        patch: SettingsUpdate,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        try:
            values = _validate_settings(patch.values)
            return await db.update_settings(telegram_id, values)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/engine/start")
    async def start_engine(x_telegram_init_data: str | None = Header(default=None)):
        telegram_id = await authorize_header(x_telegram_init_data)
        try:
            mode = await engine.start_user(telegram_id)
            return {"ok": True, "mode": mode}
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/engine/stop")
    async def stop_engine(x_telegram_init_data: str | None = Header(default=None)):
        telegram_id = await authorize_header(x_telegram_init_data)
        await engine.stop_user(telegram_id)
        return {"ok": True, "enabled": False}

    @app.post("/api/positions/{position_id}/panic")
    async def panic_sell(
        position_id: int,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        try:
            await engine.panic_sell(telegram_id, position_id)
            return {"ok": True, "message": "Panic sell submitted; await the trade confirmation message."}
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/wallet/withdraw")
    async def withdraw(
        action: ActionRequest,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        if not action.confirm:
            raise HTTPException(status_code=400, detail="Explicit confirmation is required.")
        if await db.active_positions(telegram_id):
            raise HTTPException(status_code=409, detail="Close all open positions before withdrawing.")
        try:
            destination = str(Pubkey.from_string(action.destination or ""))
            await engine.stop_user(telegram_id)
            amount, signature = await wallet.withdraw_all(telegram_id, destination)
            await db.set_payout_address(telegram_id, destination)
            return {"ok": True, "amount_sol": format_sol(amount), "signature": signature}
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/wallet/wipe")
    async def wipe(
        action: ActionRequest,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        if not action.confirm:
            raise HTTPException(status_code=400, detail="Explicit confirmation is required.")
        try:
            destination = str(Pubkey.from_string(action.destination or ""))
            await engine.stop_user(telegram_id)
            new_address, new_secret, payout, signature = await wallet.rotate_wallet(
                telegram_id, destination
            )
            await engine.reset_wallet_watcher(telegram_id)
            private_message = await bot.send_message(
                telegram_id,
                "NEW PAUSED 9N8 WALLET — SAVE THIS RECOVERY KEY\n\n"
                f"Address:\n{new_address}\n\n"
                f"Private key (base58):\n{new_secret}\n\n"
                "This message auto-deletes in 60 seconds. Anyone with this key can take the wallet assets.",
            )
            asyncio.create_task(_delete_sensitive_later(bot, telegram_id, private_message.message_id))
            return {
                "ok": True, "new_address": new_address,
                "payout_sol": format_sol(payout), "signature": signature,
                "notice": "Recovery key sent in an auto-deleting Telegram message.",
            }
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.websocket("/ws/telemetry")
    async def websocket_telemetry(websocket: WebSocket):
        # Receive initData in the first frame rather than the URL, so reverse
        # proxies and access logs are less likely to record the signed payload.
        await websocket.accept()
        queue = None
        try:
            first = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            payload = json.loads(first)
            init_data = str(payload.get("initData") or "")
            parsed = verify_telegram_init_data(init_data, settings.telegram_bot_token)
            telegram_id = int(parsed["id"])
            existing = await db.get_user(telegram_id)
            if not existing and await db.user_count() >= settings.max_users:
                await websocket.close(code=4429)
                return
            await db.ensure_user(telegram_id)
            queue = await telemetry.subscribe(telegram_id)
            await websocket.send_json({"type": "connected", "telegram_id": telegram_id})
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError, asyncio.TimeoutError):
            await websocket.close(code=4401)
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise
        finally:
            if queue is not None:
                await telemetry.unsubscribe(telegram_id, queue)

    return app


async def _delete_sensitive_later(bot: Bot, telegram_id: int, message_id: int) -> None:
    await asyncio.sleep(60)
    try:
        await bot.delete_message(telegram_id, message_id)
    except Exception:
        logger.warning("Could not auto-delete wallet recovery message %s", message_id)
