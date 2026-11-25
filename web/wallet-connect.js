import { createAppKit } from "@reown/appkit";
import { mainnet, base, bsc, solana } from "@reown/appkit/networks";
import { WagmiAdapter } from "@reown/appkit-adapter-wagmi";
import { SolanaAdapter } from "@reown/appkit-adapter-solana";

const API = "https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot-next";
const telegram = window.Telegram && window.Telegram.WebApp;
const connectButton = document.getElementById("connectWalletSession");
const verifyButton = document.getElementById("verifyWalletSession");
const disconnectButton = document.getElementById("disconnectWalletSession");
const sessionStatus = document.getElementById("walletSessionStatus");
const networkSelect = document.getElementById("network");

let modal = null;
let reownProjectId = null;
let currentAddress = "";
let currentProviderState = null;
let appKitReady = false;
let connectionBusy = false;
let injectedSession = null;

const evmNetworks = [mainnet, base, bsc];
const networkById = {
  "1": mainnet,
  "8453": base,
  "56": bsc
};
const networkLabels = {
  "1": "Ethereum Mainnet",
  "8453": "Base Mainnet",
  "56": "BNB Smart Chain",
  "mainnet-beta": "Solana Mainnet"
};

function setStatus(message, kind = "info") {
  if (!sessionStatus) return;
  sessionStatus.textContent = message;
  sessionStatus.dataset.kind = kind;
}
function shortAddress(value) {
  return value && value.length > 16 ? value.slice(0, 8) + "…" + value.slice(-6) : value;
}
function bytesToHex(text) {
  return "0x" + Array.from(new TextEncoder().encode(text))
    .map((value) => value.toString(16).padStart(2, "0")).join("");
}
function bytesToBase64(value) {
  const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
function normalizeEvmChainId(value) {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  const text = String(value ?? "");
  if (/^0x[0-9a-f]+$/i.test(text)) return String(parseInt(text, 16));
  if (/^\d+$/.test(text)) return text;
  return "";
}
async function post(body) {
  if (!telegram?.initData) {
    throw new Error("Open 9N8 Connect from your Telegram private chat to attach this wallet to your account.");
  }
  const response = await fetch(API, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ...body, initData: telegram.initData }),
    cache: "no-store",
    credentials: "omit"
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok !== true) {
    throw new Error(payload.message || payload.error || "Wallet request rejected.");
  }
  return payload;
}
function mapWalletProvider(name) {
  const value = String(name || "").toLowerCase();
  if (value.includes("phantom")) return "phantom";
  if (value.includes("trust")) return "trust_wallet";
  if (value.includes("metamask")) return "metamask";
  return "walletconnect";
}
function getWalletConnectionType() {
  const value = String(modal?.getWalletProviderType?.() || "").toLowerCase();
  return value.includes("injected") ? "injected_session" : "walletconnect_session";
}
function setWalletState(state) {
  currentProviderState = state || null;
  const isConnected = Boolean(state?.isConnected || modal?.getIsConnected?.());
  currentAddress = String(state?.address || modal?.getAddress?.() || "");
  if (isConnected && currentAddress) {
    verifyButton.disabled = false;
    disconnectButton.disabled = false;
    setStatus("Wallet connected: " + shortAddress(currentAddress) + ". Verify ownership to save this session to your 9N8 account.", "success");
  } else {
    verifyButton.disabled = true;
    disconnectButton.disabled = true;
    currentAddress = "";
  }
}
function injectedEvmCandidates() {
  const candidates = [];
  const ethereum = window.ethereum;
  if (Array.isArray(ethereum?.providers)) candidates.push(...ethereum.providers);
  if (ethereum) candidates.push(ethereum);
  if (window.phantom?.ethereum) candidates.push(window.phantom.ethereum);
  if (window.trustwallet?.ethereum) candidates.push(window.trustwallet.ethereum);
  return [...new Set(candidates)];
}
function injectedWalletName(provider) {
  if (provider?.isMetaMask && !provider?.isBraveWallet) return "MetaMask";
  if (provider?.isTrust || provider?.isTrustWallet) return "Trust Wallet";
  if (provider?.isPhantom) return "Phantom";
  return "Injected wallet";
}
async function connectInjectedWallet() {
  const selected = networkSelect?.value || "eip155:1";
  if (selected.startsWith("solana:")) {
    const provider = window.phantom?.solana || window.solana;
    if (!provider || typeof provider.connect !== "function" || typeof provider.signMessage !== "function") {
      throw new Error("No injected Solana wallet was detected. Open 9N8 inside Phantom or use WalletConnect QR pairing.");
    }
    const connected = await provider.connect();
    const key = connected?.publicKey || provider.publicKey;
    const address = key?.toString?.() || String(key || "");
    if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(address)) throw new Error("The injected wallet did not return a valid Solana address.");
    injectedSession = { namespace: "solana", chainId: "mainnet-beta", address, network: "Solana Mainnet", walletName: "Phantom", provider };
    return injectedSession;
  }
  const candidates = injectedEvmCandidates();
  const provider = candidates.find((item) => item.isMetaMask && !item.isBraveWallet) || candidates.find((item) => item.isTrust || item.isTrustWallet) || candidates[0];
  if (!provider || typeof provider.request !== "function") throw new Error("No injected EVM wallet was detected. Open 9N8 inside MetaMask or Trust Wallet, or configure WalletConnect.");
  const accounts = await provider.request({ method: "eth_requestAccounts" });
  const address = Array.isArray(accounts) ? String(accounts[0] || "") : "";
  if (!/^0x[a-fA-F0-9]{40}$/.test(address)) throw new Error("The injected wallet did not return a valid EVM address.");
  const chainId = normalizeEvmChainId(await provider.request({ method: "eth_chainId" }));
  const expected = selected.split(":")[1];
  if (expected && chainId !== expected) throw new Error("Switch your wallet to the selected network, then connect again.");
  const network = networkLabels[chainId] || "";
  if (!network) throw new Error("The connected EVM network is not supported by 9N8.");
  injectedSession = { namespace: "eip155", chainId, address, network, walletName: injectedWalletName(provider), provider };
  return injectedSession;
}
async function initialize() {
  if (!connectButton || !verifyButton || !disconnectButton) return;
  if (!telegram?.initData) {
    connectButton.disabled = true;
    setStatus("Open this page from the Connect button inside your 9N8 Telegram private chat.", "warning");
    return;
  }
  connectButton.disabled = true;
  setStatus("Checking WalletConnect configuration…");
  try {
    const response = await fetch(API + "?view=wallet-config", { cache: "no-store", credentials: "omit" });
    const config = await response.json();
    if (!response.ok || config.ok !== true) throw new Error("Could not read wallet connection settings.");
    reownProjectId = config.reownProjectId;
    if (!reownProjectId) {
      connectButton.disabled = false;
      setStatus("WalletConnect QR is not configured. Open 9N8 inside MetaMask, Trust Wallet or Phantom to use the injected-wallet connection.", "warning");
      return;
    }
    const wagmiAdapter = new WagmiAdapter({ projectId: reownProjectId, networks: evmNetworks });
    const solanaAdapter = new SolanaAdapter();
    modal = createAppKit({
      adapters: [wagmiAdapter, solanaAdapter],
      networks: [...evmNetworks, solana],
      projectId: reownProjectId,
      metadata: {
        name: "9N8 Trading Intelligence",
        description: "Connect a wallet to 9N8. This connection does not approve trades or transfers.",
        url: location.origin,
        icons: []
      },
      features: { analytics: false, email: false, socials: [] },
      themeMode: "dark"
    });
    modal.subscribeProvider((state) => setWalletState(state));
    modal.subscribeWalletInfo?.(({ name }) => {
      const info = name ? "Connected wallet: " + name + "." : "";
      if (info && currentAddress) setStatus(info + " Verify ownership to link it to 9N8.", "success");
    });
    if (modal.getIsConnected?.()) {
      setWalletState({
        isConnected: true,
        address: modal.getAddress?.(),
        provider: modal.getWalletProvider?.(),
        chainId: modal.getChainId?.()
      });
    }
    appKitReady = true;
    connectButton.disabled = false;
    setStatus("WalletConnect ready. Choose a network, then connect Phantom, Trust Wallet, MetaMask or another supported wallet.", "success");
  } catch (error) {
    connectButton.disabled = true;
    setStatus(error?.message || "Wallet connection initialization failed.", "error");
  }
}

