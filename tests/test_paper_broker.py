import pytest

from connectors.paper_broker import PaperBroker
from models import MarketSnapshot, MarketType, Side, TradeIntent


def snapshot():
    return MarketSnapshot("paper","BTC/USDT",MarketType.CRYPTO_SPOT,99.9,100.0,99.95)


def intent(side: Side):
    if side is Side.BUY:
        return TradeIntent("paper","BTC/USDT",MarketType.CRYPTO_SPOT,side,100,98,106,2,"test",{})
    return TradeIntent("paper","BTC/USDT",MarketType.CRYPTO_SPOT,side,100,102,94,2,"test",{})


@pytest.mark.asyncio
async def test_long_entry_and_exit_use_fees_and_slippage():
    broker=PaperBroker(1000,fee_rate=0.001,slippage_bps=5)
    buy=await broker.execute(intent(Side.BUY),1,snapshot(),"b1")
    assert buy.average_price > snapshot().ask
    assert buy.fee_usd > 0
    account=await broker.account({"paper:BTC/USDT":snapshot()})
    assert account.equity_usd < 1000
    sell=await broker.execute(intent(Side.SELL),1,snapshot(),"s1")
    assert sell.average_price < snapshot().bid
    assert broker.positions == {}


@pytest.mark.asyncio
async def test_short_entry_and_cover_preserve_equity_accounting():
    broker=PaperBroker(1000,fee_rate=0.001,slippage_bps=5)
    await broker.execute(intent(Side.SELL),1,snapshot(),"short1")
    account=await broker.account({"paper:BTC/USDT":snapshot()})
    assert account.equity_usd < 1000
    cover=await broker.execute(intent(Side.BUY),1,snapshot(),"cover1")
    assert cover.fee_usd > 0
    assert broker.positions == {}


@pytest.mark.asyncio
async def test_paper_broker_refuses_oversized_close():
    broker=PaperBroker(1000)
    await broker.execute(intent(Side.BUY),1,snapshot(),"b1")
    with pytest.raises(ValueError):
        await broker.execute(intent(Side.SELL),2,snapshot(),"s2")


@pytest.mark.asyncio
async def test_partial_close_allocates_entry_fee_proportionally():
    broker = PaperBroker(1000, fee_rate=0.001, slippage_bps=0)
    entry = await broker.execute(intent(Side.BUY), 1, snapshot(), "entry-fee")
    assert entry.raw["entry_fee"] == pytest.approx(0.1)

    up = MarketSnapshot("paper", "BTC/USDT", MarketType.CRYPTO_SPOT, 110, 110.1, 110.05)
    partial = await broker.execute(intent(Side.SELL), 0.5, up, "partial-fee")
    assert partial.raw["entry_fee_allocated"] == pytest.approx(0.05)
    assert partial.raw["remaining_entry_fee"] == pytest.approx(0.05)
    assert partial.raw["exit_fee"] == pytest.approx(0.055)
    assert partial.raw["net_pnl"] == pytest.approx(4.895)
    assert broker.positions["paper:BTC/USDT"].fees_paid == pytest.approx(0.05)

    down = MarketSnapshot("paper", "BTC/USDT", MarketType.CRYPTO_SPOT, 105, 105.1, 105.05)
    final = await broker.execute(intent(Side.SELL), 0.5, down, "final-fee")
    assert final.raw["entry_fee_allocated"] == pytest.approx(0.05)
    assert final.raw["remaining_entry_fee"] == 0
    assert final.raw["net_pnl"] == pytest.approx(2.3975)
    assert broker.positions == {}


@pytest.mark.asyncio
async def test_paper_broker_rejects_non_finite_and_invalid_quotes():
    broker = PaperBroker(1000)
    bad = MarketSnapshot("paper", "BTC/USDT", MarketType.CRYPTO_SPOT, 100, 99, 100)
    with pytest.raises(ValueError, match="uncrossed"):
        await broker.execute(intent(Side.BUY), 1, bad, "bad-quote")
    with pytest.raises(ValueError, match="finite"):
        await broker.execute(intent(Side.BUY), float("nan"), snapshot(), "nan-quantity")


def test_paper_configuration_rejects_unbounded_fees_and_slippage():
    with pytest.raises(ValueError):
        PaperBroker(1000, fee_rate=0.1)
    with pytest.raises(ValueError):
        PaperBroker(1000, slippage_bps=1000)


@pytest.mark.asyncio
async def test_stock_paper_fill_requires_sufficient_displayed_top_of_book_size():
    broker = PaperBroker(1000, fee_rate=0.001, slippage_bps=5)
    stock = MarketSnapshot(
        "alpaca-stocks", "AAPL", MarketType.STOCK,
        200.10, 200.12, 200.11,
        bid_size=5, ask_size=3,
    )
    stock_buy = TradeIntent(
        "alpaca-stocks", "AAPL", MarketType.STOCK, Side.BUY,
        200.11, 195, 215, 5, "stock paper test", {},
    )

    with pytest.raises(ValueError, match="displayed top-of-book size"):
        await broker.execute(stock_buy, 4, stock, "too-large-stock-buy")

    fill = await broker.execute(stock_buy, 2, stock, "valid-stock-buy")
    assert fill.quantity == 2
    assert fill.average_price > stock.ask


@pytest.mark.asyncio
async def test_stock_paper_exit_requires_visible_bid_size():
    broker = PaperBroker(1000, fee_rate=0.001, slippage_bps=0)
    stock_entry_quote = MarketSnapshot(
        "alpaca-stocks", "AAPL", MarketType.STOCK,
        200.10, 200.12, 200.11,
        bid_size=10, ask_size=10,
    )
    stock_buy = TradeIntent(
        "alpaca-stocks", "AAPL", MarketType.STOCK, Side.BUY,
        200.11, 195, 215, 5, "stock paper test", {},
    )
    await broker.execute(stock_buy, 5, stock_entry_quote, "stock-entry")

    thin_bid = MarketSnapshot(
        "alpaca-stocks", "AAPL", MarketType.STOCK,
        200.10, 200.12, 200.11,
        bid_size=2, ask_size=10,
    )
    stock_sell = TradeIntent(
        "alpaca-stocks", "AAPL", MarketType.STOCK, Side.SELL,
        200.11, 205, 190, 5, "stock paper close", {},
    )
    with pytest.raises(ValueError, match="displayed top-of-book size"):
        await broker.execute(stock_sell, 5, thin_bid, "stock-exit-too-large")
    assert "alpaca-stocks:AAPL" in broker.positions
