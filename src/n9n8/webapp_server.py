from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
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
from .math_engine import format_sol, from_lamports, usd_to_lamports
from .paper_store import PaperStore
from .oracle import PriceOracle
from .telemetry import TelemetryHub
from .wallet import WalletManager
from .vercel_queue import VercelQueueClient, VercelQueueError

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
    engine: AutonomousEngine | None,
    wallet: WalletManager,
    oracle: PriceOracle,
    bot: Bot,
    telemetry: TelemetryHub | None,
    worker_queue: VercelQueueClient | None = None,
) -> FastAPI:
    app = FastAPI(title="9N8 Telegram Mini App", docs_url=None, redoc_url=None)
    app.state.db = db
    app.state.engine = engine
    app.state.wallet = wallet
    app.state.oracle = oracle
    app.state.bot = bot
    app.state.telemetry = telemetry
    app.state.worker_queue = worker_queue
    app.state.paper_store = None
    if WEBAPP_DIR.exists():
        app.mount("/assets", StaticFiles(directory=str(WEBAPP_DIR)), name="assets")

    async def authorize_header(init_data: str | None) -> int:
        try:
            parsed = verify_telegram_init_data(init_data or "", settings.telegram_bot_token)
            telegram_id = int(parsed["id"])
            if telegram_id != settings.authorized_user_id:
                raise HTTPException(status_code=403, detail="This 9N8 bot is private and not enabled for this Telegram account.")
            existing = await db.get_user(telegram_id)
            if not existing and await db.user_count() >= settings.max_users:
                raise HTTPException(status_code=429, detail="9N8 onboarding capacity is currently full.")
            await db.ensure_user(telegram_id)
            return telegram_id
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    async def wake_worker(request: Request, payload: dict[str, Any], idempotency_key: str) -> None:
        if worker_queue is None:
            raise HTTPException(status_code=503, detail="Persistent worker queue is not configured.")
        token = (
            request.headers.get("x-vercel-oidc-token")
            or os.getenv("VERCEL_QUEUE_API_TOKEN", "")
        )
        try:
            await worker_queue.send(
                settings.vercel_queue_topic,
                payload,
                token,
                idempotency_key,
            )
        except VercelQueueError as exc:
            logger.warning("Worker wake-up failed after Neon state was saved (%s)", type(exc).__name__)
            raise HTTPException(
                status_code=503,
                detail="Your request was saved, but the worker could not be notified. Check its status before repeating any funds movement.",
            ) from exc

    @app.get("/")
    async def index():
        index_path = WEBAPP_DIR / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=404, detail="Mini App frontend is not installed.")
        return FileResponse(index_path)

    @app.get("/health")
    async def health():
        return {"ok": True, "service": "9n8-solana-telegram", "mode": settings.trading_mode}

    async def get_paper_store() -> PaperStore:
        store = app.state.paper_store
        if store is None:
            store = PaperStore(db)
            await store.initialize()
            app.state.paper_store = store
        return store

    async def get_live_readiness(telegram_id: int) -> dict[str, Any]:
        store = await get_paper_store()
        user = await db.get_user(telegram_id)
        result: dict[str, Any] = {
            "operator_live_switch": bool(settings.live_trading_enabled),
            "wallet_connected": bool(user and user.get("solana_address")),
            "activation_confirmed": bool(user and user.get("activation_paid")),
            "paper_days": await store.active_days(telegram_id),
            "wallet_balance_sol": "0",
            "wallet_balance_usd": "0",
            "minimum_wallet_usd": "10",
            "ready": False,
            "blockers": [],
        }
        if not result["operator_live_switch"]:
            result["blockers"].append("The operator has not enabled the global live switch.")
        if not result["wallet_connected"]:
            result["blockers"].append("Create or import a Solana wallet first.")
        if not result["activation_confirmed"]:
            result["blockers"].append("Complete the one-time activation check.")
        if result["paper_days"] < 7:
            result["blockers"].append(f"Complete paper practice on 7 different UTC days ({result['paper_days']}/7).")
        if result["wallet_connected"]:
            try:
                balance = int(await wallet.balance(telegram_id))
                price = await oracle.sol_usd()
                value = from_lamports(balance) * price
                result["wallet_balance_sol"] = format_sol(balance)
                result["wallet_balance_usd"] = str(value.quantize(__import__("decimal").Decimal("0.01")))
                if value < 10:
                    result["blockers"].append("The wallet needs at least $10 of SOL.")
            except Exception as exc:
                result["blockers"].append("Wallet balance check failed: " + type(exc).__name__)
        result["ready"] = not result["blockers"]
        return result

    @app.get("/api/state")
    async def state(x_telegram_init_data: str | None = Header(default=None)):
        telegram_id = await authorize_header(x_telegram_init_data)
        store = await get_paper_store()
        user = await db.get_user(telegram_id)
        live_config = await db.get_settings(telegram_id)
        paper_config = await store.settings(telegram_id, "paper")
        sol_price = None
        price_source = "unavailable"
        try:
            sol_price = str(await oracle.sol_usd())
            price_source = getattr(oracle, "source", "oracle")
        except Exception:
            pass
        balance_lamports = 0
        if user and user.get("solana_address"):
            try:
                balance_lamports = int(await wallet.balance(telegram_id))
            except Exception:
                balance_lamports = 0
        paper_account = await store.account(telegram_id)
        paper_positions = await store.positions(telegram_id)
        live_positions = await db.active_positions(telegram_id)
        paper_enabled = await store.mode_enabled(telegram_id, "paper")
        live_enabled = await store.mode_enabled(telegram_id, "live")
        return {
            "modes": {
                "paper": {"enabled": paper_enabled, "label": "PAPER · VIRTUAL FUNDS"},
                "live": {
                    "enabled": live_enabled,
                    "operator_enabled": bool(settings.live_trading_enabled),
                    "label": "LIVE · REAL SOL",
                },
            },
            "activation_paid": bool(user and user.get("activation_paid")),
            "wallet_address": user.get("solana_address") if user else None,
            "wallet_balance_sol": format_sol(balance_lamports),
            "wallet_balance_usd": str(
                (from_lamports(balance_lamports) * __import__("decimal").Decimal(sol_price)).quantize(__import__("decimal").Decimal("0.01"))
            ) if sol_price else None,
            "sol_usd": sol_price,
            "price_source": price_source,
            "paper": {
                "account": paper_account,
                "practice_days": await store.active_days(telegram_id),
                "positions": paper_positions,
                "settings": paper_config,
                "history": await store.history(telegram_id, 20),
            },
            "live": {
                "readiness": await get_live_readiness(telegram_id),
                "positions": live_positions,
                "settings": live_config,
                "daily_realized_pnl_sol": str(from_lamports(
                    (await db.daily_risk_snapshot(telegram_id, utc_day()) or {}).get("realized_pnl_lamports", 0)
                )),
                "receipts": await db.list_trade_receipts(telegram_id, 20),
            },
        }

    @app.get("/api/paper/settings")
    async def get_paper_settings(x_telegram_init_data: str | None = Header(default=None)):
        telegram_id = await authorize_header(x_telegram_init_data)
        return await (await get_paper_store()).settings(telegram_id, "paper")

    @app.post("/api/paper/settings")
    async def post_paper_settings(
        patch: SettingsUpdate,
        request: Request,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        try:
            from .bot_handlers import _validated_setting
            allowed = {
                "buy_amount_sol", "slippage_pct", "take_profit_pct", "take_profit_2_pct",
                "take_profit_2_sell_pct", "trailing_stop_pct", "stop_loss_pct",
                "max_hold_seconds", "jito_tip_lamports", "priority_fee_lamports",
                "min_liquidity_sol", "min_lp_locked_pct",
                "max_top10_concentration_pct", "max_dev_wallet_pct",
            }
            if not patch.values or set(patch.values) - allowed:
                raise ValueError("Only paper trading fields may be changed here.")
            values = {key: _validated_setting(key, str(value)) for key, value in patch.values.items()}
            updated = await (await get_paper_store()).update_settings(telegram_id, values, "paper")
            if engine is None:
                await wake_worker(request, {"kind": "sync_user", "telegram_id": telegram_id},
                                  f"paper-settings:{telegram_id}:{uuid.uuid4().hex}")
            return updated
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/paper/start")
    async def start_paper(
        request: Request,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        store = await get_paper_store()
        try:
            if engine is not None:
                account = await engine.start_paper_user(telegram_id)
            else:
                sol_price = await oracle.sol_usd()
                account = await store.create_account(telegram_id, usd_to_lamports("1000", sol_price))
                await store.touch_day(telegram_id, utc_day())
                await store.set_mode_enabled(telegram_id, "paper", True)
                await db.security_event(telegram_id, "paper_start_requested", "Separate virtual practice mode requested from the Mini App.")
                await wake_worker(request, {"kind": "sync_user", "telegram_id": telegram_id},
                                  f"paper-start:{telegram_id}:{uuid.uuid4().hex}")
            return {"ok": True, "paper_enabled": True, "account": account,
                    "message": "Paper mode started. Only virtual SOL is used."}
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/paper/stop")
    async def stop_paper(
        request: Request,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        store = await get_paper_store()
        if engine is not None:
            await engine.stop_paper_user(telegram_id)
        else:
            await store.set_mode_enabled(telegram_id, "paper", False)
            await db.security_event(telegram_id, "paper_stop_requested", "New virtual entries paused from the Mini App.")
            await wake_worker(request, {"kind": "sync_user", "telegram_id": telegram_id},
                              f"paper-stop:{telegram_id}:{uuid.uuid4().hex}")
        return {"ok": True, "paper_enabled": False}

    @app.get("/api/live/readiness")
    async def live_readiness_route(x_telegram_init_data: str | None = Header(default=None)):
        telegram_id = await authorize_header(x_telegram_init_data)
        return await get_live_readiness(telegram_id)

    @app.post("/api/live/start")
    async def start_live(
        request: Request,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        readiness = await get_live_readiness(telegram_id)
        if not readiness["ready"]:
            raise HTTPException(status_code=409, detail="LIVE MODE BLOCKED: " + " ".join(readiness["blockers"]))
        try:
            if engine is not None:
                await engine.start_live_user(telegram_id)
            else:
                await db.update_settings(telegram_id, {"live_enabled": True, "enabled": True})
                await db.security_event(telegram_id, "live_start_requested", "User explicitly requested Live mode after readiness passed.")
                await wake_worker(request, {"kind": "sync_user", "telegram_id": telegram_id},
                                  f"live-start:{telegram_id}:{uuid.uuid4().hex}")
            return {"ok": True, "live_enabled": True, "message": "Live mode requested after readiness checks."}
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/live/stop")
    async def stop_live(
        request: Request,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        if engine is not None:
            await engine.stop_live_user(telegram_id)
        else:
            await db.update_settings(telegram_id, {"live_enabled": False, "enabled": False})
            await db.security_event(telegram_id, "live_stop_requested", "Live entries paused from the Mini App.")
            await wake_worker(request, {"kind": "sync_user", "telegram_id": telegram_id},
                              f"live-stop:{telegram_id}:{uuid.uuid4().hex}")
        return {"ok": True, "live_enabled": False, "message": "New live entries paused. Existing exits remain monitored."}

    @app.get("/api/settings")
    async def get_settings(x_telegram_init_data: str | None = Header(default=None)):
        telegram_id = await authorize_header(x_telegram_init_data)
        return await db.get_settings(telegram_id)

    @app.post("/api/settings")
    async def post_settings(
        patch: SettingsUpdate,
        request: Request,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        try:
            values = _validate_settings(patch.values)
            updated = await db.update_settings(telegram_id, values)
            if engine is None:
                await wake_worker(
                    request,
                    {"kind": "sync_user", "telegram_id": telegram_id},
                    f"settings:{telegram_id}:{uuid.uuid4().hex}",
                )
            return updated
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/engine/start")
    async def start_engine(
        request: Request,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        try:
            user = await db.get_user(telegram_id)
            if not user or not user.get("solana_address"):
                raise HTTPException(status_code=409, detail="Create or import a Solana wallet first.")
            if settings.trading_mode == "live":
                if not settings.live_trading_enabled:
                    raise HTTPException(status_code=409, detail="Live execution remains locked by the server.")
                if not user.get("activation_paid"):
                    raise HTTPException(status_code=409, detail="The live activation fee is not confirmed.")
            if engine is not None:
                mode = await engine.start_user(telegram_id)
                return {"ok": True, "mode": mode, "enabled": True}
            config = await db.update_settings(telegram_id, {"enabled": True})
            await db.security_event(telegram_id, "engine_start_requested", "Start requested through the Vercel Mini App.")
            await wake_worker(
                request,
                {"kind": "sync_user", "telegram_id": telegram_id},
                f"start:{telegram_id}:{uuid.uuid4().hex}",
            )
            return {
                "ok": True,
                "mode": settings.trading_mode,
                "enabled": bool(config.get("enabled")),
                "message": "Start request saved. The persistent trading worker will activate the supervisor.",
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/engine/stop")
    async def stop_engine(
        request: Request,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        if engine is not None:
            await engine.stop_user(telegram_id)
        else:
            await db.update_settings(telegram_id, {"enabled": False})
            await db.security_event(telegram_id, "engine_stop_requested", "Stop requested through the Vercel Mini App.")
            await wake_worker(
                request,
                {"kind": "sync_user", "telegram_id": telegram_id},
                f"stop:{telegram_id}:{uuid.uuid4().hex}",
            )
        return {"ok": True, "enabled": False}

    @app.post("/api/positions/{position_id}/panic")
    async def panic_sell(
        position_id: int,
        request: Request,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        try:
            if engine is not None:
                await engine.panic_sell(telegram_id, position_id)
                return {"ok": True, "message": "Panic sell submitted; await the trade confirmation message."}
            position = await db.get_position(position_id, telegram_id)
            if not position or position.get("status") != "open":
                raise HTTPException(status_code=404, detail="Open position not found.")
            job = await db.enqueue_job(
                telegram_id, "panic_sell", {"position_id": position_id},
                f"webapp:panic:{telegram_id}:{position_id}",
            )
            await wake_worker(
                request,
                {"kind": "job", "job_id": int(job["id"]), "telegram_id": telegram_id},
                f"job:{job['id']}",
            )
            return {"ok": True, "status": job["status"], "job_id": job["id"], "message": "Panic sell queued for the persistent worker."}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/wallet/withdraw")
    async def withdraw(
        action: ActionRequest,
        request: Request,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        if not action.confirm:
            raise HTTPException(status_code=400, detail="Explicit confirmation is required.")
        if await db.active_positions(telegram_id):
            raise HTTPException(status_code=409, detail="Close all open positions before withdrawing.")
        try:
            destination = str(Pubkey.from_string(action.destination or ""))
            if engine is not None:
                await engine.stop_user(telegram_id)
                amount, signature = await wallet.withdraw_all(telegram_id, destination)
                await db.set_payout_address(telegram_id, destination)
                return {"ok": True, "amount_sol": format_sol(amount), "signature": signature}
            await db.update_settings(telegram_id, {"enabled": False})
            job = await db.enqueue_job(
                telegram_id, "withdraw_all", {"destination": destination},
                f"webapp:withdraw:{telegram_id}:{await db.get_wallet_address(telegram_id)}:{await wallet.balance(telegram_id)}:{destination}",
            )
            await wake_worker(
                request,
                {"kind": "job", "job_id": int(job["id"]), "telegram_id": telegram_id},
                f"job:{job['id']}",
            )
            return {"ok": True, "status": job["status"], "job_id": job["id"], "message": "Withdrawal queued. The bot will send its result in Telegram."}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/wallet/wipe")
    async def wipe(
        action: ActionRequest,
        request: Request,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        if not action.confirm:
            raise HTTPException(status_code=400, detail="Explicit confirmation is required.")
        if await db.active_positions(telegram_id):
            raise HTTPException(status_code=409, detail="Close all open positions before rotating the wallet.")
        try:
            destination = str(Pubkey.from_string(action.destination or ""))
            if engine is not None:
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
            await db.update_settings(telegram_id, {"enabled": False})
            job = await db.enqueue_job(
                telegram_id, "rotate_wallet", {"destination": destination},
                f"webapp:rotate:{telegram_id}:{await db.get_wallet_address(telegram_id)}",
            )
            await wake_worker(
                request,
                {"kind": "job", "job_id": int(job["id"]), "telegram_id": telegram_id},
                f"job:{job['id']}",
            )
            return {"ok": True, "status": job["status"], "job_id": job["id"], "message": "Wallet rotation queued for the persistent worker."}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}")
    async def get_job_status(
        job_id: int,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        telegram_id = await authorize_header(x_telegram_init_data)
        if not hasattr(db, "get_job"):
            raise HTTPException(status_code=404, detail="Worker job status is unavailable in this local database mode.")
        job = await db.get_job(job_id, telegram_id)
        if not job:
            raise HTTPException(status_code=404, detail="Worker job not found.")
        return job

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
            if telegram_id != settings.authorized_user_id:
                await websocket.close(code=4403)
                return
            existing = await db.get_user(telegram_id)
            if not existing and await db.user_count() >= settings.max_users:
                await websocket.close(code=4429)
                return
            await db.ensure_user(telegram_id)
            if telemetry is not None:
                queue = await telemetry.subscribe(telegram_id)
            await websocket.send_json({"type": "connected", "telegram_id": telegram_id})
            last_event_id = 0
            while True:
                if queue is not None:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    await websocket.send_json(event)
                elif hasattr(db, "events_after"):
                    events = await db.events_after(telegram_id, last_event_id, limit=100)
                    for event in events:
                        last_event_id = max(last_event_id, int(event.get("id") or 0))
                        await websocket.send_json(event)
                    if not events:
                        await asyncio.sleep(1)
                    else:
                        await asyncio.sleep(0.1)
                else:
                    await asyncio.sleep(5)
                    await websocket.send_json({"type": "heartbeat"})
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError, asyncio.TimeoutError):
            await websocket.close(code=4401)
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise
        finally:
            if queue is not None and telemetry is not None:
                await telemetry.unsubscribe(telegram_id, queue)

    return app


async def _delete_sensitive_later(bot: Bot, telegram_id: int, message_id: int) -> None:
    await asyncio.sleep(60)
    try:
        await bot.delete_message(telegram_id, message_id)
    except Exception:
        logger.warning("Could not auto-delete wallet recovery message %s", message_id)
