export const SOL_MINT = "So11111111111111111111111111111111111111112";
export const USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
export const MAX_SWAP_SOL_LAMPORTS = 50_000_000n; // 0.05 SOL per action
export const DAILY_SWAP_SOL_LAMPORTS = 100_000_000n; // 0.10 SOL rolling day
export const MAX_OPEN_SWAP_REQUESTS = 1;
export const MAX_PRICE_IMPACT_FRACTION = 0.02; // reject >2%
export const SWAP_QUOTE_TTL_SECONDS = 55;

export const JUPITER_EXPIRY_SAFETY_MARGIN_MS = 1_500;
const AGGREGATOR_ROUTERS = new Set(["metis", "dflow", "okx"]);

export function assertJupiterOrderExpiry(order: any, nowMs = Date.now()): string {
  const router = String(order?.router ?? "").trim().toLowerCase();
  if (router === "jupiterz") {
    const expiry = typeof order?.expireAt === "string" ? Date.parse(order.expireAt) : NaN;
    if (!Number.isFinite(expiry) || expiry <= nowMs + JUPITER_EXPIRY_SAFETY_MARGIN_MS) {
      throw new Error("jupiter_rfq_quote_expired_or_expiry_missing");
    }
    return new Date(Math.min(nowMs + SWAP_QUOTE_TTL_SECONDS * 1_000,
      expiry - JUPITER_EXPIRY_SAFETY_MARGIN_MS)).toISOString();
  }
  if (AGGREGATOR_ROUTERS.has(router)) {
    const height = Number(order?.lastValidBlockHeight);
    if (!Number.isSafeInteger(height) || height <= 0) {
      throw new Error("jupiter_last_valid_block_height_missing");
    }
    return new Date(nowMs + SWAP_QUOTE_TTL_SECONDS * 1_000).toISOString();
  }
  throw new Error("jupiter_router_unsupported_for_expiry_validation");
}

export function assertJupiterBlockHeightValid(lastValidBlockHeight: unknown, currentBlockHeight: unknown): void {
  const lastValid = Number(lastValidBlockHeight);
  const current = Number(currentBlockHeight);
  if (!Number.isSafeInteger(lastValid) || lastValid <= 0 ||
      !Number.isSafeInteger(current) || current <= 0) throw new Error("jupiter_block_height_invalid");
  if (current > lastValid) throw new Error("jupiter_transaction_blockhash_expired");
}

export function assertJupiterRfqQuoteValid(expireAt: unknown, nowMs = Date.now()): void {
  const expiry = typeof expireAt === "string" ? Date.parse(expireAt) : NaN;
  if (!Number.isFinite(expiry) || expiry <= nowMs + JUPITER_EXPIRY_SAFETY_MARGIN_MS) {
    throw new Error("jupiter_rfq_quote_expired_or_expiry_missing");
  }
}

export function decimalToRaw(value: string, decimals: number): bigint {
  if (!Number.isInteger(decimals) || decimals < 0 || decimals > 18) {
    throw new Error("token_decimals_invalid");
  }
  const clean = String(value ?? "").trim();
  if (!/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(clean)) throw new Error("amount_invalid");
  const [whole, fraction = ""] = clean.split(".");
  if (fraction.length > decimals) throw new Error("amount_precision_exceeded");
  const units = BigInt(whole) * (10n ** BigInt(decimals)) +
    BigInt((fraction + "0".repeat(decimals)).slice(0, decimals) || "0");
  if (units <= 0n) throw new Error("amount_must_be_positive");
  return units;
}

export function rawToDecimal(raw: string | bigint, decimals: number): string {
  const amount = BigInt(raw);
  if (!Number.isInteger(decimals) || decimals < 0 || decimals > 18 || amount < 0n) {
    throw new Error("amount_format_invalid");
  }
  const scale = 10n ** BigInt(decimals);
  const whole = amount / scale;
  const fraction = (amount % scale).toString().padStart(decimals, "0").replace(/0+$/, "");
  return fraction ? whole.toString() + "." + fraction : whole.toString();
}

