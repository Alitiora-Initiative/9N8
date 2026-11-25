from datetime import datetime, timezone
import pytest

from aiogram.types import Chat, Message, User

from core.telegram_controller import TelegramController


class FakeStore:
    async def set_control(self, key, value):
        return None

    async def pending_approvals(self, limit=20):
        return []


class FakeEngine:
    mode = "paper"
    store = FakeStore()


def _private_message(user_id: int, chat_type: str = "private") -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="Owner"),
        text="/start",
    )


def test_telegram_controller_starts_in_disabled_mode_without_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123456")
    controller = TelegramController(FakeEngine())
    assert controller.enabled is False
    assert controller.bot is None


def test_telegram_owner_authorization_is_private_chat_only(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123456,987654")
    controller = TelegramController(FakeEngine())
    assert controller._authorized(_private_message(123456))
    assert not controller._authorized(_private_message(123456, "group"))
    assert not controller._authorized(_private_message(111111))


def test_telegram_dashboard_and_approval_callbacks_are_short_and_valid(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123456")
    controller = TelegramController(FakeEngine())
    dashboard = controller._dashboard_keyboard()
    callbacks = [
        button.callback_data
        for row in dashboard.inline_keyboard
        for button in row
    ]
    assert "dash:paper" in callbacks
    assert "dash:pending" in callbacks
    keyboard = controller._approval_keyboard("ABCDEF123456")
    approval_callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert approval_callbacks == ["approve:ABCDEF123456", "deny:ABCDEF123456"]
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks + approval_callbacks)


@pytest.mark.asyncio
async def test_telegram_send_fails_closed_without_a_valid_bot(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123456")
    controller = TelegramController(FakeEngine())
    assert not await controller._send_message(123456, "No message is sent.")
    assert await controller.run() is None
    await controller.close()
