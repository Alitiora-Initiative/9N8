export type InlineButton = { text: string; callback_data?: string; web_app?: { url: string }; url?: string };
export type InlineKeyboard = { inline_keyboard: InlineButton[][] };

export type IntegrationHealth =
  | "CONNECTED"
  | "NOT CONFIGURED"
  | "AUTH FAILED"
  | "RATE LIMITED"
  | "UNAVAILABLE"
  | "INVALID RESPONSE";

export type EngineDiagnosticsConfig = {
  telegramTokenConfigured: boolean;
  telegramApiHealth: IntegrationHealth;
  webhookSecretConfigured: boolean;
  supabaseUrlConfigured: boolean;
  supabaseServiceCredentialConfigured: boolean;
  solanaRpcEndpointAvailable: boolean;
  jupiterApiKeyConfigured: boolean;
  jupiterApiHealth: IntegrationHealth;
  walletConnectProjectConfigured: boolean;
};

/**
 * Reports configuration presence separately from explicit read-only provider
 * probes. It deliberately does not describe the separate Python worker as online.
 */
export function buildEngineStatusText(config: EngineDiagnosticsConfig): string {
  const status = (available: boolean) => available ? "CONFIGURED / AVAILABLE" : "MISSING";
  return [
    "🧠 9N8 ENGINE & DIAGNOSTICS", "",
    "CONFIGURATION PRESENCE (names and value presence only)",
    "Telegram bot token: " + status(config.telegramTokenConfigured),
    "Telegram webhook secret: " + status(config.webhookSecretConfigured),
    "Supabase project URL: " + status(config.supabaseUrlConfigured),
    "Supabase server credential: " + status(config.supabaseServiceCredentialConfigured),
    "Solana RPC endpoint: " + status(config.solanaRpcEndpointAvailable),
    "Jupiter API key: " + status(config.jupiterApiKeyConfigured),
    "WalletConnect / Reown project: " + status(config.walletConnectProjectConfigured), "",
    "LIVE READ-ONLY PROVIDER CHECKS",
    "Telegram Bot API getMe: " + config.telegramApiHealth,
    "Jupiter Swap V2 quote-only request: " + config.jupiterApiHealth,
    "The Jupiter probe requests a quote only; it has no taker wallet, signs no transaction and submits no swap.", "",
    "IMPLEMENTED COMPONENTS",
    "• TypeScript/Deno Telegram webhook and interactive control panels",
    "• Jupiter Swap V2 quote/order/execute/status handlers with amount, expiry, taker and price-impact guards",
    "• Separate paper ledger and paper worker; paper mode does not sign or broadcast swaps",
    "• Python Solana codebase: Helius launch monitoring, token-risk checks, wallet vault, position supervision and Jupiter/Jito execution modules",
    "• Binance, Bybit and OKX API-permission inspection; these connections are separate from on-chain wallet trading and Robinhood", "",
    "V2 REBUILD INVENTORY (REPOSITORY MODULES)",
    "• Python V2: typed trade intents, UTC OHLCV normalization, quote-risk checks, execution states, idempotency and quote-only Paper adapter",
    "• Quant engine: Kelly sizing caps, Sharpe, Sortino, drawdown, historical VaR, Monte Carlo, ATR, VWAP and Bollinger Bands",
    "• Market microstructure: order-book spread/depth/imbalance, slippage estimation, maker/taker costs and AMM impermanent-loss calculation",
    "• Research models: ADF, cointegration, Kalman, GBM scenarios, HMM, Random Forest, walk-forward validation and sentiment adapters",
    "• Data adapters: CCXT, Yahoo Finance, CoinMarketCap metadata, GraphQL, JSON-LD, Playwright, Scrapy, Selenium and VADER",
    "• Native modules: Rust Solana math, C++ order-book risk, Go OHLCV gateway and C# broker-risk adapter",
    "• Platform/research files: Solidity EVM policy, Pine Script, MQL4/MQL5 signal relays, R statistics and Julia Monte Carlo",
    "• Heavy research/ML/EVM dependencies are separate profiles and are not loaded by the production wallet-signing worker", "",
    "INTEGRATION STATUS",
    "• This Telegram webhook reads its Supabase ledgers. The Python worker's Neon wallet, positions, PnL, heartbeat and detailed strategy settings are NOT synchronized into this screen.",
    "• Python modules existing in GitHub does not prove a worker process is deployed, healthy or connected to mainnet.",
    "• V2 domain and native modules are repository code, not proof of deployed Go/Rust/C++/C# services; those endpoints are not connected to this webhook.",
    "• Python worker Neon wallet, positions, PnL, strategy settings and heartbeat remain unsynchronized with this Telegram screen.",
    "• Solidity policy is compiled source only and is not deployed; Pine/MQL scripts are research/signal adapters, not connected to chart or broker accounts.",
    "• WalletConnect can pair Solana, Ethereum, Base and BNB wallets and verify address ownership. Pairing does NOT mean the bot can execute trades on that chain.",
    "• Only the existing Solana path has an on-chain swap connector. Base, Ethereum and BNB trade execution, market listeners, risk adapters and receipt reconciliation are NOT CONNECTED.",
    "• Non-custodial wallet creation derives keys in the browser; the server receives public addresses and AES-256-GCM encrypted backup bytes only. The server cannot decrypt the recovery phrase.",
    "• Robinhood brokerage and equities execution are NOT CONNECTED; Robinhood is not a blockchain.",
    "• A configured key means only that a value is present. It does not prove the provider accepted it or that a network request succeeds.", "",
    "LIVE-TRADING SAFETY",
    "• Live trading remains gated by operator configuration, wallet checks, risk limits and a separate confirmation.",
    "• No trade was placed by opening this diagnostic screen. Never send a seed phrase or private key in Telegram."
  ].join("\n");
}

