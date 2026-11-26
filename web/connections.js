const API = "https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot-next";
const tg = window.Telegram && window.Telegram.WebApp;
const $ = (id) => document.getElementById(id);
const providerSelect = $("provider");
const passphraseFields = $("okxPassphraseFields");
const passphraseInput = $("okxPassphrase");
const bybitRegionFields = $("bybitRegionFields");
const bybitRegionInput = $("bybitRegion");
bybitRegionInput.addEventListener("change", syncExchangeFields);
const saveButton = $("saveExchange");
const exchangeStatus = $("exchangeStatus");
const balanceButton = $("loadBinanceBalance");
const balanceTarget = $("binanceBalance");
const marketHint = $("marketPermissionHint");
const autoDetectRow = $("binanceAutoDetect");
const autoDetectInput = $("autoDetectBinanceMarkets");
const marketFieldset = $("marketPermissions");
const marketInputs = () => Array.from(document.querySelectorAll('input[name="exchangeMarket"]'));

async function apiPost(body) {
  if (!tg || !tg.initData) throw new Error("Open 9N8 from your Telegram private chat first.");
  const response = await fetch(API, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ...body, initData: tg.initData }),
    cache: "no-store",
    credentials: "omit",
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok !== true) throw new Error(payload.message || payload.error || "Request rejected.");
  return payload;
}

function syncExchangeFields() {
  const provider = providerSelect.value;
  const isOkx = provider === "okx";
  const isBinance = provider === "binance";
  passphraseFields.classList.toggle("hidden", !isOkx);
  bybitRegionFields.classList.toggle("hidden", provider !== "bybit");
  $("bybitRegionNote").classList.toggle("hidden", provider !== "bybit" || bybitRegionInput.value !== "eea");
  passphraseInput.required = isOkx;
  autoDetectRow.classList.toggle("hidden", !isBinance);
  const autoDetect = isBinance && autoDetectInput.checked;
  marketFieldset.classList.toggle("hidden", autoDetect);

  const stocks = $("stocksMarketOption");
  stocks.classList.add("hidden");
  const stockInput = stocks.querySelector("input");
  stockInput.disabled = true;
  stockInput.checked = false;

  if (isBinance) {
    marketHint.textContent = autoDetect
      ? "Automatic mode reads the Binance API-key switches and account-level product flags, then reports which products are enabled. USDⓈ-M and COIN-M share Binance’s Futures key permission."
      : "Manual mode checks only the selected products against the Binance API-key switches and account-level product flags.";
    saveButton.textContent = "Check Binance permissions and save";
    exchangeStatus.textContent = "Choose the markets you want. Keep Withdraw, Internal Transfer and Universal Transfer turned off.";
  } else if (isOkx) {
    marketHint.textContent = "OKX has one broad Trade permission, not a separate API-key switch for each product. The account mode and available instruments still matter. Trade permission may also allow internal funding transfers; 9N8 does not use transfer endpoints.";
    saveButton.textContent = "Check OKX permissions and save";
    exchangeStatus.textContent = "Enter the API key, secret and the passphrase you created with the key. Choose your OKX account region.";
  } else {
    marketHint.textContent = "Bybit uses one API key for Spot and derivatives. Choose the region where your account was opened. Spot, Futures and Options are checked against Bybit’s returned permissions. Margin availability also depends on your Unified Account settings.";
    saveButton.textContent = "Check Bybit permissions and save";
    exchangeStatus.textContent = "Bybit needs an API key and secret. Do not enable Wallet, Account Transfer or Withdraw permissions.";
  }
}

providerSelect.addEventListener("change", syncExchangeFields);
autoDetectInput.addEventListener("change", syncExchangeFields);
syncExchangeFields();

function explainPermissionStatus(value) {
  const text = String(value || "");
  if (text === "verified") return "verified";
  if (text === "shared_spot_margin_stock_permission_verified_availability_varies") return "permission verified; product availability varies by region";
  if (text === "spot_trade_scope_present_account_margin_mode_must_be_enabled") return "Spot permission verified; enable margin in your exchange account";
  if (text === "trade_scope_verified_spot_borrow_enabled") return "Trade permission and spot borrowing reported enabled";
  if (text === "trade_scope_verified_account_margin_setting_must_be_checked") return "Trade permission verified; check your margin setting in OKX";
  if (text === "trade_scope_verified_account_mode_compatible") return "Trade permission verified; account mode appears compatible";
  if (text === "trade_scope_verified_account_mode_must_be_changed_in_okx") return "Trade permission verified; change your account mode in OKX before using this product";
  return text.replaceAll("_", " ");
}

