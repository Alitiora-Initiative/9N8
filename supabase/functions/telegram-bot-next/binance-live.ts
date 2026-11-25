export type BinanceCredentials = { apiKey: string; apiSecret: string };
export type BinanceSide = "BUY" | "SELL";
export type BinanceBook = {
  bids: Array<[string, string]>;
  asks: Array<[string, string]>;
};
export type BinanceSymbolInfo = {
  symbol: string;
  status: string;
  isSpotTradingAllowed?: boolean;
  baseAsset: string;
  quoteAsset: string;
  quoteAssetPrecision?: number;
  filters?: Array<Record<string, unknown>>;
};
export type DepthEstimate = {
  referencePrice: number;
  fillPrice: number;
  baseQuantity: number;
  quoteNotional: number;
  spreadBps: number;
  slippageBps: number;
  liquidityUsd: number;
  levelsUsed: number;
};

export class BinanceHttpError extends Error {
  code: number | null;
  httpStatus: number;
  retryable: boolean;
  constructor(message: string, code: number | null, httpStatus: number, retryable: boolean) {
    super(message);
    this.name = "BinanceHttpError";
    this.code = code;
    this.httpStatus = httpStatus;
    this.retryable = retryable;
  }
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function hmacSha256(secret: string, value: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(value));
  return bytesToHex(new Uint8Array(signature));
}

/** Signed Spot REST request. POST bodies are form-encoded and signed exactly as sent. */
export async function binanceSignedRequest<T = Record<string, unknown>>(
  credentials: BinanceCredentials,
  method: "GET" | "POST" | "DELETE",
  path: string,
  params: Record<string, string | number | boolean | undefined>,
  fetcher: typeof fetch = fetch,
  baseUrl = "https://api.binance.com",
): Promise<T> {
  if (!credentials.apiKey || !credentials.apiSecret) throw new Error("binance_credentials_missing");
  if (!/^\/(?:api\/v3|sapi\/v1)\/[A-Za-z0-9/_-]+$/.test(path)) {
    throw new Error("binance_path_not_allowlisted");
  }
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null) query.set(key, String(value));
  }
  query.set("recvWindow", "5000");
  query.set("timestamp", String(Date.now()));
  const unsigned = query.toString();
  const signature = await hmacSha256(credentials.apiSecret, unsigned);
  const headers = {
    "X-MBX-APIKEY": credentials.apiKey,
    accept: "application/json",
  };
  const url = method === "GET" || method === "DELETE"
    ? baseUrl + path + "?" + unsigned + "&signature=" + signature
    : baseUrl + path;
  const response = await fetcher(url, {
    method,
    headers: method === "POST"
      ? { ...headers, "content-type": "application/x-www-form-urlencoded" }
      : headers,
    body: method === "POST" ? unsigned + "&signature=" + signature : undefined,
    cache: "no-store",
    credentials: "omit",
  });
  const raw = await response.text();
  let data: any = {};
  try { data = raw ? JSON.parse(raw) : {}; } catch { data = { msg: "Binance returned a non-JSON response" }; }
  if (!response.ok) {
    const code = Number.isFinite(Number(data?.code)) ? Number(data.code) : null;
    const message = String(data?.msg ?? "Binance request failed").slice(0, 240);
    throw new BinanceHttpError(message, code, response.status, response.status >= 500 || response.status === 429);
  }
  return data as T;
}

function decimalPlaces(value: string): number {
  const normalized = value.trim().toLowerCase();
  if (!/^(?:\d+\.\d*|\d*\.\d+|\d+)(?:e-?\d+)?$/.test(normalized)) return 0;
  const [mantissa, exponentText] = normalized.split("e");
  const fraction = (mantissa.split(".")[1] ?? "").length;
  return Math.max(0, fraction - Number(exponentText ?? 0));
}

function decimalUnits(value: string, scale: number): bigint {
  const [whole = "0", fraction = ""] = value.split(".");
  const padded = (fraction + "0".repeat(scale)).slice(0, scale);
  return BigInt((whole || "0") + padded);
}

