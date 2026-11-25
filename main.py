from __future__ import annotations

import asyncio
import logging

from config import Settings
from core.engine import TradingEngine
from core.telegram_controller import TelegramController
from logging_setup import configure_logging
from storage import TradeStore

log = logging.getLogger(__name__)


async def async_main() -> None:
    settings = Settings.load()
    configure_logging(settings.log_level)

    store = TradeStore(settings.database_path)
    await store.open()

    engine = TradingEngine(settings, store)
    telegram = TelegramController(engine)
    engine.notifier = telegram

    telegram_task: asyncio.Task | None = None
    try:
        await engine.connect()
        if telegram.enabled:
            telegram_task = asyncio.create_task(
                telegram.run(), name="telegram-controller"
            )
        log.warning(
            "9N8 started in %s mode. Live order permission: %s",
            settings.mode.value.upper(),
            settings.live_enabled,
        )
        await engine.start()
    finally:
        engine.stop_event.set()
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
        logging.getLogger(__name__).warning("Shutdown requested by operator.")
