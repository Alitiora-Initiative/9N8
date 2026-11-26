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
from .security import TokenSecurity
from .telemetry import TelemetryHub
from .wallet import WalletManager
from .webapp_server import create_webapp

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("9n8.solana")


async def run() -> None:
    load_dotenv()
    settings = Settings.from_env()
    db = Database(settings.database_path, settings.encryption_key)
    await db.initialize()

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
    web_app = create_webapp(settings, db, engine, wallet, oracle, bot, telemetry)
    server = uvicorn.Server(uvicorn.Config(
        web_app,
        host=settings.webapp_host,
        port=settings.webapp_port,
        log_level=os.getenv("UVICORN_LOG_LEVEL", "info"),
        access_log=False,
    ))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    # Start SOL credit watchers for every persisted wallet, including paused
    # users, then restore the positions/entry supervisors of enabled users.
    for user in await db.all_users_with_wallets():
        try:
            await engine.watch_wallet(int(user["telegram_id"]))
        except Exception as exc:
            logger.error("Could not start wallet watcher for %s (%s)", user["telegram_id"], type(exc).__name__)

    # Restart previously enabled user's monitors only after the application
    # components are ready. Each user remains governed by their persisted config.
    for telegram_id in await db.enabled_users():
        try:
            mode = await engine.start_user(telegram_id)
            logger.info("Resumed enabled Telegram account %s in %s mode", telegram_id, mode)
        except Exception as exc:
            logger.error("Could not resume Telegram account %s (%s)", telegram_id, type(exc).__name__)

    async def poll_telegram() -> None:
        logger.info("Starting aiogram polling; trading mode=%s", settings.trading_mode)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())

    server_task = asyncio.create_task(server.serve(), name="9n8-fastapi")
    polling_task = asyncio.create_task(poll_telegram(), name="9n8-telegram")
    stop_task = asyncio.create_task(stop_event.wait(), name="9n8-shutdown")
    logger.info(
        "9N8 started: web=%s:%s mode=%s helius=%s treasury=%s",
        settings.webapp_host,
        settings.webapp_port,
        settings.trading_mode,
        "configured",
        settings.treasury,
    )
    logger.warning(
        "Live trading guard: %s. Never enable with a wallet holding funds until testnet/paper, "
        "RPC, Jito bundle landing, slippage, risk screens and withdrawal recovery have been verified.",
        "ENABLED BY CONFIG" if settings.live_trading_enabled else "LOCKED",
    )

    try:
        done, pending = await asyncio.wait(
            {server_task, polling_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task is not stop_task:
                exception = task.exception()
                if exception:
                    raise exception
        server.should_exit = True
    finally:
        server.should_exit = True
        for task in (server_task, polling_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(server_task, polling_task, stop_task, return_exceptions=True)
        await engine.shutdown()
        await security.close()
        await executor.close()
        await oracle.close()
        await helius.close()
        await bot.session.close()
        logger.info("9N8 shutdown complete.")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("9N8 interrupted by operator.")


if __name__ == "__main__":
    main()
