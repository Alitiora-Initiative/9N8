import { createAppKit } from "@reown/appkit";
import { WagmiAdapter } from "@reown/appkit-adapter-wagmi";
import { SolanaAdapter } from "@reown/appkit-adapter-solana";
import { mainnet, base, solana } from "@reown/appkit/networks";
import { defineChain } from "viem";

const API = "https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot-next";
const tg = window.Telegram && window.Telegram.WebApp;
const $ = (id) => document.getElementById(id);
const providerSelect = $("provider");
const passphraseFields = $("okxPassphraseFields");
const passphraseInput = $("okxPassphrase");
const saveButton = $("saveExchange");
const exchangeStatus = $("exchangeStatus");
const balanceButton = $("loadBinanceBalance");
const balanceTarget = $("binanceBalance");

async function apiPost(body) {
  if (!tg || !tg.initData) throw new Error("Open 9N8 from your Telegram private chat first.");
  const response = await fetch(API, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ ...body, initData: tg.initData }), cache: "no-store", credentials: "omit" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok !== true) throw new Error(payload.message || payload.error || "Request rejected.");
  return payload;
}

function syncExchangeFields() {
  const isOkx = providerSelect.value === "okx";
  passphraseFields.classList.toggle("hidden", !isOkx);
  passphraseInput.required = isOkx;
  saveButton.textContent = isOkx ? "Verify OKX Spot read access & save securely" : "Verify Binance Spot LIVE key & save securely";
  exchangeStatus.textContent = isOkx ? "OKX requires API key, secret and the API passphrase set when the key was created. Choose the account region." : "Use a Binance Spot-only key with withdrawals and all transfer permissions disabled.";
}
providerSelect.addEventListener("change", syncExchangeFields);
syncExchangeFields();

$("exchangeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const provider = providerSelect.value;
  const credentials = { apiKey: $("apiKey").value.trim(), apiSecret: $("apiSecret").value.trim() };
  if (provider === "okx") { credentials.passphrase = passphraseInput.value; credentials.region = $("okxRegion").value; }
  saveButton.disabled = true;
  exchangeStatus.textContent = "Validating private exchange account access…";
  try {
    const result = await apiPost({ action: "connect_exchange", provider, environment: "live", credentials });
    window.n8ActiveExchangeConnectionId = result.connectionId || "";
    window.n8ActiveExchangeProvider = provider;
    exchangeStatus.textContent = provider === "okx" ? "OKX read access validated and credentials saved in Vault. Trade, withdrawal and transfer scopes are not verified by this read-only check. Real orders remain locked." : "Binance Spot key validated. Read/Spot scopes accepted; withdrawals and transfers are disabled. Real orders remain locked.";
    $("exchangeForm").reset();
    syncExchangeFields();
    balanceTarget.textContent = "Connection saved. Refresh balances to read the connected exchange account.";
  } catch (error) { exchangeStatus.textContent = error.message || "Exchange connection failed."; }
  finally {
    credentials.apiKey = ""; credentials.apiSecret = ""; credentials.passphrase = "";
    $("apiKey").value = ""; $("apiSecret").value = ""; passphraseInput.value = "";
    saveButton.disabled = false;
  }
});

balanceButton.addEventListener("click", async () => {
  const provider = providerSelect.value;
  balanceButton.disabled = true;
  balanceTarget.textContent = "Loading private " + (provider === "okx" ? "OKX" : "Binance") + " Spot balances…";
  try {
    const result = await apiPost({ action: provider === "okx" ? "okx_account" : "binance_account" });
    window.n8ActiveExchangeConnectionId = result.connection && result.connection.id || "";
    window.n8ActiveExchangeProvider = provider;
    const balances = Array.isArray(result.balances) ? result.balances : [];
    balanceTarget.replaceChildren();
    if (!balances.length) balanceTarget.textContent = "No non-zero Spot balances returned.";
    for (const item of balances) {
      const row = document.createElement("div"); row.className = "book-row";
      const asset = document.createElement("b"); asset.textContent = String(item.asset || "");
      const amount = document.createElement("span"); amount.textContent = "Free " + Number(item.free || 0).toLocaleString("en-US", { maximumFractionDigits: 8 }) + " · Locked " + Number(item.locked || 0).toLocaleString("en-US", { maximumFractionDigits: 8 });
      row.append(asset, amount); balanceTarget.append(row);
    }
    if (result.quoteBalance) {
      const row = document.createElement("div"); row.className = "book-row";
      const label = document.createElement("b"); label.textContent = "USDT quote balance";
      const amount = document.createElement("span"); amount.textContent = "Free " + Number(result.quoteBalance.free || 0).toFixed(4) + " · Risk-gate USD estimate $" + Number(result.quoteBalance.availableUsdApprox || 0).toFixed(2);
      row.append(label, amount); balanceTarget.append(row);
    }
    if (provider === "okx") { const note = document.createElement("p"); note.className = "muted"; note.textContent = "OKX read access only. Trade, withdrawal and transfer API-key scopes remain unverified. Live order execution is locked."; balanceTarget.append(note); }
  } catch (error) { balanceTarget.textContent = error.message || "Could not fetch exchange balances."; }
  finally { balanceButton.disabled = false; }
});

