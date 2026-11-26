from __future__ import annotations

import logging
import os

from aiogram import Bot

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

    # Vercel handles only request/response work. The Daemon owns Telegram
    # polling, Helius subscriptions, launch detection and transaction execution.
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
        fresh = float(age or 0) <= 45
        return {
            "connected": fresh,
            "status": row["status"] if fresh else "stale",
            "trading_mode": row["trading_mode"],
            "last_seen_at": row["last_seen_at"].isoformat(),
            "metadata": row["metadata"],
        }

    return app


app = create_serverless_app()