export function buildMainKeyboard(solanaControlUrl: string, walletAndJupiterUrl: string): InlineKeyboard {
  return { inline_keyboard: [
    [{ text: "🚀 Open 9N8 Control App", web_app: { url: solanaControlUrl } }],
    [{ text: "🧪 Paper Trading", callback_data: "n8:solana_paper" }, { text: "🔴 Live Controls", callback_data: "n8:solana_live" }],
    [{ text: "🧭 Live Readiness Review", callback_data: "n8:live_readiness" }],
    [{ text: "🎯 Manual Quick-Snipe", callback_data: "n8:snipe" }, { text: "📊 Positions", callback_data: "n8:solana_positions" }],
    [{ text: "⚙️ Trading Settings", callback_data: "n8:solana_settings" }, { text: "🛡 Anti-Rug Filters", callback_data: "n8:solana_safety" }],
    [{ text: "🌐 Switch Chain / Market", callback_data: "n8:networks" }],
    [{ text: "🧠 Engine & Diagnostics", callback_data: "n8:engine" }],
    [{ text: "📜 Trade History", callback_data: "n8:history" }, { text: "💸 Withdraw", callback_data: "n8:withdraw" }],
    [{ text: "💳 Non-custodial Wallets & Watch", web_app: { url: walletAndJupiterUrl + (walletAndJupiterUrl.includes("?") ? "&" : "?") + "view=wallet" } }],
    [{ text: "📚 Services", callback_data: "n8:services" }, { text: "📡 Live Status", callback_data: "n8:live_status" }],
  ] };
}

export function buildNetworkSelectorText(networks: {
  base?: { note: string }; ethereum?: { note: string }; bnb?: { note: string }; robinhood?: { note: string };
}): string {
  return [
    "🌐 NETWORKS & MARKETS", "",
    "🟣 SOLANA MAINNET — CONNECTED EXECUTION PATH",
    "The existing Solana connector is available, but real-money execution remains subject to wallet ownership, risk limits, connector readiness and explicit owner approval.",
    "WalletConnect can pair an external Solana wallet. It does not expose or send recovery phrases to Telegram.", "",
    "🔵 BASE — WALLET LINKING AVAILABLE · TRADING NOT CONNECTED",
    networks.base?.note ?? "Wallet pairing is available in the secure app; Base trade execution is not connected.", "",
    "🔷 ETHEREUM — WALLET LINKING AVAILABLE · TRADING NOT CONNECTED",
    networks.ethereum?.note ?? "Wallet pairing is available in the secure app; Ethereum trade execution is not connected.", "",
    "🟡 BNB SMART CHAIN — WALLET LINKING AVAILABLE · TRADING NOT CONNECTED",
    networks.bnb?.note ?? "Wallet pairing is available in the secure app; BNB trade execution is not connected.", "",
    "🟢 ROBINHOOD / EQUITIES — BROKERAGE NOT CONNECTED",
    networks.robinhood?.note ?? "No Robinhood brokerage connector is deployed.", "",
    "IMPORTANT: WalletConnect pairing and ownership verification are not trading adapters. Do not deposit funds into a bot-controlled address; 9N8 does not currently generate custodial multi-chain wallets. No recovery phrase or private key is requested in Telegram.",
  ].join("\n");
}

export function buildNetworkSelectorKeyboard(walletAndJupiterUrl: string): InlineKeyboard {
  return { inline_keyboard: [
    [{ text: "🟣 Solana · EXECUTION PATH", callback_data: "n8:network:solana" }],
    [{ text: "🔵 Base · LINK WALLET / NO TRADING", callback_data: "n8:network:base" }],
    [{ text: "🔷 Ethereum · LINK WALLET / NO TRADING", callback_data: "n8:network:ethereum" }],
    [{ text: "🟡 BNB Chain · LINK WALLET / NO TRADING", callback_data: "n8:network:bnb" }],
    [{ text: "🟢 Robinhood / Equities · NOT CONNECTED", callback_data: "n8:network:robinhood" }],
    [{ text: "🔐 Open Secure Wallet Connections", web_app: { url: walletAndJupiterUrl } }],
    [{ text: "🏠 Main Dashboard", callback_data: "n8:services" }],
  ] };
}

export function buildManualQuickSnipeKeyboard(walletAndJupiterUrl: string): InlineKeyboard {
  return { inline_keyboard: [
    [{ text: "🔐 Open secure wallet & Jupiter app", web_app: { url: walletAndJupiterUrl } }],
    [{ text: "❌ Cancel / Back to Dashboard", callback_data: "n8:services" }],
  ] };
}

export function buildLiveStatusKeyboard(walletAndJupiterUrl: string, migrationUrl: string): InlineKeyboard {
  return { inline_keyboard: [
    [{ text: "🔐 Connect / Verify Wallet", web_app: { url: walletAndJupiterUrl } }],
    [{ text: "🧭 Open Live Readiness Review", web_app: { url: migrationUrl } }],
    [{ text: "🔄 Refresh Live Status", callback_data: "n8:live_status" }],
    [{ text: "🏠 Back to Dashboard", callback_data: "n8:services" }],
  ] };
}

export function buildReturnKeyboard(): InlineKeyboard {
  return { inline_keyboard: [[{ text: "🏠 Back to Dashboard", callback_data: "n8:services" }]] };
}
