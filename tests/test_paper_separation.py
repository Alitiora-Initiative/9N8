import pytest

from src.n9n8.paper_engine import PaperQuoteOnlyClient


@pytest.mark.asyncio
async def test_paper_quote_adapter_exposes_quotes_but_no_sign_or_submit_methods():
    calls = []

    async def quote(input_mint, output_mint, amount_raw):
        calls.append((input_mint, output_mint, amount_raw))
        return {
            "outAmount": "12345",
            "otherAmountThreshold": "12000",
            "priceImpactPct": "0.1",
        }

    client = PaperQuoteOnlyClient(quote)

    result = await client.quote("SOL", "TOKEN", 50_000_000)

    assert result["outAmount"] == "12345"
    assert calls == [("SOL", "TOKEN", 50_000_000)]
    assert callable(client.quote)
    assert not hasattr(client, "build_swap")
    assert not hasattr(client, "send_transaction")
    assert not hasattr(client, "sign_transaction")


@pytest.mark.asyncio
async def test_paper_quote_adapter_rejects_non_mapping_quote_payload():
    async def invalid_quote(*_args):
        return ["not", "a", "quote"]

    client = PaperQuoteOnlyClient(invalid_quote)

    with pytest.raises(RuntimeError, match="invalid paper quote"):
        await client.quote("SOL", "TOKEN", 10)
