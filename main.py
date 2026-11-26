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
    interrupted = await store.recover_interrupted_approvals()
    if interrupted:
        log.critical(
            "Recovered %d interrupted approval(s). Trading is locked until provider-side order/fill reconciliation is completed.",
            interrupted,
        )

    engine = TradingEngine(settings, store)
    telegram = TelegramController(engine)
    engine.notifier = telegram

    telegram_task: asyncio.Task | None = None
    try:
        # Bring the private control plane online before touching market providers.
        # This lets the owner see startup failures and use /status or /paper even
        # when an exchange API is temporarily unavailable.
        if telegram.enabled:
            telegram_task = asyncio.create_task(
                telegram.run(), name="telegram-controller"
            )

        reconnect_delay = 2.0
        provider_was_down = False
        last_provider_notice_at = 0.0
        loop = asyncio.get_running_loop()
        while True:
            try:
                await engine.connect()
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                now = loop.time()
                should_notify = (
                    not provider_was_down
                    or now - last_provider_notice_at >= 300.0
                )
                if should_notify:
                    if not provider_was_down:
                        log.exception(
                            "9N8 provider startup failed (%s). Telegram remains available.",
                            type(exc).__name__,
                        )
                    else:
                        log.error(
                            "9N8 provider is still unavailable (%s). Retrying in %.1fs.",
                            type(exc).__name__,
                            reconnect_delay,
                        )
                    last_provider_notice_at = now
                    if telegram.enabled:
                        await telegram.send(
                            "9N8 market connection is unavailable. No trade was placed. "
                            f"Retrying provider connection in {reconnect_delay:.0f}s. "
                            "Use /status to inspect the bot or /paper for the virtual account."
                        )
                provider_was_down = True
                await engine.close()
                engine.connectors.clear()
                engine.specs.clear()
                engine.snapshots.clear()
                engine.stop_event.clear()
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(60.0, reconnect_delay * 2)

        log.warning(
            "9N8 started in %s mode. Live order permission: %s",
            settings.mode.value.upper(),
            settings.live_enabled,
        )
        if telegram.enabled:
            if provider_was_down:
                await telegram.send("9N8 market providers have recovered; monitoring is online again.")
            await telegram.send(
                f"9N8 is online in {settings.mode.value.upper()} mode. "
                "New entries require individual Telegram approval. Use /paper for the virtual account."
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
