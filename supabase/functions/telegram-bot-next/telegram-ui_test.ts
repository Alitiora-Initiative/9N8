import { buildEngineStatusText, buildMainKeyboard, buildManualQuickSnipeKeyboard, buildNetworkSelectorKeyboard, buildNetworkSelectorText } from "./telegram-ui.ts";

function flatten(keyboard: { inline_keyboard: { text: string; callback_data?: string; web_app?: { url: string } }[][] }) {
  return keyboard.inline_keyboard.flat();
}

Deno.test("dashboard exposes Telegram control screens", () => {
  const buttons = flatten(buildMainKeyboard("https://control.example", "https://wallet.example"));
  const labels = buttons.map((button) => button.text);
  for (const required of [
    "🧪 Paper Trading", "🔴 Live Controls", "🎯 Manual Quick-Snipe", "📊 Positions",
    "⚙️ Trading Settings", "🛡 Anti-Rug Filters", "🌐 Switch Chain", "🧠 Engine & Diagnostics", "📜 Trade History", "💸 Withdraw",
  ]) {
    if (!labels.includes(required)) throw new Error("Missing dashboard control: " + required);
  }
  if (buttons.some((button) => button.web_app && !button.web_app.url.startsWith("https://"))) {
    throw new Error("Web App controls must use HTTPS URLs.");
  }
});

Deno.test("network selector reports EVM chains as unavailable", () => {
  const networks = {
    base: { note: "Base is not deployed." },
    ethereum: { note: "Ethereum is not deployed." },
    bnb: { note: "BNB is not deployed." },
  };
  const screen = buildNetworkSelectorText(networks);
  for (const expected of ["SOLANA MAINNET — ACTIVE", "BASE — NOT CONNECTED", "ETHEREUM — NOT CONNECTED", "BNB CHAIN — NOT CONNECTED"]) {
    if (!screen.includes(expected)) throw new Error("Missing status: " + expected);
  }
  const buttons = flatten(buildNetworkSelectorKeyboard("https://wallet.example"));
  if (!buttons.some((button) => button.callback_data === "n8:network:base")) throw new Error("Base selector missing.");
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
    "WalletConnect project ID: CONFIGURED / AVAILABLE",
    "Jupiter Swap V2 quote/order/execute/status handlers",
    "V2 REBUILD INVENTORY (REPOSITORY MODULES)",
    "Rust Solana math, C++ order-book risk, Go OHLCV gateway and C# broker-risk adapter",
    "Solidity policy is compiled source only and is not deployed",
    "V2 domain layer and native services are not yet wired as the production composition root",
    "Python worker's Neon wallet, positions, PnL, heartbeat and detailed strategy settings are NOT synchronized",
    "Base, Ethereum and BNB Chain execution are NOT CONNECTED",
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
