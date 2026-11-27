"""Compare native TA-Lib and pandas-ta-classic outputs on a supplied OHLCV CSV.

The script does not fetch prices or produce live trade orders. Rows should already
be normalized, UTC, chronological and closed.
"""
import argparse
import pandas as pd
import talib
import pandas_ta_classic as ta


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("csv_path")
    args=parser.parse_args()
    frame=pd.read_csv(args.csv_path)
    required={"timestamp","open","high","low","close","volume"}
    if not required.issubset(frame.columns):
        raise SystemExit("CSV must contain timestamp, open, high, low, close, volume")
    if frame["timestamp"].duplicated().any() or not frame["timestamp"].is_monotonic_increasing:
        raise SystemExit("CSV timestamps must be unique and chronological")
    close=frame["close"].astype("float64")
    talib_rsi=talib.RSI(close.to_numpy(),timeperiod=14)
    classic_rsi=ta.rsi(close,length=14).to_numpy()
    valid=~pd.isna(classic_rsi)
    if valid.any():
        difference=abs(talib_rsi[valid]-classic_rsi[valid])
        print({"rows":len(frame),"rsi_overlap":int(valid.sum()),"maximum_abs_difference":float(difference.max())})
    else:
        raise SystemExit("Not enough closed candles to compare RSI")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
