from __future__ import annotations

import asyncio
import logging

from config import Settings
from core.dashboard_api import DashboardAPIServer
from core.engine import TradingEngine
from core.telegram_controller import TelegramController
from logging_setup import configure_logging
from storage import TradeStore


async def async_main() -> None:
    settings = Settings.load()
    configure_logging(settings.log_level)
    store = TradeStore(settings.database_path)
    await store.open()
    engine = TradingEngine(settings, store)
    telegram = TelegramController(engine)
    engine.notifier = telegram
    dashboard = DashboardAPIServer(engine)
    telegram_task = None
    try:
        await engine.connect()
        await dashboard.start()
        if telegram.enabled:
            telegram_task = asyncio.create_task(telegram.run(), name="telegram-controller")
        logging.getLogger(__name__).warning("9N8 dashboard started in %s mode", settings.mode.value)
        await engine.start()
    finally:
        engine.stop_event.set()
        await dashboard.close()
        await engine.close()
        if telegram_task:
            telegram_task.cancel()
            await asyncio.gather(telegram_task, return_exceptions=True)
        await telegram.close()
        await store.close()


if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logging.getLogger(__name__).warning("Shutdown requested")
