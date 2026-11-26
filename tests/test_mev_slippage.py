from unittest.mock import AsyncMock

import pytest

from src.n9n8.mev_executor import JupiterJitoExecutor


@pytest.mark.asyncio
async def test_jupiter_v2_quote_uses_user_slippage_override_when_selected():
    executor = JupiterJitoExecutor.__new__(JupiterJitoExecutor)
    executor._jupiter_get = AsyncMock(return_value={"outAmount": "100", "otherAmountThreshold": "98"})

    result = await executor.quote("SOL", "TOKEN", 1_000_000, 1_500)

    assert result["otherAmountThreshold"] == "98"
    executor._jupiter_get.assert_awaited_once_with("/order", {
        "inputMint": "SOL",
        "outputMint": "TOKEN",
        "amount": "1000000",
        "slippageBps": "1500",
    })


@pytest.mark.asyncio
async def test_jupiter_v2_quote_keeps_automatic_slippage_when_no_override_given():
    executor = JupiterJitoExecutor.__new__(JupiterJitoExecutor)
    executor._jupiter_get = AsyncMock(return_value={"outAmount": "100", "otherAmountThreshold": "99"})

    await executor.quote("SOL", "TOKEN", 2_000_000)

    executor._jupiter_get.assert_awaited_once_with("/order", {
        "inputMint": "SOL",
        "outputMint": "TOKEN",
        "amount": "2000000",
    })


@pytest.mark.asyncio
async def test_jupiter_v2_quote_rejects_slippage_outside_supported_limits():
    executor = JupiterJitoExecutor.__new__(JupiterJitoExecutor)
    executor._jupiter_get = AsyncMock()

    with pytest.raises(ValueError, match="1 and 5000"):
        await executor.quote("SOL", "TOKEN", 1_000_000, 5_001)

    executor._jupiter_get.assert_not_awaited()
