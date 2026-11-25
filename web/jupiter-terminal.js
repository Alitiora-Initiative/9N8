import { VersionedTransaction } from "@solana/web3.js";

const API = "https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot-next";
const SOL_MINT = "So11111111111111111111111111111111111111112";
const tg = window.Telegram && window.Telegram.WebApp;
const $ = (id) => document.getElementById(id);

let config = null;
let quote = null;
let triggerTokens = null;
let lastOrderId = null;

function status(message, kind = "info") {
  const target = $("jupiterSwapStatus");
  if (target) {
    target.textContent = message;
    target.dataset.kind = kind;
  }
}
function triggerStatus(message, kind = "info") {
  const target = $("jupiterTriggerStatus");
  if (target) {
    target.textContent = message;
    target.dataset.kind = kind;
  }
}
function b64ToBytes(value) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}
function bytesToB64(value) {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary);
}
function base58Encode(bytes) {
  const alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  let value = 0n;
  for (const byte of bytes) value = value * 256n + BigInt(byte);
  let output = "";
  while (value > 0n) {
    output = alphabet[Number(value % 58n)] + output;
    value /= 58n;
  }
  for (let i = 0; i < bytes.length && bytes[i] === 0; i++) output = "1" + output;
  return output || "1";
}
function currentWallet() {
  return typeof window.n8GetSolanaWalletSession === "function"
    ? window.n8GetSolanaWalletSession()
    : null;
}
async function post(body) {
  if (!tg?.initData) throw new Error("Open 9N8 from your private Telegram bot chat first.");
  const response = await fetch(API, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ...body, initData: tg.initData }),
    cache: "no-store",
    credentials: "omit",
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok !== true) {
    throw new Error(payload.message || payload.error || "Jupiter request failed.");
  }
  return payload;
}
function updateSideLabels() {
  const side = $("jupiterSide").value;
  $("jupiterAmountUnit").textContent = side === "buy" ? "SOL" : "token units";
  $("jupiterAmount").placeholder = side === "buy" ? "0.01" : "1000";
  quote = null;
  $("jupiterQuoteCard").classList.add("hidden");
  $("jupiterSignSwap").disabled = true;
}
function renderQuote(data) {
  $("jupiterQuoteInput").textContent = data.inputAmount + (data.side === "buy" ? " SOL" : " token");
  $("jupiterQuoteOutput").textContent = data.outputAmount + (data.side === "buy" ? " token" : " SOL");
  $("jupiterQuoteMinimum").textContent = data.minimumOutput + (data.side === "buy" ? " token" : " SOL");
  $("jupiterQuoteImpact").textContent = (Number(data.priceImpactPct) * 100).toFixed(3) + "%";
  $("jupiterQuoteRoute").textContent = "Router: " + String(data.router || "Jupiter") +
    (data.feeBps == null ? "" : " · fee: " + Number(data.feeBps).toFixed(2) + " bps");
  $("jupiterQuoteExpiry").textContent = "Expires " + new Date(data.expiresAt).toLocaleTimeString() +
    " · request " + String(data.requestId).slice(0, 12);
  $("jupiterQuoteCard").classList.remove("hidden");
}
async function loadConfig() {
  try {
    config = await post({ action: "jupiter_live_config" });
    const cert = config.certificationStatus === "passed";
    const manual = config.manualWalletExecutionEnabled === true;
    const delegated = config.delegatedAutomationEnabled === true;
    $("jupiterCertNotice").textContent = cert
      ? "Connector certification: PASSED. Manual wallet execution: " + (manual ? "enabled" : "still locked") +
        " · delegated automation: " + (delegated ? "enabled" : "still locked")
      : "LIVE EXECUTION LOCKED · Certification status: " + String(config.certificationStatus || "testing").toUpperCase() +
        ". Live quote review is available, but the wallet-sign/execute action remains disabled until signature, integrity, idempotency, reconciliation and withdrawal tests pass.";
    if (!config.triggerApiConfigured) {
      triggerStatus("Jupiter Trigger automation is not configured yet. Add JUPITER_API_KEY to the Supabase Edge Function Secrets before wallet login or vault creation.", "warning");
      $("jupiterTriggerLogin").disabled = true;
    } else {
      triggerStatus("Jupiter Trigger API key detected. Vault automation remains " +
        (delegated ? "available after wallet login." : "locked while delegated-execution certification is pending."), "info");
      $("jupiterTriggerLogin").disabled = false;
    }
    $("jupiterCreateDca").disabled = !delegated || !config.triggerApiConfigured;
    return config;
  } catch (error) {
    $("jupiterCertNotice").textContent = error.message || "Could not read Jupiter certification.";
    triggerStatus(error.message || "Could not read Trigger settings.");
    $("jupiterGetQuote").disabled = true;
    $("jupiterTriggerLogin").disabled = true;
    return null;
  }
}

