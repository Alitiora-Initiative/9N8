from __future__ import annotations

import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

log = logging.getLogger(__name__)


class TelegramController:
    def __init__(self, engine):
        self.engine = engine
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        raw_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
        self.allowed_user_ids = {
            int(value.strip()) for value in raw_ids.split(",")
            if value.strip().isdigit()
        }
        self.bot = Bot(self.token) if self.token else None
        self.dispatcher = Dispatcher()
        self.dispatcher.message.register(self.status, Command("status"))
        self.dispatcher.message.register(self.pause, Command("pause"))
        self.dispatcher.message.register(self.panic, Command("panic"))

    @property
    def enabled(self) -> bool:
        return self.bot is not None and bool(self.allowed_user_ids)

    def _authorized(self, message: Message) -> bool:
        return bool(
            message.from_user
            and message.from_user.id in self.allowed_user_ids
        )

    async def status(self, message: Message) -> None:
        if not self._authorized(message):
            await message.answer("Unauthorized.")
            return
        await message.answer(await self.engine.status_text())

    async def pause(self, message: Message) -> None:
        if not self._authorized(message):
            await message.answer("Unauthorized.")
            return
        await self.engine.risk.pause()
        await message.answer(
            "9N8 PAUSED. New entries are blocked. Existing positions remain under monitoring."
        )

    async def panic(self, message: Message) -> None:
        if not self._authorized(message):
            await message.answer("Unauthorized.")
            return
        await message.answer(
            "PANIC received. 9N8 is cancelling outstanding orders and attempting to close known positions."
        )
        try:
            await self.engine.panic()
        except Exception:
            log.exception("Emergency flatten did not complete.")
            await message.answer(
                "WARNING: flattening was incomplete. Trading remains paused. "
                "Check each exchange/broker/wallet manually now; do not assume positions are closed."
            )

    async def send(self, text: str) -> None:
        if not self.enabled:
            return
        for user_id in self.allowed_user_ids:
            try:
                await self.bot.send_message(user_id, text[:4000])
            except Exception:
                log.exception("Telegram notification failed for authorized chat.")

    async def run(self) -> None:
        if not self.enabled:
            log.warning(
                "Telegram controller disabled: set TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_ALLOWED_USER_IDS to numeric owner IDs."
            )
            return
        await self.dispatcher.start_polling(self.bot, handle_signals=False)

    async def close(self) -> None:
        if self.dispatcher and self.dispatcher.is_running:
            await self.dispatcher.stop_polling()
        if self.bot:
            await self.bot.session.close()
