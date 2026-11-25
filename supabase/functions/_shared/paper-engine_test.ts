import {
  decidePaperAction,
  emaSeries,
  rsiWilder,
  simulatedFillPrice,
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
