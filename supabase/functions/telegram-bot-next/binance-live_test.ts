import {
  BinanceHttpError,
  binanceSignedRequest,
  buildSpotMarketOrder,
  canonicalJson,
  estimateDepthExecution,
  mapBinanceOrderStatus,
  sha256Hex,
  type BinanceSymbolInfo,
} from "./binance-live.ts";

const symbol: BinanceSymbolInfo = {
  symbol: "BTCUSDT",
  status: "TRADING",
  isSpotTradingAllowed: true,
  baseAsset: "BTC",
  quoteAsset: "USDT",
  quoteAssetPrecision: 2,
  filters: [
    { filterType: "MARKET_LOT_SIZE", minQty: "0.00001", maxQty: "1000", stepSize: "0.00001" },
    { filterType: "MIN_NOTIONAL", minNotional: "5", applyToMarket: true },
  ],
};

Deno.test("market buy uses quoteOrderQty and never invents base quantity", () => {
  const order = buildSpotMarketOrder(symbol, "BUY", { quoteOrderQty: "12.50" }, 90000, "9n81234567890123456789012345678901");
  if (order.quoteOrderQty !== "12.50" || "quantity" in order || order.type !== "MARKET") {
    throw new Error("BUY order payload does not use quote amount correctly");
  }
});

Deno.test("market sell uses base quantity and enforces the exchange step size", () => {
  const order = buildSpotMarketOrder(symbol, "SELL", { quantity: "0.00120" }, 90000, "9n81234567890123456789012345678901");
  if (order.quantity !== "0.00120" || "quoteOrderQty" in order) throw new Error("SELL order payload invalid");
  let rejected = false;
  try {
    buildSpotMarketOrder(symbol, "SELL", { quantity: "0.00121" }, 90000, "9n81234567890123456789012345678901");
  } catch (error) {
    rejected = error instanceof Error && error.message === "market_sell_quantity_violates_lot_size";
  }
  if (!rejected) throw new Error("A quantity off the Binance lot step was accepted");
});

Deno.test("market order validation rejects non-trading and non-USDT markets", () => {
  let rejected = false;
  try {
    buildSpotMarketOrder({ ...symbol, status: "HALT" }, "BUY", { quoteOrderQty: "10" }, 90000, "9n81234567890123456789012345678901");
  } catch { rejected = true; }
  if (!rejected) throw new Error("Non-trading symbol was accepted");
  rejected = false;
  try {
    buildSpotMarketOrder({ ...symbol, quoteAsset: "BTC" }, "BUY", { quoteOrderQty: "10" }, 90000, "9n81234567890123456789012345678901");
  } catch { rejected = true; }
  if (!rejected) throw new Error("Non-USDT quote market was accepted");
});

Deno.test("depth estimate consumes book levels and reports conservative VWAP", () => {
  const estimate = estimateDepthExecution("BUY", {
    bids: [["99", "10"]],
    asks: [["100", "0.05"], ["101", "1"]],
  }, { quoteOrderQty: 10 });
  if (!(estimate.fillPrice >= 100 && estimate.fillPrice < 101)) throw new Error("Buy VWAP is outside ask book");
  if (estimate.levelsUsed !== 2 || estimate.slippageBps <= 0 || estimate.liquidityUsd <= 0) {
    throw new Error("Depth/slippage metrics were not calculated");
  }
});

Deno.test("depth estimator refuses to extrapolate beyond visible liquidity", () => {
  let rejected = false;
  try {
    estimateDepthExecution("SELL", {
      bids: [["99", "0.001"]],
      asks: [["100", "1"]],
    }, { quantity: 0.01 });
  } catch (error) {
    rejected = error instanceof Error && error.message === "binance_visible_depth_insufficient";
  }
  if (!rejected) throw new Error("Insufficient visible depth was accepted");
});

Deno.test("canonical JSON is stable and SHA-256 is a 64-character digest", async () => {
  const one = canonicalJson({ b: 2, a: { z: true, c: 3 } });
  const two = canonicalJson({ a: { c: 3, z: true }, b: 2 });
  if (one !== two) throw new Error("Canonical JSON changes with object property order");
  const digest = await sha256Hex(one);
  if (!/^[0-9a-f]{64}$/.test(digest)) throw new Error("SHA-256 digest is malformed");
});

Deno.test("Binance response status mapping is conservative", () => {
  if (mapBinanceOrderStatus("PARTIALLY_FILLED") !== "partially_filled" ||
      mapBinanceOrderStatus("FILLED") !== "filled" ||
      mapBinanceOrderStatus("EXPIRED") !== "cancelled" ||
      mapBinanceOrderStatus("UNRECOGNIZED") !== "unknown") {
    throw new Error("Unexpected Binance order state mapping");
  }
});

Deno.test("signed POST request sends HMAC signature in the form body", async () => {
  let seenUrl = "";
  let seenInit: RequestInit | undefined;
  const fakeFetch: typeof fetch = async (input, init) => {
    seenUrl = String(input);
    seenInit = init;
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  };
  await binanceSignedRequest(
    { apiKey: "public-test-key", apiSecret: "secret-test-key" },
    "POST",
    "/api/v3/order/test",
    { symbol: "BTCUSDT", side: "BUY", type: "MARKET", quoteOrderQty: "10" },
    fakeFetch,
  );
  const body = String(seenInit?.body ?? "");
  if (seenUrl !== "https://api.binance.com/api/v3/order/test" ||
      seenInit?.method !== "POST" ||
      !body.includes("symbol=BTCUSDT") ||
      !body.includes("quoteOrderQty=10") ||
      !/signature=[0-9a-f]{64}/.test(body) ||
      (seenInit?.headers as Record<string, string>)["X-MBX-APIKEY"] !== "public-test-key") {
    throw new Error("Signed Binance POST request is missing expected fields");
  }
});

Deno.test("Binance signed request rejects unapproved endpoint paths", async () => {
  let rejected = false;
  try {
    await binanceSignedRequest(
      { apiKey: "public-test-key", apiSecret: "secret-test-key" },
      "POST",
      "/sapi/v1/capital/withdraw/apply",
      {},
      async () => new Response("{}", { status: 200 }),
    );
  } catch (error) {
    rejected = error instanceof Error && error.message === "binance_path_not_allowlisted";
  }
  if (!rejected) throw new Error("A withdrawal endpoint passed the executor allowlist");
});

Deno.test("Binance HTTP errors preserve status and retry classification", async () => {
  let error: unknown;
  try {
    await binanceSignedRequest(
      { apiKey: "public-test-key", apiSecret: "secret-test-key" },
      "POST",
      "/api/v3/order",
      { symbol: "BTCUSDT" },
      async () => new Response(JSON.stringify({ code: -1013, msg: "Filter failure" }), { status: 400 }),
    );
  } catch (caught) { error = caught; }
  if (!(error instanceof BinanceHttpError) || error.code !== -1013 ||
      error.httpStatus !== 400 || error.retryable) {
    throw new Error("Binance rejection was not classified as a non-retryable validation failure");
  }
});
