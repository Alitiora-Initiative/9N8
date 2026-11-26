"""Telegram runtime ownership for 9N8.

Production Telegram updates are delivered to the Supabase webhook. The Python
worker may send notifications but must not call getUpdates or overwrite the
webhook-owned command menu. Long polling is reserved for an explicitly selected
local standalone process after its webhook has been removed.
"""

from __future__ import annotations


def should_poll_telegram(worker_only: bool, transport: str | None = None) -> bool:
    """Return whether this process may use Telegram long polling.

    Worker-only deployments are always outbound-only, even if a stale environment
    variable asks for polling. Combined/local processes default to webhook mode.
    """
    if worker_only:
        return False
    mode = (transport or "webhook").strip().lower()
    if mode not in {"webhook", "polling"}:
        raise ValueError("N9N8_TELEGRAM_TRANSPORT must be 'webhook' or 'polling'.")
    return mode == "polling"
