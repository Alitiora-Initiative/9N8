from __future__ import annotations

import hmac
import logging
import os

from aiogram import Bot
from fastapi import Header, HTTPException, Request

from .config import Settings
from .helius import HeliusClient
from .oracle import PriceOracle
from .postgres_database import PostgresDatabase
from .wallet import WalletManager
from .vercel_queue import VercelQueueClient
from .webapp_server import create_webapp

logger = logging.getLogger("9n8.vercel")


def create_serverless_app():
    settings = Settings.from_env()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required by the Vercel 9N8 API.")
    db = PostgresDatabase(settings.database_url, settings.encryption_key)
    helius = HeliusClient(
        settings.solana_rpc_url,
        settings.solana_wss_url,
        settings.helius_api_key,
    )
    oracle = PriceOracle(api_key=settings.pyth_api_key)
    wallet = WalletManager(helius, db, oracle, settings.treasury)
    bot = Bot(settings.telegram_bot_token)
    worker_queue = VercelQueueClient(settings.vercel_queue_region)

    # Vercel handles request/response work. Supabase owns inbound Telegram
    # webhook updates and command menus. The Daemon is outbound-only for Telegram
    # notifications and owns Helius subscriptions, launch detection and execution.
    app = create_webapp(
        settings=settings,
        db=db,
        engine=None,
        wallet=wallet,
        oracle=oracle,
        bot=bot,
        telemetry=None,
        worker_queue=worker_queue,
    )

    @app.on_event("startup")
    async def initialize_services() -> None:
        await db.initialize()
        await worker_queue.start()

    @app.on_event("shutdown")
    async def close_services() -> None:
        await oracle.close()
        await helius.close()
        await bot.session.close()
        await worker_queue.close()
        await db.close()

    def authorize_worker_bridge(provided: str | None) -> None:
        expected = os.getenv("N9N8_WORKER_BRIDGE_SECRET", "")
        if len(expected) < 32 or not provided or not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Worker bridge authentication failed.")

    def function_oidc_token(request: Request) -> str:
        token = request.headers.get("x-vercel-oidc-token", "")
        if not token:
            raise HTTPException(status_code=503, detail="Vercel Function OIDC token is unavailable.")
        return token

    @app.post("/api/internal/worker/queue/receive")
    async def worker_queue_receive(
        request: Request,
        x_n9n8_worker_auth: str | None = Header(default=None),
    ):
        authorize_worker_bridge(x_n9n8_worker_auth)
        token = function_oidc_token(request)
        payload = await request.json()
        limit = max(1, min(int(payload.get("limit") or 10), 10))
        try:
            messages = await worker_queue.receive(
                settings.vercel_queue_topic,
                settings.vercel_queue_consumer,
                token,
                limit=limit,
                visibility_timeout_seconds=60,
            )
        except Exception as exc:
            logger.warning("Queue receive bridge failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Queue receive temporarily unavailable.") from exc
        return {
            "messages": [
                {
                    "message_id": item.message_id,
                    "receipt_handle": item.receipt_handle,
                    "delivery_count": item.delivery_count,
                    "body": item.body,
                }
                for item in messages
            ]
        }

    @app.post("/api/internal/worker/queue/ack")
    async def worker_queue_ack(
        request: Request,
        x_n9n8_worker_auth: str | None = Header(default=None),
    ):
        authorize_worker_bridge(x_n9n8_worker_auth)
        token = function_oidc_token(request)
        payload = await request.json()
        receipt_handle = str(payload.get("receipt_handle") or "")
        if not receipt_handle:
            raise HTTPException(status_code=422, detail="Queue receipt handle is required.")
        try:
            await worker_queue.acknowledge(
                settings.vercel_queue_topic,
                settings.vercel_queue_consumer,
                receipt_handle,
                token,
            )
        except Exception as exc:
            logger.warning("Queue acknowledgement bridge failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Queue acknowledgement temporarily unavailable.") from exc
        return {"ok": True}

    @app.get("/api/worker/status")
    async def worker_status():
        pool = db._pool()
        row = await pool.fetchrow(
            """SELECT worker_id,status,trading_mode,last_seen_at,metadata
               FROM public.n9n8_worker_heartbeats
               ORDER BY last_seen_at DESC LIMIT 1"""
        )
        if not row:
            return {"connected": False, "status": "not_started"}
        age = await pool.fetchval(
            "SELECT EXTRACT(EPOCH FROM now() - $1::timestamptz)",
            row["last_seen_at"],
        )
        fresh = float(age or 0) <= 300
        return {
            "connected": fresh,
            "status": row["status"] if fresh else "idle_or_stale",
            "presence_note": "Heartbeat records last database activity; an idle Daemon can be polling Vercel Queues without writing to Neon.",
            "trading_mode": row["trading_mode"],
            "last_seen_at": row["last_seen_at"].isoformat(),
            "metadata": row["metadata"],
        }

    return app


app = create_serverless_app()
