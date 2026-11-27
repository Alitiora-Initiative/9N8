from datetime import datetime, timezone

import pytest

from src.n9n8.v2.contracts import TradeIntent, TradeSide, TradingMode
from src.n9n8.v2.execution import (
    ExecutionState, LiveReadiness, PaperQuoteAdapter, assert_mode_isolated,
    idempotency_key, transition,
)


def make_intent(mode=TradingMode.LIVE):
    return TradeIntent(
        intent_id="intent-001",
        telegram_user_id=12345,
        mode=mode,
        mint="So11111111111111111111111111111111111111112",
        side=TradeSide.BUY,
        amount_raw=1000000,
        created_at=datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc),
    )


def test_live_gate_requires_each_independent_approval():
    LiveReadiness(True, True, True, True, True, True, True, True).assert_ready()
    blocked = LiveReadiness(True, False, True, True, True, True, True, True)
    with pytest.raises(PermissionError, match="user has not enabled"):
        blocked.assert_ready()
    with pytest.raises(PermissionError, match="emergency kill switch"):
        LiveReadiness(True, True, True, True, True, True, True, False).assert_ready()


def test_unknown_submission_can_only_be_reconciled_not_retried():
    assert transition(ExecutionState.CREATED, ExecutionState.PREFLIGHT) is ExecutionState.PREFLIGHT
    assert transition(ExecutionState.SUBMITTED, ExecutionState.UNKNOWN) is ExecutionState.UNKNOWN
    assert transition(ExecutionState.UNKNOWN, ExecutionState.CONFIRMED) is ExecutionState.CONFIRMED
    with pytest.raises(ValueError, match="Invalid execution transition"):
        transition(ExecutionState.UNKNOWN, ExecutionState.SUBMITTED)


def test_idempotency_key_is_stable_and_mode_scoped():
    assert idempotency_key(make_intent()) == idempotency_key(make_intent())
    assert idempotency_key(make_intent()) != idempotency_key(make_intent(TradingMode.PAPER))


@pytest.mark.asyncio
async def test_paper_adapter_has_quote_only_surface():
    async def quote(input_mint, output_mint, amount_raw, slippage_bps):
        return {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount_raw)}

    adapter = PaperQuoteAdapter(quote)
    result = await adapter.quote("SOL", "USDC", 100, 50)
    assert result["amount"] == "100"
    assert not hasattr(adapter, "sign")
    assert not hasattr(adapter, "execute")
    assert not hasattr(adapter, "withdraw")


def test_mode_mismatch_is_rejected():
    with pytest.raises(PermissionError, match="mode mismatch"):
        assert_mode_isolated(make_intent(), adapter_mode=TradingMode.PAPER)
