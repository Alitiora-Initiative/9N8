import { buildMainKeyboard, buildManualQuickSnipeKeyboard, buildNetworkSelectorKeyboard, buildNetworkSelectorText } from "./telegram-ui.ts";

function flatten(keyboard: { inline_keyboard: { text: string; callback_data?: string; web_app?: { url: string } }[][] }) {
  return keyboard.inline_keyboard.flat();
}

Deno.test("dashboard exposes Telegram control screens", () => {
  const buttons = flatten(buildMainKeyboard("https://control.example", "https://wallet.example"));
  const labels = buttons.map((button) => button.text);
  for (const required of [
    "🧪 Paper Trading", "🔴 Live Controls", "🎯 Manual Quick-Snipe", "📊 Positions",
    "⚙️ Trading Settings", "🛡 Anti-Rug Filters", "🌐 Switch Chain", "📜 Trade History", "💸 Withdraw",
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