function isStepMultiple(value: string, step: string): boolean {
  const scale = Math.max(decimalPlaces(value), decimalPlaces(step));
  const stepUnits = decimalUnits(step, scale);
  return stepUnits > 0n && decimalUnits(value, scale) % stepUnits === 0n;
}

function positiveDecimal(raw: string | number, label: string): string {
  const value = String(raw).trim();
  if (!/^(?:\d+\.\d*|\d*\.\d+|\d+)$/.test(value) || !Number.isFinite(Number(value)) || Number(value) <= 0) {
    throw new Error(label + "_invalid");
  }
  return value;
}

function filter(symbol: BinanceSymbolInfo, type: string): Record<string, unknown> | undefined {
  return (symbol.filters ?? []).find((entry) => entry.filterType === type);
}

/** Builds a market order without rounding quantities or inventing symbol precision. */
export function buildSpotMarketOrder(
  symbol: BinanceSymbolInfo,
  side: BinanceSide,
  size: { quoteOrderQty?: string | number; quantity?: string | number },
  referencePrice: number,
  clientOrderId: string,
): Record<string, string> {
  if (!/^[A-Z0-9]{5,20}$/.test(symbol.symbol) || symbol.status !== "TRADING" ||
      symbol.isSpotTradingAllowed === false || symbol.quoteAsset !== "USDT") {
    throw new Error("binance_symbol_not_supported_for_spot_usdt");
  }
  if (!/^[A-Za-z0-9_-]{8,36}$/.test(clientOrderId)) throw new Error("binance_client_order_id_invalid");
  const result: Record<string, string> = {
    symbol: symbol.symbol,
    side,
    type: "MARKET",
    newClientOrderId: clientOrderId,
    newOrderRespType: "FULL",
  };
  if (side === "BUY") {
    if (size.quoteOrderQty === undefined || size.quantity !== undefined) {
      throw new Error("market_buy_requires_quote_order_qty_only");
    }
    const quoteOrderQty = positiveDecimal(size.quoteOrderQty, "quote_order_qty");
    const precision = Number(symbol.quoteAssetPrecision ?? 8);
    if (decimalPlaces(quoteOrderQty) > precision) throw new Error("quote_order_qty_precision_exceeded");
    const notional = Number(quoteOrderQty);
    if (notional < 10) throw new Error("market_buy_below_10_usdt");
    const notionalFilter = filter(symbol, "NOTIONAL");
    const minNotional = Number(notionalFilter?.minNotional ?? 0);
    const applyMin = notionalFilter?.applyMinToMarket !== false;
    if (applyMin && minNotional > 0 && notional < minNotional) {
      throw new Error("market_buy_below_exchange_min_notional");
    }
    result.quoteOrderQty = quoteOrderQty;
  } else {
    if (size.quantity === undefined || size.quoteOrderQty !== undefined) {
      throw new Error("market_sell_requires_base_quantity_only");
    }
    const quantity = positiveDecimal(size.quantity, "base_quantity");
    const lot = filter(symbol, "MARKET_LOT_SIZE") ?? filter(symbol, "LOT_SIZE");
    const minQty = String(lot?.minQty ?? "0");
    const maxQty = String(lot?.maxQty ?? "0");
    const stepSize = String(lot?.stepSize ?? "0");
    if (Number(quantity) < Number(minQty) || Number(quantity) > Number(maxQty) ||
        !isStepMultiple(quantity, stepSize)) {
      throw new Error("market_sell_quantity_violates_lot_size");
    }
    if (!Number.isFinite(referencePrice) || referencePrice <= 0) throw new Error("reference_price_invalid");
    const notional = Number(quantity) * referencePrice;
    const minNotionalFilter = filter(symbol, "MIN_NOTIONAL");
    const notionalFilter = filter(symbol, "NOTIONAL");
    const minNotional = Number(notionalFilter?.minNotional ?? minNotionalFilter?.minNotional ?? 0);
    const minApplies = notionalFilter
      ? notionalFilter.applyMinToMarket !== false
      : minNotionalFilter?.applyToMarket !== false;
    if (minApplies && minNotional > 0 && notional < minNotional) {
      throw new Error("market_sell_below_exchange_min_notional");
    }
    result.quantity = quantity;
  }
  return result;
}

