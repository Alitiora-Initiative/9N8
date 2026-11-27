"""VectorBT research runner. It consumes a historical CSV and never places orders."""
import argparse
import numpy as np
import pandas as pd
import vectorbt as vbt


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--fast",type=int,default=20)
    parser.add_argument("--slow",type=int,default=50)
    parser.add_argument("--fees",type=float,default=0.001)
    args=parser.parse_args()
    if args.fast<2 or args.slow<=args.fast or not 0<=args.fees<0.1:
        parser.error("invalid periods or fee rate")
    frame=pd.read_csv(args.csv_path,parse_dates=["timestamp"]).sort_values("timestamp")
    if frame["timestamp"].duplicated().any() or len(frame)<args.slow+2:
        raise SystemExit("historical input has duplicate timestamps or insufficient bars")
    close=frame.set_index("timestamp")["close"].astype(float)
    fast=vbt.MA.run(close,window=args.fast).ma
    slow=vbt.MA.run(close,window=args.slow).ma
    entries=(fast>slow)&(fast.shift(1)<=slow.shift(1))
    exits=(fast<slow)&(fast.shift(1)>=slow.shift(1))
    portfolio=vbt.Portfolio.from_signals(close,entries,exits,fees=args.fees,init_cash=1_000.0)
    print(portfolio.stats())
    return 0

if __name__=="__main__":
    raise SystemExit(main())
