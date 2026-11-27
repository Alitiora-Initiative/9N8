from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from .contracts import TradeIntent, TradingMode


class ExecutionState(str, Enum):
    CREATED = "created"
    PREFLIGHT = "preflight"
    AWAITING_APPROVAL = "awaiting_approval"
    SIGNED = "signed"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


_ALLOWED_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.CREATED: frozenset({ExecutionState.PREFLIGHT, ExecutionState.CANCELLED}),
    ExecutionState.PREFLIGHT: frozenset({
        ExecutionState.AWAITING_APPROVAL, ExecutionState.FAILED, ExecutionState.CANCELLED,
    }),
    ExecutionState.AWAITING_APPROVAL: frozenset({
        ExecutionState.SIGNED, ExecutionState.CANCELLED, ExecutionState.FAILED,
    }),
    ExecutionState.SIGNED: frozenset({
        ExecutionState.SUBMITTED, ExecutionState.FAILED, ExecutionState.UNKNOWN,
    }),
    ExecutionState.SUBMITTED: frozenset({
        ExecutionState.CONFIRMED, ExecutionState.FAILED, ExecutionState.UNKNOWN,
    }),
    # A possibly-submitted intent can only be reconciled, never submitted again.
    ExecutionState.UNKNOWN: frozenset({ExecutionState.CONFIRMED, ExecutionState.FAILED}),
    ExecutionState.CONFIRMED: frozenset(),
    ExecutionState.FAILED: frozenset(),
    ExecutionState.CANCELLED: frozenset(),
}


def transition(current: ExecutionState, target: ExecutionState) -> ExecutionState:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Invalid execution transition: {current.value} -> {target.value}")
    return target


def idempotency_key(intent: TradeIntent) -> str:
    payload = {
        "intent_id": intent.intent_id,
        "telegram_user_id": intent.telegram_user_id,
        "mode": intent.mode.value,
        "mint": intent.mint,
        "side": intent.side.value,
        "amount_raw": str(intent.amount_raw),
        "created_at": intent.created_at.isoformat(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LiveReadiness:
    operator_live_switch: bool
    user_live_consent: bool
    wallet_verified: bool
    risk_approved: bool
    fresh_quote: bool
    slippage_approved: bool
    owner_approval_confirmed: bool
    kill_switch_clear: bool

    def blockers(self) -> tuple[str, ...]:
        labels = (
            ("operator_live_switch", "operator live switch is off"),
            ("user_live_consent", "user has not enabled live trading"),
            ("wallet_verified", "wallet ownership is not verified"),
            ("risk_approved", "risk checks have not passed"),
            ("fresh_quote", "quote is missing or stale"),
            ("slippage_approved", "slippage or price-impact checks have not passed"),
            ("owner_approval_confirmed", "explicit owner approval is missing"),
            ("kill_switch_clear", "emergency kill switch is active"),
        )
        return tuple(message for field, message in labels if not getattr(self, field))

    def assert_ready(self) -> None:
        blockers = self.blockers()
        if blockers:
            raise PermissionError("Live execution blocked: " + "; ".join(blockers))


class PaperQuoteAdapter:
    """Quote-only adapter. It exposes no signing, submitting or withdrawal method."""

    def __init__(self, quote: Callable[..., Awaitable[dict[str, Any]]]):
        if not callable(quote):
            raise TypeError("PaperQuoteAdapter requires an asynchronous quote function")
        self._quote = quote

    async def quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int | None = None,
    ) -> dict[str, Any]:
        if amount_raw <= 0:
            raise ValueError("amount_raw must be positive")
        result = (
            await self._quote(input_mint, output_mint, amount_raw)
            if slippage_bps is None
            else await self._quote(input_mint, output_mint, amount_raw, slippage_bps)
        )
        if not isinstance(result, dict):
            raise TypeError("quote provider returned a non-object response")
        return result


def assert_mode_isolated(intent: TradeIntent, *, adapter_mode: TradingMode) -> None:
    if intent.mode is not adapter_mode:
        raise PermissionError(
            f"Execution mode mismatch: intent={intent.mode.value}, adapter={adapter_mode.value}"
        )
