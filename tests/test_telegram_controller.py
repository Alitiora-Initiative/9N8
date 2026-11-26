from datetime import datetime, timezone
import pytest

from aiogram.types import Chat, Message, User

from core.telegram_controller import TelegramController


class FakeStore:
    def __init__(self):
        self.controls = {}

    async def set_control(self, key, value):
        self.controls[key] = value

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


@pytest.mark.asyncio
async def test_telegram_does_not_delete_or_override_existing_webhook(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123456")
    controller = TelegramController(FakeEngine())
    store = controller.engine.store
    controller.bot = SimpleNamespace(
        get_webhook_info=AsyncMock(
            return_value=SimpleNamespace(url="https://example.invalid/telegram-webhook")
        )
    )
    controller.send = AsyncMock()

    await controller.run()

    assert store.controls["telegram_polling_status"] == "webhook_conflict"
    controller.send.assert_awaited_once()
    controller.dispatcher.start_polling = AsyncMock()
    controller.dispatcher.start_polling.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_retries_transient_private_send_errors(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    class TelegramNetworkError(Exception):
        pass

    class TelegramRetryAfter(Exception):
        def __init__(self):
            self.retry_after = 0.001

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123456")
    controller = TelegramController(FakeEngine())
    attempts = 0

    async def send_message(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TelegramNetworkError("transient network failure")
        if attempts == 2:
            raise TelegramRetryAfter()
        return SimpleNamespace(message_id=7)

    controller.bot = SimpleNamespace(send_message=send_message)
    assert await controller._send_message(123456, "test delivery")
    assert attempts == 3