/** Estimates a market fill from visible public depth; it does not predict guaranteed exchange execution. */
export function estimateDepthExecution(
  side: BinanceSide,
  book: BinanceBook,
  size: { quoteOrderQty?: number; quantity?: number },
): DepthEstimate {
  const clean = (levels: Array<[string, string]>) => levels
    .map(([price, quantity]) => ({ price: Number(price), quantity: Number(quantity) }))
    .filter((level) => Number.isFinite(level.price) && level.price > 0 &&
      Number.isFinite(level.quantity) && level.quantity > 0);
  const bids = clean(book.bids).sort((a, b) => b.price - a.price);
  const asks = clean(book.asks).sort((a, b) => a.price - b.price);
  if (!bids.length || !asks.length) throw new Error("binance_order_book_empty");
  const bestBid = bids[0].price;
  const bestAsk = asks[0].price;
  if (bestAsk < bestBid) throw new Error("binance_order_book_crossed");
  const referencePrice = (bestBid + bestAsk) / 2;
  const levels = side === "BUY" ? asks : bids;
  const quoteMode = side === "BUY";
  let remaining = quoteMode ? Number(size.quoteOrderQty) : Number(size.quantity);
  if (!Number.isFinite(remaining) || remaining <= 0) throw new Error("binance_order_size_invalid");
  const requested = remaining;
  let baseQuantity = 0;
  let quoteNotional = 0;
  let levelsUsed = 0;
  for (const level of levels) {
    if (remaining <= 1e-10) break;
    if (quoteMode) {
      const usedQuote = Math.min(remaining, level.price * level.quantity);
      quoteNotional += usedQuote;
      baseQuantity += usedQuote / level.price;
      remaining -= usedQuote;
    } else {
      const usedBase = Math.min(remaining, level.quantity);
      baseQuantity += usedBase;
      quoteNotional += usedBase * level.price;
      remaining -= usedBase;
    }
    levelsUsed++;
  }
  if (remaining > Math.max(1e-8, requested * 1e-8)) throw new Error("binance_visible_depth_insufficient");
  const fillPrice = quoteNotional / baseQuantity;
  const spreadBps = ((bestAsk - bestBid) / referencePrice) * 10000;
  const adverse = side === "BUY"
    ? (fillPrice - referencePrice) / referencePrice
    : (referencePrice - fillPrice) / referencePrice;
  const boundary = side === "BUY" ? referencePrice * 1.01 : referencePrice * 0.99;
  const liquidityUsd = (side === "BUY" ? asks : bids)
    .filter((level) => side === "BUY" ? level.price <= boundary : level.price >= boundary)
    .reduce((sum, level) => sum + level.price * level.quantity, 0);
  return {
    referencePrice, fillPrice, baseQuantity, quoteNotional,
    spreadBps, slippageBps: Math.max(0, adverse * 10000), liquidityUsd, levelsUsed,
  };
}

export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
  const object = value as Record<string, unknown>;
  return "{" + Object.keys(object).sort().map((key) =>
    JSON.stringify(key) + ":" + canonicalJson(object[key])
  ).join(",") + "}";
}

export async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return bytesToHex(new Uint8Array(digest));
}

export function mapBinanceOrderStatus(status: string): "pending" | "open" | "partially_filled" | "filled" | "cancelled" | "rejected" | "unknown" {
  switch (status) {
    case "NEW": return "open";
    case "PENDING_NEW": return "pending";
    case "PARTIALLY_FILLED": return "partially_filled";
    case "FILLED": return "filled";
    case "CANCELED":
    case "EXPIRED":
    case "EXPIRED_IN_MATCH": return "cancelled";
    case "REJECTED": return "rejected";
    default: return "unknown";
  }
}
