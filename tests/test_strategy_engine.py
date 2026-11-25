import time
import unittest

from src.market_data import Candle
from src.strategy_engine import evaluate_strategy, screen_market


class StrategyEngineTests(unittest.TestCase):
    def ticker(self, *, bid="100", ask="100.1", volume="1000000", age=0):
        return {
            "bidPx": bid,
            "askPx": ask,
            "volCcy24h": volume,
            "ts": str(int(time.time() * 1000) - age * 1000),
        }

    def test_market_screen_accepts_fresh_liquid_tight_market(self):
        result = screen_market(self.ticker())
        self.assertTrue(result.accepted)
        self.assertGreater(result.quote_volume_24h, 0)

    def test_market_screen_rejects_wide_spread_stale_quote_and_low_volume(self):
        self.assertFalse(screen_market(self.ticker(bid="90", ask="100")).accepted)
        self.assertFalse(screen_market(self.ticker(age=60)).accepted)
        self.assertFalse(screen_market(self.ticker(volume="10")).accepted)

    def test_momentum_requires_breakout_rsi_and_three_times_volume(self):
        candles = [
            Candle(str(i), 100, 101, 99, 100, 20)
            for i in range(54)
        ]
        candles.append(Candle("last", 100, 103, 99, 102, 100))
        signal = evaluate_strategy("momentum", candles)
        self.assertTrue(signal.eligible)
        self.assertGreater(signal.indicators["volume_multiple"], 3.0)

    def test_unknown_strategy_fails_closed(self):
        candles = [Candle(str(i), 100, 101, 99, 100, 20) for i in range(60)]
        signal = evaluate_strategy("arbitrage", candles)
        self.assertFalse(signal.eligible)
        self.assertEqual(signal.reason, "Unsupported strategy.")

    def test_insufficient_history_fails_closed(self):
        candles = [Candle(str(i), 100, 101, 99, 100, 20) for i in range(30)]
        signal = evaluate_strategy("trend", candles)
        self.assertFalse(signal.eligible)


if __name__ == "__main__":
    unittest.main()
