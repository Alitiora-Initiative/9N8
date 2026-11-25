import test from "node:test";
import assert from "node:assert/strict";
import { ema, rsi, analyseCandles, parseCommand } from "../src/index.js";

test("EMA calculates the exponential moving average", () => {
  assert.equal(ema([1, 2, 3, 4, 5, 6], 3), 5);
});

test("RSI returns 100 for a strictly rising series", () => {
  assert.equal(rsi([1, 2, 3, 4, 5, 6, 7, 8], 3), 100);
});

test("signal analysis returns explicit indicator values", () => {
  const candles = Array.from({ length: 40 }, (_, index) => ({
    datetime: new Date(Date.UTC(2026, 0, index + 1)).toISOString(),
    close: 100 + Math.sin(index / 2) * 2
  }));
  const result = analyseCandles("TEST", candles);
  assert.equal(result.symbol, "TEST");
  assert.ok(["BULLISH SETUP", "BEARISH SETUP", "NO CLEAR SETUP"].includes(result.label));
  assert.ok(Number.isFinite(result.ema_fast));
  assert.ok(Number.isFinite(result.ema_slow));
  assert.ok(result.rsi >= 0 && result.rsi <= 100);
});

test("Telegram bot command parser handles bot mentions and arguments", () => {
  assert.deepEqual(parseCommand("/setbe@nine8bot 150 0"), {
    name: "setbe",
    args: ["150", "0"]
  });
});

test("non-command messages are ignored", () => {
  assert.equal(parseCommand("hello 9N8"), null);
});
