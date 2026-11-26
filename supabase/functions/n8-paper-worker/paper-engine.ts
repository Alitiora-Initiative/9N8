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

export type BookLevel = { price: number; quantity: number };
export type PaperOrderBook = { bids: BookLevel[]; asks: BookLevel[]; updateId?: number };
export type PaperBookFill = {
  fillPrice: number; baseQuantity: number; quoteNotional: number;
  referencePrice: number; spreadBps: number; slippageBps: number; levelsUsed: number;
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
    // If both protective levels are touched within one candle, assume the adverse
    // stop was reached first. This is the conservative candle-only interpretation.
    if (position.stopLossPrice > 0 && latest.low <= position.stopLossPrice) {
      return { action: "sell", reason: "Stop-loss threshold touched intrabar", emaFast, emaSlow, rsi };
    }
    if (position.takeProfitPrice > 0 && latest.high >= position.takeProfitPrice) {
      return { action: "sell", reason: "Take-profit threshold touched intrabar", emaFast, emaSlow, rsi };
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


/** Use real Binance depth levels to estimate quote-notional buys and base-quantity sells. */
export function estimateOrderBookFill(
  side: "buy" | "sell",
  book: PaperOrderBook,
  size: { quoteNotional?: number; baseQuantity?: number },
): PaperBookFill {
  const clean = (levels: BookLevel[]) => levels
    .filter((level) => Number.isFinite(level.price) && level.price > 0 &&
      Number.isFinite(level.quantity) && level.quantity > 0)
    .map((level) => ({ price: level.price, quantity: level.quantity }));
  const bids = clean(book.bids).sort((a, b) => b.price - a.price);
  const asks = clean(book.asks).sort((a, b) => a.price - b.price);
  if (!bids.length || !asks.length) throw new Error("paper_order_book_empty");
  const bestBid=bids[0].price, bestAsk=asks[0].price;
  if(bestAsk<bestBid)throw new Error("paper_order_book_crossed");
  const referencePrice=(bestBid+bestAsk)/2;
  if(!(referencePrice>0))throw new Error("paper_order_book_reference_invalid");
  const levels=side==="buy"?asks:bids;
  let remaining=side==="buy"?Number(size.quoteNotional):Number(size.baseQuantity);
  if(!Number.isFinite(remaining)||remaining<=0)throw new Error(side==="buy"?"paper_quote_notional_invalid":"paper_base_quantity_invalid");
  const original=remaining;
  let baseQuantity=0,quoteNotional=0,levelsUsed=0;
  for(const level of levels){
    if(remaining<=1e-10)break;
    if(side==="buy"){
      const quoteTaken=Math.min(remaining,level.price*level.quantity);
      baseQuantity+=quoteTaken/level.price;
      quoteNotional+=quoteTaken;
      remaining-=quoteTaken;
    }else{
      const baseTaken=Math.min(remaining,level.quantity);
      baseQuantity+=baseTaken;
      quoteNotional+=baseTaken*level.price;
      remaining-=baseTaken;
    }
    levelsUsed++;
  }
  if(remaining>Math.max(1e-8,original*1e-8))throw new Error("paper_order_book_depth_insufficient");
  if(!(baseQuantity>0)||!(quoteNotional>0))throw new Error("paper_order_book_fill_invalid");
  const fillPrice=quoteNotional/baseQuantity;
  const spreadBps=((bestAsk-bestBid)/referencePrice)*10_000;
  const adverseMove=side==="buy"?(fillPrice-referencePrice)/referencePrice:(referencePrice-fillPrice)/referencePrice;
  return {fillPrice,baseQuantity,quoteNotional,referencePrice,spreadBps,slippageBps:Math.max(0,adverseMove*10_000),levelsUsed};
}
