import assert from "node:assert/strict";
import test from "node:test";
import { VersionedTransaction } from "@solana/web3.js";

// One-signature, v0 transaction with one static signer, no instructions and no LUTs.
// This verifies the exact deserialize/serialize API used for Jupiter order payloads.
function minimalV0Transaction() {
  return new Uint8Array([
    1,                         // compact-u16 signature count
    ...new Uint8Array(64),     // signature bytes
    0x80,                      // v0 message prefix
    1, 0, 0,                   // message header
    1,                         // static account count
    ...new Uint8Array(32),     // static account key
    ...new Uint8Array(32),    // recent blockhash
    0,                         // compiled instruction count
    0,                         // address lookup table count
  ]);
}

test("Jupiter v0 transaction payload can deserialize and serialize with web3.js v3", () => {
  const encoded = minimalV0Transaction();
  const transaction = VersionedTransaction.deserialize(encoded);

  assert.equal(transaction.version, 0);
  assert.equal(typeof transaction.serialize, "function");

  const serialized = transaction.serialize();
  assert.ok(serialized instanceof Uint8Array);
  assert.deepEqual(serialized, encoded);
});

test("malformed Jupiter transaction payload is rejected", () => {
  assert.throws(() => VersionedTransaction.deserialize(new Uint8Array([1, 2, 3])));
});
