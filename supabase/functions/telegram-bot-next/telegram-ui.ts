export type InlineButton = { text: string; callback_data?: string; web_app?: { url: string }; url?: string };
export type InlineKeyboard = { inline_keyboard: InlineButton[][] };

export type EngineDiagnosticsConfig = {
  telegramTokenConfigured: boolean;
  webhookSecretConfigured: boolean;
  supabaseUrlConfigured: boolean;
  supabaseServiceCredentialConfigured: boolean;
  solanaRpcEndpointAvailable: boolean;
  jupiterApiKeyConfigured: boolean;
  walletConnectProjectConfigured: boolean;
};

/**
 * Reports implemented capabilities and environment-variable presence only.
 * It deliberately does not describe credentials as authenticated or the
 * separate Python worker as online.
 */
export function buildEngineStatusText(config: EngineDiagnosticsConfig): string {
  const status = (available: boolean) => available ? "CONFIGURED / AVAILABLE" : "MISSING";
  return [
    "🧠 9N8 ENGINE & DIAGNOSTICS", "",
    "CONFIGURATION PRESENCE (not a connectivity or credential-validity test)",
    "Telegram bot token: " + status(config.telegramTokenConfigured),
    "Telegram webhook secret: " + status(config.webhookSecretConfigured),
    "Supabase project URL: " + status(config.supabaseUrlConfigured),
    "Supabase server credential: " + status(config.supabaseServiceCredentialConfigured),
    "Solana RPC endpoint: " + status(config.solanaRpcEndpointAvailable),
    "Jupiter API key: " + status(config.jupiterApiKeyConfigured),
    "WalletConnect project ID: " + status(config.walletConnectProjectConfigured), "",
    "IMPLEMENTED COMPONENTS",
    "• TypeScript/Deno Telegram webhook and interactive control panels",
    "• Jupiter Swap V2 quote/order/execute/status handlers with amount, expiry, taker and price-impact guards",
    "• Separate paper ledger and paper worker; paper mode does not sign or broadcast swaps",
    "• Python Solana codebase: Helius launch monitoring, token-risk checks, wallet vault, position supervision and Jupiter/Jito execution modules",
    "• Binance, Bybit and OKX API-permission inspection; withdrawal/transfer scopes are rejected by the permission checks", "",
    "V2 REBUILD INVENTORY (REPOSITORY MODULES)",
    "• Python V2: typed trade intents, UTC OHLCV normalization, quote-risk checks, execution states, idempotency and quote-only Paper adapter",
    "• Quant engine: Kelly sizing caps, Sharpe, Sortino, drawdown, historical VaR, Monte Carlo, ATR, VWAP and Bollinger Bands",
    "• Market microstructure: order-book spread/depth/imbalance, slippage estimation, maker/taker fees and AMM impermanent-loss calculation",
    "• Research models: ADF, cointegration, Kalman filter, GBM scenarios, HMM, Random Forest, walk-forward validation and sentiment adapters",
    "• Data adapters: CCXT, Yahoo Finance, CoinMarketCap metadata, GraphQL, JSON-LD, Playwright, Scrapy, Selenium and VADER",
    "• Native modules: Rust Solana math, C++ order-book risk, Go OHLCV gateway and C# broker-risk adapter",
    "• Platform/research files: Solidity EVM policy, Pine Script, MQL4/MQL5 signal relays, R statistics and Julia Monte Carlo",
    "• Heavy research/ML/EVM dependencies are separate profiles and are not loaded by the production wallet-signing worker", "",
    "INTEGRATION STATUS",
    "• This Telegram webhook reads its Supabase ledgers. The Python worker's Neon wallet, positions, PnL, heartbeat and detailed strategy settings are NOT synchronized into this screen.",
    "• Python modules existing in GitHub does not prove a worker process is deployed, healthy or connected to mainnet.",
    "• The V2 domain layer and native services are not yet wired as the production composition root; Go/Rust/C++/C# endpoints are not deployed or connected to this webhook.",
    "• Solidity policy is compiled source only and is not deployed. Pine/MQL files are research/signal adapters, not connected to a chart or broker account.",
    "• Python worker Neon wallet, positions, PnL, strategy settings and heartbeat remain unsynchronized with the Telegram webhook.",
    "• Base, Ethereum and BNB Chain execution are NOT CONNECTED.",
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
    [{ text: "🎯 Manual Quick-Snipe", callback_data: "n8:snipe" }, { text: "📊 Positions", callback_data: "n8:solana_positions" }],
    [{ text: "⚙️ Trading Settings", callback_data: "n8:solana_settings" }, { text: "🛡 Anti-Rug Filters", callback_data: "n8:solana_safety" }],
    [{ text: "🌐 Switch Chain", callback_data: "n8:networks" }],
    [{ text: "🧠 Engine & Diagnostics", callback_data: "n8:engine" }],
    [{ text: "📜 Trade History", callback_data: "n8:history" }, { text: "💸 Withdraw", callback_data: "n8:withdraw" }],
    [{ text: "💼 Wallet & Jupiter", web_app: { url: walletAndJupiterUrl } }],
    [{ text: "📚 Services", callback_data: "n8:services" }, { text: "📡 Live Status", callback_data: "n8:live_status" }],
  ] };
}

export function buildNetworkSelectorText(networks: { base?: { note: string }; ethereum?: { note: string }; bnb?: { note: string } }): string {
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
