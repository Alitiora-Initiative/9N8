import {
  decidePaperAction,
  emaSeries,
  rsiWilder,
  simulatedFillPrice,
  estimateOrderBookFill,
  type PaperCandle,
} from "./paper-engine.ts";

Deno.test("EMA returns null until its seed window is complete", () => {
  const values = emaSeries([1, 2, 3, 4, 5], 3);
  if (values[0] !== null || values[1] !== null) throw new Error("EMA seeded too early");
  if (values[2] !== 2 || values[3] === null) throw new Error("EMA seed is incorrect");
});

Deno.test("RSI remains within the 0-100 range", () => {
  const values = [10, 11, 10.5, 12, 11, 13, 12.5, 14, 13, 15, 14, 16, 15, 17, 16, 18, 17];
  const value = rsiWilder(values, 14);
  if (value === null || value < 0 || value > 100) throw new Error("RSI is outside its valid range");
});

Deno.test("paper strategy does not trade before enough closed candles", () => {
  const result = decidePaperAction([], null, null);
  if (result.action !== "hold") throw new Error("Strategy must wait for market history");
});

Deno.test("paper strategy exits a position when stop loss is reached", () => {
  const candles: PaperCandle[] = Array.from({ length: 25 }, (_, index) => ({
    openTime: index * 60_000,
    closeTime: (index + 1) * 60_000 - 1,
    open: 100,
    high: 101,
    low: 90,
    close: index === 24 ? 97 : 100,
    volume: 10,
  }));
  const result = decidePaperAction(candles, { stopLossPrice: 98, takeProfitPrice: 104 }, null);
  if (result.action !== "sell" || !result.reason.includes("Stop-loss")) {
    throw new Error("Stop-loss must close the paper position");
  }
});

Deno.test("simulated fills add slippage to buys and subtract it from sells", () => {
  const buy = simulatedFillPrice(100, "buy", 10);
  const sell = simulatedFillPrice(100, "sell", 10);
  if (!(buy > 100 && sell < 100)) throw new Error("Slippage direction is incorrect");
});

Deno.test("simulated fill rejects invalid market prices", () => {
  let rejected = false;
  try { simulatedFillPrice(0, "buy", 10); } catch { rejected = true; }
  if (!rejected) throw new Error("Invalid reference price was accepted");
});

Deno.test("paper stop-loss uses candle low and conservatively wins when stop and target both touch", () => {
  const candles: PaperCandle[] = Array.from({ length: 25 }, (_, index) => ({
    openTime: index * 60_000, closeTime: (index + 1) * 60_000 - 1,
    open: 100, high: index === 24 ? 106 : 101,
    low: index === 24 ? 94 : 99, close: index === 24 ? 102 : 100, volume: 10,
  }));
  const result = decidePaperAction(candles, { stopLossPrice: 98, takeProfitPrice: 104 }, null);
  if (result.action !== "sell" || !result.reason.includes("Stop-loss")) throw new Error("Stop-loss must win when both thresholds touch");
});
Deno.test("paper buy fill consumes ask depth and computes VWAP", () => {
  const fill = estimateOrderBookFill("buy", {
    bids: [{ price: 99, quantity: 10 }],
    asks: [{ price: 100, quantity: 1 }, { price: 102, quantity: 2 }],
  }, { quoteNotional: 150 });
  if (!(fill.fillPrice > 100 && fill.fillPrice < 102)) throw new Error("Buy VWAP did not consume ask depth");
  if (Math.abs(fill.quoteNotional - 150) > 1e-8 || fill.levelsUsed !== 2 || fill.slippageBps <= 0) {
    throw new Error("Buy fill is missing notional/depth/slippage");
  }
});
Deno.test("paper sell fill consumes bid depth and computes VWAP", () => {
  const fill = estimateOrderBookFill("sell", {
    bids: [{ price: 101, quantity: 1 }, { price: 99, quantity: 1 }],
    asks: [{ price: 102, quantity: 10 }],
  }, { baseQuantity: 1.5 });
  if (!(fill.fillPrice < 101 && fill.fillPrice > 99)) throw new Error("Sell VWAP did not consume bid depth");
  if (Math.abs(fill.baseQuantity - 1.5) > 1e-8 || fill.levelsUsed !== 2) throw new Error("Sell fill base quantity/depth incorrect");
});
Deno.test("paper order-book fill rejects insufficient visible depth", () => {
  let rejected = false;
  try {
    estimateOrderBookFill("buy", {
      bids: [{ price: 99, quantity: 1 }], asks: [{ price: 100, quantity: 0.1 }],
    }, { quoteNotional: 100 });
  } catch (error) { rejected = error instanceof Error && error.message === "paper_order_book_depth_insufficient"; }
  if (!rejected) throw new Error("Insufficient book depth must reject a simulated fill");
});