connectButton?.addEventListener("click", async () => {
  const selected = networkSelect?.value || "eip155:1";
  const namespace = selected.startsWith("solana:") ? "solana" : "eip155";
  try {
    if (appKitReady && modal) {
      if (namespace === "solana") {
        await modal.open({ view: "Connect", namespace: "solana" });
      } else {
        const chainId = selected.split(":")[1];
        const target = networkById[chainId] || mainnet;
        try { await modal.switchNetwork(target); } catch (_) {}
        await modal.open({ view: "Connect", namespace: "eip155" });
      }
    } else {
      const session = await connectInjectedWallet();
      currentAddress = session.address;
      verifyButton.disabled = false;
      disconnectButton.disabled = false;
      setStatus(session.walletName + " connected: " + shortAddress(session.address) + " · " + session.network + ". Verify ownership to link it to 9N8.", "success");
    }
  } catch (error) {
    setStatus(error?.message || "Wallet connection was not completed.", "error");
  }
});

verifyButton?.addEventListener("click", async () => {
  if ((!appKitReady || !modal) && !injectedSession) return;
  if (connectionBusy) return;
  connectionBusy = true;
  verifyButton.disabled = true;
  try {
    const selected = networkSelect?.value || "eip155:1";
    const namespace = injectedSession?.namespace || (selected.startsWith("solana:") ? "solana" : "eip155");
    const providers = modal?.getProviders?.() || {};
    const provider = injectedSession?.provider || (namespace === "solana" ? providers.solana : providers.eip155);
    const address = String(injectedSession?.address || modal?.getAddress?.() || currentProviderState?.address || "");
    if (!provider || !address) throw new Error("No connected wallet provider or address was found. Reopen the wallet connection.");
    let chainId = "";
    if (namespace === "solana") {
      chainId = "mainnet-beta";
      if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(address)) throw new Error("The connected Solana address could not be validated.");
    } else {
      chainId = injectedSession?.chainId || normalizeEvmChainId(modal?.getChainId?.() || currentProviderState?.chainId);
      const expected = selected.split(":")[1];
      if (!chainId) throw new Error("Could not determine the connected EVM chain.");
      if (expected && chainId !== expected) throw new Error("Your wallet is on chain " + chainId + ". Switch to the selected network and reconnect.");
      if (!/^0x[a-fA-F0-9]{40}$/.test(address)) throw new Error("The connected EVM address is invalid.");
    }
    const walletInfo = modal?.getWalletInfo?.() || {};
    const walletName = injectedSession?.walletName || String(walletInfo.name || "WalletConnect");
    const providerName = injectedSession ? mapWalletProvider(walletName) : mapWalletProvider(walletName);
    const connectionType = injectedSession ? "injected_session" : getWalletConnectionType();
    setStatus("Creating a one-time ownership challenge…");
    const challenge = await post({ action: "wallet_challenge", provider: providerName, connectionType, namespace, chainId, address });
    setStatus("Approve the ownership message inside your wallet. It does not authorize a transaction…");
    let signature;
    if (namespace === "eip155") {
      if (typeof provider.request !== "function") throw new Error("The connected EVM wallet cannot sign a message.");
      signature = await provider.request({ method: "personal_sign", params: [bytesToHex(challenge.message), address] });
    } else {
      if (typeof provider.signMessage !== "function") throw new Error("The connected Solana wallet cannot sign a message.");
      const signed = await provider.signMessage(new TextEncoder().encode(challenge.message));
      const signatureBytes = signed?.signature || signed;
      signature = bytesToBase64(signatureBytes);
    }
    const result = await post({ action: "wallet_verify", challengeId: challenge.challengeId, signature, walletName, connectionType });
    disconnectButton.disabled = false;
    setStatus("Verified and linked: " + result.address + " · " + result.network + ". Execution remains approval-only; no transaction permission was granted.", "success");
    const status = document.getElementById("walletStatus");
    if (status) status.textContent = "Verified wallet session: " + result.address + " · " + result.network + ". No wallet transaction was sent.";
  } catch (error) {
    setStatus(error?.message || "Wallet ownership verification failed.", "error");
  } finally {
    connectionBusy = false;
    verifyButton.disabled = injectedSession ? false : !modal?.getIsConnected?.();
  }
});
disconnectButton?.addEventListener("click", async () => {
  if ((!modal && !injectedSession) || connectionBusy) return;
  connectionBusy = true;
  disconnectButton.disabled = true;
  try {
    const selected = networkSelect?.value || "eip155:1";
    const namespace = injectedSession?.namespace || (selected.startsWith("solana:") ? "solana" : "eip155");
    const address = String(injectedSession?.address || modal?.getAddress?.() || currentProviderState?.address || "");
    const chainId = injectedSession?.chainId || (namespace === "solana" ? "mainnet-beta" : normalizeEvmChainId(modal?.getChainId?.() || currentProviderState?.chainId));
    const network = injectedSession?.network || (namespace === "solana" ? "Solana Mainnet" : networkLabels[chainId] || "");
    if (address && network) await post({ action: "wallet_revoke", namespace, address, chainId, network });
    if (modal) { try { await modal.adapter?.connectionControllerClient?.disconnect?.(); } catch (_) {} }
    if (injectedSession?.provider?.disconnect) { try { await injectedSession.provider.disconnect(); } catch (_) {} }
    injectedSession = null;
    currentAddress = "";
    verifyButton.disabled = true;
    disconnectButton.disabled = true;
    setStatus("Wallet session disconnected and its 9N8 link revoked.", "success");
  } catch (error) {
    setStatus(error?.message || "Could not revoke the wallet link.", "error");
    disconnectButton.disabled = false;
  } finally {
    connectionBusy = false;
  }
});
networkSelect?.addEventListener("change", () => {
  if (modal?.getIsConnected?.() || injectedSession) {
    setStatus("Network selection changed. Reconnect or switch the wallet to the selected network before verifying.", "warning");
    verifyButton.disabled = true;
  }
});

initialize();
