"""Backtrader event-driven paper backtest; no live broker connection is configured."""
import argparse
import pandas as pd
import backtrader as bt


class TrendPaperStrategy(bt.Strategy):
    params = (("fast",20),("slow",50))
    def __init__(self):
        fast = bt.indicators.SimpleMovingAverage(self.data.close,period=self.p.fast)
        slow = bt.indicators.SimpleMovingAverage(self.data.close,period=self.p.slow)
        self.cross = bt.indicators.CrossOver(fast,slow)
    def next(self):
        if not self.position and self.cross > 0:
            self.buy()
        elif self.position and self.cross < 0:
            self.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("csv_path")
    args=parser.parse_args()
    frame=pd.read_csv(args.csv_path,parse_dates=["timestamp"])
    required={"timestamp","open","high","low","close","volume"}
    if not required.issubset(frame.columns):
        raise SystemExit("CSV must contain timestamp, open, high, low, close and volume")
    if frame["timestamp"].duplicated().any() or not frame["timestamp"].is_monotonic_increasing:
        raise SystemExit("CSV candles must be unique and chronological")
    frame=frame.set_index("timestamp")
    cerebro=bt.Cerebro(stdstats=False)
    cerebro.adddata(bt.feeds.PandasData(dataname=frame))
    cerebro.addstrategy(TrendPaperStrategy)
    cerebro.broker.setcash(1000.0)
    cerebro.broker.setcommission(commission=0.001)
    print("paper backtest starting value:",cerebro.broker.getvalue())
    cerebro.run()
    print("paper backtest final value:",cerebro.broker.getvalue())


if __name__=="__main__":
    main()
