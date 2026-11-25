import unittest

from src.market_data import Candle
from src.strategy import ema, rsi, analyse


class StrategyTests(unittest.TestCase):
    def test_ema_rejects_too_few_values(self):
        with self.assertRaises(ValueError):
            ema([1.0, 2.0], 3)

    def test_rsi_is_bounded(self):
        increasing = [float(i) for i in range(20)]
        self.assertGreaterEqual(rsi(increasing), 0)
        self.assertLessEqual(rsi(increasing), 100)

    def test_analyse_returns_observation(self):
        values = [100 + i * 0.2 for i in range(50)]
        candles = [Candle(str(i), v, v + 0.2, v - 0.2, v) for i, v in enumerate(values)]
        result = analyse("TEST", candles)
        self.assertEqual(result.symbol, "TEST")
        self.assertIn(result.label, {"BULLISH SETUP", "BEARISH SETUP", "NO CLEAR SETUP"})


if __name__ == "__main__":
    unittest.main()
