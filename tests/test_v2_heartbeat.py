from datetime import datetime, timedelta, timezone

import pytest

from src.n9n8.v2.health import WorkerHeartbeat


NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def test_worker_lease_only_reports_recent_running_heartbeats_alive():
    live = WorkerHeartbeat("worker-a", "running", "paper", NOW, lease_seconds=60)
    assert live.is_alive(now=NOW + timedelta(seconds=59))
    assert not live.is_alive(now=NOW + timedelta(seconds=61))
    stopped = WorkerHeartbeat("worker-a", "stopped", "paper", NOW)
    assert not stopped.is_alive(now=NOW)


def test_worker_lease_rejects_unknown_status_and_naive_timestamps():
    with pytest.raises(ValueError, match="unsupported worker status"):
        WorkerHeartbeat("worker-a", "healthy-ish", "live", NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        WorkerHeartbeat("worker-a", "running", "paper", datetime(2026, 11, 27, 12, 0))
    heartbeat = WorkerHeartbeat("worker-a", "running", "live", NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        heartbeat.is_alive(now=datetime(2026, 11, 27, 12, 0))
