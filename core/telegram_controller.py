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
        self.dispatcher.message.register(self.menu, Command("start", "menu"))
        self.dispatcher.message.register(self.status, Command("status"))
        self.dispatcher.message.register(self.positions, Command("positions"))
        self.dispatcher.message.register(self.history, Command("history"))
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

    def _dashboard_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Status", callback_data="dash:status"),
                InlineKeyboardButton(text="📍 Positions", callback_data="dash:positions"),
            ],
            [
                InlineKeyboardButton(text="🧾 Pending approvals", callback_data="dash:pending"),
                InlineKeyboardButton(text="📜 Trade history", callback_data="dash:history"),
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
            "Every new trade needs a separate approval. Risk-managed exits remain active.\n"
            "Select a control below.",
            reply_markup=self._dashboard_keyboard(),
        )

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
            if data.startswith("dash:"):
                action = data.split(":", 1)[1]
                if action == "status":
                    result = await self.engine.status_text()
                elif action == "positions":
                    rows = await self.engine.store.positions()
                    result = "OPEN POSITIONS\\n" + (
                        "\\n\\n".join(
                            f"{p['symbol']} · {p['side'].upper()} · qty {float(p['quantity']):.8g} "
                            f"· entry {float(p['entry_price']):.10g} · mark {float(p['mark_price']):.10g}"
                            for p in rows[:20]
                        ) if rows else "None"
                    )
                elif action == "pending":
                    rows = await self.engine.store.pending_approvals(20)
                    result = "PENDING APPROVALS\\n" + (
                        "\\n".join(
                            f"{r['approval_id']} · {r['payload']['intent']['side'].upper()} "
                            f"{r['payload']['intent']['symbol']} · expires {r['expires_at']}"
                            for r in rows
                        ) if rows else "None"
                    )
                elif action == "history":
                    rows = await self.engine.store.query_trades(10)
                    result = "RECENT TRADES\\n" + (
                        "\\n".join(
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
                        InlineKeyboardButton(text="🚨 CONFIRM FLATTEN", callback_data="panic:confirm"),
                        InlineKeyboardButton(text="Cancel", callback_data="panic:cancel"),
                    ]])
                    await callback.answer("Confirm emergency flatten.", show_alert=True)
                    await message.answer(
                        "This attempts to cancel open orders and close known positions at market. "
                        "Check every connected account after the operation. Proceed?",
                        reply_markup=confirm,
                    )
                    return
                else:
                    await callback.answer("Unknown dashboard action.", show_alert=True)
                    return
                await callback.answer("Done.")
                await message.answer(result[:4000], reply_markup=self._dashboard_keyboard())
                return
            if data == "panic:cancel":
                await callback.answer("Cancelled.")
                await message.edit_reply_markup(reply_markup=None)
                return
            if data == "panic:confirm":
                await callback.answer("Emergency flatten started.", show_alert=True)
                await message.edit_reply_markup(reply_markup=None)
                await message.answer(
                    "Emergency flatten requested. 9N8 is pausing entries and attempting exits."
                )
                try:
                    await self.engine.panic()
                    await message.answer("9N8 panic routine completed. Verify every exchange and wallet directly.")
                except Exception as exc:
                    await message.answer(
                        "WARNING: flatten was incomplete. Trading remains paused. "
                        "Inspect open orders and balances manually. " + str(exc)[:500]
                    )
                return
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
            log.warning("Telegram control action failed (%s).", type(exc).__name__)
            try:
                await callback.answer("Action rejected.", show_alert=True)
            except Exception:
                pass
            await message.edit_reply_markup(reply_markup=None)
            await message.answer("Action rejected: " + str(exc)[:1000])

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
