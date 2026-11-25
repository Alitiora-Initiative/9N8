import unittest

from signal_engine import Candle, analyse, ema, rsi


class IndicatorTests(unittest.TestCase):
    def test_ema_returns_none_until_period(self):
        self.assertEqual(ema([1, 2, 3, 4], 3), [None, None, 2.0, 3.0])

    def test_rsi_up_only_is_100(self):
        self.assertEqual(rsi([float(i) for i in range(20)]), 100.0)

    def test_analyse_returns_neutral_on_flat_series(self):
        candles = [
            Candle(f"2026-01-{(i // 24) + 1:02d} {i % 24:02d}:00:00", 100, 101, 99, 100, 10)
            for i in range(100)
        ]
        result = analyse(candles, "TEST", "15min")
        self.assertEqual(result["direction"], "NONE")
        self.assertEqual(result["price"], 100)


if __name__ == "__main__":
    unittest.main()