$("exchangeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const provider = providerSelect.value;
  const autoDetectMarkets = provider === "binance" && autoDetectInput.checked;
  const requestedMarkets = marketInputs().filter((input) => input.checked && !input.disabled).map((input) => input.value);
  if (!autoDetectMarkets && !requestedMarkets.length) {
    exchangeStatus.textContent = "Choose at least one market first.";
    return;
  }

  const credentials = {
    apiKey: $("apiKey").value.trim(),
    apiSecret: $("apiSecret").value.trim(),
  };
  if (provider === "okx") {
    credentials.passphrase = passphraseInput.value;
    credentials.region = $("okxRegion").value;
  }
  if (provider === "bybit") {
    credentials.bybitRegion = bybitRegionInput.value;
  }

  saveButton.disabled = true;
  exchangeStatus.textContent = "Checking the permissions with your exchange…";
  try {
    const request = {
      action: "connect_exchange",
      provider,
      environment: "live",
      autoDetectMarkets,
      credentials,
    };
    if (!autoDetectMarkets) request.requestedMarkets = requestedMarkets;
    const result = await apiPost(request);
    window.n8ActiveExchangeConnectionId = result.connectionId || "";
    window.n8ActiveExchangeProvider = provider;
    const permissions = result.permissions || {};
    const marketPermissions = permissions.marketPermissions || {};
    const details = Object.entries(marketPermissions).map(([market, status]) =>
      market.replaceAll("_", " ").toUpperCase() + ": " + explainPermissionStatus(status)
    );
    const providerName = provider === "okx" ? "OKX" : provider === "bybit" ? "Bybit" : "Binance";
    const key = permissions.keyPermissions || {};
    const account = permissions.accountProducts || {};
    const flag = (value) => value === true ? "ON" : value === false ? "OFF" : "unknown";
    const inventory = provider === "binance" ? [
      "API key — Read " + flag(key.reading) + "; IP restriction " + flag(key.ipRestricted) +
        "; Spot/Margin " + flag(key.spotAndMarginTrading) + "; Margin " + flag(key.margin) +
        "; Futures " + flag(key.futures) + "; Options " + flag(key.vanillaOptions) +
        "; Portfolio Margin " + flag(key.portfolioMarginTrading) + "; Withdrawals " + flag(key.withdrawals) +
        "; Internal Transfer " + flag(key.internalTransfer) + "; Universal Transfer " + flag(key.universalTransfer) +
        "; FIX Trade " + flag(key.fixApiTrade) + "; FIX Read-only " + flag(key.fixReadOnly) + ".",
      "Account — Margin " + flag(account.marginEnabled) + "; Futures " + flag(account.futuresEnabled) +
        "; Options " + flag(account.optionsEnabled) + "; Portfolio Margin " + flag(account.portfolioMarginEnabled) + ".",
      permissions.futuresScopeNote || "",
      permissions.stockProductsNote || "",
      "Portfolio Margin status: " + String(permissions.portfolioMarginStatus || "not reported"),
    ].filter(Boolean) : [];
    const selectedSummary = autoDetectMarkets
      ? "Auto-detected products: " + ((permissions.detectedMarkets || []).join(", ") || "no trade product scopes")
      : "Selected: " + requestedMarkets.map((market) => market.replaceAll("_", " ")).join(", ");
    exchangeStatus.textContent = [
      providerName + " account access checked and saved.",
      selectedSummary,
      ...inventory,
      ...details,
      permissions.tradeScopeWarning ? "Important: " + permissions.tradeScopeWarning : "",
      permissions.marginNote ? "Margin: " + permissions.marginNote : "",
      "No exchange orders are placed by connecting this key. Real trading is still locked.",
    ].filter(Boolean).join("\n");
    $("exchangeForm").reset();
    providerSelect.value = provider;
    autoDetectInput.checked = true;
    syncExchangeFields();
    balanceTarget.textContent = "Connection saved. Refresh balances to read the connected exchange account.";
  } catch (error) {
    exchangeStatus.textContent = error.message || "Exchange connection failed.";
  } finally {
    credentials.apiKey = "";
    credentials.apiSecret = "";
    credentials.passphrase = "";
    $("apiKey").value = "";
    $("apiSecret").value = "";
    passphraseInput.value = "";
    saveButton.disabled = false;
  }
});

balanceButton.addEventListener("click", async () => {
  const provider = providerSelect.value;
  balanceButton.disabled = true;
  const providerName = provider === "okx" ? "OKX" : provider === "bybit" ? "Bybit" : "Binance";
  balanceTarget.textContent = "Loading private " + providerName + " account balances…";
  try {
    const action = provider === "okx" ? "okx_account" : provider === "bybit" ? "bybit_account" : "binance_account";
    const result = await apiPost({ action });
    window.n8ActiveExchangeConnectionId = result.connection && result.connection.id || "";
    window.n8ActiveExchangeProvider = provider;
    const balances = Array.isArray(result.balances) ? result.balances : [];
    balanceTarget.replaceChildren();
    if (!balances.length) balanceTarget.textContent = "No non-zero balances returned.";
    for (const item of balances) {
      const row = document.createElement("div");
      row.className = "book-row";
      const asset = document.createElement("b");
      asset.textContent = String(item.asset || "");
      const amount = document.createElement("span");
      amount.textContent = "Free " + Number(item.free || 0).toLocaleString("en-US", { maximumFractionDigits: 8 }) +
        " · Locked " + Number(item.locked || 0).toLocaleString("en-US", { maximumFractionDigits: 8 });
      row.append(asset, amount);
      balanceTarget.append(row);
    }
    if (result.quoteBalance) {
      const row = document.createElement("div");
      row.className = "book-row";
      const label = document.createElement("b");
      label.textContent = "USDT quote balance";
      const amount = document.createElement("span");
      amount.textContent = "Free " + Number(result.quoteBalance.free || 0).toFixed(4) +
        " · Risk-gate USD estimate $" + Number(result.quoteBalance.availableUsdApprox || 0).toFixed(2);
      row.append(label, amount);
      balanceTarget.append(row);
    }
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = result.note || "Account balances only. Connecting an API key does not place a trade.";
    balanceTarget.append(note);
  } catch (error) {
    balanceTarget.textContent = error.message || "Could not fetch exchange balances.";
  } finally {
    balanceButton.disabled = false;
  }
});