export function base58Encode(bytes: Uint8Array): string {
  const alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  let value = 0n;
  for (const byte of bytes) value = value * 256n + BigInt(byte);
  let output = "";
  while (value > 0n) {
    const remainder = Number(value % 58n);
    output = alphabet[remainder] + output;
    value /= 58n;
  }
  for (let i = 0; i < bytes.length && bytes[i] === 0; i++) output = "1" + output;
  return output || "1";
}

export function base64ToBytes(value: string): Uint8Array {
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(value) || value.length % 4 !== 0) {
    throw new Error("transaction_base64_invalid");
  }
  const binary = atob(value);
  const output = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) output[i] = binary.charCodeAt(i);
  return output;
}

export function bytesToBase64(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary);
}

export async function sha256HexBytes(value: Uint8Array): Promise<string> {
  const copy = new Uint8Array(value).buffer;
  const digest = await crypto.subtle.digest("SHA-256", copy);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

export function finitePositive(value: unknown, code = "amount_invalid"): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) throw new Error(code);
  return parsed;
}

export function priceImpactFraction(value: unknown): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) throw new Error("jupiter_price_impact_invalid");
  if (parsed > MAX_PRICE_IMPACT_FRACTION) throw new Error("jupiter_price_impact_too_high");
  return parsed;
}

export function assertSolanaOrder(order: any, walletAddress: string): void {
  if (!order || typeof order !== "object" ||
      typeof order.requestId !== "string" || !order.requestId ||
      typeof order.transaction !== "string" || !order.transaction) {
    throw new Error(String(order?.errorMessage || "jupiter_order_transaction_unavailable"));
  }
  const version = Number(order.transactionVersion ?? 0);
  if (version !== 0) throw new Error("jupiter_transaction_version_unsupported");
  if (String(order.taker ?? walletAddress) !== walletAddress) {
    throw new Error("jupiter_order_taker_mismatch");
  }
  finitePositive(order.inAmount, "jupiter_input_amount_invalid");
  finitePositive(order.outAmount, "jupiter_output_amount_invalid");
  finitePositive(order.otherAmountThreshold, "jupiter_minimum_output_invalid");
  priceImpactFraction(order.priceImpactPct ?? 0);
}

export function assertSwapExecution(response: any): "success" | "failed" {
  const status = String(response?.status ?? "");
  if (status === "Success" && Number(response?.code ?? 0) === 0) return "success";
  if (status === "Failed") return "failed";
  throw new Error("jupiter_execution_status_unknown");
}

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export async function jupiterApi<T = any>(
  path: string,
  options: { method?: "GET" | "POST"; body?: unknown; apiKey?: string; accessToken?: string; fetcher?: FetchLike } = {},
): Promise<T> {
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("..")) {
    throw new Error("jupiter_path_invalid");
  }
  const headers: Record<string, string> = { accept: "application/json" };
  if (options.body !== undefined) headers["content-type"] = "application/json";
  if (options.apiKey) headers["x-api-key"] = options.apiKey;
  if (options.accessToken) headers.authorization = "Bearer " + options.accessToken;
  const fetcher = options.fetcher ?? fetch;
  const response = await fetcher("https://api.jup.ag" + path, {
    method: options.method ?? "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    cache: "no-store",
    credentials: "omit",
  });
  const raw = await response.text();
  let payload: any = {};
  try { payload = raw ? JSON.parse(raw) : {}; } catch {
    throw new Error("jupiter_response_invalid");
  }
  if (!response.ok) {
    const message = String(payload?.error || payload?.message || "jupiter_http_" + response.status)
      .replace(/[^A-Za-z0-9_. -]/g, "").slice(0, 120);
    const error = new Error("jupiter_http_" + response.status + ":" + message);
    (error as any).status = response.status;
    throw error;
  }
  return payload as T;
}
