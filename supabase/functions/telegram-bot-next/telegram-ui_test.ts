import { buildEngineStatusText, buildLiveStatusKeyboard, buildMainKeyboard, buildManualQuickSnipeKeyboard, buildNetworkSelectorKeyboard, buildNetworkSelectorText } from "./telegram-ui.ts";

function flatten(keyboard: { inline_keyboard: { text: string; callback_data?: string; web_app?: { url: string } }[][] }) {
  return keyboard.inline_keyboard.flat();
}

Deno.test("dashboard exposes Telegram control screens", () => {
  const buttons = flatten(buildMainKeyboard("https://control.example", "https://wallet.example"));
  const labels = buttons.map((button) => button.text);
  for (const required of [
    "🧪 Paper Trading", "🔴 Live Controls", "🎯 Manual Quick-Snipe", "📊 Positions",
    "🧭 Live Readiness Review", "⚙️ Trading Settings", "🛡 Anti-Rug Filters", "🌐 Switch Chain / Market", "🧠 Engine & Diagnostics", "📜 Trade History", "💸 Withdraw",
  ]) {
    if (!labels.includes(required)) throw new Error("Missing dashboard control: " + required);
  }
  if (buttons.some((button) => button.web_app && !button.web_app.url.startsWith("https://"))) {
    throw new Error("Web App controls must use HTTPS URLs.");
  }
});

Deno.test("network selector distinguishes wallet pairing from trade execution", () => {
  const networks = {
    base: { note: "Base wallet pairing is available; trading is not connected." },
    ethereum: { note: "Ethereum wallet pairing is available; trading is not connected." },
    bnb: { note: "BNB wallet pairing is available; trading is not connected." },
    robinhood: { note: "Robinhood brokerage adapter is not connected." },
  };
  const screen = buildNetworkSelectorText(networks);
  for (const expected of [
    "SOLANA MAINNET — CONNECTED EXECUTION PATH",
    "BASE — WALLET LINKING AVAILABLE · TRADING NOT CONNECTED",
    "ETHEREUM — WALLET LINKING AVAILABLE · TRADING NOT CONNECTED",
    "BNB SMART CHAIN — WALLET LINKING AVAILABLE · TRADING NOT CONNECTED",
    "ROBINHOOD / EQUITIES — BROKERAGE NOT CONNECTED",
    "No recovery phrase or private key is requested in Telegram",
  ]) if (!screen.includes(expected)) throw new Error("Missing network status: " + expected);
  const buttons = flatten(buildNetworkSelectorKeyboard("https://wallet.example"));
  for (const callback of ["n8:network:base", "n8:network:ethereum", "n8:network:bnb", "n8:network:robinhood"]) {
    if (!buttons.some((button) => button.callback_data === callback)) throw new Error("Missing market selector: " + callback);
  }
  if (!buttons.some((button) => button.web_app?.url === "https://wallet.example")) throw new Error("Secure wallet connection link missing.");
});

Deno.test("live status menu provides wallet, readiness, refresh and dashboard actions", () => {
  const buttons = flatten(buildLiveStatusKeyboard("https://wallet.example", "https://control.example/migrate"));
  if (!buttons.some((button) => button.web_app?.url === "https://wallet.example")) throw new Error("Wallet navigation missing.");
  if (!buttons.some((button) => button.web_app?.url === "https://control.example/migrate")) throw new Error("Readiness navigation missing.");
  if (!buttons.some((button) => button.callback_data === "n8:live_status")) throw new Error("Live refresh callback missing.");
  if (!buttons.some((button) => button.callback_data === "n8:services")) throw new Error("Dashboard navigation missing.");
});

Deno.test("manual snipe opens secure wallet flow without automatic-buy callbacks", () => {
  const buttons = flatten(buildManualQuickSnipeKeyboard("https://wallet.example"));
  if (!buttons.some((button) => button.web_app?.url === "https://wallet.example")) throw new Error("Wallet app button missing.");
  if (buttons.some((button) => /buy|execute|auto/i.test(button.callback_data ?? ""))) {
    throw new Error("Manual snipe cannot trigger an automatic order.");
  }
});

Deno.test("engine diagnostics separates configured variables from runtime health", () => {
  const full = buildEngineStatusText({
    telegramTokenConfigured: true,
    webhookSecretConfigured: true,
    supabaseUrlConfigured: true,
    supabaseServiceCredentialConfigured: true,
    solanaRpcEndpointAvailable: true,
    jupiterApiKeyConfigured: true,
    walletConnectProjectConfigured: true,
  });
  for (const expected of [
    "🧠 9N8 ENGINE & DIAGNOSTICS",
    "CONFIGURATION PRESENCE (not a connectivity or credential-validity test)",
    "Jupiter API key: CONFIGURED / AVAILABLE",
    "Jupiter Swap V2 quote/order/execute/status handlers",
    "Python worker's Neon wallet, positions, PnL, heartbeat and detailed strategy settings are NOT synchronized",
    "V2 REBUILD INVENTORY (REPOSITORY MODULES)",
    "Rust Solana math, C++ order-book risk, Go OHLCV gateway and C# broker-risk adapter",
    "V2 domain and native modules are repository code, not proof of deployed Go/Rust/C++/C# services",
    "Solidity policy is compiled source only and is not deployed; Pine/MQL scripts are research/signal adapters",
    "Base, Ethereum and BNB trade execution, market listeners, risk adapters and receipt reconciliation are NOT CONNECTED.",
    "Robinhood brokerage and equities execution are NOT CONNECTED; Robinhood is not a blockchain.",
    "No trade was placed by opening this diagnostic screen",
  ]) {
    if (!full.includes(expected)) throw new Error("Missing truthful engine diagnostic: " + expected);
  }
  if (/Python worker: ONLINE|worker heartbeat: HEALTHY/i.test(full)) {
    throw new Error("Diagnostics must not claim the separate worker is online.");
  }

  const missing = buildEngineStatusText({
    telegramTokenConfigured: false,
    webhookSecretConfigured: false,
    supabaseUrlConfigured: false,
    supabaseServiceCredentialConfigured: false,
    solanaRpcEndpointAvailable: false,
    jupiterApiKeyConfigured: false,
    walletConnectProjectConfigured: false,
  });
  for (const expected of [
    "Telegram bot token: MISSING",
    "Telegram webhook secret: MISSING",
    "Supabase project URL: MISSING",
    "Solana RPC endpoint: MISSING",
    "Jupiter API key: MISSING",
  ]) {
    if (!missing.includes(expected)) throw new Error("Missing unavailable-config state: " + expected);
  }
});
