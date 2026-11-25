from __future__ import annotations

import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

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
        self.dispatcher.message.register(self.resume, Command("resume"))
        self.dispatcher.message.register(self.pending, Command("pending"))
        self.dispatcher.callback_query.register(self.approval_callback)

    @property
    def enabled(self) -> bool:
        return self.bot is not None and bool(self.allowed_user_ids)

    def _authorized(self, message: Message) -> bool:
        return bool(
            message.from_user
            and message.from_user.id in self.allowed_user_ids
            and message.chat
            and message.chat.type == "private"
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

    async def resume(self, message: Message) -> None:
        if not self._authorized(message):
            await message.answer("Unauthorized. Use the private owner chat.")
            return
        try:
            await self.engine.risk.resume()
            await message.answer(
                "9N8 resumed. New trade signals will still require individual approval."
            )
        except Exception as exc:
            await message.answer("Resume rejected: " + str(exc)[:500])

    async def pending(self, message: Message) -> None:
        if not self._authorized(message):
            await message.answer("Unauthorized. Use the private owner chat.")
            return
        rows = await self.engine.store.pending_approvals(20)
        if not rows:
            await message.answer("There are no pending trade proposals.")
            return
        lines = []
        for row in rows:
            raw = row["payload"]["intent"]
            lines.append(
                f"{row['approval_id']} · {raw['side'].upper()} {raw['symbol']} "
                f"· {raw['venue']} · expires {row['expires_at']}"
            )
        await message.answer("PENDING TRADE PROPOSALS\n" + "\n".join(lines))

    async def approval_callback(self, callback: CallbackQuery) -> None:
        user = callback.from_user
        message = callback.message
        if (
            not user
            or user.id not in self.allowed_user_ids
            or message is None
            or not getattr(message, "chat", None)
            or message.chat.type != "private"
        ):
            await callback.answer("Unauthorized. Use the private owner chat.", show_alert=True)
            return
        data = str(callback.data or "")
        try:
            action, approval_id = data.split(":", 1)
            if not approval_id or len(approval_id) > 32:
                raise ValueError("Invalid approval ID.")
            if action == "approve":
                await callback.answer("Rechecking live price and risk…")
                result = await self.engine.approve_pending(approval_id, str(user.id))
            elif action == "deny":
                await callback.answer("Proposal denied.")
                result = await self.engine.deny_pending(approval_id, str(user.id))
            else:
                await callback.answer("Unknown approval action.", show_alert=True)
                return
            await message.edit_reply_markup(reply_markup=None)
            await message.answer(result[:4000])
        except Exception as exc:
            log.warning("Telegram trade approval action failed (%s).", type(exc).__name__)
            await message.edit_reply_markup(reply_markup=None)
            await message.answer("Trade action rejected: " + str(exc)[:1000])

    async def send_approval(self, approval_id: str, text: str) -> None:
        if not self.enabled:
            log.warning("Approval %s queued but Telegram controller is disabled.", approval_id)
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ APPROVE TRADE",
                callback_data="approve:" + approval_id,
            ),
            InlineKeyboardButton(
                text="❌ DENY",
                callback_data="deny:" + approval_id,
            ),
        ]])
        for user_id in self.allowed_user_ids:
            try:
                await self.bot.send_message(
                    chat_id=user_id, text=text[:4000], reply_markup=keyboard
                )
            except Exception as exc:
                log.warning(
                    "Could not send approval %s to authorized chat (%s).",
                    approval_id, type(exc).__name__,
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
