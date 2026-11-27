"""Optional TensorFlow LSTM research experiment; not a production price oracle."""
import argparse
import numpy as np
import pandas as pd
import tensorflow as tf


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--window",type=int,default=30)
    parser.add_argument("--epochs",type=int,default=5)
    args=parser.parse_args()
    if args.window<5 or args.epochs<1:
        parser.error("window and epochs must be positive")
    frame=pd.read_csv(args.csv_path)
    close=frame["close"].astype("float32").to_numpy()
    if len(close)<args.window+20 or not np.isfinite(close).all() or (close<=0).any():
        raise SystemExit("input needs more valid, positive closed candle prices")
    returns=np.diff(np.log(close)).astype("float32")
    sequences=[];targets=[]
    for i in range(args.window,len(returns)):
        sequences.append(returns[i-args.window:i]);targets.append(returns[i])
    x=np.asarray(sequences,dtype="float32")[...,None]
    y=np.asarray(targets,dtype="float32")
    split=int(len(x)*0.8)
    if split<10 or len(x)-split<5:
        raise SystemExit("not enough samples for chronological holdout")
    model=tf.keras.Sequential([
        tf.keras.layers.Input(shape=(args.window,1)),
        tf.keras.layers.LSTM(24),
        tf.keras.layers.Dense(1),
    ])
    model.compile(optimizer="adam",loss="mse")
    model.fit(x[:split],y[:split],epochs=args.epochs,verbose=0,shuffle=False)
    score=model.evaluate(x[split:],y[split:],verbose=0)
    print({"chronological_holdout_mse":float(score),"research_only":True})


if __name__=="__main__":
    main()
