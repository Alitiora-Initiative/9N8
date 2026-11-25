export type PaperCandle = {
  openTime: number;
  closeTime: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  quoteVolume?: number;
  trades?: number;
};

export type PaperPosition = {
  stopLossPrice: number;
  takeProfitPrice: number;
};

export type PaperDecision = {
  action: "buy" | "sell" | "hold";
  reason: string;
  emaFast: number | null;
  emaSlow: number | null;
  rsi: number | null;
};

export function emaSeries(values: number[], period: number): Array<number | null> {
  if (!Number.isInteger(period) || period < 2) throw new Error("period_must_be_at_least_2");
  const result: Array<number | null> = Array(values.length).fill(null);
  if (values.length < period) return result;
  const seed = values.slice(0, period).reduce((sum, value) => sum + value, 0) / period;
  let ema = seed;
  result[period - 1] = ema;
  const multiplier = 2 / (period + 1);
  for (let i = period; i < values.length; i++) {
    ema = (values[i] - ema) * multiplier + ema;
    result[i] = ema;
  }
  return result;
}

export function rsiWilder(values: number[], period = 14): number | null {
  if (!Number.isInteger(period) || period < 2) throw new Error("period_must_be_at_least_2");
  if (values.length <= period) return null;
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= period; i++) {
    const delta = values[i] - values[i - 1];
    gain += Math.max(delta, 0);
    loss += Math.max(-delta, 0);
  }
  let averageGain = gain / period;
  let averageLoss = loss / period;
  for (let i = period + 1; i < values.length; i++) {
    const delta = values[i] - values[i - 1];
    averageGain = (averageGain * (period - 1) + Math.max(delta, 0)) / period;
    averageLoss = (averageLoss * (period - 1) + Math.max(-delta, 0)) / period;
  }
  if (averageLoss === 0) return averageGain === 0 ? 50 : 100;
  const relativeStrength = averageGain / averageLoss;
  return 100 - 100 / (1 + relativeStrength);
}

export function decidePaperAction(
  candles: PaperCandle[],
  position: PaperPosition | null,
  previousCandleOpenTime: number | null,
): PaperDecision {
  const closes = candles.map((candle) => candle.close);
  const fast = emaSeries(closes, 9);
  const slow = emaSeries(closes, 21);
  const index = closes.length - 1;
  const emaFast = fast[index] ?? null;
  const emaSlow = slow[index] ?? null;
  const rsi = rsiWilder(closes, 14);
  const hold = (reason: string): PaperDecision => ({ action: "hold", reason, emaFast, emaSlow, rsi });

  if (candles.length < 23 || emaFast === null || emaSlow === null) {
    return hold("Waiting for at least 23 closed one-minute candles");
  }
  const latest = candles[index];
  if (previousCandleOpenTime !== null && latest.openTime <= previousCandleOpenTime) {
    return hold("This candle was already evaluated");
  }
  if (!(latest.close > 0) || !Number.isFinite(latest.close)) return hold("Invalid market price");

  if (position) {
    if (position.stopLossPrice > 0 && latest.close <= position.stopLossPrice) {
      return { action: "sell", reason: "Stop-loss threshold reached", emaFast, emaSlow, rsi };
    }
    if (position.takeProfitPrice > 0 && latest.close >= position.takeProfitPrice) {
      return { action: "sell", reason: "Take-profit threshold reached", emaFast, emaSlow, rsi };
    }
    if ((fast[index - 1] ?? 0) >= (slow[index - 1] ?? 0) && emaFast < emaSlow) {
      return { action: "sell", reason: "EMA 9 crossed below EMA 21", emaFast, emaSlow, rsi };
    }
    return hold("Position held: exit conditions not met");
  }

  const crossedUp = (fast[index - 1] ?? 0) <= (slow[index - 1] ?? 0) && emaFast > emaSlow;
  if (!crossedUp) return hold("No confirmed bullish EMA crossover");
  if (rsi === null || rsi < 48) return hold("Bullish crossover rejected: RSI momentum is too weak");
  if (rsi > 72) return hold("Bullish crossover rejected: RSI is overbought");
  if (latest.close < emaSlow) return hold("Bullish crossover rejected: price is below EMA 21");
  if (!(latest.volume > 0)) return hold("Bullish crossover rejected: no candle volume");
  return { action: "buy", reason: "EMA 9 crossed above EMA 21 with RSI confirmation", emaFast, emaSlow, rsi };
}

export function simulatedFillPrice(referencePrice: number, side: "buy" | "sell", slippageBps: number): number {
  if (!Number.isFinite(referencePrice) || referencePrice <= 0) throw new Error("invalid_reference_price");
  if (!Number.isFinite(slippageBps) || slippageBps < 0 || slippageBps > 500) {
    throw new Error("invalid_slippage_bps");
  }
  const factor = side === "buy" ? 1 + slippageBps / 10_000 : 1 - slippageBps / 10_000;
  return referencePrice * factor;
}