const walletStatus = $("walletSessionStatus");
const connectWalletButton = $("connectWalletSession");
const verifyWalletButton = $("verifyWalletSession");
const disconnectWalletButton = $("disconnectWalletSession");
const walletAppSelect = $("walletSessionProvider");
const projectId = String(import.meta.env.VITE_REOWN_PROJECT_ID || "").trim();
const SOLANA_MAINNET_ID = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp";
const bsc = defineChain({ id: 56, name: "BNB Smart Chain", nativeCurrency: { name: "BNB", symbol: "BNB", decimals: 18 }, rpcUrls: { default: { http: ["https://bsc-dataseed.binance.org"] } }, blockExplorers: { default: { name: "BscScan", url: "https://bscscan.com" } } });
const evmNetworks = [mainnet, base, bsc];
let modal = null;
let accountState = null;
let walletProviders = {};
let injectedSession = null;
let verifiedSession = null;

if (projectId) {
  try {
    const wagmi = new WagmiAdapter({ projectId, networks: evmNetworks });
    modal = createAppKit({ adapters: [wagmi, new SolanaAdapter()], networks: [...evmNetworks, solana], projectId, metadata: { name: "9N8 Trading Intelligence", description: "Wallet ownership verification", url: window.location.origin, icons: [window.location.origin + "/favicon.ico"] }, features: { analytics: false }, themeMode: "dark", themeVariables: { "--w3m-accent": "#28d18c" } });
    modal.subscribeAccount((state) => { accountState = state; const session = fromAppKit(); if (session) { verifyWalletButton.disabled = false; walletStatus.textContent = session.walletName + " connected on " + session.network + ". Verify ownership to sign the 9N8 message."; } });
    modal.subscribeProviders((state) => { walletProviders = state || {}; });
    walletStatus.textContent = "WalletConnect is ready. Choose a wallet app and connect.";
  } catch (error) { modal = null; walletStatus.textContent = "WalletConnect could not initialise: " + (error.message || "configuration error"); }
} else {
  walletStatus.textContent = "WalletConnect QR pairing needs VITE_REOWN_PROJECT_ID in Vercel. Injected MetaMask, Trust Wallet and Phantom connections may still work in their own browsers.";
}

function walletType() { return walletAppSelect.value || "walletconnect"; }
function walletName(provider) { return ({ metamask: "MetaMask", trust_wallet: "Trust Wallet", phantom: "Phantom", walletconnect: "WalletConnect wallet" })[provider] || "Wallet"; }
function evmNetwork(chainId) { return ({ "1": "Ethereum Mainnet", "8453": "Base Mainnet", "56": "BNB Smart Chain" })[chainId] || ""; }
function fromAppKit() {
  if (!accountState) return null;
  const caip = String(accountState.caipAddress || "");
  const parts = caip.split(":");
  const ns = parts[0] || String(accountState.namespace || "");
  const addr = String(accountState.address || parts[2] || "");
  if (ns === "eip155") {
    let chain = (parts[1] || String(accountState.chainId || "")).replace(/^eip155:/, "");
    if (chain.startsWith("0x")) chain = String(parseInt(chain, 16));
    const network = evmNetwork(chain);
    if (!addr || !network || !walletProviders.eip155) return null;
    return { namespace: ns, chainId: chain, address: addr, network, walletName: walletName(walletType()), provider: walletProviders.eip155, connectionType: "walletconnect_session" };
  }
  if (ns === "solana") {
    const chain = parts[1] || String(accountState.chainId || "");
    if (![SOLANA_MAINNET_ID, "mainnet", "mainnet-beta"].includes(chain) || !addr || !walletProviders.solana || typeof walletProviders.solana.signMessage !== "function") return null;
    return { namespace: ns, chainId: "mainnet-beta", address: addr, network: "Solana Mainnet", walletName: walletName(walletType()), provider: walletProviders.solana, connectionType: "walletconnect_session" };
  }
  return null;
}
function toBase64(value) { const bytes = value instanceof Uint8Array ? value : new Uint8Array(value); let binary = ""; for (const byte of bytes) binary += String.fromCharCode(byte); return btoa(binary); }
function injectedEvm(selected) {
  const list = []; const eth = window.ethereum;
  if (eth && Array.isArray(eth.providers)) list.push(...eth.providers);
  if (eth) list.push(eth);
  if (window.phantom && window.phantom.ethereum) list.push(window.phantom.ethereum);
  if (window.trustwallet && window.trustwallet.ethereum) list.push(window.trustwallet.ethereum);
  const unique = [...new Set(list)];
  if (selected === "metamask") return unique.find((p) => p.isMetaMask && !p.isBraveWallet) || null;
  if (selected === "phantom") return unique.find((p) => p.isPhantom) || null;
  if (selected === "trust_wallet") return unique.find((p) => p.isTrust || p.isTrustWallet) || null;
  return null;
}
async function connectInjected(selected) {
  if (selected === "phantom") {
    const sol = window.phantom && window.phantom.solana || window.solana;
    if (sol && typeof sol.connect === "function" && typeof sol.signMessage === "function") {
      const connected = await sol.connect(); const key = connected && connected.publicKey || sol.publicKey; const address = key && key.toString ? key.toString() : String(key || "");
      if (!address) throw new Error("Phantom did not return a public address.");
      injectedSession = { namespace: "solana", chainId: "mainnet-beta", address, network: "Solana Mainnet", walletName: "Phantom", provider: sol, connectionType: "injected_session" };
      return injectedSession;
    }
  }
  const provider = injectedEvm(selected);
  if (!provider || typeof provider.request !== "function") throw new Error("No matching injected wallet found. Open 9N8 inside that wallet app or enable Reown WalletConnect QR pairing.");
  const accounts = await provider.request({ method: "eth_requestAccounts" });
  const address = Array.isArray(accounts) ? String(accounts[0] || "") : "";
  if (!address) throw new Error("Wallet did not return an EVM address.");
  const raw = String(await provider.request({ method: "eth_chainId" }));
  const chain = raw.startsWith("0x") ? String(parseInt(raw, 16)) : raw;
  const network = evmNetwork(chain);
  if (!network) throw new Error("Switch the wallet to Ethereum, Base or BNB Smart Chain mainnet.");
  injectedSession = { namespace: "eip155", chainId: chain, address, network, walletName: walletName(selected), provider, connectionType: "injected_session" };
  return injectedSession;
}
function currentSession() { return injectedSession || fromAppKit(); }

