from datetime import datetime, timezone

import pytest

from models import AccountState, MarketSnapshot, MarketType, Side, TradeIntent
from risk_manager.risk_manager import RiskManager


class FakeStore:
    def __init__(self):
        self.values={}
        self.daily={}
        self.events=[]

    async def get_control(self,key,default=""):
        return self.values.get(key,default)

    async def set_control(self,key,value):
        self.values[key]=value

    async def set_daily_equity(self,day,equity):
        if day not in self.daily:
            self.daily[day]=equity
        return self.daily[day]

    async def event(self,*args,**kwargs):
        self.events.append(args)


@pytest.fixture
def risk_config():
    return {
        "risk":{
            "risk_per_trade_pct":0.01,
            "maximum_daily_drawdown_pct":0.05,
            "maximum_concurrent_positions":3,
            "maximum_portfolio_exposure_pct":0.25,
            "maximum_perpetual_leverage":3,
            "maximum_cex_slippage_pct":0.005,
            "maximum_dex_slippage_pct":0.05,
            "maximum_forex_spread_pips":2.0,
            "max_position_notional_usd":100,
        }
    }


@pytest.mark.asyncio
async def test_position_size_obeys_notional_and_risk_caps(risk_config):
    store=FakeStore()
    risk=RiskManager(store,risk_config)
    snapshot=MarketSnapshot("test","BTC/USDT",MarketType.CRYPTO_SPOT,99.9,100.1,100)
    intent=TradeIntent("test","BTC/USDT",MarketType.CRYPTO_SPOT,Side.BUY,
                       100,98,106,2,"test",{})
    result=await risk.evaluate(intent,AccountState("test",10000,10000,10000),
                               snapshot,0,0.001)
    assert result.approved and result.size
    assert result.size.notional_usd <= 100
    assert result.size.risk_usd <= 100


@pytest.mark.asyncio
async def test_spread_and_leverage_are_hard_vetoes(risk_config):
    risk=RiskManager(FakeStore(),risk_config)
    snapshot=MarketSnapshot("test","BTC/USDT",MarketType.CRYPTO_PERP,99,101,100)
    intent=TradeIntent("test","BTC/USDT",MarketType.CRYPTO_PERP,Side.BUY,
                       100,98,106,2,"test",{"isolated_margin_confirmed":True})
    account=AccountState("test",10000,10000,10000)
    decision=await risk.evaluate(intent,account,snapshot,0,0.02,leverage=1)
    assert not decision.approved
    decision=await risk.evaluate(intent,account,snapshot,0,0.001,leverage=4)
    assert not decision.approved


@pytest.mark.asyncio
async def test_concurrent_position_cap_is_enforced(risk_config):
    risk=RiskManager(FakeStore(),risk_config)
    snapshot=MarketSnapshot("test","BTC/USDT",MarketType.CRYPTO_SPOT,99.9,100.1,100)
    intent=TradeIntent("test","BTC/USDT",MarketType.CRYPTO_SPOT,Side.BUY,
                       100,98,106,2,"test",{})
    decision=await risk.evaluate(
        intent,AccountState("test",10000,10000,10000),snapshot,
        current_exposure_usd=0,estimated_slippage_pct=0.001,current_open_positions=3,
    )
    assert not decision.approved


@pytest.mark.asyncio
async def test_daily_drawdown_trips_24_hour_kill_switch(risk_config):
    store=FakeStore()
    risk=RiskManager(store,risk_config)
    flattened=[]
    async def flatten():
        flattened.append(True)
    risk.register_flatten_callback(flatten)
    now=datetime.now(timezone.utc)
    first=AccountState("portfolio",10000,10000,10000,now)
    second=AccountState("portfolio",9499,9499,9499,now)
    assert not await risk.observe_equity(first)
    assert await risk.observe_equity(second)
    assert store.values["kill_switch"]=="true"
    assert store.values["paused"]=="true"
    assert store.values["halt_until"]
    assert flattened == [True]
