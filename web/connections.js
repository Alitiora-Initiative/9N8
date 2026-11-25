
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

