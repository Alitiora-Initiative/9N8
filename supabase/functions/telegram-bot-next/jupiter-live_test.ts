import {
  assertSolanaOrder,
  assertSwapExecution,
  base58Encode,
  base64ToBytes,
  decimalToRaw,
  jupiterApi,
  priceImpactFraction,
  rawToDecimal,
} from "./jupiter-live.ts";

import { assertEquals, assertRejects, assertThrows } from "https://deno.land/std@0.224.0/assert/mod.ts";

Deno.test("decimalToRaw and rawToDecimal preserve exact token units", () => {
  assertEquals(decimalToRaw("1.25", 6), 1_250_000n);
  assertEquals(rawToDecimal(1_250_000n, 6), "1.25");
  assertEquals(rawToDecimal(10_000_000n, 6), "10");
});

Deno.test("decimalToRaw rejects invalid, zero, negative and over-precision amounts", () => {
  assertThrows(() => decimalToRaw("0", 9), Error, "amount_must_be_positive");
  assertThrows(() => decimalToRaw("-1", 9), Error, "amount_invalid");
  assertThrows(() => decimalToRaw("1.0000000001", 9), Error, "amount_precision_exceeded");
  assertThrows(() => decimalToRaw("NaN", 9), Error, "amount_invalid");
});

Deno.test("price impact hard gate allows 2 percent and rejects more", () => {
  assertEquals(priceImpactFraction(0.02), 0.02);
  assertThrows(() => priceImpactFraction(0.02001), Error, "jupiter_price_impact_too_high");
  assertThrows(() => priceImpactFraction(-0.01), Error, "jupiter_price_impact_invalid");
});

Deno.test("assertSolanaOrder requires a wallet-bound version zero transaction", () => {
  assertSolanaOrder({
    requestId: "request-1",
    transaction: "c2lnbmVkLXRyYW5zYWN0aW9u",
    transactionVersion: 0,
    taker: "So11111111111111111111111111111111111111112",
    inAmount: "1000000",
    outAmount: "2000000",
    otherAmountThreshold: "1900000",
    priceImpactPct: 0.01,
  }, "So11111111111111111111111111111111111111112");
  assertThrows(() => assertSolanaOrder({
    requestId: "request-1", transaction: "abc", transactionVersion: 0,
    taker: "DifferentWallet", inAmount: "1", outAmount: "2", otherAmountThreshold: "1",
  }, "WalletAddress"), Error, "jupiter_order_taker_mismatch");
  assertThrows(() => assertSolanaOrder({
    requestId: "request-1", transaction: "abc", transactionVersion: 1,
    inAmount: "1", outAmount: "2", otherAmountThreshold: "1",
  }, "WalletAddress"), Error, "jupiter_transaction_version_unsupported");
  assertThrows(() => assertSolanaOrder({ errorMessage: "no route" }, "WalletAddress"), Error, "no route");
});

Deno.test("assertSwapExecution only accepts explicit provider success or failure", () => {
  assertEquals(assertSwapExecution({ status: "Success", code: 0 }), "success");
  assertEquals(assertSwapExecution({ status: "Failed" }), "failed");
  assertThrows(() => assertSwapExecution({ status: "Pending" }), Error, "jupiter_execution_status_unknown");
});

Deno.test("base58Encode preserves leading zero bytes", () => {
  assertEquals(base58Encode(new Uint8Array([0, 0, 1])), "112");
});

Deno.test("base64ToBytes rejects malformed transaction payloads", () => {
  assertEquals(new TextDecoder().decode(base64ToBytes("aGVsbG8=")), "hello");
  assertThrows(() => base64ToBytes("not valid!"), Error, "transaction_base64_invalid");
});

Deno.test("Jupiter keyless API helper sends no x-api-key and parses quote payload", async () => {
  let capturedUrl = "";
  let capturedHeaders: HeadersInit | undefined;
  const result = await jupiterApi<{ requestId: string }>("/swap/v2/order?inputMint=SOL", {
    fetcher: async (input, init) => {
      capturedUrl = String(input);
      capturedHeaders = init?.headers;
      return new Response(JSON.stringify({ requestId: "keyless-quote" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  });
  assertEquals(result.requestId, "keyless-quote");
  assertEquals(capturedUrl, "https://api.jup.ag/swap/v2/order?inputMint=SOL");
  const headers = new Headers(capturedHeaders);
  assertEquals(headers.has("x-api-key"), false);
});
