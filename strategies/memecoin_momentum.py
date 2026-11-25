from __future__ import annotations

from models import Candle, MarketType, MarketSnapshot, Side, TradeIntent
from strategies.indicators import atr, rsi


class MemecoinMomentum:
    def __init__(self, config: dict):
        self.cfg = config

    def evaluate(self, venue: str, symbol: str, snapshot: MarketSnapshot,
                 candles: list[Candle]) -> TradeIntent | None:
        if snapshot.market_type not in {MarketType.SOLANA_DEX, MarketType.EVM_DEX} or len(candles) < 20:
            return None
        previous = candles[-13:-1]
        baseline = sum(c.volume for c in previous) / len(previous)
        latest = candles[-1]
        if baseline <= 0 or latest.volume < baseline * float(self.cfg["memecoin_volume_spike_multiple"]):
            return None
        buy, sell = snapshot.buy_volume_5m, snapshot.sell_volume_5m
        if buy is None or sell is None or buy + sell <= 0:
            return None
        ratio = buy / max(sell, 1e-12)
        if ratio < float(self.cfg["memecoin_buy_sell_ratio_min"]):
            return None
        volatility = atr(candles, int(self.cfg["atr_period"]))
        momentum = rsi([c.close for c in candles], int(self.cfg["rsi_period"]))
        if volatility is None or momentum is None or momentum >= 80:
            return None
        stop_distance = volatility * float(self.cfg["atr_stop_multiple"])
        price = snapshot.ask
        return TradeIntent(
            venue,symbol,MarketType(snapshot.market_type),Side.BUY,price,
            price-stop_distance,price+stop_distance*float(self.cfg["take_profit_r_multiple"]),
            stop_distance,"5m volume spike and buy-flow confirmation",
            {"volume_multiple":latest.volume/baseline,"buy_sell_ratio":ratio,"rsi":momentum,
             "strategy":"memecoin_momentum_v1"},
        )
