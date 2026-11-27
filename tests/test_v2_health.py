from datetime import datetime, timezone

import pytest

from src.n9n8.v2.health import ComponentHealth, EngineHealth


def test_health_payload_contains_sanitized_status_only():
    health = EngineHealth(
        observed_at=datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc),
        trading_mode="paper",
        kill_switch_active=True,
        components=(
            ComponentHealth("telegram", True, True, datetime(2026, 11, 27, 11, 59, tzinfo=timezone.utc), 20),
            ComponentHealth("jupiter", True, False, last_error_code="provider_timeout"),
        ),
    )
    payload = health.public_payload()
    assert payload["trading_mode"] == "paper"
    assert payload["kill_switch_active"] is True
    assert payload["components"][0]["latency_ms"] == 20
    assert "api_key" not in str(payload).lower()
    assert "secret" not in str(payload).lower()


def test_component_names_must_be_unique_and_times_aware():
    with pytest.raises(ValueError, match="unique"):
        EngineHealth(
            observed_at=datetime.now(timezone.utc),
            trading_mode="paper",
            kill_switch_active=True,
            components=(ComponentHealth("rpc", True, False), ComponentHealth("rpc", True, False)),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        EngineHealth(
            observed_at=datetime(2026, 11, 27),
            trading_mode="paper",
            kill_switch_active=True,
            components=(),
        )
