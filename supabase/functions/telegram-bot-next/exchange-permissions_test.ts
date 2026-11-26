import {
  assessBinancePermissions,
  assessBybitPermissions,
  assessOkxPermissions,
  normalizeRequestedMarkets,
} from "./exchange-permissions.ts";

Deno.test("market selection requires at least one known product", () => {
  if (normalizeRequestedMarkets(["spot", "spot", "futures_usdm"]).join(",") !== "spot,futures_usdm") {
    throw new Error("duplicate market selections were not normalized");
  }
  for (const value of [undefined, [], ["unknown_market"]]) {
    let rejected = false;
    try { normalizeRequestedMarkets(value); } catch { rejected = true; }
    if (!rejected) throw new Error("invalid market selection was accepted");
  }
});

Deno.test("Binance accepts a key with combined Spot/Margin/Stock and Futures permissions", () => {
  const result = assessBinancePermissions({
    enableReading: true,
    enableWithdrawals: false,
    enableInternalTransfer: false,
    permitsUniversalTransfer: false,
    enableSpotAndMarginTrading: true,
    enableMargin: true,
    enableFutures: true,
    enableVanillaOptions: false,
  }, ["spot", "margin", "stocks", "futures_usdm", "futures_coinm"]);
  if (result.marketPermissions.spot !== "verified" ||
      result.marketPermissions.margin !== "verified" ||
      !result.marketPermissions.stocks.includes("verified") ||
      result.marketPermissions.futures_usdm !== "verified" ||
      result.marketPermissions.futures_coinm !== "verified") {
    throw new Error("Binance product permissions were not accepted");
  }
});

Deno.test("Binance does not reject unused Futures/Margin/Options permission fields", () => {
  const result = assessBinancePermissions({
    enableReading: true,
    enableWithdrawals: false,
    enableInternalTransfer: false,
    permitsUniversalTransfer: false,
    enableSpotAndMarginTrading: true,
    enableMargin: false,
    enableFutures: false,
    enableVanillaOptions: false,
  }, ["spot"]);
  if (result.marketPermissions.spot !== "verified") {
    throw new Error("Spot key was rejected because unselected product permissions were disabled");
  }
});

Deno.test("Binance rejects selected products when the required exchange permission is missing", () => {
  const base = {
    enableReading: true,
    enableWithdrawals: false,
    enableInternalTransfer: false,
    permitsUniversalTransfer: false,
    enableSpotAndMarginTrading: true,
    enableMargin: false,
    enableFutures: false,
    enableVanillaOptions: false,
  };
  const cases = [
    { markets: ["margin"], expected: "binance_margin_permission_required" },
    { markets: ["futures_usdm"], expected: "binance_futures_permission_required" },
    { markets: ["options"], expected: "binance_options_permission_required" },
  ];
  for (const item of cases) {
    let message = "";
    try { assessBinancePermissions(base, item.markets as any); }
    catch (error) { message = error instanceof Error ? error.message : ""; }
    if (message !== item.expected) throw new Error("Expected " + item.expected + ", got " + message);
  }
});

Deno.test("Binance still rejects withdrawal and account transfer permission", () => {
  for (const patch of [
    { enableWithdrawals: true },
    { enableInternalTransfer: true },
    { permitsUniversalTransfer: true },
  ]) {
    let rejected = false;
    try {
      assessBinancePermissions({
        enableReading: true, enableSpotAndMarginTrading: true,
        enableWithdrawals: false, enableInternalTransfer: false, permitsUniversalTransfer: false,
        ...patch,
      }, ["spot"]);
    } catch { rejected = true; }
    if (!rejected) throw new Error("A key with a funds-movement permission was accepted");
  }
});

Deno.test("Bybit accepts combined Spot, Futures and Options permissions", () => {
  const result = assessBybitPermissions({
    readOnly: 0,
    permissions: {
      Spot: ["SpotTrade"],
      ContractTrade: ["Order", "Position"],
      Derivatives: ["DerivativesTrade"],
      Options: ["OptionsTrade"],
      Wallet: [],
    },
  }, ["spot", "futures_usdm", "futures_coinm", "options"]);
  if (result.marketPermissions.spot !== "verified" ||
      result.marketPermissions.futures_usdm !== "verified" ||
      result.marketPermissions.futures_coinm !== "verified" ||
      result.marketPermissions.options !== "verified") {
    throw new Error("Bybit product permissions were not verified");
  }
});

Deno.test("Bybit margin is clearly marked as an account setting, not a separate key scope", () => {
  const result = assessBybitPermissions({
    readOnly: 0,
    permissions: { Spot: ["SpotTrade"], ContractTrade: [], Derivatives: [], Options: [], Wallet: [] },
  }, ["margin"]);
  if (!result.marketPermissions.margin.includes("account_margin_mode_must_be_enabled")) {
    throw new Error("Bybit margin account-mode requirement was hidden");
  }
});

Deno.test("Bybit rejects wallet transfer, withdrawal and read-only API keys", () => {
  for (const info of [
    { readOnly: 1, permissions: { Spot: ["SpotTrade"], Wallet: [] } },
    { readOnly: 0, permissions: { Spot: ["SpotTrade"], Wallet: ["AccountTransfer"] } },
    { readOnly: 0, permissions: { Spot: ["SpotTrade"], Wallet: ["Withdraw"] } },
  ]) {
    let rejected = false;
    try { assessBybitPermissions(info, ["spot"]); } catch { rejected = true; }
    if (!rejected) throw new Error("An unsafe or read-only Bybit API key was accepted");
  }
});

Deno.test("OKX accepts trade keys without withdrawal and records account-wide scope", () => {
  const result = assessOkxPermissions({
    perm: "read_only,trade",
    acctLv: "3",
    enableSpotBorrow: true,
  }, ["spot", "margin", "futures_usdm", "options"]);
  if (result.marketPermissions.spot !== "trade_scope_verified_account_mode_and_instrument_must_be_checked" ||
      result.marketPermissions.margin !== "trade_scope_verified_spot_borrow_enabled" ||
      result.marketPermissions.futures_usdm !== "trade_scope_verified_account_mode_compatible" ||
      result.marketPermissions.options !== "trade_scope_verified_account_mode_compatible") {
    throw new Error("OKX permission / account mode information was not preserved");
  }
});

Deno.test("OKX refuses withdrawal and cannot mistake read-only keys for trading keys", () => {
  for (const account of [
    { perm: "read_only,withdraw", acctLv: "2" },
    { perm: "read_only", acctLv: "2" },
  ]) {
    let rejected = false;
    try { assessOkxPermissions(account, ["futures_usdm"]); } catch { rejected = true; }
    if (!rejected) throw new Error("An unsafe or read-only OKX API key was accepted");
  }
});

Deno.test("OKX keeps stock products outside this exchange connector", () => {
  let rejected = false;
  try { assessOkxPermissions({ perm: "trade", acctLv: "3" }, ["stocks"]); }
  catch (error) { rejected = error instanceof Error && error.message === "okx_stock_products_not_supported_by_this_connection"; }
  if (!rejected) throw new Error("Unsupported OKX stock products were accepted");
});
