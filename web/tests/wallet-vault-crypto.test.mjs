import assert from "node:assert/strict";
import test from "node:test";
import {
  decryptWalletBackup, deriveWalletAccounts, encryptWalletBackup, generateWalletMnemonic,
  WALLET_VAULT_CIPHER, WALLET_VAULT_ITERATIONS,
} from "../wallet-vault-crypto.js";

test("generates a valid 12-word BIP-39 phrase and derives Solana/EVM public accounts", async () => {
  const mnemonic = await generateWalletMnemonic();
  assert.equal(mnemonic.trim().split(/\s+/).length, 12);
  const accounts = await deriveWalletAccounts(mnemonic, [0, 1]);
  assert.equal(accounts.filter((row) => row.chainNamespace === "solana").length, 2);
  assert.equal(accounts.filter((row) => row.chainNamespace === "eip155").length, 4);
  assert.match(accounts[0].address, /^[1-9A-HJ-NP-Za-km-z]{32,44}$/);
  assert.match(accounts[2].address, /^0x[a-fA-F0-9]{40}$/);
  assert.equal(accounts[2].address, accounts[3].address);
  assert.equal(accounts[3].address, accounts[4].address);
});

test("AES-256-GCM backup round-trips only with the correct passphrase", async () => {
  const mnemonic = await generateWalletMnemonic();
  const envelope = await encryptWalletBackup(mnemonic, "this is a long test passphrase");
  assert.equal(envelope.cipher, WALLET_VAULT_CIPHER);
  assert.equal(envelope.iterations, WALLET_VAULT_ITERATIONS);
  assert.equal(await decryptWalletBackup(envelope, "this is a long test passphrase"), mnemonic);
  await assert.rejects(() => decryptWalletBackup(envelope, "wrong passphrase"), /Could not unlock/);
});

test("AES-GCM authentication rejects modified ciphertext", async () => {
  const mnemonic = await generateWalletMnemonic();
  const envelope = await encryptWalletBackup(mnemonic, "another long passphrase");
  const bytes = Uint8Array.from(atob(envelope.ciphertext), (char) => char.charCodeAt(0));
  bytes[0] ^= 1;
  envelope.ciphertext = btoa(String.fromCharCode(...bytes));
  await assert.rejects(() => decryptWalletBackup(envelope, "another long passphrase"), /Could not unlock/);
});

test("short passphrases are rejected before storing an envelope", async () => {
  const mnemonic = await generateWalletMnemonic();
  await assert.rejects(() => encryptWalletBackup(mnemonic, "short"), /at least 12/);
});
