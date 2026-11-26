from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import suppress

import uvicorn
from aiogram import Bot, Dispatcher
from dotenv import load_dotenv

from .autonomous_engine import AutonomousEngine
from .bot_handlers import AppServices, configure_handlers, router
from .config import Settings
from .database import Database
from .helius import HeliusClient
from .launchpad_sniper import LaunchpadSniper
from .mev_executor import JupiterJitoExecutor
from .oracle import PriceOracle
from .postgres_database import PostgresDatabase
from .security import TokenSecurity
from .telemetry import TelemetryHub
from .wallet import WalletManager
from .webapp_server import create_webapp
from .worker_bridge import VercelWorkerBridge, WorkerBridgeError

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("9n8.solana")


async def run(worker_only: bool = False) -> None:
    load_dotenv()
    settings = Settings.from_env()
    if worker_only and not settings.database_url:
        raise RuntimeError("The persistent 9N8 worker requires DATABASE_URL; SQLite is local-development only.")
    worker_api_url = os.getenv("N9N8_API_URL", "").strip()
    worker_bridge_secret = os.getenv("N9N8_WORKER_BRIDGE_SECRET", "")
    if worker_only and (not worker_api_url or len(worker_bridge_secret) < 32):
        raise RuntimeError(
            "Vercel Daemon requires its private N9N8_API_URL service binding and "
            "a 32+ character N9N8_WORKER_BRIDGE_SECRET."
        )
    if settings.database_url:
        db = PostgresDatabase(settings.database_url, settings.encryption_key)
    else:
        db = Database(settings.database_path, settings.encryption_key)
    await db.initialize()
    worker_bridge = (
        VercelWorkerBridge(worker_api_url, worker_bridge_secret)
        if worker_api_url and worker_bridge_secret else None
    )
    if hasattr(db, "mark_stale_jobs_unknown"):
        stale_jobs = await db.mark_stale_jobs_unknown()
        if stale_jobs:
            logger.error("Marked %s interrupted worker jobs unknown; they will not be auto-retried.", stale_jobs)

    helius = HeliusClient(
        settings.solana_rpc_url,
        settings.solana_wss_url,
        settings.helius_api_key,
    )
    oracle = PriceOracle(api_key=settings.pyth_api_key)
    wallet = WalletManager(helius, db, oracle, settings.treasury)
    executor = JupiterJitoExecutor(settings, helius)
    sniper = LaunchpadSniper(helius)
    security = TokenSecurity(helius)
    telemetry = TelemetryHub()
    bot = Bot(settings.telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    async def notify(telegram_id: int, message: str) -> None:
        try:
            await bot.send_message(telegram_id, message, disable_web_page_preview=True)
        except Exception as exc:
            logger.warning("Could not send Telegram notification to %s (%s)", telegram_id, type(exc).__name__)

    async def publish(telegram_id: int, event: dict) -> None:
        if hasattr(db, "publish_event"):
            try:
                await db.publish_event(telegram_id, event)
            except Exception:
                logger.exception("Could not persist telemetry event for Telegram account %s", telegram_id)
        await telemetry.publish(telegram_id, event)

    engine = AutonomousEngine(
        settings=settings,
        db=db,
        helius=helius,
        wallet=wallet,
        oracle=oracle,
        executor=executor,
        sniper=sniper,
        security=security,
        notify=notify,
        telemetry=publish,
    )
    configure_handlers(AppServices(settings, db, wallet, oracle, engine))

    server = None
    server_task: asyncio.Task | None = None
    if not worker_only:
        web_app = create_webapp(settings, db, engine, wallet, oracle, bot, telemetry)
        server = uvicorn.Server(uvicorn.Config(
            web_app,
            host=settings.webapp_host,
            port=settings.webapp_port,
            log_level=os.getenv("UVICORN_LOG_LEVEL", "info"),
            access_log=False,
        ))
        server_task = asyncio.create_task(server.serve(), name="9n8-fastapi")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    for user in await db.all_users_with_wallets():
        try:
            await engine.watch_wallet(int(user["telegram_id"]))
        except Exception as exc:
            logger.error("Could not start wallet watcher for %s (%s)", user["telegram_id"], type(exc).__name__)

    active_users: set[int] = set()
    for telegram_id in await db.enabled_users():
        try:
            await engine.start_user(int(telegram_id))
            active_users.add(int(telegram_id))
        except Exception as exc:
            logger.error("Could not resume Telegram account %s (%s)", telegram_id, type(exc).__name__)

    worker_id = os.getenv("VERCEL_DEPLOYMENT_ID") or os.getenv("HOSTNAME") or os.getenv("RENDER_INSTANCE_ID") or "9n8-worker"

    async def run_worker_job(job: dict) -> None:
        job_id = int(job["id"])
        telegram_id = int(job["telegram_id"])
        job_type = str(job["job_type"])
        payload = job.get("payload") or {}
        try:
            if job_type == "panic_sell":
                await engine.panic_sell(telegram_id, int(payload["position_id"]))
                result = {"message": "Panic sell submitted; wait for on-chain confirmation."}
            elif job_type == "withdraw_all":
                if await db.active_positions(telegram_id):
                    raise RuntimeError("Close all open positions before withdrawing.")
                destination = str(payload["destination"])
                await engine.stop_user(telegram_id)
                amount, signature = await wallet.withdraw_all(telegram_id, destination)
                await db.set_payout_address(telegram_id, destination)
                result = {"amount_lamports": amount, "signature": signature}
                await notify(
                    telegram_id,
                    f"WITHDRAWAL CONFIRMED\\nAmount: {amount} lamports\\nSignature: https://solscan.io/tx/{signature}",
                )
            elif job_type == "rotate_wallet":
                if await db.active_positions(telegram_id):
                    raise RuntimeError("Close all open positions before rotating the wallet.")
                destination = str(payload["destination"])
                await engine.stop_user(telegram_id)
                address, secret, payout, signature = await wallet.rotate_wallet(telegram_id, destination)
                await engine.reset_wallet_watcher(telegram_id)
                sent = await bot.send_message(
                    telegram_id,
                    "NEW PAUSED 9N8 WALLET — SAVE THIS RECOVERY KEY\\n\\n"
                    f"Address:\\n{address}\\n\\n"
                    f"Private key (base58):\\n{secret}\\n\\n"
                    "This message will be deleted after 60 seconds. Anyone with this key can take the wallet assets.",
                )
                asyncio.create_task(_delete_sensitive_later(bot, telegram_id, sent.message_id))
                result = {"new_address": address, "payout_lamports": payout, "signature": signature}
            else:
                raise RuntimeError("Unknown worker job type.")
            await db.complete_job(job_id, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A timeout after a transaction submit can leave its landing state
            # uncertain. Do not automatically retry real-money wallet operations.
            unknown = job_type in {"withdraw_all", "rotate_wallet", "panic_sell"}
            await db.fail_job(job_id, f"{type(exc).__name__}: {str(exc)[:700]}", unknown=unknown)
            await notify(
                telegram_id,
                f"9N8 operation {job_id} ended as {'UNKNOWN — VERIFY THE SIGNATURE/WALLET BEFORE RETRYING' if unknown else 'FAILED'}: "
                f"{type(exc).__name__}: {str(exc)[:220]}",
            )

    async def reconcile_user(telegram_id: int) -> None:
        user = await db.get_user(telegram_id)
        config = await db.get_settings(telegram_id)
        if user and user.get("solana_address") and config.get("enabled"):
            if telegram_id not in engine._enabled_users:
                await engine.watch_wallet(telegram_id)
                await engine.start_user(telegram_id)
                active_users.add(telegram_id)
        elif telegram_id in engine._enabled_users:
            await engine.stop_user(telegram_id)
            active_users.discard(telegram_id)

    async def process_queued_jobs() -> None:
        # Neon is authoritative. Queue deliveries only wake the worker; job rows
        # are claimed with FOR UPDATE SKIP LOCKED and never replayed when running.
        while True:
            jobs = await db.claim_jobs(worker_id, limit=50)
            if not jobs:
                return
            for job in jobs:
                await run_worker_job(job)
            if len(jobs) < 50:
                return

    async def queue_loop() -> None:
        if worker_bridge is None:
            return
        while not stop_event.is_set():
            try:
                messages = await worker_bridge.receive(limit=10)
                if not messages:
                    await asyncio.sleep(5)
                    continue
                for message in messages:
                    try:
                        kind = str(message.body.get("kind") or "")
                        if kind == "sync_user":
                            await reconcile_user(int(message.body["telegram_id"]))
                        elif kind == "job":
                            await process_queued_jobs()
                        else:
                            logger.warning("Ignoring unknown worker wake message type: %s", kind[:40])
                        await worker_bridge.acknowledge(message.receipt_handle)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # If the Neon claim succeeded, the job state prevents a
                        # redelivery from repeating a transaction.
                        logger.exception("Could not process Vercel Queue message %s", message.message_id)
                        break
                if hasattr(db, "heartbeat"):
                    await db.heartbeat(
                        worker_id,
                        "running",
                        settings.trading_mode,
                        {"active_users": len(active_users), "queue_messages": len(messages)},
                    )
            except asyncio.CancelledError:
                raise
            except WorkerBridgeError as exc:
                logger.warning("Vercel Queue poll failed (%s); retrying.", type(exc).__name__)
                await asyncio.sleep(10)
            except Exception:
                logger.exception("9N8 Queue consumer failed; retrying.")
                await asyncio.sleep(10)

    # Recover pending Neon jobs that may have survived a daemon restart before
    # their Queue wake-up message could be acknowledged.
    if hasattr(db, "claim_jobs"):
        await process_queued_jobs()
    if hasattr(db, "heartbeat"):
        await db.heartbeat(worker_id, "running", settings.trading_mode, {"startup": True})

    async def poll_telegram() -> None:
        logger.info("Starting aiogram polling; trading mode=%s", settings.trading_mode)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())

    polling_task = asyncio.create_task(poll_telegram(), name="9n8-telegram")
    queue_task = asyncio.create_task(queue_loop(), name="9n8-vercel-queue")
    tasks = {polling_task, queue_task, asyncio.create_task(stop_event.wait(), name="9n8-shutdown")}
    if server_task is not None:
        tasks.add(server_task)

    logger.info(
        "9N8 %s started: mode=%s postgres=%s helius=%s treasury=%s",
        "worker" if worker_only else "combined",
        settings.trading_mode,
        bool(settings.database_url),
        "configured",
        settings.treasury,
    )
    logger.warning(
        "Live trading guard: %s. Do not enable until post-confirmation net-profit accounting, "
        "wallet recovery, storage backups, deployment and mainnet execution are independently verified.",
        "ENABLED BY CONFIG" if settings.live_trading_enabled else "LOCKED",
    )

    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.get_name() == "9n8-shutdown":
                continue
            exception = task.exception()
            if exception:
                raise exception
    finally:
        stop_event.set()
        if server is not None:
            server.should_exit = True
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.shutdown()
        if worker_bridge is not None:
            await worker_bridge.close()
        if hasattr(db, "heartbeat"):
            try:
                await db.heartbeat(worker_id, "stopped", settings.trading_mode)
            except Exception:
                pass
        await security.close()
        await executor.close()
        await oracle.close()
        await helius.close()
        await bot.session.close()
        if hasattr(db, "close"):
            await db.close()
        logger.info("9N8 shutdown complete.")


async def _delete_sensitive_later(bot: Bot, telegram_id: int, message_id: int) -> None:
    await asyncio.sleep(60)
    try:
        await bot.delete_message(telegram_id, message_id)
    except Exception:
        logger.warning("Could not auto-delete wallet recovery message %s", message_id)


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("9N8 interrupted by operator.")


def worker_main() -> None:
    try:
        asyncio.run(run(worker_only=True))
    except KeyboardInterrupt:
        logger.info("9N8 worker interrupted by operator.")


if __name__ == "__main__":
    main()
