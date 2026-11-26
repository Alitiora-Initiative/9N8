import {
  assertSolanaOrder,
  assertSwapExecution,
  base58Encode,
  base64ToBytes,
  decimalToRaw,
  priceImpactFraction,
  rawToDecimal,
  sha256HexBytes,
} from "./jupiter-live.ts";

Deno.test("decimal to raw uses integer arithmetic and rejects hidden rounding", () => {
  if (decimalToRaw("1.000000001", 9) !== 1_000_000_001n) throw new Error("lamport_conversion_incorrect");
  if (decimalToRaw("12.3456", 6) !== 12_345_600n) throw new Error("spl_conversion_incorrect");
  let rejected = false;
  try { decimalToRaw("0.0000000001", 9); } catch { rejected = true; }
  if (!rejected) throw new Error("precision_was_rounded");
});

Deno.test("raw decimal formatting strips only insignificant zeroes", () => {
  if (rawToDecimal("1000000001", 9) !== "1.000000001") throw new Error("sol_format_incorrect");
  if (rawToDecimal("1200000", 6) !== "1.2") throw new Error("spl_format_incorrect");
});

Deno.test("base58 and base64 helpers encode signatures and transaction bytes", () => {
  if (base58Encode(new Uint8Array([0, 0, 1])) !== "112") throw new Error("base58_encoding_incorrect");
  const bytes = new Uint8Array([0, 1, 2, 250, 255]);
  const encoded = btoa(String.fromCharCode(...bytes));
  const decoded = base64ToBytes(encoded);
  if (decoded.some((v, i) => v !== bytes[i])) throw new Error("base64_round_trip_incorrect");
});

Deno.test("quote guard rejects transactionless quotes, wrong taker and v1 transactions", () => {
  const order = {
    requestId: "r1", transaction: "AQ==", transactionVersion: 0,
    taker: "Wallet111", inAmount: "100", outAmount: "90",
    otherAmountThreshold: "85", priceImpactPct: "0.001",
  };
  assertSolanaOrder(order, "Wallet111");
  let rejected = false;
  try { assertSolanaOrder({ ...order, transaction: "" }, "Wallet111"); } catch { rejected = true; }
  if (!rejected) throw new Error("transactionless_order_accepted");
  rejected = false;
  try { assertSolanaOrder({ ...order, taker: "Wallet222" }, "Wallet111"); } catch { rejected = true; }
  if (!rejected) throw new Error("wrong_taker_accepted");
  rejected = false;
  try { assertSolanaOrder({ ...order, transactionVersion: 1 }, "Wallet111"); } catch { rejected = true; }
  if (!rejected) throw new Error("v1_transaction_not_rejected");
});

Deno.test("slippage and impact guard only accepts positive impact under two percent", () => {
  if (priceImpactFraction("0.019") !== 0.019) throw new Error("valid_impact_rejected");
  let rejected = false;
  try { priceImpactFraction("0.021"); } catch { rejected = true; }
  if (!rejected) throw new Error("high_impact_accepted");
});

Deno.test("execute status treats only explicit Success or Failed as terminal", () => {
  if (assertSwapExecution({ status: "Success", code: 0 }) !== "success") throw new Error("success_not_recognized");
  if (assertSwapExecution({ status: "Failed", code: 6001 }) !== "failed") throw new Error("failed_not_recognized");
  let rejected = false;
  try { assertSwapExecution({ status: "Pending" }); } catch { rejected = true; }
  if (!rejected) throw new Error("unknown_status_treated_terminal");
});

Deno.test("message hash is SHA-256 hex", async () => {
  const hash = await sha256HexBytes(new TextEncoder().encode("hello"));
  if (hash !== "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824") {
    throw new Error("sha256_hex_incorrect");
  }
});
