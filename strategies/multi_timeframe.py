from __future__ import annotations

from models import Candle, MarketType, Side, TradeIntent
from strategies.indicators import adx, atr, ema, macd, rsi


class MultiTimeframeStrategy:
    def __init__(self, config: dict):
        self.cfg = config

    def evaluate(self, venue: str, symbol: str, market_type: MarketType,
                 trend: list[Candle], entry: list[Candle]) -> TradeIntent | None:
        sc = self.cfg
        if len(trend) < max(int(sc["ema_slow"]) + 2, 205) or len(entry) < 60:
            return None
        trend_closes = [c.close for c in trend]
        entry_closes = [c.close for c in entry]
        ema50 = ema(trend_closes, int(sc["ema_fast"]))
        ema200 = ema(trend_closes, int(sc["ema_slow"]))
        a = adx(trend, int(sc["adx_period"]))
        volatility = atr(entry, int(sc["atr_period"]))
        strength = rsi(entry_closes, int(sc["rsi_period"]))
        m_line, m_prev_delta = macd(entry_closes)
        if None in (ema50[-1], ema200[-1], a, volatility, strength, m_line, m_prev_delta):
            return None
        if a < float(sc["minimum_adx"]) or volatility <= 0:
            return None
        fast = ema(entry_closes, int(sc["entry_ema_fast"]))
        slow = ema(entry_closes, int(sc["entry_ema_slow"]))
        if fast[-1] is None or fast[-2] is None or slow[-1] is None or slow[-2] is None:
            return None
        price = entry[-1].close
        long_regime = ema50[-1] > ema200[-1]
        short_regime = ema50[-1] < ema200[-1]
        crossed_up = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
        crossed_down = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
        distance = volatility * float(sc["atr_stop_multiple"])
        r_multiple = float(sc["take_profit_r_multiple"])

        if long_regime and crossed_up and float(sc["rsi_long_min"]) <= strength <= float(sc["rsi_long_max"]) and m_line > 0:
            return TradeIntent(
                venue,symbol,market_type,Side.BUY,price,price-distance,price+distance*r_multiple,
                distance,"H1 EMA trend aligned; M5 EMA crossover, RSI and MACD confirmed",
                {"adx":a,"atr":volatility,"rsi":strength,"strategy":"multi_timeframe_v1"},
            )
        if short_regime and crossed_down and float(sc["rsi_short_min"]) <= strength <= float(sc["rsi_short_max"]) and m_line < 0:
            if market_type in {MarketType.CRYPTO_SPOT, MarketType.SOLANA_DEX, MarketType.EVM_DEX}:
                return None
            return TradeIntent(
                venue,symbol,market_type,Side.SELL,price,price+distance,price-distance*r_multiple,
                distance,"H1 downtrend aligned; M5 EMA cross down, RSI and MACD confirmed",
                {"adx":a,"atr":volatility,"rsi":strength,"strategy":"multi_timeframe_v1"},
            )
        return None
