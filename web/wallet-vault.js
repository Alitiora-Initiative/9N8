import QRCode from "qrcode";
import {
  decryptWalletBackup, deriveWalletAccounts, encryptWalletBackup, generateWalletMnemonic,
  clearSecretElement,
} from "./wallet-vault-crypto.js";

const API = "https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/wallet-vault";
const tg = window.Telegram?.WebApp;
const $ = (id) => document.getElementById(id);
const panel = $("n8WalletVault");
if (panel) initializeWalletVault();

let activeMnemonic = "";
let activeAccounts = [];
let revealTimer = null;

function setStatus(message, kind = "info") {
  const target = $("walletVaultStatus");
  if (target) {
    target.textContent = message;
    target.dataset.kind = kind;
  }
}
function short(value) {
  return value && value.length > 18 ? value.slice(0, 9) + "…" + value.slice(-7) : value;
}
function normalizeAccount(row) {
  return {
    walletIndex: Number(row.walletIndex ?? row.wallet_index),
    chainNamespace: row.chainNamespace ?? row.chain_namespace,
    chainId: String(row.chainId ?? row.chain_id),
    network: row.network,
    address: row.address ?? row.public_address,
    derivationPath: row.derivationPath ?? row.derivation_path,
    label: row.label,
  };
}
async function api(action, extra = {}) {
  if (!tg?.initData) throw new Error("Open this wallet manager from the 9N8 Telegram private chat.");
  const response = await fetch(API, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ action, initData: tg.initData, ...extra }),
    cache: "no-store", credentials: "omit",
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok !== true) {
    const codes = {
      wallet_vault_already_exists: "A backup already exists for this Telegram account. Unlock it instead of replacing it.",
      wallet_vault_not_found: "No encrypted wallet backup was found for this Telegram account.",
    };
    throw new Error(codes[payload.error] || payload.error || "Wallet request was rejected.");
  }
  return payload;
}
function renderAccounts(rows) {
  activeAccounts = rows.map(normalizeAccount);
  const target = $("walletVaultAccounts");
  if (!target) return;
  target.replaceChildren();
  if (!activeAccounts.length) {
    target.textContent = "No wallet accounts have been created yet.";
    return;
  }
  for (const account of activeAccounts) {
    const article = document.createElement("article");
    article.className = "sim-panel";
    const title = document.createElement("strong");
    title.textContent = account.label + " · " + account.network;
    const address = document.createElement("code");
    address.style.cssText = "display:block;overflow-wrap:anywhere;margin:10px 0";
    address.textContent = account.address;
    const path = document.createElement("small");
    path.className = "sim-muted";
    path.textContent = "Derivation: " + account.derivationPath;
    const copy = document.createElement("button");
    copy.type = "button"; copy.className = "secondary"; copy.textContent = "Copy address";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(account.address);
        setStatus("Address copied. Verify the first and last characters before sending funds.", "success");
      } catch {
        setStatus("Clipboard permission was denied. Select and copy the address manually.", "warning");
      }
    });
    const qr = document.createElement("button");
    qr.type = "button"; qr.className = "secondary"; qr.textContent = "Show deposit QR";
    qr.addEventListener("click", async () => {
      try {
        const canvas = $("walletVaultQr");
        const section = $("walletVaultQrSection");
        await QRCode.toCanvas(canvas, account.address, { width: 240, margin: 2, errorCorrectionLevel: "M" });
        $("walletVaultQrLabel").textContent = account.network + " · " + short(account.address);
        section.classList.remove("hidden");
      } catch {
        setStatus("Could not render a QR code for this address.", "warning");
      }
    });
    article.append(title, address, path, copy, qr);
    target.append(article);
  }
}
function showRecoveryPhrase(mnemonic) {
  const section = $("walletVaultReveal");
  const phrase = $("walletVaultPhrase");
  clearTimeout(revealTimer);
  phrase.textContent = mnemonic;
  section.classList.remove("hidden");
  $("walletVaultBackupSaved").checked = false;
  setStatus("Recovery phrase is shown only in this private Mini App. Write it offline. It will be cleared from this screen after 60 seconds.", "warning");
  revealTimer = setTimeout(() => {
    clearSecretElement(phrase);
    section.classList.add("hidden");
    setStatus("Recovery phrase display cleared. Your encrypted backup remains stored; the phrase cannot be recovered without the passphrase.", "info");
  }, 60_000);
}
function clearRecoveryPhrase() {
  clearTimeout(revealTimer);
  activeMnemonic = "";
  clearSecretElement($("walletVaultPhrase"));
  $("walletVaultReveal").classList.add("hidden");
}
function downloadEncryptedBackup(envelope, accounts) {
  const payload = {
    app: "9N8", formatVersion: 1, warning: "Encrypted backup only. It cannot be unlocked without the passphrase you created.",
    envelope, accounts: accounts.map((row) => ({
      walletIndex: row.walletIndex, chainNamespace: row.chainNamespace, chainId: row.chainId,
      network: row.network, address: row.address, derivationPath: row.derivationPath, label: row.label,
    })),
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const href = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = href; link.download = "9n8-encrypted-wallet-backup.json";
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}
async function storeNewWallet() {
  const passphrase = $("walletVaultPassphrase").value;
  if (passphrase.length < 12) throw new Error("Create a backup passphrase with at least 12 characters.");
  setStatus("Generating your wallet in this browser…");
  const mnemonic = await generateWalletMnemonic();
  const accounts = await deriveWalletAccounts(mnemonic, [0]);
  const envelope = await encryptWalletBackup(mnemonic, passphrase);
  setStatus("Saving AES-256-GCM encrypted backup and public addresses…");
  await api("save", { envelope, accounts });
  activeMnemonic = mnemonic;
  renderAccounts(accounts);
  $("walletVaultCreate").classList.add("hidden");
  $("walletVaultUnlock").classList.remove("hidden");
  $("walletVaultDownload").onclick = () => downloadEncryptedBackup(envelope, accounts);
  $("walletVaultDownload").disabled = false;
  showRecoveryPhrase(mnemonic);
  setStatus("Wallet created. Only public addresses and encrypted backup bytes were sent to 9N8. Your passphrase and recovery phrase stayed in this browser.", "success");
}
async function unlockExistingWallet() {
  const passphrase = $("walletVaultPassphrase").value;
  if (passphrase.length < 12) throw new Error("Enter the passphrase used when the encrypted wallet backup was created.");
  setStatus("Loading encrypted backup…");
  const result = await api("load");
  setStatus("Decrypting locally with AES-256-GCM…");
  const mnemonic = await decryptWalletBackup(result.envelope, passphrase);
  activeMnemonic = mnemonic;
  renderAccounts(result.accounts || []);
  showRecoveryPhrase(mnemonic);
  $("walletVaultCreate").classList.add("hidden");
  $("walletVaultUnlock").classList.remove("hidden");
  $("walletVaultDownload").onclick = () => downloadEncryptedBackup(result.envelope, activeAccounts);
  $("walletVaultDownload").disabled = false;
  setStatus("Backup unlocked on this device. No plaintext recovery phrase was sent to 9N8.", "success");
}
async function addWallets(count) {
  if (!activeMnemonic) {
    await unlockExistingWallet();
  }
  const indexes = activeAccounts.filter((row) => row.chainNamespace === "solana")
    .map((row) => row.walletIndex);
  const start = Math.max(-1, ...indexes) + 1;
  if (start + count > 1000) throw new Error("Wallet derivation index limit reached.");
  const generated = await deriveWalletAccounts(activeMnemonic, Array.from({ length: count }, (_, i) => start + i));
  const newAccounts = generated.filter((row) => row.chainNamespace === "solana");
  await api("add_accounts", { accounts: newAccounts });
  const status = await api("status");
  renderAccounts(status.accounts || []);
  setStatus("Added " + count + " Solana wallet address(es). They share the encrypted recovery phrase; keep that phrase offline.", "success");
}
async function refreshBalances() {
  setStatus("Reading Solana mainnet balances…");
  const result = await api("balance");
  const rows = result.balances || [];
  if (!rows.length) return setStatus("No Solana accounts are stored.", "info");
  const lines = rows.map((row) => row.ok
    ? short(row.address) + " · " + Number(row.sol).toFixed(6) + " SOL"
    : short(row.address) + " · balance temporarily unavailable");
  setStatus(lines.join("\n") + "\nEVM balance RPCs and persistent deposit push alerts are not enabled in this release.", "success");
}
async function initializeWalletVault() {
  tg?.ready?.(); tg?.expand?.();
  $("walletVaultCreateButton").addEventListener("click", async () => {
    $("walletVaultCreateButton").disabled = true;
    try { await storeNewWallet(); } catch (error) { setStatus(error.message || "Wallet creation failed.", "error"); }
    finally { $("walletVaultCreateButton").disabled = false; }
  });
  $("walletVaultUnlockButton").addEventListener("click", async () => {
    $("walletVaultUnlockButton").disabled = true;
    try { await unlockExistingWallet(); } catch (error) { setStatus(error.message || "Wallet unlock failed.", "error"); }
    finally { $("walletVaultUnlockButton").disabled = false; }
  });
  $("walletVaultAddOne").addEventListener("click", async () => {
    try { await addWallets(1); } catch (error) { setStatus(error.message || "Wallet creation failed.", "error"); }
  });
  $("walletVaultAddFive").addEventListener("click", async () => {
    try { await addWallets(5); } catch (error) { setStatus(error.message || "Wallet batch creation failed.", "error"); }
  });
  $("walletVaultBalance").addEventListener("click", async () => {
    try { await refreshBalances(); } catch (error) { setStatus(error.message || "Balance check failed.", "error"); }
  });
  $("walletVaultConfirmSaved").addEventListener("click", () => {
    if (!$("walletVaultBackupSaved").checked) {
      setStatus("Confirm that you have saved the recovery phrase offline before closing it.", "warning");
      return;
    }
    clearRecoveryPhrase();
    setStatus("Backup confirmation recorded locally. Keep the encrypted backup file and recovery phrase in separate secure places.", "success");
  });
  $("walletVaultDeleteReveal").addEventListener("click", () => {
    clearRecoveryPhrase();
    setStatus("Recovery phrase display cleared from this screen.", "success");
  });
  $("walletVaultDownload").disabled = true;
  if (!tg?.initData) {
    $("walletVaultCreateButton").disabled = true;
    $("walletVaultUnlockButton").disabled = true;
    setStatus("Open the 9N8 Mini App from a private Telegram chat to authenticate this wallet.", "warning");
    return;
  }
  try {
    const result = await api("status");
    if (result.exists) {
      $("walletVaultCreate").classList.add("hidden");
      $("walletVaultUnlock").classList.remove("hidden");
      renderAccounts(result.accounts || []);
      setStatus("Encrypted wallet backup found. Enter your local passphrase and unlock it. The server cannot decrypt this backup.", "success");
    } else {
      $("walletVaultCreate").classList.remove("hidden");
      $("walletVaultUnlock").classList.add("hidden");
      setStatus("No wallet is stored yet. Create a new client-side wallet below.", "info");
    }
  } catch (error) {
    $("walletVaultCreateButton").disabled = true;
    $("walletVaultUnlockButton").disabled = true;
    setStatus(error.message || "Wallet vault status could not be checked.", "error");
  }
}
