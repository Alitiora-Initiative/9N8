export type InlineButton = { text: string; callback_data?: string; web_app?: { url: string }; url?: string };
export type InlineKeyboard = { inline_keyboard: InlineButton[][] };

export function buildMainKeyboard(solanaControlUrl: string, walletAndJupiterUrl: string): InlineKeyboard {
  return { inline_keyboard: [
    [{ text: "🚀 Open 9N8 Control App", web_app: { url: solanaControlUrl } }],
    [{ text: "🧪 Paper Trading", callback_data: "n8:solana_paper" }, { text: "🔴 Live Controls", callback_data: "n8:solana_live" }],
    [{ text: "🎯 Manual Quick-Snipe", callback_data: "n8:snipe" }, { text: "📊 Positions", callback_data: "n8:solana_positions" }],
    [{ text: "⚙️ Trading Settings", callback_data: "n8:solana_settings" }, { text: "🛡 Anti-Rug Filters", callback_data: "n8:solana_safety" }],
    [{ text: "🌐 Switch Chain", callback_data: "n8:networks" }],
    [{ text: "📜 Trade History", callback_data: "n8:history" }, { text: "💸 Withdraw", callback_data: "n8:withdraw" }],
    [{ text: "💼 Wallet & Jupiter", web_app: { url: walletAndJupiterUrl } }],
    [{ text: "📚 Services", callback_data: "n8:services" }, { text: "📡 Live Status", callback_data: "n8:live_status" }],
  ] };
}

export function buildNetworkSelectorText(networks: Record<string, { note: string }>): string {
  return [
    "🌐 SELECT ACTIVE TRADING NETWORK", "",
    "🟣 SOLANA MAINNET — ACTIVE",
    "Execution path: connected in the 9N8 chain registry.",
    "Wallet address and live balance: refresh in the secure wallet app; this Telegram webhook does not read the separate Python worker wallet database.", "",
    "🔵 BASE — NOT CONNECTED", networks.base?.note ?? "EVM wallet, RPC listener, risk checks and router are not deployed.", "",
    "🔷 ETHEREUM — NOT CONNECTED", networks.ethereum?.note ?? "EVM wallet, RPC listener, risk checks and router are not deployed.", "",
    "🟡 BNB CHAIN — NOT CONNECTED", networks.bnb?.note ?? "EVM wallet, RPC listener, risk checks and router are not deployed.", "",
    "Only Solana is execution-enabled. Selecting an unavailable chain does not create a wallet, query a balance, request a quote or send a transaction. Do not deposit assets on an unsupported chain.",
  ].join("\n");
}

export function buildNetworkSelectorKeyboard(walletAndJupiterUrl: string): InlineKeyboard {
  return { inline_keyboard: [
    [{ text: "🟣 Solana Mainnet · ACTIVE", callback_data: "n8:network:solana" }],
    [{ text: "🔵 Base · NOT CONNECTED", callback_data: "n8:network:base" }],
    [{ text: "🔷 Ethereum · NOT CONNECTED", callback_data: "n8:network:ethereum" }],
    [{ text: "🟡 BNB Chain · NOT CONNECTED", callback_data: "n8:network:bnb" }],
    [{ text: "💼 Manage Wallet / Jupiter", web_app: { url: walletAndJupiterUrl } }],
    [{ text: "🏠 Main Dashboard", callback_data: "n8:services" }],
  ] };
}

export function buildManualQuickSnipeKeyboard(walletAndJupiterUrl: string): InlineKeyboard {
  return { inline_keyboard: [
    [{ text: "🔐 Open secure wallet & Jupiter app", web_app: { url: walletAndJupiterUrl } }],
    [{ text: "❌ Cancel / Back to Dashboard", callback_data: "n8:services" }],
  ] };
}

export function buildReturnKeyboard(): InlineKeyboard {
  return { inline_keyboard: [[{ text: "🏠 Back to Dashboard", callback_data: "n8:services" }]] };
}