connectWalletButton.addEventListener("click", async () => {
  connectWalletButton.disabled = true;
  const selected = walletType();
  walletStatus.textContent = "Opening " + walletName(selected) + " connection…";
  try {
    injectedSession = null;
    if (modal) { await modal.open(); walletStatus.textContent = "Choose and connect the wallet, then press Verify wallet ownership."; }
    else { const session = await connectInjected(selected); verifyWalletButton.disabled = false; walletStatus.textContent = session.walletName + " connected on " + session.network + ". Press Verify wallet ownership to sign the 9N8 message."; }
  } catch (error) { walletStatus.textContent = error.message || "Wallet connection failed."; }
  finally { connectWalletButton.disabled = false; }
});

verifyWalletButton.addEventListener("click", async () => {
  const session = currentSession();
  if (!session) { walletStatus.textContent = "Connect a supported wallet on an accepted mainnet first."; return; }
  verifyWalletButton.disabled = true; connectWalletButton.disabled = true;
  try {
    const challenge = await apiPost({ action: "wallet_challenge", namespace: session.namespace, address: session.address, provider: walletType(), connectionType: session.connectionType, chainId: session.chainId });
    walletStatus.textContent = "Approve the harmless 9N8 message signature in your wallet. It is not a transaction.";
    let signature;
    if (session.namespace === "eip155") signature = String(await session.provider.request({ method: "personal_sign", params: [challenge.message, session.address] }));
    else { const signed = await session.provider.signMessage(new TextEncoder().encode(challenge.message)); const raw = signed && signed.signature || signed; signature = typeof raw === "string" ? raw : toBase64(raw); }
    const result = await apiPost({ action: "wallet_verify", challengeId: challenge.challengeId, signature, walletName: session.walletName, connectionType: session.connectionType });
    verifiedSession = { ...session, network: result.network };
    disconnectWalletButton.disabled = false;
    walletStatus.textContent = result.walletName + " ownership verified · " + result.address + " · " + result.network + ". No transaction or trade permission was granted.";
  } catch (error) { walletStatus.textContent = error.message || "Wallet ownership verification failed."; verifyWalletButton.disabled = false; }
  finally { connectWalletButton.disabled = false; }
});

disconnectWalletButton.addEventListener("click", async () => {
  const session = verifiedSession || currentSession();
  if (!session) return;
  disconnectWalletButton.disabled = true;
  try {
    await apiPost({ action: "wallet_revoke", namespace: session.namespace, address: session.address, chainId: session.chainId, network: session.network });
    if (modal) { try { await modal.disconnect(); } catch (_) {} }
    if (session.provider && typeof session.provider.disconnect === "function") { try { await session.provider.disconnect(); } catch (_) {} }
    verifiedSession = null; injectedSession = null; verifyWalletButton.disabled = true;
    walletStatus.textContent = "Wallet link revoked. No future 9N8 requests will be sent for this wallet record.";
  } catch (error) { walletStatus.textContent = error.message || "Could not revoke wallet link."; disconnectWalletButton.disabled = false; }
});
