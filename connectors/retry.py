"""Retries for idempotent market-data/discovery reads only.

Order submission and transaction broadcasts must never be automatically retried:
a timeout after a broadcast can mean that the provider accepted the order.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import aiohttp

log = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


class RetryableReadError(RuntimeError):
    """Transient HTTP/RPC status on an idempotent read operation."""


_TRANSIENT_CCXT_NAMES = {
    "NetworkError",
    "ExchangeNotAvailable",
    "RequestTimeout",
    "DDoSProtection",
    "RateLimitExceeded",
    "OnMaintenance",
}


def _retryable(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            asyncio.TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ServerTimeoutError,
            aiohttp.ClientPayloadError,
            RetryableReadError,
        ),
    ):
        return True
    if isinstance(exc, aiohttp.ClientResponseError):
        return exc.status in {408, 425, 429, 500, 502, 503, 504}
    return exc.__class__.__name__ in _TRANSIENT_CCXT_NAMES


def retry_read(
    *,
    attempts: int = 3,
    timeout_seconds: float = 10.0,
    base_delay_seconds: float = 0.25,
    max_delay_seconds: float = 3.0,
) -> F:
    """Bound an idempotent async read with timeout and capped exponential backoff."""
    if attempts < 1 or timeout_seconds <= 0:
        raise ValueError("Retry attempts and timeout must be positive.")

    def decorate(func: F) -> F:
        @functools.wraps(func)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(attempts):
                try:
                    return await asyncio.wait_for(
                        func(*args, **kwargs),
                        timeout=timeout_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not _retryable(exc) or attempt + 1 >= attempts:
                        raise
                    retry_after = getattr(exc, "retry_after", None)
                    try:
                        requested = float(retry_after) if retry_after is not None else 0.0
                    except (TypeError, ValueError):
                        requested = 0.0
                    delay = min(
                        max_delay_seconds,
                        max(base_delay_seconds * (2 ** attempt), requested),
                    )
                    log.warning(
                        "%s read failed transiently (%s); retry %d/%d in %.2fs.",
                        func.__qualname__,
                        type(exc).__name__,
                        attempt + 1,
                        attempts - 1,
                        delay,
                    )
                    await asyncio.sleep(delay)
            raise RuntimeError("Retry loop exited unexpectedly.")

        return wrapped  # type: ignore[return-value]

    return decorate
