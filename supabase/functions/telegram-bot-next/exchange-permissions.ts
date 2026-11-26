export const EXCHANGE_MARKETS = [
  "spot",
  "margin",
  "stocks",
  "futures_usdm",
  "futures_coinm",
  "options",
] as const;

export type ExchangeMarket = typeof EXCHANGE_MARKETS[number];

const ALLOWED = new Set<string>(EXCHANGE_MARKETS);

export function normalizeRequestedMarkets(value: unknown): ExchangeMarket[] {
  if (!Array.isArray(value)) throw new Error("select_at_least_one_market");
  const chosen = [...new Set(value.map((item) => String(item).trim()))];
  if (!chosen.length) throw new Error("select_at_least_one_market");
  if (chosen.some((item) => !ALLOWED.has(item))) throw new Error("exchange_market_not_supported");
  return chosen as ExchangeMarket[];
}

function selected(markets: ExchangeMarket[], ...names: ExchangeMarket[]): boolean {
  return names.some((name) => markets.includes(name));
}

export type MarketPermissionResult = {
  selectedMarkets: ExchangeMarket[];
  marketPermissions: Record<string, string>;
  permissionSource: string;
};

export function assessBinancePermissions(
  restrictions: Record<string, unknown>,
  markets: ExchangeMarket[],
): MarketPermissionResult {
  if (restrictions.enableReading !== true) throw new Error("binance_read_permission_required");
  if (restrictions.enableWithdrawals !== false) throw new Error("withdraw_permission_detected");
  if (restrictions.enableInternalTransfer !== false || restrictions.permitsUniversalTransfer !== false) {
    throw new Error("binance_transfer_permission_detected");
  }

  const spotMarginStock = restrictions.enableSpotAndMarginTrading === true;
  const margin = spotMarginStock && restrictions.enableMargin === true;
  const futures = restrictions.enableFutures === true;
  const options = restrictions.enableVanillaOptions === true;

  const marketPermissions: Record<string, string> = {};
  if (selected(markets, "spot")) {
    if (!spotMarginStock) throw new Error("binance_spot_margin_stock_permission_required");
    marketPermissions.spot = "verified";
  }
  if (selected(markets, "stocks")) {
    if (!spotMarginStock) throw new Error("binance_spot_margin_stock_permission_required");
    marketPermissions.stocks = "shared_spot_margin_stock_permission_verified_availability_varies";
  }
  if (selected(markets, "margin")) {
    if (!spotMarginStock || !margin) throw new Error("binance_margin_permission_required");
    marketPermissions.margin = "verified";
  }
  if (selected(markets, "futures_usdm", "futures_coinm")) {
    if (!futures) throw new Error("binance_futures_permission_required");
    if (markets.includes("futures_usdm")) marketPermissions.futures_usdm = "verified";
    if (markets.includes("futures_coinm")) marketPermissions.futures_coinm = "verified";
  }
  if (selected(markets, "options")) {
    if (!options) throw new Error("binance_options_permission_required");
    marketPermissions.options = "verified";
  }

  return {
    selectedMarkets: markets,
    marketPermissions,
    permissionSource: "Binance signed API restrictions endpoint",
  };
}

export function assessBybitPermissions(
  info: Record<string, any>,
  markets: ExchangeMarket[],
): MarketPermissionResult {
  if (Number(info.readOnly) === 1) throw new Error("bybit_api_key_is_read_only");
  const permissions = info.permissions ?? {};
  const wallet = Array.isArray(permissions.Wallet) ? permissions.Wallet : [];
  if (wallet.length) throw new Error("bybit_wallet_transfer_or_withdraw_scope_detected");

  const spot = Array.isArray(permissions.Spot) ? permissions.Spot : [];
  const contract = Array.isArray(permissions.ContractTrade) ? permissions.ContractTrade : [];
  const derivatives = Array.isArray(permissions.Derivatives) ? permissions.Derivatives : [];
  const options = Array.isArray(permissions.Options) ? permissions.Options : [];
  const hasSpot = spot.includes("SpotTrade");
  const hasContract = contract.includes("Order") && contract.includes("Position");
  const hasDerivatives = derivatives.includes("DerivativesTrade");
  const hasOptions = options.includes("OptionsTrade");
  const marketPermissions: Record<string, string> = {};

  if (selected(markets, "spot")) {
    if (!hasSpot) throw new Error("bybit_spot_trade_permission_required");
    marketPermissions.spot = "verified";
  }
  if (selected(markets, "margin")) {
    if (!hasSpot) throw new Error("bybit_spot_trade_permission_required");
    marketPermissions.margin = "spot_trade_scope_present_account_margin_mode_must_be_enabled";
  }
  if (selected(markets, "stocks")) throw new Error("bybit_stock_products_not_supported_by_this_connection");
  if (selected(markets, "futures_usdm", "futures_coinm")) {
    if (!hasContract && !hasDerivatives) throw new Error("bybit_derivatives_trade_permission_required");
    if (markets.includes("futures_usdm")) marketPermissions.futures_usdm = "verified";
    if (markets.includes("futures_coinm")) marketPermissions.futures_coinm = "verified";
  }
  if (selected(markets, "options")) {
    if (!hasOptions) throw new Error("bybit_options_trade_permission_required");
    marketPermissions.options = "verified";
  }

  return {
    selectedMarkets: markets,
    marketPermissions,
    permissionSource: "Bybit V5 query API permissions",
  };
}

export function assessOkxPermissions(
  account: Record<string, any>,
  markets: ExchangeMarket[],
): MarketPermissionResult {
  const raw = String(account.perm ?? "").toLowerCase();
  const permissions = new Set(raw.split(",").map((item) => item.trim()).filter(Boolean));
  if (permissions.has("withdraw")) throw new Error("withdraw_permission_detected");
  const hasRead = permissions.has("read_only") || permissions.has("trade");
  const hasTrade = permissions.has("trade");
  if (!hasRead) throw new Error("okx_read_permission_required");
  if (markets.length && !hasTrade) throw new Error("okx_trade_permission_required");

  const accountLevel = String(account.acctLv ?? "unknown");
  const marketPermissions: Record<string, string> = {};
  if (markets.includes("stocks")) throw new Error("okx_stock_products_not_supported_by_this_connection");
  for (const market of markets) {
    if (market === "margin") {
      marketPermissions.margin = account.enableSpotBorrow === true
        ? "trade_scope_verified_spot_borrow_enabled"
        : "trade_scope_verified_account_margin_setting_must_be_checked";
    } else if (market === "futures_usdm" || market === "futures_coinm" || market === "options") {
      marketPermissions[market] = ["2", "3", "4"].includes(accountLevel)
        ? "trade_scope_verified_account_mode_compatible"
        : "trade_scope_verified_account_mode_must_be_changed_in_okx";
    } else {
      marketPermissions.spot = "trade_scope_verified_account_mode_and_instrument_must_be_checked";
    }
  }

  return {
    selectedMarkets: markets,
    marketPermissions,
    permissionSource: "OKX account-wide Read/Trade permission; product access also depends on the account mode and instrument",
  };
}