$("jupiterSide").addEventListener("change", updateSideLabels);
updateSideLabels();

$("jupiterGetQuote").addEventListener("click", async () => {
  const wallet = currentWallet();
  if (!wallet) {
    status("Select Solana mainnet in the wallet network menu, connect Phantom/compatible wallet and verify wallet ownership first.", "warning");
    return;
  }
  const side = $("jupiterSide").value;
  const amount = String($("jupiterAmount").value || "").trim();
  const tokenMint = String($("jupiterTokenMint").value || "").trim();
  if (!amount || !tokenMint) {
    status("Enter a token mint and an amount to quote.", "warning");
    return;
  }
  $("jupiterGetQuote").disabled = true;
  $("jupiterSignSwap").disabled = true;
  status("Requesting a live Jupiter route and unsigned wallet transaction…");
  try {
    const result = await post({
      action: "jupiter_swap_order",
      walletConnectionId: wallet.walletConnectionId,
      address: wallet.address,
      side,
      tokenMint,
      amount,
    });
    quote = result;
    lastOrderId = result.orderId;
    renderQuote(result);
    const canExecute = config?.manualWalletExecutionEnabled === true;
    $("jupiterSignSwap").disabled = !canExecute;
    status(canExecute
      ? "Live quote ready. Verify the amounts and route, then sign only if you approve this exact swap."
      : "Quote ready, but real submission is still locked by Jupiter live-execution certification. No transaction has been signed or sent.", canExecute ? "success" : "warning");
  } catch (error) {
    quote = null;
    $("jupiterQuoteCard").classList.add("hidden");
    status(error.message || "Jupiter could not produce a swap quote.", "error");
  } finally {
    $("jupiterGetQuote").disabled = false;
  }
});

$("jupiterSignSwap").addEventListener("click", async () => {
  const wallet = currentWallet();
  if (!wallet || !quote) {
    status("Connect and verify your Solana wallet, then request a new quote.", "warning");
    return;
  }
  if (config?.manualWalletExecutionEnabled !== true) {
    status("Live execution is locked until connector certification is passed. No wallet signature was requested.", "warning");
    return;
  }
  if (new Date(quote.expiresAt).getTime() <= Date.now()) {
    status("This Jupiter quote has expired. Request a fresh quote.", "warning");
    $("jupiterSignSwap").disabled = true;
    return;
  }
  $("jupiterSignSwap").disabled = true;
  $("jupiterGetQuote").disabled = true;
  try {
    status("Opening your wallet to sign the exact Jupiter transaction. Read the wallet confirmation before approving…");
    const unsigned = VersionedTransaction.deserialize(b64ToBytes(quote.transaction));
    if (unsigned.message.version !== 0) throw new Error("This wallet terminal currently accepts version-0 transactions only.");
    const signed = await wallet.signTransaction(unsigned);
    if (!signed || typeof signed.serialize !== "function") throw new Error("Wallet returned an invalid signed transaction.");
    const signedTransaction = bytesToB64(signed.serialize());
    status("Wallet signature received. Verifying the signed message and submitting once through Jupiter…");
    const result = await post({
      action: "jupiter_swap_execute",
      orderId: quote.orderId,
      requestId: quote.requestId,
      signedTransaction,
    });
    lastOrderId = quote.orderId;
    if (result.status === "success") {
      status("SWAP CONFIRMED · " + result.signature + " · " + (result.explorerUrl || ""), "success");
    } else if (result.status === "failed") {
      status("Jupiter reports that the swap failed. " + (result.signature || ""), "error");
    } else {
      status("EXECUTION STATUS UNKNOWN · " + result.signature + ". 9N8 will reconcile the transaction. Do not retry or send another swap yet.", "warning");
      $("jupiterRefreshSwap").disabled = false;
    }
    quote = null;
  } catch (error) {
    status(error.message || "Wallet signing/execution did not complete. Request a new quote before another attempt.", "error");
  } finally {
    $("jupiterSignSwap").disabled = true;
    $("jupiterGetQuote").disabled = false;
  }
});

