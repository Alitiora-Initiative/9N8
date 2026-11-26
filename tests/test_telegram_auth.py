import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from src.n9n8.webapp_server import verify_telegram_init_data


def signed_init_data(token: str, auth_date: int | None = None) -> str:
    fields = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAEAAE-test",
        "user": json.dumps({"id": 1234, "first_name": "Test"}, separators=(",", ":")),
    }
    check_string = "\n".join(key + "=" + fields[key] for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_valid_telegram_init_data():
    result = verify_telegram_init_data(signed_init_data("telegram-token"), "telegram-token")
    assert result["id"] == 1234


def test_init_data_rejects_tampering():
    value = signed_init_data("telegram-token").replace("first_name", "last_name")
    with pytest.raises(ValueError):
        verify_telegram_init_data(value, "telegram-token")


def test_init_data_rejects_old_auth_date():
    old = int(time.time()) - 100_000
    with pytest.raises(ValueError):
        verify_telegram_init_data(signed_init_data("telegram-token", old), "telegram-token")
