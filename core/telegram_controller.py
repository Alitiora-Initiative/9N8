from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message

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
        self._closed = False
        self.configuration_error = ""
        self.bot = None
        if self.token:
            try:
                self.bot = Bot(self.token)
            except Exception as exc:
                self.configuration_error = type(exc).__name__
                log.error(
                    "Telegram disabled because TELEGRAM_BOT_TOKEN failed validation (%s).",
                    self.configuration_error,
                )
        self.dispatcher = Dispatcher()
        self.dispatcher.message.register(self.menu, Command("start", "menu", "help"))
        self.dispatcher.message.register(self.status, Command("status"))
        self.dispatcher.message.register(self.positions, Command("positions"))
        self.dispatcher.message.register(self.history, Command("history"))
        self.dispatcher.message.register(self.pause, Command("pause"))
        self.dispatcher.message.register(self.panic, Command("panic"))
        self.dispatcher.message.register(self.resume, Command("resume"))
        self.dispatcher.message.register(self.pending, Command("pending"))
        self.dispatcher.message.register(self.paper, Command("paper"))
        self.dispatcher.callback_query.register(self.approval_callback)
        self.dispatcher.errors.register(self.on_error)

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

    def _dashboard_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Status", callback_data="dash:status"),
                InlineKeyboardButton(text="💵 Paper account", callback_data="dash:paper"),
            ],
            [
                InlineKeyboardButton(text="📍 Positions", callback_data="dash:positions"),
                InlineKeyboardButton(text="🧾 Pending approvals", callback_data="dash:pending"),
            ],
            [
                InlineKeyboardButton(text="⏸ Pause entries", callback_data="dash:pause"),
                InlineKeyboardButton(text="▶️ Resume entries", callback_data="dash:resume"),
            ],
            [InlineKeyboardButton(text="🚨 Emergency flatten", callback_data="dash:panic")],
        ])

    async def menu(self, message: Message) -> None:
        if not self._authorized(message):
            await message.answer("Unauthorized. Use the private owner chat.")
            return
        await message.answer(
            "9N8 · PRIVATE TRADING CONTROL\n"
            "Every new entry requires an individual approval. Paper mode uses live market data and simulated fills.\n"
            "Select a control below.",
            reply_markup=self._dashboard_keyboard(),
        )
        await self.engine.store.set_control("telegram_chat_ready:" + str(message.from_user.id), "true")
        await self._send_pending_to_user(message.from_user.id)

    async def positions(self, message: Message) -> None:
        if not self._authorized(message):
            await message.answer("Unauthorized. Use the private owner chat.")
            return
        rows = await self.engine.store.positions()
        if not rows:
            await message.answer("No open positions are recorded.")
            return
        lines = []
        for p in rows[:20]:
            lines.append(
                f"{p['symbol']} · {p['side'].upper()} · {p['venue']}\n"
                f"Qty {float(p['quantity']):.8g} · entry {float(p['entry_price']):.10g} "
                f"· mark {float(p['mark_price']):.10g}\n"
                f"Unrealized PnL {float(p.get('unrealized_pnl_usd') or 0):+.4f} USD"
            )
        await message.answer("OPEN POSITIONS\n\n" + "\n\n".join(lines))

    async def history(self, message: Message) -> None:
        if not self._authorized(message):
            await message.answer("Unauthorized. Use the private owner chat.")
            return
        rows = await self.engine.store.query_trades(10)
        if not rows:
            await message.answer("No trades recorded yet.")
            return
        lines = []
        for trade in rows:
            pnl = float(trade.get("realized_pnl_usd") or 0)
            lines.append(
                f"{trade['symbol']} · {trade['side'].upper()} · {trade['status']}\n"
                f"Qty {float(trade['quantity']):.8g} · avg {float(trade['average_price']):.10g} "
                f"· realized {pnl:+.4f} USD"
            )
        await message.answer("RECENT TRADE HISTORY\n\n" + "\n\n".join(lines))

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
            await message.answer("Unauthorized. Use the private owner chat.")
            return
        confirm = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚨 CONFIRM FLATTEN", callback_data="panic:confirm"),
            InlineKeyboardButton(text="Cancel", callback_data="panic:cancel"),
        ]])
        await message.answer(
            "PANIC SELL ALL confirmation. 9N8 will pause entries, cancel known open orders "
            "and attempt to close tracked positions. Provider-side verification is still required.",
            reply_markup=confirm,
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

    async def _approval_text(self, row: dict) -> str:
        payload = row.get("payload") or {}
        intent = payload.get("intent") or {}
        size = payload.get("size") or {}
        return (
            "9N8 TRADE APPROVAL REQUIRED\n"
            f"{str(intent.get('side', 'unknown')).upper()} {intent.get('symbol', 'unknown')}\n"
            f"Venue: {intent.get('venue', 'unknown')} · mode: {self.engine.mode.upper()}\n"
            f"Reference entry: {intent.get('entry_price', 'n/a')}\n"
            f"Estimated quantity: {size.get('quantity', 'n/a')}\n"
            f"Notional: {size.get('notional_usd', 'n/a')} USD · stop risk: {size.get('risk_usd', 'n/a')} USD\n"
            f"Stop: {intent.get('stop_loss', 'n/a')} · Target: {intent.get('take_profit', 'n/a')}\n"
            f"Reason: {str(intent.get('reason', ''))[:500]}\n"
            f"Approval ID: {row.get('approval_id')}\n"
            f"Expires: {row.get('expires_at')}\n"
            "Approval rechecks the latest quote and all risk limits. No order exists until approved."
        )

    def _approval_keyboard(self, approval_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ APPROVE TRADE", callback_data="approve:" + approval_id),
            InlineKeyboardButton(text="❌ DENY", callback_data="deny:" + approval_id),
        ]])

    async def _send_message(
        self, chat_id: int, text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        if not self.bot:
            return False
        for attempt in range(3):
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=text[:4000],
                    reply_markup=reply_markup,
                )
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_name = type(exc).__name__
                retryable = error_name in {
                    "TelegramRetryAfter", "TelegramNetworkError",
                    "TelegramServerError", "TimeoutError",
                }
                if error_name == "TelegramForbiddenError":
                    log.warning(
                        "Telegram private chat is not available for an authorized user; "
                        "the proposal remains stored for /pending recovery."
                    )
                    return False
                if error_name == "TelegramUnauthorizedError":
                    log.critical(
                        "Telegram rejected the configured bot token. Polling and notifications need token repair."
                    )
                    return False
                if not retryable or attempt >= 2:
                    log.warning("Telegram send failed (%s).", error_name)
                    return False
                retry_after = getattr(exc, "retry_after", 0.0)
                try:
                    delay = float(retry_after) if retry_after else 2 ** attempt
                except (TypeError, ValueError):
                    delay = 2 ** attempt
                await asyncio.sleep(min(10.0, max(0.5, delay)))
        return False

    async def _send_pending_to_user(self, user_id: int) -> int:
        try:
            rows = await self.engine.store.pending_approvals(20)
        except Exception as exc:
            log.error("Could not read pending approvals for Telegram recovery (%s).", type(exc).__name__)
            return 0
        delivered = 0
        for row in rows:
            ok = await self._send_message(
                user_id,
                await self._approval_text(row),
                self._approval_keyboard(str(row["approval_id"])),
            )
            delivered += int(ok)
        return delivered

    async def pending(self, message: Message) -> None:
        if not self._authorized(message):
            await message.answer("Unauthorized. Use the private owner chat.")
            return
        rows = await self.engine.store.pending_approvals(20)
        if not rows:
            await message.answer("There are no pending trade proposals.")
            return
        delivered = await self._send_pending_to_user(message.from_user.id)
        await message.answer(
            f"{len(rows)} unexpired trade proposal(s) found. "
            f"Sent {delivered} approval card(s) to this private chat."
        )

    async def paper(self, message: Message) -> None:
        if not self._authorized(message):
            await message.answer("Unauthorized. Use the private owner chat.")
            return
        try:
            await message.answer(await self.engine.paper_status_text())
        except Exception as exc:
            log.error("Paper account summary failed (%s).", type(exc).__name__)
            await message.answer("Paper account status is temporarily unavailable. Try /status.")

    async def on_error(self, event: ErrorEvent) -> bool:
        exc = event.exception
        log.error("Telegram update handler failed (%s).", type(exc).__name__)
        update = event.update
        message = getattr(update, "message", None)
        if isinstance(message, Message) and self._authorized(message):
            await self._send_message(
                message.chat.id,
                "9N8 could not complete that Telegram request. No unconfirmed trade will be replayed. Try /status, /paper or /pending.",
            )
        callback = getattr(update, "callback_query", None)
        if callback and callback.from_user.id in self.allowed_user_ids:
            try:
                await callback.answer("The request failed; inspect /status and /pending.", show_alert=True)
            except Exception:
                pass
        return True

    async def _safe_remove_keyboard(self, message: Message) -> None:
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception as exc:
            log.debug("Could not remove an old Telegram keyboard (%s).", type(exc).__name__)

    async def approval_callback(self, callback: CallbackQuery) -> None:
        user = callback.from_user
        message = callback.message
        if (
            not user
            or user.id not in self.allowed_user_ids
            or not isinstance(message, Message)
            or not getattr(message, "chat", None)
            or message.chat.type != "private"
        ):
            try:
                await callback.answer(
                    "Unauthorized. Use the private owner chat.",
                    show_alert=True,
                )
            except Exception:
                pass
            return

        try:
            # Acknowledge exactly once so Telegram's callback spinner always clears.
            try:
                await callback.answer("Processing…")
            except Exception:
                pass

            data = str(callback.data or "")
            if data.startswith("dash:"):
                action = data.split(":", 1)[1]
                if action == "status":
                    result = await self.engine.status_text()
                elif action == "paper":
                    result = await self.engine.paper_status_text()
                elif action == "positions":
                    rows = await self.engine.store.positions()
                    result = "OPEN POSITIONS\n" + (
                        "\n\n".join(
                            f"{p['symbol']} · {p['side'].upper()} · qty {float(p['quantity']):.8g} "
                            f"· entry {float(p['entry_price']):.10g} · mark {float(p['mark_price']):.10g}"
                            for p in rows[:20]
                        ) if rows else "None"
                    )
                elif action == "pending":
                    rows = await self.engine.store.pending_approvals(20)
                    sent = await self._send_pending_to_user(user.id)
                    result = (
                        f"PENDING APPROVALS: {len(rows)} active · {sent} approval cards sent\n"
                        + (
                            "\n".join(
                                f"{r['approval_id']} · {r['payload']['intent']['side'].upper()} "
                                f"{r['payload']['intent']['symbol']} · expires {r['expires_at']}"
                                for r in rows
                            ) if rows else "None"
                        )
                    )
                elif action == "history":
                    rows = await self.engine.store.query_trades(10)
                    result = "RECENT TRADES\n" + (
                        "\n".join(
                            f"{r['symbol']} · {r['status']} · PnL {float(r.get('realized_pnl_usd') or 0):+.4f} USD"
                            for r in rows
                        ) if rows else "None"
                    )
                elif action == "pause":
                    await self.engine.risk.pause()
                    result = "9N8 paused. New proposals are blocked; risk-managed exits continue."
                elif action == "resume":
                    await self.engine.risk.resume()
                    result = "9N8 resumed. New proposals still require individual Telegram approval."
                elif action == "panic":
                    confirm = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(
                            text="🚨 CONFIRM FLATTEN",
                            callback_data="panic:confirm",
                        ),
                        InlineKeyboardButton(text="Cancel", callback_data="panic:cancel"),
                    ]])
                    await self._send_message(
                        message.chat.id,
                        "Emergency flatten confirmation. This pauses new entries and attempts to close tracked positions. "
                        "In paper mode only virtual positions are closed.",
                        confirm,
                    )
                    return
                else:
                    result = "Unknown dashboard action."
                await self._send_message(
                    message.chat.id,
                    result[:4000],
                    self._dashboard_keyboard(),
                )
                return

            if data == "panic:cancel":
                await self._safe_remove_keyboard(message)
                await self._send_message(message.chat.id, "Emergency flatten cancelled.")
                return

            if data == "panic:confirm":
                await self._safe_remove_keyboard(message)
                await self._send_message(
                    message.chat.id,
                    "Emergency flatten requested. New entries are being paused and tracked positions are being processed.",
                )
                try:
                    await self.engine.panic()
                    await self._send_message(
                        message.chat.id,
                        "9N8 panic routine completed. Verify the final state in the paper dashboard or directly with every live provider.",
                    )
                except Exception as exc:
                    await self._send_message(
                        message.chat.id,
                        "WARNING: flatten was incomplete. Trading remains paused. "
                        "Inspect orders and balances manually. " + str(exc)[:500],
                    )
                return

            action, approval_id = data.split(":", 1)
            if not approval_id or len(approval_id) > 32:
                raise ValueError("Invalid approval ID.")
            if action == "approve":
                result = await self.engine.approve_pending(approval_id, str(user.id))
            elif action == "deny":
                result = await self.engine.deny_pending(approval_id, str(user.id))
            else:
                await self._send_message(message.chat.id, "Unknown approval action.")
                return

            await self._safe_remove_keyboard(message)
            await self._send_message(message.chat.id, result[:4000])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Telegram control action failed (%s).", type(exc).__name__)
            await self._safe_remove_keyboard(message)
            await self._send_message(
                message.chat.id,
                "Action rejected: " + str(exc)[:1000],
            )

    async def send_approval(self, approval_id: str, text: str) -> None:
        if not self.enabled:
            log.warning(
                "Approval %s is stored but Telegram delivery is disabled; "
                "configure the bot token and owner ID, then use /pending.",
                approval_id,
            )
            return
        keyboard = self._approval_keyboard(approval_id)
        delivered = 0
        for user_id in self.allowed_user_ids:
            delivered += int(await self._send_message(user_id, text, keyboard))
        try:
            await self.engine.store.set_control(
                "telegram_last_approval_delivery",
                "delivered" if delivered else "not_delivered",
            )
        except Exception:
            log.warning("Could not store Telegram approval delivery status.")

    async def send(self, text: str) -> None:
        if not self.enabled:
            return
        for user_id in self.allowed_user_ids:
            await self._send_message(user_id, text)


    async def run(self) -> None:
        if not self.enabled:
            log.warning(
                "Telegram controller disabled: set a valid TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_ALLOWED_USER_IDS to numeric owner IDs."
            )
            return
        delay = 1.0
        while not self._closed:
            try:
                await self.dispatcher.start_polling(
                    self.bot,
                    handle_signals=False,
                    allowed_updates=["message", "callback_query"],
                )
                if self._closed:
                    return
                log.warning("Telegram polling returned unexpectedly; restarting.")
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_name = type(exc).__name__
                if error_name == "TelegramUnauthorizedError":
                    log.critical("Telegram polling stopped: bot token was rejected.")
                    try:
                        await self.engine.store.set_control("telegram_polling_status", "unauthorized")
                    except Exception:
                        pass
                    return
                log.error(
                    "Telegram polling disconnected (%s); retrying in %.1fs.",
                    error_name,
                    delay,
                )
                try:
                    await self.engine.store.set_control("telegram_polling_status", "reconnecting")
                except Exception:
                    pass
                await asyncio.sleep(delay)
                delay = min(60.0, delay * 2)
            else:
                delay = 1.0

    async def close(self) -> None:
        self._closed = True
        if self.dispatcher and self.dispatcher.is_running:
            try:
                await self.dispatcher.stop_polling()
            except Exception as exc:
                log.warning("Telegram polling shutdown reported %s.", type(exc).__name__)
        if self.bot:
            try:
                await self.bot.session.close()
            except Exception as exc:
                log.warning("Telegram bot session shutdown reported %s.", type(exc).__name__)
