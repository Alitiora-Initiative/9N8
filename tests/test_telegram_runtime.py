import pytest

from src.n9n8.telegram_runtime import should_poll_telegram


def test_worker_only_runtime_never_starts_telegram_polling():
    assert should_poll_telegram(worker_only=True, transport="polling") is False


def test_webhook_transport_is_the_safe_default():
    assert should_poll_telegram(worker_only=False) is False
    assert should_poll_telegram(worker_only=False, transport="webhook") is False


def test_standalone_polling_requires_explicit_opt_in():
    assert should_poll_telegram(worker_only=False, transport="polling") is True


@pytest.mark.parametrize("transport", ["", "getUpdates", "webhooks", "unknown"])
def test_invalid_telegram_transport_is_rejected(transport):
    with pytest.raises(ValueError, match="N9N8_TELEGRAM_TRANSPORT"):
        should_poll_telegram(worker_only=False, transport=transport)
