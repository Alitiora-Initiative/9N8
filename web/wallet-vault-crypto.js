import { generateMnemonic, mnemonicToSeed, validateMnemonic } from "@scure/bip39";
import { wordlist } from "@scure/bip39/wordlists/english.js";
import { HDKey } from "@scure/bip32";
import { derivePath } from "ed25519-hd-key";
import nacl from "tweetnacl";
import { privateKeyToAccount } from "viem/accounts";

export const WALLET_VAULT_FORMAT_VERSION = 1;
export const WALLET_VAULT_CIPHER = "AES-256-GCM";
export const WALLET_VAULT_KDF = "PBKDF2-SHA256";
export const WALLET_VAULT_ITERATIONS = 600_000;
const AAD = new TextEncoder().encode("9N8-WALLET-VAULT-v1");
const EVM_NETWORKS = [
  { chainId: "1", network: "Ethereum Mainnet" },
  { chainId: "8453", network: "Base Mainnet" },
  { chainId: "56", network: "BNB Smart Chain" },
  { chainId: "42161", network: "Arbitrum One" },
];

function bytesToHex(bytes) {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
function bytesToBase64(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
function base64ToBytes(value) {
  if (typeof value !== "string" || value.length % 4 !== 0 || !/^[A-Za-z0-9+/]+={0,2}$/.test(value)) {
    throw new Error("Wallet backup encoding is invalid.");
  }
  const binary = atob(value);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}
function base58Encode(bytes) {
  const alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  let value = 0n;
  for (const byte of bytes) value = value * 256n + BigInt(byte);
  let result = "";
  while (value > 0n) {
    const remainder = Number(value % 58n);
    result = alphabet[remainder] + result;
    value /= 58n;
  }
  for (let i = 0; i < bytes.length && bytes[i] === 0; i++) result = "1" + result;
  return result || "1";
}
function assertPassphrase(passphrase) {
  if (typeof passphrase !== "string" || passphrase.length < 12 || passphrase.length > 256) {
    throw new Error("Use a backup passphrase with at least 12 characters.");
  }
}
function assertMnemonic(mnemonic) {
  if (typeof mnemonic !== "string" || !validateMnemonic(mnemonic.trim(), wordlist)) {
    throw new Error("The recovery phrase is not a valid BIP-39 English phrase.");
  }
}
function createSolanaAccount(seed, walletIndex) {
  if (!Number.isInteger(walletIndex) || walletIndex < 0 || walletIndex > 1000) {
    throw new Error("Wallet index is invalid.");
  }
  const derivationPath = "m/44'/501'/" + walletIndex + "'/0'";
  const derived = derivePath(derivationPath, bytesToHex(seed));
  const privateSeed = new Uint8Array(derived.key);
  const keyPair = nacl.sign.keyPair.fromSeed(privateSeed);
  const address = base58Encode(keyPair.publicKey);
  privateSeed.fill(0);
  keyPair.secretKey.fill(0);
  return {
    walletIndex, chainNamespace: "solana", chainId: "mainnet-beta",
    network: "Solana Mainnet", address, derivationPath,
    label: walletIndex === 0 ? "W1 (Main)" : "W" + (walletIndex + 1),
  };
}
function createEvmAccounts(seed) {
  const root = HDKey.fromMasterSeed(seed);
  const path = "m/44'/60'/0'/0/0";
  const child = root.derive(path);
  const privateKey = child.privateKey;
  if (!privateKey || privateKey.length !== 32) throw new Error("Could not derive the EVM account.");
  try {
    const account = privateKeyToAccount(("0x" + bytesToHex(privateKey)));
    return EVM_NETWORKS.map(({ chainId, network }) => ({
      walletIndex: 0, chainNamespace: "eip155", chainId, network,
      address: account.address, derivationPath: path, label: "EVM (Shared)",
    }));
  } finally {
    privateKey.fill(0);
  }
}
export async function generateWalletMnemonic() {
  return generateMnemonic(wordlist, 128);
}
export async function deriveWalletAccounts(mnemonic, solanaIndexes = [0]) {
  assertMnemonic(mnemonic);
  const seed = await mnemonicToSeed(mnemonic.trim());
  try {
    const solanaAccounts = [...new Set(solanaIndexes)].sort((a, b) => a - b)
      .map((index) => createSolanaAccount(seed, index));
    return [...solanaAccounts, ...createEvmAccounts(seed)];
  } finally {
    seed.fill(0);
  }
}
async function deriveEncryptionKey(passphrase, salt) {
  assertPassphrase(passphrase);
  const material = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(passphrase), "PBKDF2", false, ["deriveKey"],
  );
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", salt, iterations: WALLET_VAULT_ITERATIONS, hash: "SHA-256" },
    material, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"],
  );
}
export async function encryptWalletBackup(mnemonic, passphrase) {
  assertMnemonic(mnemonic);
  assertPassphrase(passphrase);
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const key = await deriveEncryptionKey(passphrase, salt);
  const plaintext = new TextEncoder().encode(JSON.stringify({
    version: WALLET_VAULT_FORMAT_VERSION, mnemonic: mnemonic.trim(),
  }));
  try {
    const encrypted = await crypto.subtle.encrypt(
      { name: "AES-GCM", iv, additionalData: AAD, tagLength: 128 }, key, plaintext,
    );
    return {
      formatVersion: WALLET_VAULT_FORMAT_VERSION,
      cipher: WALLET_VAULT_CIPHER,
      kdf: WALLET_VAULT_KDF,
      iterations: WALLET_VAULT_ITERATIONS,
      salt: bytesToBase64(salt),
      iv: bytesToBase64(iv),
      ciphertext: bytesToBase64(new Uint8Array(encrypted)),
    };
  } finally {
    plaintext.fill(0);
    salt.fill(0);
    iv.fill(0);
  }
}
export async function decryptWalletBackup(envelope, passphrase) {
  if (!envelope || envelope.formatVersion !== WALLET_VAULT_FORMAT_VERSION ||
      envelope.cipher !== WALLET_VAULT_CIPHER || envelope.kdf !== WALLET_VAULT_KDF ||
      envelope.iterations !== WALLET_VAULT_ITERATIONS) throw new Error("Unsupported encrypted wallet backup format.");
  const salt = base64ToBytes(envelope.salt);
  const iv = base64ToBytes(envelope.iv);
  const ciphertext = base64ToBytes(envelope.ciphertext);
  if (salt.length !== 16 || iv.length !== 12 || ciphertext.length < 32 || ciphertext.length > 65536) {
    throw new Error("Encrypted wallet backup has invalid lengths.");
  }
  const key = await deriveEncryptionKey(passphrase, salt);
  let plaintext;
  try {
    plaintext = new Uint8Array(await crypto.subtle.decrypt(
      { name: "AES-GCM", iv, additionalData: AAD, tagLength: 128 }, key, ciphertext,
    ));
    const payload = JSON.parse(new TextDecoder().decode(plaintext));
    if (payload?.version !== WALLET_VAULT_FORMAT_VERSION) throw new Error("Backup version is invalid.");
    assertMnemonic(payload.mnemonic);
    return payload.mnemonic;
  } catch {
    throw new Error("Could not unlock the wallet backup. Check the passphrase or backup integrity.");
  } finally {
    salt.fill(0);
    iv.fill(0);
    ciphertext.fill(0);
    if (plaintext) plaintext.fill(0);
  }
}
export function clearSecretElement(element) {
  if (element) element.textContent = "";
}
