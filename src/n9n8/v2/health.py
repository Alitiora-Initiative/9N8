from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    name: str
    configured: bool
    connected: bool
    last_success_at: datetime | None = None
    latency_ms: int | None = None
    last_error_code: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("component name is required")
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError("latency_ms cannot be negative")
        if self.last_success_at is not None and (
            self.last_success_at.tzinfo is None or self.last_success_at.utcoffset() is None
        ):
            raise ValueError("last_success_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class EngineHealth:
    observed_at: datetime
    trading_mode: str
    kill_switch_active: bool
    components: tuple[ComponentHealth, ...]

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        names = [component.name for component in self.components]
        if len(names) != len(set(names)):
            raise ValueError("component names must be unique")

    def public_payload(self) -> dict[str, object]:
        return {
            "observed_at": self.observed_at.astimezone(timezone.utc).isoformat(),
            "trading_mode": self.trading_mode,
            "kill_switch_active": self.kill_switch_active,
            "components": [
                {
                    "name": component.name,
                    "configured": component.configured,
                    "connected": component.connected,
                    "last_success_at": (
                        component.last_success_at.astimezone(timezone.utc).isoformat()
                        if component.last_success_at else None
                    ),
                    "latency_ms": component.latency_ms,
                    "last_error_code": component.last_error_code,
                }
                for component in self.components
            ],
        }


@dataclass(frozen=True, slots=True)
class WorkerHeartbeat:
    """Sanitized worker lease; presence alone is not provider health."""

    worker_id: str
    status: str
    trading_mode: str
    last_seen_at: datetime
    lease_seconds: int = 90

    def __post_init__(self) -> None:
        if not self.worker_id.strip():
            raise ValueError("worker_id is required")
        if self.status not in {"starting", "running", "stopping", "stopped", "error"}:
            raise ValueError("unsupported worker status")
        if self.trading_mode not in {"paper", "live"}:
            raise ValueError("trading_mode must be paper or live")
        if self.last_seen_at.tzinfo is None or self.last_seen_at.utcoffset() is None:
            raise ValueError("last_seen_at must be timezone-aware")
        if isinstance(self.lease_seconds, bool) or not 5 <= self.lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 5 and 3600")

    def is_alive(self, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        age = (current.astimezone(timezone.utc) - self.last_seen_at.astimezone(timezone.utc)).total_seconds()
        return self.status == "running" and -5 <= age <= self.lease_seconds