$("jupiterRefreshSwap").addEventListener("click", async () => {
  if (!lastOrderId) return;
  $("jupiterRefreshSwap").disabled = true;
  status("Checking the transaction signature on Solana. This does not resubmit it…");
  try {
    const result = await post({ action: "jupiter_swap_status", orderId: lastOrderId });
    const order = result.order || {};
    if (order.status === "success") {
      status("SWAP CONFIRMED · " + order.signature, "success");
      if (result.explorerUrl) status("SWAP CONFIRMED · " + result.explorerUrl, "success");
      $("jupiterRefreshSwap").disabled = true;
    } else if (order.status === "failed") {
      status("Solana reports that the transaction failed. It was not resubmitted.", "error");
      $("jupiterRefreshSwap").disabled = true;
    } else if (order.status === "expired") {
      status("The unsigned quote expired. No swap was submitted.", "warning");
      $("jupiterRefreshSwap").disabled = true;
    } else {
      status("Still waiting for final on-chain status. Do not submit the same order again.", "warning");
      $("jupiterRefreshSwap").disabled = false;
    }
  } catch (error) {
    status(error.message || "Could not refresh transaction status.", "error");
    $("jupiterRefreshSwap").disabled = false;
  }
});

function getTriggerToken() {
  return triggerTokens && triggerTokens.expiresAt &&
    new Date(triggerTokens.expiresAt).getTime() > Date.now() + 15000
    ? triggerTokens
    : null;
}
async function connectJupiterAutomation() {
  const wallet = currentWallet();
  if (!wallet) throw new Error("Connect and verify a Solana-mainnet wallet first.");
  triggerStatus("Requesting a wallet-bound Jupiter Trigger challenge…");
  const challenge = await post({
    action: "jupiter_trigger_challenge",
    walletConnectionId: wallet.walletConnectionId,
    address: wallet.address,
  });
  triggerStatus("Sign the Jupiter authentication message in your wallet. This is not a transaction or deposit.");
  const signed = await wallet.provider.signMessage(new TextEncoder().encode(challenge.challenge));
  const sigBytes = signed?.signature || signed;
  const signature = base58Encode(sigBytes instanceof Uint8Array ? sigBytes : new Uint8Array(sigBytes));
  const auth = await post({
    action: "jupiter_trigger_verify",
    walletConnectionId: wallet.walletConnectionId,
    address: wallet.address,
    signature,
  });
  triggerTokens = {
    accessToken: auth.accessToken,
    refreshToken: auth.refreshToken,
    expiresAt: auth.expiresAt,
  };
  const vault = await post({
    action: "jupiter_trigger_vault",
    walletConnectionId: wallet.walletConnectionId,
    address: wallet.address,
    accessToken: auth.accessToken,
  });
  $("jupiterTriggerHistory").disabled = false;
  $("jupiterDcaControls").classList.remove("hidden");
  triggerStatus(vault.registered
    ? "Jupiter vault detected. Delegated automation still remains locked until certification passes."
    : "Jupiter vault registration was created. This has not deposited funds. Delegated automation remains locked until certification passes.", "success");
  await refreshTriggerHistory();
}
async function refreshTriggerHistory() {
  const wallet = currentWallet();
  if (!wallet) throw new Error("Reconnect your verified Solana wallet.");
  let token = getTriggerToken();
  if (!token && triggerTokens?.refreshToken) {
    const refreshed = await post({
      action: "jupiter_trigger_refresh",
      walletConnectionId: wallet.walletConnectionId,
      address: wallet.address,
      refreshToken: triggerTokens.refreshToken,
    });
    triggerTokens = {
      accessToken: refreshed.accessToken,
      refreshToken: refreshed.refreshToken,
      expiresAt: refreshed.expiresAt,
    };
    token = getTriggerToken();
  }
  if (!token) throw new Error("Reconnect to Jupiter automation to refresh its short-lived wallet session.");
  const result = await post({
    action: "jupiter_trigger_history",
    walletConnectionId: wallet.walletConnectionId,
    address: wallet.address,
    accessToken: token.accessToken,
  });
  $("jupiterDcaHistory").textContent = JSON.stringify(result.orders, null, 2);
}
$("jupiterTriggerLogin").addEventListener("click", async () => {
  $("jupiterTriggerLogin").disabled = true;
  try { await connectJupiterAutomation(); }
  catch (error) { triggerStatus(error.message || "Jupiter authentication failed.", "error"); }
  finally { $("jupiterTriggerLogin").disabled = !config?.triggerApiConfigured; }
});
$("jupiterTriggerHistory").addEventListener("click", async () => {
  $("jupiterTriggerHistory").disabled = true;
  try { await refreshTriggerHistory(); }
  catch (error) { triggerStatus(error.message || "Could not fetch DCA order history.", "error"); }
  finally { $("jupiterTriggerHistory").disabled = !getTriggerToken(); }
});
$("jupiterCreateDca").addEventListener("click", async () => {
  const wallet = currentWallet();
  const token = getTriggerToken();
  if (!wallet || !token) {
    triggerStatus("Reconnect your verified Solana wallet and Jupiter Trigger session.", "warning");
    return;
  }
  if (config?.delegatedAutomationEnabled !== true) {
    triggerStatus("Delegated automation remains locked until Trigger vault, DCA, cancellation and withdrawal certification has passed. No deposit was created.", "warning");
    return;
  }
  const mint = String($("jupiterDcaMint").value || "").trim();
  const total = Number($("jupiterDcaTotal").value);
  const count = Number($("jupiterDcaCount").value);
  const interval = Number($("jupiterDcaInterval").value);
  if (!mint || !Number.isFinite(total) || total <= 0 || !Number.isInteger(count) || count < 2) {
    triggerStatus("Enter a valid mint, total SOL and at least two DCA rounds.", "warning");
    return;
  }
  const totalLamports = BigInt(Math.floor(total * 1e9)).toString();
  $("jupiterCreateDca").disabled = true;
  try {
    triggerStatus("Crafting a wallet-signed deposit. Review the Jupiter vault receiver and amount before signing…");
    const deposit = await post({
      action: "jupiter_trigger_deposit_craft",
      walletConnectionId: wallet.walletConnectionId,
      address: wallet.address,
      accessToken: token.accessToken,
      orderType: "dca",
      inputMint: SOL_MINT,
      outputMint: mint,
      amount: totalLamports,
    });
    const tx = VersionedTransaction.deserialize(b64ToBytes(deposit.deposit.transaction));
    if (tx.message.version !== 0) throw new Error("Unsupported deposit transaction version.");
    const confirmed = window.confirm(
      "JUPITER AUTOMATION DEPOSIT\n\n" +
      "Input: " + total + " SOL\n" +
      "Token mint: " + mint + "\n" +
      "Jupiter vault receiver: " + deposit.deposit.receiverAddress + "\n" +
      "Rounds: " + count + "\n" +
      "Interval: " + interval + " seconds\n\n" +
      "This moves the deposited SOL into a Jupiter/Privy-managed vault, not your original wallet. Continue to wallet signing?"
    );
    if (!confirmed) throw new Error("Deposit cancelled before signing.");
    const signed = await wallet.signTransaction(tx);
    const depositSignedTx = bytesToB64(signed.serialize());
    const created = await post({
      action: "jupiter_trigger_create_dca",
      walletConnectionId: wallet.walletConnectionId,
      address: wallet.address,
      accessToken: token.accessToken,
      depositRequestId: deposit.deposit.requestId,
      depositSignedTx,
      inputAmount: totalLamports,
      orderCount: count,
      intervalSeconds: interval,
    });
    triggerStatus("DCA order created: " + created.orderId + ". Check the order history for keeper fills and remaining vault balance.", "success");
    await refreshTriggerHistory();
  } catch (error) {
    triggerStatus(error.message || "Could not create the DCA order.", "error");
  } finally {
    $("jupiterCreateDca").disabled = config?.delegatedAutomationEnabled !== true;
  }
});

window.addEventListener("n8-wallet-session", () => {
  quote = null;
  lastOrderId = null;
  $("jupiterQuoteCard").classList.add("hidden");
  $("jupiterSignSwap").disabled = true;
  $("jupiterRefreshSwap").disabled = true;
  status("Wallet session updated. Get a fresh quote for this verified Solana wallet.");
  triggerTokens = null;
  $("jupiterTriggerHistory").disabled = true;
});
loadConfig();
