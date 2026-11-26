import { isAddress as isEvmAddress, verifyMessage as verifyEvmMessage } from "npm:viem@2.58.4";
import { PublicKey, VersionedTransaction } from "npm:@solana/web3.js@1.98.4";
import {
  assertSolanaOrder, base58Encode, base64ToBytes, decimalToRaw,
  jupiterApi, MAX_SWAP_SOL_LAMPORTS, DAILY_SWAP_SOL_LAMPORTS,
  priceImpactFraction, rawToDecimal, sha256HexBytes, SOL_MINT, SWAP_QUOTE_TTL_SECONDS,
} from "./jupiter-live.ts";
import type { OkxRegion } from "./okx-live.ts";
import {
  assessBinancePermissions,
  detectBinanceMarkets,
  assessBybitPermissions,
  assessOkxPermissions,
  normalizeRequestedMarkets,
  type ExchangeMarket,
} from "./exchange-permissions.ts";
import {
  binanceSignedRequest,
  buildSpotMarketOrder,
  canonicalJson,
  estimateDepthExecution,
  mapBinanceOrderStatus,
  sha256Hex,
  type BinanceBook,
  type BinanceSymbolInfo,
} from "./binance-live.ts";
import { getNetwork, isEvmAddressCandidate, isSolanaAddressCandidate, NETWORKS, networkExecutionEnabled } from "./networks.ts";


const TOKEN = (Deno.env.get("TELEGRAM_BOT_TOKEN") ?? "").trim();
const REOWN_PROJECT_ID = (Deno.env.get("REOWN_PROJECT_ID") ?? "").trim();
const JUPITER_API_KEY = (Deno.env.get("JUPITER_API_KEY") ?? "").trim();
const SOLANA_RPC_URL = (Deno.env.get("SOLANA_RPC_URL") ?? "https://api.mainnet-beta.solana.com").trim();
const HOOK_SECRET = (Deno.env.get("TELEGRAM_WEBHOOK_SECRET") ?? "").trim();
const SUPABASE_URL = (Deno.env.get("SUPABASE_URL") ?? "").trim().replace(/\/+$/, "");
const SUPABASE_SERVICE_KEY = (() => {
  const legacy = (Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "").trim();
  if (legacy) return legacy;
  try {
    const parsed = JSON.parse(Deno.env.get("SUPABASE_SECRET_KEYS") ?? "{}");
    return String(parsed.default ?? parsed.service_role ?? "").trim();
  } catch {
    return "";
  }
})();
const FUNCTION_URL = SUPABASE_URL + "/functions/v1/telegram-bot-next";
const MINI_APP_URL = "https://9n8-secure-connect.vercel.app/connect";
const MIGRATION_PAGE_URL = "https://9n8-secure-connect.vercel.app/migrate";
const SETUP_PAGE_URL = "https://9n8-secure-connect.vercel.app/setup";
const CORS_HEADERS = {
  "access-control-allow-origin": "https://9n8-secure-connect.vercel.app",
  "access-control-allow-methods": "GET, POST, OPTIONS",
  "access-control-allow-headers": "content-type",
  "vary": "Origin",
};
const enc = new TextEncoder();

type Provider = "okx" | "binance" | "bybit";
type BybitRegion = "global" | "eea" | "uae" | "turkey" | "kazakhstan" | "georgia" | "indonesia" | "japan";
type Credentials = {
  apiKey: string;
  apiSecret: string;
  passphrase?: string;
  region?: OkxRegion;
  bybitRegion?: BybitRegion;
};
type Obj = Record<string, unknown>;

function response(data: Obj, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...CORS_HEADERS, "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}
function clean(value: unknown, max = 100): string {
  return String(value ?? "").replace(/[\u0000-\u001f\u007f]/g, "").trim().slice(0, max);
}
function hex(bytes: Uint8Array): string {
  return Array.from(bytes).map((v) => v.toString(16).padStart(2, "0")).join("");
}
function b64(bytes: Uint8Array): string {
  let value = "";
  for (const byte of bytes) value += String.fromCharCode(byte);
  return btoa(value);
}
async function mac(keyBytes: BufferSource, message: string): Promise<Uint8Array> {
  const key = await crypto.subtle.importKey("raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(message)));
}
function same(a: string, b: string): boolean {
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
async function db(path: string, method = "GET", body?: unknown, prefer?: string): Promise<any> {
  if (!SUPABASE_URL || !SUPABASE_SERVICE_KEY) throw new Error("backend_not_configured");
  const headers: Record<string, string> = {
    apikey: SUPABASE_SERVICE_KEY,
    authorization: "Bearer " + SUPABASE_SERVICE_KEY,
    accept: "application/json",
  };
  if (body !== undefined) headers["content-type"] = "application/json";
  if (prefer) headers.prefer = prefer;
  const r = await fetch(SUPABASE_URL + "/rest/v1/" + path, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  if (!r.ok) throw new Error("database_request_failed_" + r.status);
  if (!text) return null;
  try { return JSON.parse(text); } catch { return text; }
}
async function audit(userId: string | null, chatId: number | null, event: string, outcome = "ok", metadata: Obj = {}) {
  try {
    await db("n8_audit_events", "POST", {
      user_id: userId, chat_id: chatId, event_type: event, outcome, metadata,
    }, "return=minimal");
  } catch {
    // Never log raw request bodies, exchange credentials, webhook bodies, or secrets.
  }
}
async function userRow(user: any): Promise<any> {
  const telegramId = Number(user?.id);
  if (!Number.isSafeInteger(telegramId) || telegramId <= 0) throw new Error("telegram_identity_invalid");
  const rows = await db("n8_users?on_conflict=telegram_user_id", "POST", {
    telegram_user_id: telegramId,
    telegram_username: clean(user?.username, 64) || null,
    first_name: clean(user?.first_name, 80) || null,
  }, "resolution=merge-duplicates,return=representation");
  const row = Array.isArray(rows) ? rows[0] : null;
  if (!row?.id) throw new Error("user_registration_failed");
  await db("n8_risk_policies?on_conflict=user_id", "POST", { user_id: row.id },
    "resolution=ignore-duplicates,return=minimal");
  return row;
}
async function telegram(method: string, payload: Obj): Promise<any> {
  if (!TOKEN) throw new Error("telegram_not_configured");
  const r = await fetch("https://api.telegram.org/bot" + TOKEN + "/" + method, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j?.ok !== true) throw new Error("telegram_action_failed");
  return j.result;
}
function escapeRichHtml(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function richHtmlFromPlainText(value: string): string {
  return value.split(/\r?\n/).map((line) => "<p>" + (escapeRichHtml(line) || "&nbsp;") + "</p>").join("");
}
function splitText(value: string, maxCodePoints: number): string[] {
  const points = Array.from(value);
  const chunks: string[] = [];
  for (let i = 0; i < points.length; i += maxCodePoints) {
    chunks.push(points.slice(i, i + maxCodePoints).join(""));
  }
  return chunks.length ? chunks : [""];
}
async function send(chatId: number, text: string, markup?: Obj) {
  // Telegram sendMessage is limited to 4096 characters after entity parsing.
  // Chunk plain text at Unicode code-point boundaries; never call undocumented methods.
  const chunks = splitText(String(text ?? ""), 3900);
  for (let i = 0; i < chunks.length; i++) {
    const payload: Obj = {
      chat_id: chatId,
      text: chunks[i],
      disable_web_page_preview: true,
    };
    if (markup && i === chunks.length - 1) payload.reply_markup = markup;
    await telegram("sendMessage", payload);
  }
}
async function sendReportPhoto(chatId: number, signedUrl: string, caption: string, markup?: Obj) {
  const url = new URL(signedUrl);
  const backend = new URL(SUPABASE_URL);
  if (url.protocol !== "https:" || url.hostname !== backend.hostname ||
      !url.pathname.startsWith("/storage/v1/object/sign/")) {
    throw new Error("report_image_url_not_allowed");
  }
  const payload: Obj = {
    chat_id: chatId,
    photo: signedUrl,
    caption: Array.from(String(caption ?? "")).slice(0, 1024).join(""),
  };
  if (markup) payload.reply_markup = markup;
  await telegram("sendPhoto", payload);
}
async function sendReportDocument(chatId: number, signedUrl: string, caption: string) {
  const url = new URL(signedUrl);
  const backend = new URL(SUPABASE_URL);
  if (url.protocol !== "https:" || url.hostname !== backend.hostname ||
      !url.pathname.startsWith("/storage/v1/object/sign/")) {
    throw new Error("report_document_url_not_allowed");
  }
  await telegram("sendDocument", {
    chat_id: chatId,
    document: signedUrl,
    caption: Array.from(String(caption ?? "")).slice(0, 1024).join(""),
  });
}
const SOLANA_SNIPER_URL = "https://9n8-secure-connect.vercel.app/";

function solanaPanelKeyboard(section = "dashboard"): Obj {
  const labels: Record<string, string> = {
    dashboard: "Open Solana Sniper", paper: "Open Paper Trading", live: "Open Live Controls",
    positions: "Open Positions", settings: "Open Trading Settings", safety: "Open Safety Filters",
  };
  const safeSection = Object.prototype.hasOwnProperty.call(labels, section) ? section : "dashboard";
  const url = SOLANA_SNIPER_URL + (safeSection === "dashboard" ? "" : "#" + safeSection);
  return { inline_keyboard: [
    [{ text: labels[safeSection], web_app: { url } }],
    [{ text: "🏠 9N8 Main Menu", callback_data: "n8:services" }],
  ] };
}

function networkSelectorText(): string {
  return [
    "🌐 9N8 NETWORK SELECTOR", "",
    "Active execution network: Solana Mainnet",
    "Solana is the only connected on-chain execution path. Its existing Paper and Live safety controls remain separate.", "",
    "BASE · NOT CONNECTED", NETWORKS.base.note, "",
    "ETHEREUM · NOT CONNECTED", NETWORKS.ethereum.note, "",
    "BNB CHAIN · NOT CONNECTED", NETWORKS.bnb.note, "",
    "Selecting an unsupported chain never connects a wallet, requests a quote, or submits a transaction. Do not deposit funds on an unsupported network.",
  ].join("\n");
}

function networkSelectorKeyboard(): Obj {
  return { inline_keyboard: [
    [{ text: "✅ Solana Mainnet · Active", callback_data: "n8:network:solana" }],
    [{ text: "Base · Not connected", callback_data: "n8:network:base" }],
    [{ text: "Ethereum · Not connected", callback_data: "n8:network:ethereum" }],
    [{ text: "BNB Chain · Not connected", callback_data: "n8:network:bnb" }],
    [{ text: "↩ Back to 9N8 dashboard", callback_data: "n8:services" }],
  ] };
}

function mainKeyboard(): Obj {
  return { inline_keyboard: [
    [{ text: "🚀 OPEN SOLANA SNIPER CONTROL", web_app: { url: SOLANA_SNIPER_URL } }],
    [{ text: "🧪 Paper Trading", callback_data: "n8:solana_paper" }, { text: "🔴 Live Controls", callback_data: "n8:solana_live" }],
    [{ text: "📊 Positions", callback_data: "n8:solana_positions" }, { text: "⚙️ Trading Settings", callback_data: "n8:solana_settings" }],
    [{ text: "🛡 Safety Filters", callback_data: "n8:solana_safety" }],
    [{ text: "🌐 Networks / Chains", callback_data: "n8:networks" }],
    [{ text: "Connect Exchanges", web_app: { url: MINI_APP_URL } }, { text: "Wallet Watch", web_app: { url: SOLANA_SNIPER_URL + "#live" } }],
    [{ text: "📚 Services", callback_data: "n8:services" }, { text: "📡 Live Status", callback_data: "n8:live_status" }],
  ] };
}

async function verifyInitData(raw: string, maxAgeSeconds = 300): Promise<any> {
  if (!TOKEN || !raw || raw.length > 12000) throw new Error("telegram_auth_invalid");
  const p = new URLSearchParams(raw);
  const supplied = (p.get("hash") ?? "").toLowerCase();
  const authDate = Number(p.get("auth_date") ?? "0");
  const now = Math.floor(Date.now() / 1000);
  if (!/^[a-f0-9]{64}$/.test(supplied) || !Number.isSafeInteger(authDate)) throw new Error("telegram_auth_invalid");
  if (authDate > now + 30 || now - authDate > maxAgeSeconds) throw new Error("telegram_auth_expired");
  const entries = Array.from(p.entries()).filter(([key]) => key !== "hash");
  entries.sort(([a], [b]) => a.localeCompare(b));
  const check = entries.map(([key, value]) => key + "=" + value).join("\n");
  const derived = await mac(enc.encode("WebAppData"), TOKEN); // Telegram: HMAC key=WebAppData, message=bot token.
  const calculated = hex(await mac(new Uint8Array(derived).buffer, check));
  if (!same(calculated, supplied)) throw new Error("telegram_auth_invalid");
  let user: any;
  try { user = JSON.parse(p.get("user") ?? "null"); } catch { throw new Error("telegram_auth_invalid"); }
  if (!Number.isSafeInteger(Number(user?.id)) || Number(user.id) <= 0) throw new Error("telegram_auth_invalid");
  const chatType = p.get("chat_type");
  if (chatType && chatType !== "private") throw new Error("private_chat_required");
  return user;
}

async function jsonGet(url: string, headers: Record<string, string>): Promise<any> {
  const r = await fetch(url, { method: "GET", headers });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error("exchange_credentials_rejected");
  return data;
}
const OKX_API_ORIGINS: Record<OkxRegion, string> = {
  global: "https://openapi.okx.com",
  us: "https://us.okx.com",
  eea: "https://eea.okx.com",
};

async function validateOKX(c: Credentials, markets: ExchangeMarket[]) {
  if (!c.passphrase) throw new Error("okx_passphrase_required");
  const region = c.region ?? "global";
  const origin = OKX_API_ORIGINS[region];
  if (!origin) throw new Error("okx_region_unsupported");
  const timeResponse = await fetch(origin + "/api/v5/public/time", { cache: "no-store" });
  const timeJson = await timeResponse.json().catch(() => ({}));
  const milliseconds = Number(timeJson?.data?.[0]?.ts);
  if (!timeResponse.ok || String(timeJson?.code ?? "") !== "0" || !Number.isFinite(milliseconds)) {
    throw new Error("exchange_temporarily_unavailable");
  }
  const timestamp = new Date(milliseconds).toISOString();
  const path = "/api/v5/account/config";
  const signature = b64(await mac(enc.encode(c.apiSecret), timestamp + "GET" + path));
  const data = await jsonGet(origin + path, {
    "OK-ACCESS-KEY": c.apiKey,
    "OK-ACCESS-SIGN": signature,
    "OK-ACCESS-TIMESTAMP": timestamp,
    "OK-ACCESS-PASSPHRASE": c.passphrase,
    accept: "application/json",
  });
  if (String(data?.code ?? "") !== "0" || !data?.data?.[0]) {
    throw new Error("exchange_credentials_rejected");
  }
  const account = data.data[0];
  const assessment = assessOkxPermissions(account, markets);
  const permissions = String(account.perm ?? "").toLowerCase().split(",").map((value: string) => value.trim());
  return {
    label: "OKX LIVE · " + region.toUpperCase(),
    permissions: {
      read: permissions.includes("read_only") || permissions.includes("trade"),
      trade: permissions.includes("trade"),
      withdraw: false,
      ...assessment,
      accountLevel: String(account.acctLv ?? "unknown"),
      spotBorrowEnabled: account.enableSpotBorrow === true,
      tradeScopeWarning: "OKX has one account-wide Trade permission. It is not split into Spot/Futures permissions and may also allow internal funding transfers. 9N8 does not call transfer or withdrawal endpoints.",
    },
    accountHint: "OKX " + region.toUpperCase() + " account · authenticated",
  };
}

async function binanceSignedGet(c: Credentials, path: string): Promise<any> {
  const query = "recvWindow=5000&timestamp=" + Date.now();
  const signature = hex(await mac(enc.encode(c.apiSecret), query));
  return await jsonGet("https://api.binance.com" + path + "?" + query + "&signature=" + signature, {
    "X-MBX-APIKEY": c.apiKey,
    accept: "application/json",
  });
}

async function validateBinanceLive(c: Credentials, requestedMarkets?: ExchangeMarket[]) {
  // Read Binance API-key scopes and account-level product activation separately.
  const restrictions = await binanceSignedGet(c, "/sapi/v1/account/apiRestrictions");
  const accountInfo = await binanceSignedGet(c, "/sapi/v1/account/info");

  // Empty selection is intentional: inspect every product the key and account allow.
  // These are signed GET requests only; no order endpoint is called.
  assessBinancePermissions(restrictions, []);
  const detected = detectBinanceMarkets(restrictions, accountInfo);
  const markets = requestedMarkets ?? detected.selectedMarkets;
  const assessment = assessBinancePermissions(restrictions, markets);
  const unavailable = markets.filter((market) => !detected.selectedMarkets.includes(market));
  if (unavailable.length) {
    throw new Error("binance_selected_product_not_enabled:" + unavailable.join(","));
  }

  let spotAccount: any = null;
  if (markets.some((market) => ["spot", "margin"].includes(market))) {
    spotAccount = await binanceSignedGet(c, "/api/v3/account");
    if (!Array.isArray(spotAccount?.balances)) throw new Error("binance_spot_account_read_failed");
    // canWithdraw on /api/v3/account is an account capability, not an API-key scope.
    // Key-level withdrawal and transfer flags were checked by apiRestrictions above.
  }

  const label = markets.length
    ? "Binance LIVE · " + markets.map((market) => market.toUpperCase()).join(" + ")
    : "Binance LIVE · READ-ONLY PERMISSION DISCOVERY";
  return {
    label,
    permissions: {
      read: restrictions.enableReading === true,
      withdraw: restrictions.enableWithdrawals === true,
      internalTransfer: restrictions.enableInternalTransfer === true,
      universalTransfer: restrictions.permitsUniversalTransfer === true,
      selectedMarkets: markets,
      detectedMarkets: detected.selectedMarkets,
      marketPermissions: {
        ...detected.marketPermissions,
        ...assessment.marketPermissions,
      },
      permissionSource: "Binance signed /sapi/v1/account/apiRestrictions + /sapi/v1/account/info",
      keyPermissions: {
        reading: restrictions.enableReading === true,
        withdrawals: restrictions.enableWithdrawals === true,
        internalTransfer: restrictions.enableInternalTransfer === true,
        universalTransfer: restrictions.permitsUniversalTransfer === true,
        spotAndMarginTrading: restrictions.enableSpotAndMarginTrading === true,
        margin: restrictions.enableMargin === true,
        futures: restrictions.enableFutures === true,
        vanillaOptions: restrictions.enableVanillaOptions === true,
        portfolioMarginTrading: restrictions.enablePortfolioMarginTrading === true,
        fixApiTrade: restrictions.enableFixApiTrade === true,
        fixReadOnly: restrictions.enableFixReadOnly === true,
        ipRestricted: restrictions.ipRestrict === true,
      },
      accountProducts: detected.accountProducts,
      futuresScopeNote: "USDⓈ-M and COIN-M share Binance's enableFutures API-key permission and isFutureEnabled account flag; these responses do not expose separate USDⓈ-M and COIN-M toggles.",
      portfolioMarginStatus: restrictions.enablePortfolioMarginTrading === true &&
        detected.accountProducts.portfolioMarginEnabled
          ? "verified"
          : restrictions.enablePortfolioMarginTrading === true
          ? "binance_account_product_not_enabled"
          : "api_key_permission_not_enabled",
      stockProductsNote: "The Spot/Margin/Stock API-key flag is shared. 9N8 does not report legacy stock products as a verified market.",
      liveOrdersEnabled: false,
    },
    accountHint: spotAccount?.uid == null ? null : "Binance UID · " + mask(String(spotAccount.uid)),
  };
}

const BYBIT_API_ORIGINS: Record<BybitRegion, string> = {
  global: "https://api.bybit.com",
  eea: "https://api.bybit.eu",
  uae: "https://api.bybit.ae",
  turkey: "https://api.bybit.tr",
  kazakhstan: "https://api.bybit.kz",
  georgia: "https://api.bybitgeorgia.ge",
  indonesia: "https://api.bybit.id",
  japan: "https://api.manepa.jp",
};

async function bybitSignedGet(
  c: Credentials,
  path: string,
  params: Record<string, string> = {},
): Promise<any> {
  const timestamp = String(Date.now());
  const recvWindow = "5000";
  const queryString = new URLSearchParams(
    Object.entries(params).sort(([a], [b]) => a.localeCompare(b)),
  ).toString();
  const signature = hex(await mac(
    enc.encode(c.apiSecret),
    timestamp + c.apiKey + recvWindow + queryString,
  ));
  const region = c.bybitRegion ?? "global";
  const origin = BYBIT_API_ORIGINS[region];
  if (!origin) throw new Error("bybit_region_unsupported");
  const url = origin + path + (queryString ? "?" + queryString : "");
  return await jsonGet(url, {
    "X-BAPI-API-KEY": c.apiKey,
    "X-BAPI-SIGN": signature,
    "X-BAPI-SIGN-TYPE": "2",
    "X-BAPI-TIMESTAMP": timestamp,
    "X-BAPI-RECV-WINDOW": recvWindow,
    accept: "application/json",
  });
}

async function validateBybit(c: Credentials, markets: ExchangeMarket[]) {
  const data = await bybitSignedGet(c, "/v5/user/query-api");
  if (Number(data?.retCode) !== 0 || !data?.result) {
    throw new Error("exchange_credentials_rejected");
  }
  const assessment = assessBybitPermissions(data.result, markets);
  return {
    label: "Bybit LIVE · " + (c.bybitRegion ?? "global").toUpperCase() + " · " + markets.map((market) => market.toUpperCase()).join(" + "),
    permissions: {
      read: true,
      withdraw: false,
      walletTransfers: false,
      ...assessment,
      readOnly: Number(data.result.readOnly) === 1,
      region: c.bybitRegion ?? "global",
      marginNote: markets.includes("margin")
        ? "Bybit does not show a separate Margin API-key scope. SpotTrade is checked; your Unified Account must also have margin enabled."
        : undefined,
    },
    accountHint: data.result.note ? "Bybit API key · " + clean(data.result.note, 40) : "Bybit account · authenticated",
  };
}

async function validateExchange(
  provider: Provider,
  environment: string,
  credentials: Credentials,
  markets?: ExchangeMarket[],
) {
  if (!credentials.apiKey || !credentials.apiSecret) throw new Error("api_key_and_secret_required");
  if (credentials.apiKey.length > 512 || credentials.apiSecret.length > 1024) {
    throw new Error("credential_field_too_long");
  }
  if (environment !== "live") throw new Error("unsupported_provider_environment");
  if (provider === "binance") return await validateBinanceLive(credentials, markets);
  if (!markets?.length) throw new Error("select_at_least_one_market");
  if (provider === "bybit") return await validateBybit(credentials, markets);
  if (provider === "okx") return await validateOKX(credentials, markets);
  throw new Error("unsupported_provider");
}
async function saveExchange(telegramId: number, provider: Provider, environment: string,
  label: string, credentials: Credentials, permissions: Obj, accountHint: string | null = null): Promise<string> {
  const result = await db("rpc/n8_store_exchange_secret", "POST", {
    p_telegram_user_id: telegramId,
    p_provider: provider,
    p_environment: environment,
    p_label: label,
    p_credentials: credentials,
    p_permissions: permissions,
    p_account_hint: accountHint,
  });
  const id = typeof result === "string" ? result : Array.isArray(result) ? result[0] : result;
  if (typeof id !== "string" || !id) throw new Error("credential_storage_failed");
  return id;
}
function walletAddressValid(namespace: string, address: string): boolean {
  if (namespace === "eip155") return /^0x[a-fA-F0-9]{40}$/.test(address);
  if (namespace === "solana") return /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(address);
  return false;
}
function decodeBase58(value: string): Uint8Array {
  const alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  let number = 0n;
  for (const char of value) {
    const digit = alphabet.indexOf(char);
    if (digit < 0) throw new Error("wallet_address_invalid");
    number = number * 58n + BigInt(digit);
  }
  const bytes: number[] = [];
  while (number > 0n) {
    bytes.unshift(Number(number & 255n));
    number >>= 8n;
  }
  for (let i = 0; i < value.length && value[i] === "1"; i++) bytes.unshift(0);
  return new Uint8Array(bytes);
}
function decodeBase64(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}
async function verifyWalletProof(challenge: any, signature: string): Promise<boolean> {
  if (challenge.chain_namespace === "eip155") {
    if (!isEvmAddress(challenge.public_address) || !/^0x[0-9a-fA-F]{130}$/.test(signature)) return false;
    return await verifyEvmMessage({
      address: challenge.public_address as any,
      message: challenge.message,
      signature: signature as any,
    });
  }
  if (challenge.chain_namespace === "solana") {
    const publicKey = decodeBase58(String(challenge.public_address));
    const proof = decodeBase64(signature);
    if (publicKey.length !== 32 || proof.length !== 64) return false;
    // Copy into ArrayBuffer-backed views for WebCrypto's strict BufferSource overloads.
    const publicKeyData = new Uint8Array(publicKey).buffer;
    const signatureData = new Uint8Array(proof).buffer;
    const key = await crypto.subtle.importKey("raw", publicKeyData, { name: "Ed25519" }, false, ["verify"]);
    return await crypto.subtle.verify("Ed25519", key, signatureData, enc.encode(String(challenge.message)));
  }
  return false;
}
async function createWalletChallenge(user: any, body: any): Promise<Response> {
  const namespace = String(body?.namespace ?? "");
  const address = clean(body?.address, 128);
  const provider = String(body?.provider ?? "walletconnect");
  const connectionType = String(body?.connectionType ?? "walletconnect_session");
  const allowedProviders = ["walletconnect", "phantom", "trust_wallet", "metamask"];
  const allowedConnectionTypes = ["walletconnect_session", "injected_session"];
  const evmNetworks: Record<string, string> = {
    "1": "Ethereum Mainnet",
    "8453": "Base Mainnet",
    "56": "BNB Smart Chain",
  };
  const chainId = clean(body?.chainId, 64);
  const network = namespace === "solana"
    ? (chainId === "mainnet-beta" ? "Solana Mainnet" : "")
    : evmNetworks[chainId] ?? "";
  if (!allowedProviders.includes(provider) || !allowedConnectionTypes.includes(connectionType) ||
      !walletAddressValid(namespace, address) || !network) {
    return response({ ok: false, message: "Choose a supported EVM or Solana mainnet account and connect it through the wallet modal." }, 400);
  }
  const row = await userRow(user);
  const nonce = hex(crypto.getRandomValues(new Uint8Array(32)));
  const issuedAt = new Date();
  const expiresAt = new Date(issuedAt.getTime() + 5 * 60 * 1000);
  const message = [
    "9N8 WALLET OWNERSHIP VERIFICATION",
    "Application: 9N8 Trading Intelligence",
    "Origin: https://9n8-secure-connect.vercel.app",
    "Telegram account: " + String(user.id),
    "Wallet address: " + address,
    "Network: " + namespace + ":" + chainId,
    "Purpose: Prove control of this public wallet address.",
    "Nonce: " + nonce,
    "Issued at: " + issuedAt.toISOString(),
    "Expires at: " + expiresAt.toISOString(),
    "",
    "IMPORTANT: This signature verifies address ownership only. It does not authorize a trade, transfer, token approval, or any other transaction.",
  ].join("\n");
  await db("n8_wallet_challenges", "POST", {
    user_id: row.id, provider, chain_namespace: namespace, chain_id: chainId,
    network, public_address: address, nonce, message, expires_at: expiresAt.toISOString(),
  }, "return=minimal");
  await audit(row.id, null, "wallet_challenge_created", "ok", { provider, namespace, chain_id: chainId });
  return response({ ok: true, challengeId: nonce, message, expiresAt: expiresAt.toISOString(), network });
}
async function verifyAndSaveWallet(user: any, body: any): Promise<Response> {
  const row = await userRow(user);
  const challengeId = clean(body?.challengeId, 64);
  const signature = clean(body?.signature, 1024);
  if (!/^[a-f0-9]{64}$/.test(challengeId) || !signature) {
    return response({ ok: false, message: "Wallet proof is incomplete. Reconnect and try again." }, 400);
  }
  const now = new Date().toISOString();
  const challenges = await db(
    "n8_wallet_challenges?select=*&user_id=eq." + encodeURIComponent(row.id) +
      "&nonce=eq." + encodeURIComponent(challengeId) +
      "&used_at=is.null&expires_at=gt." + encodeURIComponent(now) + "&limit=1"
  );
  const challenge = Array.isArray(challenges) ? challenges[0] : null;
  if (!challenge) return response({ ok: false, message: "Wallet challenge expired or already used. Start the connection again." }, 400);
  let valid = false;
  try { valid = await verifyWalletProof(challenge, signature); } catch { valid = false; }
  if (!valid) {
    await audit(row.id, null, "wallet_signature_invalid", "rejected", { namespace: challenge.chain_namespace });
    return response({ ok: false, message: "The wallet did not sign the verification message. No wallet was linked." }, 400);
  }
  const consumed = await db(
    "n8_wallet_challenges?id=eq." + encodeURIComponent(challenge.id) +
      "&user_id=eq." + encodeURIComponent(row.id) + "&used_at=is.null&expires_at=gt." + encodeURIComponent(now),
    "PATCH", { used_at: now }, "return=representation"
  );
  if (!Array.isArray(consumed) || consumed.length !== 1) {
    return response({ ok: false, message: "This wallet challenge has already been used. Reconnect and try again." }, 409);
  }
  const walletName = clean(body?.walletName, 60) || "WalletConnect";
  const provider = ["phantom", "trust_wallet", "metamask"].includes(challenge.provider)
    ? challenge.provider : "walletconnect";
  const connectionType = clean(body?.connectionType, 40);
  const saved = await db(
    "n8_wallet_connections?on_conflict=user_id,chain_namespace,network,public_address",
    "POST",
    {
      user_id: row.id, provider, chain_namespace: challenge.chain_namespace, chain_id: challenge.chain_id,
      network: challenge.network, public_address: challenge.public_address,
      label: walletName + " · verified wallet session",
      status: "verified",
      connection_type: ["walletconnect_session", "injected_session"].includes(connectionType)
        ? connectionType : "walletconnect_session",
      last_connected_at: now, disconnected_at: null,
    },
    "resolution=merge-duplicates,return=representation"
  );
  const walletRow = Array.isArray(saved) ? saved[0] : null;
  if (!walletRow?.id) throw new Error("wallet_storage_failed");
  await audit(row.id, null, "wallet_session_verified", "ok", {
    provider, namespace: challenge.chain_namespace, network: challenge.network,
    wallet_connection_id: walletRow.id,
  });
  return response({
    ok: true, connectionId: walletRow.id, address: mask(String(challenge.public_address)),
    network: challenge.network, walletName, status: "verified",
    executionMode: walletRow.execution_mode ?? "approval",
    autoTradingEnabled: walletRow.auto_trade_enabled === true,
  });
}
async function revokeWalletSession(user: any, body: any): Promise<Response> {
  const row = await userRow(user);
  const namespace = String(body?.namespace ?? "");
  const address = clean(body?.address, 128);
  const chainId = clean(body?.chainId, 64);
  const network = clean(body?.network, 80);
  if (!walletAddressValid(namespace, address) || !chainId || !network) {
    return response({ ok: false, message: "Select a valid linked wallet before disconnecting." }, 400);
  }
  const changed = await db(
    "n8_wallet_connections?user_id=eq." + encodeURIComponent(row.id) +
      "&chain_namespace=eq." + encodeURIComponent(namespace) +
      "&chain_id=eq." + encodeURIComponent(chainId) +
      "&network=eq." + encodeURIComponent(network) +
      "&public_address=eq." + encodeURIComponent(address) + "&status=neq.revoked",
    "PATCH", { status: "revoked", disconnected_at: new Date().toISOString(), auto_trade_enabled: false },
    "return=representation"
  );
  if (!Array.isArray(changed) || changed.length === 0) {
    return response({ ok: false, message: "No active linked wallet matched this account and address." }, 404);
  }
  await audit(row.id, null, "wallet_session_revoked", "ok", { namespace, network });
  return response({ ok: true, message: "Wallet link revoked. No future 9N8 wallet requests will be sent for this record." });
}
function mask(address: string): string {
  return address.length > 14 ? address.slice(0, 6) + "..." + address.slice(-5) : address;
}
function connectHtml(): Response {
  const html = [
"<!doctype html>",
"<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'><meta name='theme-color' content='#111827'>",
"<title>9N8 Secure Connections</title><script src='https://telegram.org/js/telegram-web-app.js?67'></script>",
"<style>:root{color-scheme:dark;font-family:system-ui,sans-serif;background:#0b1220;color:#edf2f7}*{box-sizing:border-box}body{margin:0;padding:18px;max-width:620px;margin:auto}h1{font-size:24px;margin:8px 0}p{color:#aab7ca;line-height:1.45}.card{background:#131e30;border:1px solid #26364d;border-radius:16px;padding:16px;margin:14px 0}label{display:block;font-size:13px;color:#afbdd0;margin:12px 0 6px}input,select{width:100%;padding:13px;border-radius:10px;border:1px solid #3a4b63;background:#0e1726;color:#fff;font-size:15px}input[type=checkbox]{width:auto;margin-right:8px}fieldset{border:1px solid #3a4b63;border-radius:10px;margin:14px 0;padding:8px 12px}fieldset label{margin:7px 0}button{width:100%;padding:14px;border:0;border-radius:10px;background:#2dbb80;color:#061a12;font-weight:700;font-size:15px;margin-top:15px}button:disabled{opacity:.55}small{color:#9aabc1}.warn{color:#f3c978}.status{min-height:24px;margin-top:10px;white-space:pre-wrap}.hidden{display:none}.pill{display:inline-block;background:#203b36;color:#8ce1bb;padding:5px 9px;border-radius:20px;font-size:12px}</style></head><body>",
"<div class='pill'>9N8 · Secure connection</div><h1>Connect your accounts</h1>",
"<p>Production API credentials are sent over HTTPS to 9N8 and stored encrypted. This step reads API-key permissions and account product settings only; it does not place orders, transfer assets or withdraw funds. Never enter credentials in Telegram chat.</p>",
"<div class='card'><h2>Exchange API connection</h2><p class='warn'>Production API permission discovery only. Use a key with Reading enabled and withdrawals / internal transfers disabled. This connection form never submits orders.</p>",
"<form id='exchangeForm' autocomplete='off'><label for='provider'>Exchange</label><select id='provider' required><option value='binance'>Binance Production API</option><option value='bybit'>Bybit Production API</option><option value='okx'>OKX Production API</option></select>",
"<label for='environment'>Environment</label><select id='environment' required><option value='live'>Production / Live API (read settings only)</option></select>",
  "<div id='binanceAutoRow'><label><input id='autoDetectMarkets' type='checkbox' checked> Auto-detect all products enabled on this Binance API key and account</label><small>Reads Spot, Margin, USDⓈ-M Futures, COIN-M Futures, Options and Portfolio Margin flags.</small></div>",
  "<fieldset id='marketFieldset'><legend>Products to verify (Bybit / OKX, or manual Binance selection)</legend><label><input type='checkbox' name='markets' value='spot' checked> Spot</label><label><input type='checkbox' name='markets' value='margin'> Margin</label><label><input type='checkbox' name='markets' value='futures_usdm'> USDⓈ-M Futures</label><label><input type='checkbox' name='markets' value='futures_coinm'> COIN-M Futures</label><label><input type='checkbox' name='markets' value='options'> Options</label></fieldset>",
"<label for='apiKey'>Production API key</label><input id='apiKey' type='password' autocomplete='off' required maxlength='512'>",
"<label for='apiSecret'>Production API secret</label><input id='apiSecret' type='password' autocomplete='off' required maxlength='1024'>",
"<div id='passphraseRow'><label for='passphrase'>OKX API passphrase</label><input id='passphrase' type='password' autocomplete='off' maxlength='256'></div>",
"<button id='exchangeButton' type='submit'>Verify and save connection</button><div id='exchangeStatus' class='status' role='status'></div></form></div>",
"<div class='card'><h2>Wallet address watch</h2><p>Link a public address for future read-only portfolio monitoring. This does not connect to your wallet or authorize transactions. Never enter a recovery phrase or private key.</p>",
"<form id='walletForm' autocomplete='off'><label for='walletProvider'>Wallet</label><select id='walletProvider'><option value='trust_wallet'>Trust Wallet</option><option value='phantom'>Phantom</option><option value='walletconnect'>WalletConnect address (watch-only)</option><option value='manual'>Other / public address</option></select>",
"<label for='chain'>Network</label><select id='chain'><option value='eip155:1'>Ethereum mainnet</option><option value='eip155:8453'>Base mainnet</option><option value='eip155:56'>BNB Smart Chain</option><option value='solana:mainnet-beta'>Solana mainnet</option></select>",
"<label for='address'>Public wallet address</label><input id='address' type='text' autocomplete='off' spellcheck='false' maxlength='128' required>",
"<button id='walletButton' type='submit'>Save watch-only address</button><div id='walletStatus' class='status' role='status'></div></form></div>",
"<small>Closing this page clears the form. Connections belong only to the Telegram account that opened this page.</small>",
"<script>(function(){const tg=window.Telegram&&window.Telegram.WebApp;const exchangeForm=document.getElementById('exchangeForm');const walletForm=document.getElementById('walletForm');const provider=document.getElementById('provider');const environment=document.getElementById('environment');const passphraseRow=document.getElementById('passphraseRow');const passphrase=document.getElementById('passphrase');const exchangeStatus=document.getElementById('exchangeStatus');const walletStatus=document.getElementById('walletStatus');if(tg){tg.ready();tg.expand()}",
"const autoDetect=document.getElementById('autoDetectMarkets');const marketFieldset=document.getElementById('marketFieldset');const binanceAutoRow=document.getElementById('binanceAutoRow');",
  "function showProvider(){const isBinance=provider.value==='binance';environment.innerHTML='';const o=document.createElement('option');o.value='live';o.textContent='Production / Live API (read settings only)';environment.appendChild(o);passphraseRow.classList.toggle('hidden',provider.value!=='okx');passphrase.required=provider.value==='okx';binanceAutoRow.classList.toggle('hidden',!isBinance);if(!isBinance)autoDetect.checked=false;marketFieldset.classList.toggle('hidden',isBinance&&autoDetect.checked)}provider.addEventListener('change',showProvider);autoDetect.addEventListener('change',showProvider);showProvider();",
"async function post(data){if(!tg||!tg.initData)throw new Error('Open this page from the 9N8 Telegram private chat.');const r=await fetch(location.pathname,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(Object.assign({initData:tg.initData},data))});const j=await r.json().catch(()=>({}));if(!r.ok||j.ok!==true)throw new Error(j.message||'Request failed.');return j}",
"exchangeForm.addEventListener('submit',async function(e){e.preventDefault();const button=document.getElementById('exchangeButton');button.disabled=true;exchangeStatus.textContent='Reading API-key and account product permissions…';const credentials={apiKey:document.getElementById('apiKey').value.trim(),apiSecret:document.getElementById('apiSecret').value.trim()};if(provider.value==='okx')credentials.passphrase=passphrase.value;const selected=Array.from(document.querySelectorAll('input[name=markets]:checked')).map(x=>x.value);const auto=provider.value==='binance'&&autoDetect.checked;if(!auto&&selected.length===0){exchangeStatus.textContent='Select at least one product to inspect.';button.disabled=false;return}try{const payload={action:'connect_exchange',provider:provider.value,environment:environment.value,credentials:credentials,autoDetectMarkets:auto};if(!auto)payload.requestedMarkets=selected;const j=await post(payload);const p=j.permissions||{};const detected=p.detectedMarkets||j.selectedMarkets||[];const k=p.keyPermissions||{};const a=p.accountProducts||{};const flag=v=>v===true?'ON':v===false?'OFF':'unknown';const mk=p.marketPermissions||{};const productSettings='\\nAPI-key flags: Read '+flag(k.reading)+', IP restriction '+flag(k.ipRestricted)+', Spot/Margin '+flag(k.spotAndMarginTrading)+', Margin '+flag(k.margin)+', Futures '+flag(k.futures)+', Options '+flag(k.vanillaOptions)+', Portfolio Margin '+flag(k.portfolioMarginTrading)+', Withdrawals '+flag(k.withdrawals)+', Internal Transfer '+flag(k.internalTransfer)+', Universal Transfer '+flag(k.universalTransfer)+', FIX Trade '+flag(k.fixApiTrade)+', FIX Read-only '+flag(k.fixReadOnly)+'.'+'\\nAccount products: Margin '+flag(a.marginEnabled)+', Futures '+flag(a.futuresEnabled)+', Options '+flag(a.optionsEnabled)+', Portfolio Margin '+flag(a.portfolioMarginEnabled)+'.'+'\\nDetected product checks: Spot '+String(mk.spot||'not detected')+', Margin '+String(mk.margin||'not detected')+', USD-M Futures '+String(mk.futures_usdm||'not detected')+', COIN-M Futures '+String(mk.futures_coinm||'not detected')+', Options '+String(mk.options||'not detected')+', Portfolio Margin '+String(p.portfolioMarginStatus||'not detected')+'.'+'\\n'+String(p.futuresScopeNote||'');exchangeStatus.textContent='Connected: '+j.label+'\\nDetected products: '+(detected.length?detected.join(', '):'No trading product scopes; read-only settings saved.')+(provider.value==='binance'?productSettings:'')+'\\nWithdrawal and transfer scopes are rejected. No order was placed.';exchangeForm.reset();autoDetect.checked=true;showProvider()}catch(err){exchangeStatus.textContent=err.message||'Connection failed.'}finally{credentials.apiKey='';credentials.apiSecret='';credentials.passphrase='';document.getElementById('apiKey').value='';document.getElementById('apiSecret').value='';passphrase.value='';button.disabled=false}});",
"walletForm.addEventListener('submit',async function(e){e.preventDefault();const button=document.getElementById('walletButton');button.disabled=true;walletStatus.textContent='Saving public address…';const selected=document.getElementById('chain');const parts=selected.value.split(':');try{const j=await post({action:'watch_wallet',provider:document.getElementById('walletProvider').value,namespace:parts[0],chainId:parts.slice(1).join(':'),network:selected.selectedOptions[0].textContent,address:document.getElementById('address').value.trim()});walletStatus.textContent='Saved '+j.address+' as a watch-only address. No wallet access or signing was granted.';document.getElementById('address').value=''}catch(err){walletStatus.textContent=err.message||'Could not save address.'}finally{button.disabled=false}})})();</script></body></html>"
  ].join("\n");
  return new Response(html, {
    status: 200,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      "referrer-policy": "no-referrer",
      "content-security-policy": "default-src 'none'; script-src https://telegram.org 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org",
    },
  });
}

function setupHtml(): Response {
  const html = [
    "<!doctype html>",
    "<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta name='theme-color' content='#111827'><title>9N8 Webhook Setup</title>",
    "<style>:root{color-scheme:dark;font-family:system-ui,sans-serif;background:#0b1220;color:#edf2f7}body{margin:0;padding:22px;max-width:540px;margin:auto}.card{margin-top:28px;padding:20px;background:#131e30;border:1px solid #26364d;border-radius:16px}label{display:block;margin:15px 0 7px;color:#aab7ca;font-size:14px}input{width:100%;box-sizing:border-box;padding:14px;border:1px solid #40516a;border-radius:10px;background:#0b1422;color:#fff;font-size:15px}button{width:100%;padding:14px;margin-top:16px;border:0;border-radius:10px;background:#2dbb80;color:#061a12;font-weight:700;font-size:15px}button:disabled{opacity:.6}.muted{color:#aab7ca;line-height:1.5}.warn{color:#f2c879}.status{white-space:pre-wrap;margin-top:12px;min-height:24px}</style></head><body>",
    "<div class='card'><h1>Activate 9N8 Telegram webhook</h1>",
    "<p class='muted'>This switches your Telegram bot from the old single-owner endpoint to the new universal candidate. Only do this after TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET are saved in the 9N8 Supabase Edge Function Secrets.</p>",
    "<p class='warn'>Enter only the webhook secret you created. Never enter your BotFather token here, in GitHub, or in this page.</p>",
    "<form id='setupForm'><label for='secret'>TELEGRAM_WEBHOOK_SECRET</label><input id='secret' type='password' autocomplete='off' required minlength='32' maxlength='256'><button id='activate' type='submit'>Register new webhook</button><div id='status' class='status' role='status'></div></form>",
    "<p class='muted'>The webhook secret is sent over HTTPS to the 9N8 server. The page does not save it in browser storage. Telegram receives the same secret as its webhook header token.</p></div>",
    "<script>(function(){const form=document.getElementById('setupForm');const secret=document.getElementById('secret');const status=document.getElementById('status');const button=document.getElementById('activate');form.addEventListener('submit',async function(e){e.preventDefault();button.disabled=true;status.textContent='Registering webhook with Telegram…';const value=secret.value;try{const r=await fetch(location.pathname,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({action:'register_webhook',setupSecret:value})});const j=await r.json().catch(()=>({}));if(!r.ok||j.ok!==true)throw new Error(j.message||'Webhook registration failed.');status.textContent='Webhook registered successfully. Telegram will now send updates to the new 9N8 candidate. You can close this page and send /start to your bot.';secret.value='';}catch(err){status.textContent=err.message||'Registration failed. Check the secrets in Supabase.';}finally{button.disabled=false;}});})();</script>",
    "</body></html>"
  ].join("\n");
  return new Response(html, {
    status: 200,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      "referrer-policy": "no-referrer",
      "content-security-policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    },
  });
}

async function paperTerminalData(userId: string): Promise<Obj> {
  const sessions = await db(
    "n8_paper_sessions?select=id,market_type,status,started_at,last_activity_at,initial_balance_usd,cash_balance_usd,tick_count,last_tick_at,last_tick_error,config&user_id=eq." +
      encodeURIComponent(userId) + "&order=created_at.desc&limit=1"
  );
  const session = Array.isArray(sessions) ? sessions[0] : null;
  if (!session) return { session: null, tick: null, positions: [], trades: [] };
  const [ticks, positions, trades] = await Promise.all([
    db("n8_paper_ticks?select=tick_at,symbols_scanned,symbols_failed,signals_generated,opened_trades,closed_trades,cash_balance_usd,equity_usd,realized_pnl_usd,unrealized_pnl_usd,status,metadata&session_id=eq." +
      encodeURIComponent(String(session.id)) + "&order=tick_at.desc&limit=1"),
    db("n8_paper_positions?select=id,symbol,quantity,average_entry_price,mark_price,unrealized_pnl_usd,realized_pnl_usd,stop_loss_price,take_profit_price,opened_at&session_id=eq." +
      encodeURIComponent(String(session.id)) + "&status=eq.open&order=opened_at.desc&limit=20"),
    db("n8_paper_trades?select=id,symbol,side,quantity,fill_price,reference_price,notional_usd,fee_usd,realized_pnl_usd,signal_reason,filled_at,metadata&session_id=eq." +
      encodeURIComponent(String(session.id)) + "&order=filled_at.desc&limit=50"),
  ]);
  return {
    session,
    tick: Array.isArray(ticks) ? ticks[0] ?? null : null,
    positions: Array.isArray(positions) ? positions : [],
    trades: Array.isArray(trades) ? trades : [],
  };
}
async function liveBinanceAccount(user: any, userId: string): Promise<Obj> {
  const rows = await db(
    "n8_exchange_connections?select=id,label,account_hint,permissions,last_validated_at&user_id=eq." +
      encodeURIComponent(userId) + "&provider=eq.binance&environment=eq.live&status=eq.active&order=created_at.desc&limit=1"
  );
  const connection = Array.isArray(rows) ? rows[0] : null;
  if (!connection?.id) throw new Error("binance_live_connection_missing");
  const credentials = await db("rpc/n8_get_exchange_credentials", "POST", {
    p_telegram_user_id: Number(user.id),
    p_connection_id: connection.id,
  });
  const exchangeCredentials = {
    apiKey: clean(credentials?.apiKey, 512),
    apiSecret: clean(credentials?.apiSecret, 1024),
  };
  // Re-read all Binance product permissions on every refresh. Never assume the
  // connection's original market list is still current after an API-key change.
  const validation = await validateBinanceLive(exchangeCredentials);
  const detectedMarkets = validation.permissions.detectedMarkets as ExchangeMarket[];

  if (!detectedMarkets.some((market) => ["spot", "margin"].includes(market))) {
    return {
      connection: { id: connection.id, label: connection.label, accountHint: connection.account_hint },
      balances: [],
      permissions: validation.permissions,
      updatedAt: new Date().toISOString(),
      liveOrdersEnabled: false,
      note: "Binance key permissions and account product settings were refreshed. This panel displays Spot balances only; detected Futures, Margin and Options access is listed separately.",
    };
  }

  const account = await binanceSignedGet(exchangeCredentials, "/api/v3/account");
  if (!Array.isArray(account?.balances)) {
    throw new Error("binance_spot_account_read_failed");
  }
  const balances = account.balances
    .map((entry: any) => ({
      asset: String(entry.asset ?? ""),
      free: Number(entry.free ?? 0),
      locked: Number(entry.locked ?? 0),
    }))
    .filter((entry: any) => Number.isFinite(entry.free) && Number.isFinite(entry.locked) &&
      entry.free + entry.locked > 0)
    .slice(0, 100);
  const quote = balances.find((entry: any) => entry.asset === "USDT") ?? { free: 0, locked: 0 };
  const observedAt = new Date().toISOString();
  const evidence = {
    source: "binance_signed_spot_account",
    connectionId: connection.id,
    quoteAsset: "USDT",
    availableQuote: quote.free,
    availableQuoteUsdValuation: "USDT nominal USD peg at 1.00",
    balances,
    accountCanTrade: true,
    observedAt,
  };
  await db("n8_live_balance_checks", "POST", {
    user_id: userId,
    account_type: "exchange",
    connection_id: connection.id,
    provider: "binance",
    quote_asset: "USDT",
    available_quote: quote.free,
    available_quote_usd: quote.free,
    observed_at: observedAt,
    source: "binance_signed_spot_account",
    evidence_hash: await sha256Hex(canonicalJson(evidence)),
    evidence,
  }, "return=minimal");
  await audit(userId, null, "binance_balance_refreshed", "ok", {
    connection_id: connection.id,
    quote_asset: "USDT",
    available_quote: quote.free,
    observed_at: observedAt,
  });
  return {
    connection: { id: connection.id, label: connection.label, accountHint: connection.account_hint },
    balances,
    quoteBalance: { asset: "USDT", free: quote.free, locked: quote.locked, availableUsdApprox: quote.free },
    permissions: validation.permissions,
    accountCanTrade: true,
    updatedAt: observedAt,
    liveOrdersEnabled: false,
    note: "Spot balances only. Your other selected permissions are listed separately; no order was placed.",
  };
}
async function liveOkxAccount(user: any, userId: string): Promise<Obj> {
  const rows = await db(
    "n8_exchange_connections?select=id,label,account_hint,permissions,last_validated_at&user_id=eq." +
      encodeURIComponent(userId) + "&provider=eq.okx&environment=eq.live&status=eq.active&order=created_at.desc&limit=1"
  );
  const connection = Array.isArray(rows) ? rows[0] : null;
  if (!connection?.id) throw new Error("okx_live_connection_missing");
  const stored = await db("rpc/n8_get_exchange_credentials", "POST", {
    p_telegram_user_id: Number(user.id),
    p_connection_id: connection.id,
  });
  const regionValue = String(stored?.region ?? "global");
  const credentials: Credentials = {
    apiKey: clean(stored?.apiKey, 512),
    apiSecret: clean(stored?.apiSecret, 1024),
    passphrase: clean(stored?.passphrase, 256),
    region: (["global", "us", "eea"].includes(regionValue) ? regionValue : "global") as OkxRegion,
  };
  const markets = Array.isArray(connection.permissions?.selectedMarkets)
    ? normalizeRequestedMarkets(connection.permissions.selectedMarkets)
    : ["spot" as ExchangeMarket];
  const validation = await validateOKX(credentials, markets);
  const origin = OKX_API_ORIGINS[credentials.region ?? "global"];
  const timestampResponse = await fetch(origin + "/api/v5/public/time", { cache: "no-store" });
  const timePayload = await timestampResponse.json().catch(() => ({}));
  const milliseconds = Number(timePayload?.data?.[0]?.ts);
  if (!timestampResponse.ok || String(timePayload?.code ?? "") !== "0" || !Number.isFinite(milliseconds)) {
    throw new Error("exchange_temporarily_unavailable");
  }
  const timestamp = new Date(milliseconds).toISOString();
  const path = "/api/v5/account/balance";
  const signature = b64(await mac(
    enc.encode(credentials.apiSecret),
    timestamp + "GET" + path,
  ));
  const balancePayload = await jsonGet(origin + path, {
    "OK-ACCESS-KEY": credentials.apiKey,
    "OK-ACCESS-SIGN": signature,
    "OK-ACCESS-TIMESTAMP": timestamp,
    "OK-ACCESS-PASSPHRASE": credentials.passphrase ?? "",
    accept: "application/json",
  });
  if (String(balancePayload?.code ?? "") !== "0" || !balancePayload?.data?.[0]) {
    throw new Error("exchange_credentials_rejected");
  }
  const account = balancePayload.data[0];
  const details = Array.isArray(account.details) ? account.details : [];
  const balances = details.map((entry: any) => ({
    asset: String(entry.ccy ?? ""),
    free: Number(entry.availBal ?? 0),
    locked: Math.max(0, Number(entry.eq ?? 0) - Number(entry.availBal ?? 0)),
  })).filter((entry: any) => Number.isFinite(entry.free) && Number.isFinite(entry.locked) &&
    entry.free + entry.locked > 0).slice(0, 100);
  const quote = balances.find((entry: any) => entry.asset === "USDT") ?? { free: 0, locked: 0 };
  const observedAt = new Date().toISOString();
  const evidence = {
    source: "okx_signed_account_balance",
    connectionId: connection.id,
    region: credentials.region,
    quoteAsset: "USDT",
    availableQuote: quote.free,
    availableQuoteUsdValuation: "USDT nominal USD peg at 1.00",
    balances,
    accountLevel: account.acctLv ?? null,
    permissions: validation.permissions,
    observedAt,
  };
  await db("n8_live_balance_checks", "POST", {
    user_id: userId,
    account_type: "exchange",
    connection_id: connection.id,
    provider: "okx",
    quote_asset: "USDT",
    available_quote: quote.free,
    available_quote_usd: quote.free,
    observed_at: observedAt,
    source: "okx_signed_account_balance",
    evidence_hash: await sha256Hex(canonicalJson(evidence)),
    evidence,
  }, "return=minimal");
  await audit(userId, null, "okx_balance_refreshed", "ok", {
    connection_id: connection.id,
    quote_asset: "USDT",
    available_quote: quote.free,
    observed_at: observedAt,
    permission_scopes: "checked from signed account config",
  });
  return {
    connection: { id: connection.id, label: connection.label, accountHint: connection.account_hint },
    balances,
    quoteBalance: { asset: "USDT", free: quote.free, locked: quote.locked, equity: Number(account.totalEq ?? 0), availableUsdApprox: quote.free },
    permissions: validation.permissions,
    accountEquityUsdApprox: Number(account.totalEq ?? 0),
    updatedAt: observedAt,
    liveOrdersEnabled: false,
    note: "Account balances only. OKX product availability still depends on your account mode and supported instruments; no order was placed.",
  };
}

async function liveBybitAccount(user: any, userId: string): Promise<Obj> {
  const rows = await db(
    "n8_exchange_connections?select=id,label,account_hint,permissions,last_validated_at&user_id=eq." +
      encodeURIComponent(userId) + "&provider=eq.bybit&environment=eq.live&status=eq.active&order=created_at.desc&limit=1"
  );
  const connection = Array.isArray(rows) ? rows[0] : null;
  if (!connection?.id) throw new Error("bybit_live_connection_missing");
  const stored = await db("rpc/n8_get_exchange_credentials", "POST", {
    p_telegram_user_id: Number(user.id),
    p_connection_id: connection.id,
  });
  const regionValue = String(stored?.bybitRegion ?? "global");
  const credentials: Credentials = {
    apiKey: clean(stored?.apiKey, 512),
    apiSecret: clean(stored?.apiSecret, 1024),
    bybitRegion: (["global", "eea", "uae", "turkey", "kazakhstan", "georgia", "indonesia", "japan"].includes(regionValue)
      ? regionValue : "global") as BybitRegion,
  };
  const markets = Array.isArray(connection.permissions?.selectedMarkets)
    ? normalizeRequestedMarkets(connection.permissions.selectedMarkets)
    : ["spot" as ExchangeMarket];
  const validation = await validateBybit(credentials, markets);
  const payload = await bybitSignedGet(credentials, "/v5/account/wallet-balance", { accountType: "UNIFIED" });
  if (Number(payload?.retCode) !== 0 || !payload?.result?.list?.[0]) {
    throw new Error("bybit_unified_account_balance_unavailable");
  }
  const account = payload.result.list[0];
  const coins = Array.isArray(account.coin) ? account.coin : [];
  const balances = coins.map((entry: any) => {
    const total = Number(entry.walletBalance ?? 0);
    const free = Number(entry.availableToWithdraw ?? entry.transferBalance ?? entry.walletBalance ?? 0);
    return {
      asset: String(entry.coin ?? ""),
      free,
      locked: Math.max(0, total - free),
    };
  }).filter((entry: any) => Number.isFinite(entry.free) && Number.isFinite(entry.locked) &&
    entry.free + entry.locked > 0).slice(0, 100);
  const quote = balances.find((entry: any) => entry.asset === "USDT") ?? { free: 0, locked: 0 };
  const observedAt = new Date().toISOString();
  await audit(userId, null, "bybit_balance_refreshed", "ok", {
    connection_id: connection.id,
    quote_asset: "USDT",
    available_quote: quote.free,
    observed_at: observedAt,
  });
  return {
    connection: { id: connection.id, label: connection.label, accountHint: connection.account_hint },
    balances,
    quoteBalance: { asset: "USDT", free: quote.free, locked: quote.locked, availableUsdApprox: quote.free },
    accountEquityUsdApprox: Number(account.totalEquity ?? 0),
    permissions: validation.permissions,
    updatedAt: observedAt,
    liveOrdersEnabled: false,
    note: "Bybit Unified Account balances only. Margin mode must be enabled in Bybit where required; no order was placed.",
  };
}

async function verifiedSolanaWallet(userId: string, walletConnectionId: string, address: string) {
  if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(address) || !walletConnectionId) {
    throw new Error("verified_solana_wallet_required");
  }
  const rows = await db(
    "n8_wallet_connections?select=id,user_id,provider,chain_namespace,chain_id,network,public_address,status,verified_at,connection_type&id=eq." +
      encodeURIComponent(walletConnectionId) + "&user_id=eq." + encodeURIComponent(userId) +
      "&chain_namespace=eq.solana&chain_id=eq.mainnet-beta&network=eq.Solana%20Mainnet&status=eq.verified&limit=1"
  );
  const wallet = Array.isArray(rows) ? rows[0] : null;
  if (!wallet || wallet.public_address !== address || !wallet.verified_at ||
      !["phantom", "trust_wallet", "walletconnect"].includes(String(wallet.provider)) ||
      !["walletconnect_session", "injected_session"].includes(String(wallet.connection_type))) {
    throw new Error("verified_solana_wallet_required");
  }
  return wallet;
}

async function solanaMintDecimals(mint: string): Promise<number> {
  let publicKey: PublicKey;
  try { publicKey = new PublicKey(mint); } catch { throw new Error("solana_mint_invalid"); }
  const canonical = publicKey.toBase58();
  if (canonical !== mint) throw new Error("solana_mint_invalid");
  if (canonical === SOL_MINT) return 9;
  const rpc = await fetch(SOLANA_RPC_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      jsonrpc: "2.0", id: 1, method: "getTokenSupply",
      params: [canonical, { commitment: "confirmed" }],
    }),
    cache: "no-store",
    credentials: "omit",
  });
  const payload = await rpc.json().catch(() => ({}));
  const decimals = Number(payload?.result?.value?.decimals);
  const supply = BigInt(String(payload?.result?.value?.amount ?? "0"));
  if (!rpc.ok || payload?.error || !Number.isInteger(decimals) ||
      decimals < 0 || decimals > 18 || supply <= 0n) {
    throw new Error("solana_mint_not_found_or_not_a_token");
  }
  return decimals;
}

async function rpcSignatureStatus(signature: string): Promise<any> {
  const rpc = await fetch(SOLANA_RPC_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      jsonrpc: "2.0", id: 1, method: "getSignatureStatuses",
      params: [[signature], { searchTransactionHistory: true }],
    }),
    cache: "no-store",
    credentials: "omit",
  });
  const payload = await rpc.json().catch(() => ({}));
  if (!rpc.ok || payload?.error) throw new Error("solana_status_unavailable");
  return payload?.result?.value?.[0] ?? null;
}

async function createJupiterSwapOrder(user: any, row: any, body: any): Promise<Response> {
  requireJupiterApiKey();
  const address = clean(body?.address, 64);
  const wallet = await verifiedSolanaWallet(row.id, clean(body?.walletConnectionId, 64), address);
  const side = String(body?.side ?? "").toLowerCase();
  if (!["buy", "sell"].includes(side)) throw new Error("swap_side_invalid");
  const tokenMint = clean(body?.tokenMint, 64);
  if (!tokenMint || tokenMint === SOL_MINT) throw new Error("swap_token_mint_invalid");

  const tokenDecimals = await solanaMintDecimals(tokenMint);
  const inputMint = side === "buy" ? SOL_MINT : tokenMint;
  const outputMint = side === "buy" ? tokenMint : SOL_MINT;
  const inputDecimals = side === "buy" ? 9 : tokenDecimals;
  const outputDecimals = side === "buy" ? tokenDecimals : 9;
  const amountRaw = decimalToRaw(String(body?.amount ?? ""), inputDecimals);
  const query = new URLSearchParams({
    inputMint,
    outputMint,
    amount: amountRaw.toString(),
    taker: wallet.public_address,
  });
  const order = await jupiterApi<any>("/swap/v2/order?" + query.toString(), {
    apiKey: JUPITER_API_KEY || undefined,
  });
  assertSolanaOrder(order, wallet.public_address);
  const impact = priceImpactFraction(order.priceImpactPct ?? 0);
  let tx: VersionedTransaction;
  try {
    tx = VersionedTransaction.deserialize(base64ToBytes(String(order.transaction)));
  } catch {
    throw new Error("jupiter_transaction_decode_failed");
  }
  if (tx.message.version !== 0) throw new Error("jupiter_transaction_version_unsupported");
  const payer = tx.message.staticAccountKeys?.[0]?.toBase58?.() ?? "";
  if (payer !== wallet.public_address) throw new Error("jupiter_transaction_fee_payer_mismatch");

  // Keep a conservative per-order and rolling 24-hour SOL-equivalent cap until
  // the connector's certification process has completed.
  const solEquivalent = side === "buy" ? amountRaw : BigInt(String(order.outAmount));
  if (solEquivalent > MAX_SWAP_SOL_LAMPORTS) throw new Error("swap_per_order_cap_exceeded");
  const history = await db(
    "n8_jupiter_swaps?select=sol_lamports_equivalent,status,expires_at,created_at&user_id=eq." +
      encodeURIComponent(row.id) + "&created_at=gte." +
      encodeURIComponent(new Date(Date.now() - 86400000).toISOString()) +
      "&order=created_at.desc&limit=25"
  );
  const prior = Array.isArray(history) ? history : [];
  if (prior.some((item: any) =>
    ["executing", "unknown"].includes(String(item.status)) ||
    (item.status === "quote_ready" && new Date(item.expires_at).getTime() > Date.now())
  )) throw new Error("swap_previous_request_unresolved");
  const dailyTotal = prior
    .filter((item: any) => ["success", "executing", "unknown"].includes(String(item.status)))
    .reduce((sum: bigint, item: any) => sum + BigInt(String(item.sol_lamports_equivalent ?? "0")), 0n);
  if (dailyTotal + solEquivalent > DAILY_SWAP_SOL_LAMPORTS) throw new Error("swap_daily_cap_exceeded");

  const messageHash = await sha256HexBytes(tx.message.serialize());
  const expiresAt = new Date(Date.now() + SWAP_QUOTE_TTL_SECONDS * 1000).toISOString();
  const stored = await db("n8_jupiter_swaps", "POST", {
    user_id: row.id,
    wallet_connection_id: wallet.id,
    request_id: String(order.requestId),
    input_mint: inputMint,
    output_mint: outputMint,
    input_amount_raw: String(order.inAmount),
    output_amount_raw: String(order.outAmount),
    minimum_output_raw: String(order.otherAmountThreshold),
    input_decimals: inputDecimals,
    output_decimals: outputDecimals,
    router: String(order.router ?? "unknown"),
    price_impact_pct: impact,
    sol_lamports_equivalent: solEquivalent.toString(),
    transaction_message_sha256: messageHash,
    status: "quote_ready",
    expires_at: expiresAt,
    quote_metadata: {
      mode: order.mode ?? "ultra",
      feeBps: order.feeBps ?? null,
      feeMint: order.feeMint ?? null,
      lastValidBlockHeight: order.lastValidBlockHeight ?? null,
      expireAt: order.expireAt ?? null,
    },
  }, "return=representation");
  const saved = Array.isArray(stored) ? stored[0] : null;
  if (!saved?.id) throw new Error("jupiter_quote_storage_failed");
  await audit(row.id, null, "jupiter_swap_quote_created", "ok", {
    side, order_id: saved.id, request_id: order.requestId, router: order.router, price_impact_pct: impact,
  });
  return response({
    ok: true,
    orderId: saved.id,
    requestId: order.requestId,
    side,
    tokenMint,
    inputMint,
    outputMint,
    inputAmount: rawToDecimal(String(order.inAmount), inputDecimals),
    outputAmount: rawToDecimal(String(order.outAmount), outputDecimals),
    minimumOutput: rawToDecimal(String(order.otherAmountThreshold), outputDecimals),
    inputAmountRaw: String(order.inAmount),
    outputAmountRaw: String(order.outAmount),
    outputDecimals,
    router: order.router,
    priceImpactPct: impact,
    feeBps: order.feeBps ?? null,
    expiresAt,
    transaction: order.transaction,
    transactionVersion: 0,
    lastValidBlockHeight: order.lastValidBlockHeight ?? null,
    liveExecutionCertification: "locked_until_passed",
  });
}

async function executeJupiterSwap(user: any, row: any, body: any): Promise<Response> {
  const orderId = clean(body?.orderId, 64);
  const requestId = clean(body?.requestId, 128);
  const signedTransaction = clean(body?.signedTransaction, 120000);
  if (!orderId || !requestId || !signedTransaction) throw new Error("swap_signed_payload_missing");

  const rows = await db(
    "n8_jupiter_swaps?select=*&id=eq." + encodeURIComponent(orderId) +
      "&request_id=eq." + encodeURIComponent(requestId) +
      "&user_id=eq." + encodeURIComponent(row.id) + "&limit=1"
  );
  const saved = Array.isArray(rows) ? rows[0] : null;
  if (!saved) throw new Error("swap_quote_not_found");
  if (saved.status !== "quote_ready") throw new Error("swap_request_already_claimed_or_final");
  if (new Date(saved.expires_at).getTime() <= Date.now()) {
    await db(
      "n8_jupiter_swaps?id=eq." + encodeURIComponent(orderId) +
        "&user_id=eq." + encodeURIComponent(row.id) + "&status=eq.quote_ready",
      "PATCH", { status: "expired", updated_at: new Date().toISOString() }, "return=minimal"
    );
    throw new Error("swap_quote_expired");
  }

  const certificationRows = await db(
    "n8_jupiter_connector_certification?select=status,live_manual_wallet_execution_enabled&provider=eq.jupiter_swap_v2&limit=1"
  );
  const certification = Array.isArray(certificationRows) ? certificationRows[0] : null;
  if (!certification || certification.status !== "passed" ||
      certification.live_manual_wallet_execution_enabled !== true) {
    await db(
      "n8_jupiter_swaps?id=eq." + encodeURIComponent(orderId) + "&user_id=eq." + encodeURIComponent(row.id),
      "PATCH", { status: "failed", result_payload: { error: "jupiter_execution_certification_locked" }, updated_at: new Date().toISOString() },
      "return=minimal"
    );
    throw new Error("jupiter_execution_certification_locked");
  }
  const walletRows = await db(
    "n8_wallet_connections?select=id,public_address,status,chain_namespace,chain_id,network,verified_at&id=eq." +
      encodeURIComponent(String(saved.wallet_connection_id)) + "&user_id=eq." +
      encodeURIComponent(row.id) + "&status=eq.verified&limit=1"
  );
  const wallet = Array.isArray(walletRows) ? walletRows[0] : null;
  if (!wallet || !wallet.verified_at || wallet.chain_namespace !== "solana" ||
      wallet.chain_id !== "mainnet-beta") throw new Error("verified_solana_wallet_required");

  let tx: VersionedTransaction;
  try { tx = VersionedTransaction.deserialize(base64ToBytes(signedTransaction)); }
  catch { throw new Error("swap_signed_transaction_invalid"); }
  if (tx.message.version !== 0) throw new Error("jupiter_transaction_version_unsupported");
  const payer = tx.message.staticAccountKeys?.[0]?.toBase58?.() ?? "";
  if (payer !== wallet.public_address) throw new Error("swap_signed_transaction_owner_mismatch");
  const messageBytes = tx.message.serialize();
  const messageHash = await sha256HexBytes(messageBytes);
  if (messageHash !== saved.transaction_message_sha256) throw new Error("swap_signed_transaction_changed");
  const signature = tx.signatures?.[0];
  if (!signature || signature.length !== 64 || signature.every((v: number) => v === 0)) {
    throw new Error("wallet_transaction_signature_missing");
  }
  const publicKeyData = new Uint8Array(new PublicKey(wallet.public_address).toBytes()).buffer;
  const signatureData = new Uint8Array(signature).buffer;
  const messageData = new Uint8Array(messageBytes).buffer;
  const publicKey = await crypto.subtle.importKey("raw", publicKeyData, { name: "Ed25519" }, false, ["verify"]);
  if (!await crypto.subtle.verify("Ed25519", publicKey, signatureData, messageData)) {
    throw new Error("wallet_transaction_signature_invalid");
  }

  const encodedSignature = base58Encode(signature);
  const now = new Date().toISOString();
  const claimed = await db(
    "n8_jupiter_swaps?id=eq." + encodeURIComponent(orderId) +
      "&user_id=eq." + encodeURIComponent(row.id) +
      "&request_id=eq." + encodeURIComponent(requestId) +
      "&status=eq.quote_ready&expires_at=gt." + encodeURIComponent(now),
    "PATCH",
    { status: "executing", owner_signed_at: now, signature: encodedSignature, updated_at: now },
    "return=representation"
  );
  if (!Array.isArray(claimed) || claimed.length !== 1) {
    throw new Error("swap_request_already_claimed_or_final");
  }

  // Certification is deliberately checked after the owner signature is verified,
  // and before any transaction is handed to Jupiter for submission.

  try {
    const execution = await jupiterApi<any>("/swap/v2/execute", {
      method: "POST",
      apiKey: JUPITER_API_KEY || undefined,
      body: {
        signedTransaction,
        requestId,
        ...(saved.quote_metadata?.lastValidBlockHeight
          ? { lastValidBlockHeight: Number(saved.quote_metadata.lastValidBlockHeight) }
          : {}),
      },
    });
    const state = execution.status === "Success" && Number(execution.code ?? 0) === 0
      ? "success"
      : execution.status === "Failed" ? "failed" : "unknown";
    const actualSignature = String(execution.signature || encodedSignature);
    await db(
      "n8_jupiter_swaps?id=eq." + encodeURIComponent(orderId) + "&user_id=eq." + encodeURIComponent(row.id),
      "PATCH",
      { status: state, signature: actualSignature, result_payload: execution, updated_at: new Date().toISOString() },
      "return=minimal"
    );
    await audit(row.id, null, "jupiter_swap_execute_" + state,
      state === "success" ? "ok" : "warning",
      { order_id: orderId, signature: actualSignature, router: saved.router }
    );
    return response({
      ok: true, status: state, signature: actualSignature, result: execution,
      explorerUrl: "https://solscan.io/tx/" + actualSignature,
    });
  } catch {
    // The transaction has been signed and atomically claimed: never blindly resubmit it.
    await db(
      "n8_jupiter_swaps?id=eq." + encodeURIComponent(orderId) + "&user_id=eq." + encodeURIComponent(row.id),
      "PATCH",
      { status: "unknown", signature: encodedSignature, result_payload: { error: "execute_outcome_unknown" }, updated_at: new Date().toISOString() },
      "return=minimal"
    );
    await audit(row.id, null, "jupiter_swap_execute_unknown", "warning", { order_id: orderId, signature: encodedSignature });
    return response({
      ok: true, status: "unknown", signature: encodedSignature,
      explorerUrl: "https://solscan.io/tx/" + encodedSignature,
      message: "The result is unknown. 9N8 will check Solana for this signature; do not retry this swap.",
    }, 202);
  }
}

async function getJupiterLiveConfig(row: any): Promise<Response> {
  const cert = await jupiterTriggerCertification();
  return response({
    ok: true,
    manualWalletExecutionEnabled: cert?.status === "passed" && cert?.live_manual_wallet_execution_enabled === true,
    delegatedAutomationEnabled: cert?.status === "passed" && cert?.delegated_automation_enabled === true,
    certificationStatus: cert?.status ?? "missing",
    jupiterApiConfigured: Boolean(JUPITER_API_KEY),
    triggerApiConfigured: Boolean(JUPITER_API_KEY),
    maxSwapSol: "0.05",
    dailySwapSol: "0.10",
    maxPriceImpactPct: 2,
  });
}
async function getJupiterSwapStatus(user: any, row: any, body: any): Promise<Response> {
  const orderId = clean(body?.orderId, 64);
  const rows = await db(
    "n8_jupiter_swaps?select=id,status,signature,input_mint,output_mint,input_amount_raw,output_amount_raw,input_decimals,output_decimals,router,price_impact_pct,expires_at,created_at,result_payload&user_id=eq." +
      encodeURIComponent(row.id) + "&id=eq." + encodeURIComponent(orderId) + "&limit=1"
  );
  const order = Array.isArray(rows) ? rows[0] : null;
  if (!order) throw new Error("swap_quote_not_found");
  let status = String(order.status);
  if (["executing", "unknown"].includes(status) && order.signature) {
    try {
      const observed = await rpcSignatureStatus(String(order.signature));
      if (observed?.err) status = "failed";
      else if (observed?.confirmationStatus === "confirmed" ||
          observed?.confirmationStatus === "finalized") status = "success";
      if (status !== order.status) {
        await db(
          "n8_jupiter_swaps?id=eq." + encodeURIComponent(order.id) +
            "&user_id=eq." + encodeURIComponent(row.id),
          "PATCH", { status, updated_at: new Date().toISOString() }, "return=minimal"
        );
      }
    } catch {
      // Preserve the unknown state: a failed status lookup is not proof that the tx failed.
    }
  }
  if (status === "quote_ready" && new Date(order.expires_at).getTime() <= Date.now()) {
    status = "expired";
    await db(
      "n8_jupiter_swaps?id=eq." + encodeURIComponent(order.id) +
        "&user_id=eq." + encodeURIComponent(row.id) + "&status=eq.quote_ready",
      "PATCH", { status, updated_at: new Date().toISOString() }, "return=minimal"
    );
  }
  return response({
    ok: true, order: { ...order, status },
    explorerUrl: order.signature ? "https://solscan.io/tx/" + order.signature : null,
  });
}


async function jupiterTriggerCertification() {
  const rows = await db(
    "n8_jupiter_connector_certification?select=status,delegated_automation_enabled,live_manual_wallet_execution_enabled&provider=eq.jupiter_swap_v2&limit=1"
  );
  return Array.isArray(rows) ? rows[0] ?? null : null;
}
function requireJupiterApiKey() {
  if (!JUPITER_API_KEY) throw new Error("jupiter_api_key_not_configured");
}
async function triggerAccess(wallet: any, accessToken: string, path: string, method: "GET" | "POST" = "GET", body?: unknown) {
  requireJupiterApiKey();
  if (!accessToken || accessToken.length > 4096) throw new Error("jupiter_trigger_auth_required");
  return await jupiterApi<any>(path, {
    method,
    apiKey: JUPITER_API_KEY,
    accessToken,
    body,
  });
}
async function jupiterTriggerChallenge(row: any, body: any): Promise<Response> {
  requireJupiterApiKey();
  const address = clean(body?.address, 64);
  await verifiedSolanaWallet(row.id, clean(body?.walletConnectionId, 64), address);
  const result = await jupiterApi<any>("/trigger/v2/auth/challenge", {
    method: "POST",
    apiKey: JUPITER_API_KEY,
    body: { walletPubkey: address, type: "message" },
  });
  const challenge = String(result?.challenge ?? result?.message ?? "");
  if (!challenge) throw new Error("jupiter_trigger_challenge_invalid");
  return response({ ok: true, challenge, walletPubkey: address });
}
async function jupiterTriggerVerify(row: any, body: any): Promise<Response> {
  requireJupiterApiKey();
  const address = clean(body?.address, 64);
  await verifiedSolanaWallet(row.id, clean(body?.walletConnectionId, 64), address);
  const signature = clean(body?.signature, 256);
  if (!signature) throw new Error("jupiter_trigger_signature_required");
  const result = await jupiterApi<any>("/trigger/v2/auth/verify", {
    method: "POST",
    apiKey: JUPITER_API_KEY,
    body: {
      walletPubkey: address,
      type: "message",
      signature,
      authMode: "access_refresh",
    },
  });
  const accessToken = String(result?.accessToken ?? "");
  const refreshToken = String(result?.refreshToken ?? "");
  if (!accessToken || !refreshToken) throw new Error("jupiter_trigger_auth_response_invalid");
  // Trigger credentials are returned only to this Telegram-authenticated owner session.
  // They are not written to Postgres or browser localStorage.
  return response({
    ok: true,
    accessToken,
    refreshToken,
    expiresAt: result?.expiresAt ?? null,
    tokenType: result?.tokenType ?? "Bearer",
  });
}
async function jupiterTriggerRefresh(row: any, body: any): Promise<Response> {
  requireJupiterApiKey();
  const refreshToken = clean(body?.refreshToken, 4096);
  if (!refreshToken) throw new Error("jupiter_trigger_refresh_required");
  const address = clean(body?.address, 64);
  await verifiedSolanaWallet(row.id, clean(body?.walletConnectionId, 64), address);
  const result = await jupiterApi<any>("/trigger/v2/auth/refresh", {
    method: "POST",
    apiKey: JUPITER_API_KEY,
    body: { refreshToken },
  });
  if (!result?.accessToken || !result?.refreshToken) throw new Error("jupiter_trigger_refresh_invalid");
  return response({
    ok: true,
    accessToken: String(result.accessToken),
    refreshToken: String(result.refreshToken),
    expiresAt: result.expiresAt ?? null,
    tokenType: result.tokenType ?? "Bearer",
  });
}
async function jupiterTriggerVault(row: any, body: any): Promise<Response> {
  const address = clean(body?.address, 64);
  await verifiedSolanaWallet(row.id, clean(body?.walletConnectionId, 64), address);
  const accessToken = clean(body?.accessToken, 4096);
  try {
    const vault = await triggerAccess(null, accessToken, "/trigger/v2/vault");
    return response({ ok: true, vault, registered: true });
  } catch (error) {
    if (!(error instanceof Error) || !error.message.includes("jupiter_http_404")) throw error;
    const registered = await triggerAccess(null, accessToken, "/trigger/v2/vault/register");
    return response({ ok: true, vault: registered, registered: false });
  }
}
async function jupiterTriggerDepositCraft(row: any, body: any): Promise<Response> {
  const address = clean(body?.address, 64);
  const wallet = await verifiedSolanaWallet(row.id, clean(body?.walletConnectionId, 64), address);
  const cert = await jupiterTriggerCertification();
  if (!cert || cert.status !== "passed" || cert.delegated_automation_enabled !== true) {
    throw new Error("jupiter_delegated_execution_certification_locked");
  }
  const accessToken = clean(body?.accessToken, 4096);
  const orderType = String(body?.orderType ?? "");
  if (orderType !== "dca") throw new Error("jupiter_trigger_order_type_unsupported");
  const inputMint = new PublicKey(clean(body?.inputMint, 64)).toBase58();
  const outputMint = new PublicKey(clean(body?.outputMint, 64)).toBase58();
  if (inputMint === outputMint) throw new Error("jupiter_trigger_mint_pair_invalid");
  const inputAmount = String(body?.amount ?? "");
  if (!/^[1-9]\d*$/.test(inputAmount)) throw new Error("jupiter_trigger_amount_invalid");
  if (orderType === "dca") {
    const requestedLamports = BigInt(inputAmount);
    if (requestedLamports > 500000000n) throw new Error("jupiter_dca_total_cap_exceeded");
    const recent = await db(
      "n8_jupiter_automation_orders?select=input_amount_raw,status,created_at&user_id=eq." +
        encodeURIComponent(row.id) + "&wallet_connection_id=eq." + encodeURIComponent(wallet.id) +
        "&order_family=eq.dca&created_at=gte." + encodeURIComponent(new Date(Date.now() - 86400000).toISOString()) +
        "&order=created_at.desc&limit=50"
    );
    const pendingSpend = (Array.isArray(recent) ? recent : [])
      .filter((entry: any) => ["deposit_pending", "creating", "unknown", "active", "cancel_pending", "cancel_confirming", "cancel_unknown"].includes(String(entry.status)))
      .reduce((sum: bigint, entry: any) => sum + BigInt(String(entry.input_amount_raw ?? "0")), 0n);
    if (pendingSpend + requestedLamports > 500000000n) throw new Error("jupiter_dca_daily_cap_exceeded");
  }
  const orderCount = Number(body?.orderCount ?? 0);
  const intervalSeconds = Number(body?.intervalSeconds ?? 0);
  let usdPerDcaRound: number | null = null;
  if (orderType === "dca") {
    if (inputMint !== SOL_MINT || !Number.isInteger(orderCount) || orderCount < 2 || orderCount > 1000 ||
        !Number.isInteger(intervalSeconds) || intervalSeconds < 60 || intervalSeconds > 31536000) {
      throw new Error("jupiter_dca_parameters_invalid");
    }
    const priceResponse = await jupiterApi<any>("/price/v3?ids=" + encodeURIComponent(SOL_MINT), { apiKey: JUPITER_API_KEY });
    const solPrice = Number(priceResponse?.[SOL_MINT]?.usdPrice ?? priceResponse?.data?.[SOL_MINT]?.usdPrice);
    if (!Number.isFinite(solPrice) || solPrice <= 0) throw new Error("jupiter_sol_price_unavailable");
    usdPerDcaRound = (Number(BigInt(inputAmount)) / 1e9 * solPrice) / orderCount;
    if (usdPerDcaRound < 10) throw new Error("jupiter_dca_round_below_minimum_usd");
  }
  const requestBody: Record<string, unknown> = {
    inputMint,
    outputMint,
    userAddress: wallet.public_address,
    amount: inputAmount,
    orderType,
  };
  const result = await triggerAccess(wallet, accessToken, "/trigger/v2/deposit/craft", "POST", requestBody);
  if (!result?.transaction || !result?.requestId || !result?.receiverAddress) {
    throw new Error("jupiter_trigger_deposit_craft_invalid");
  }
  let depositTx: VersionedTransaction;
  try { depositTx = VersionedTransaction.deserialize(base64ToBytes(String(result.transaction))); }
  catch { throw new Error("jupiter_trigger_deposit_transaction_invalid"); }
  if (depositTx.message.version !== 0 ||
      (depositTx.message.staticAccountKeys?.[0]?.toBase58?.() ?? "") !== wallet.public_address) {
    throw new Error("jupiter_trigger_deposit_transaction_invalid");
  }
  const depositMessageHash = await sha256HexBytes(depositTx.message.serialize());
  const rows = await db("n8_jupiter_automation_orders", "POST", {
    user_id: row.id,
    wallet_connection_id: wallet.id,
    jupiter_order_id: String(result.requestId),
    order_family: orderType === "dca" ? "dca" : "price",
    input_mint: inputMint,
    output_mint: outputMint,
    input_amount_raw: inputAmount,
    vault_address: String(result.receiverAddress),
    status: "deposit_pending",
    order_metadata: {
      depositRequestId: result.requestId, mint: result.mint ?? inputMint, amount: result.amount ?? inputAmount,
      depositMessageHash, orderCount: orderType === "dca" ? orderCount : null,
      intervalSeconds: orderType === "dca" ? intervalSeconds : null, usdPerDcaRound,
    },
  }, "return=representation");
  if (!Array.isArray(rows) || !rows[0]?.id) throw new Error("jupiter_trigger_deposit_record_failed");
  return response({
    ok: true,
    deposit: {
      id: rows[0].id,
      requestId: result.requestId,
      transaction: result.transaction,
      receiverAddress: result.receiverAddress,
      mint: result.mint ?? inputMint,
      amount: result.amount ?? inputAmount,
      decimals: result.tokenDecimals ?? result.decimals ?? null,
    },
    custodyDisclosure: "This deposit moves the selected input token from your wallet into a Jupiter/Privy-managed vault for Trigger automation. It is not kept in your original wallet. You must review the receiver, token and amount before signing. Automation can be cancelled through a wallet-signed withdrawal.",
  });
}
async function jupiterTriggerCreateDca(row: any, body: any): Promise<Response> {
  const address = clean(body?.address, 64);
  const wallet = await verifiedSolanaWallet(row.id, clean(body?.walletConnectionId, 64), address);
  const cert = await jupiterTriggerCertification();
  if (!cert || cert.status !== "passed" || cert.delegated_automation_enabled !== true) {
    throw new Error("jupiter_delegated_execution_certification_locked");
  }
  const accessToken = clean(body?.accessToken, 4096);
  const depositRequestId = clean(body?.depositRequestId, 128);
  const depositSignedTx = clean(body?.depositSignedTx, 120000);
  const orderCount = Number(body?.orderCount);
  const intervalSeconds = Number(body?.intervalSeconds);
  if (!depositRequestId || !depositSignedTx || !Number.isInteger(orderCount) ||
      orderCount < 2 || orderCount > 1000 || !Number.isInteger(intervalSeconds) ||
      intervalSeconds < 60 || intervalSeconds > 31536000) {
    throw new Error("jupiter_dca_parameters_invalid");
  }
  const localRows = await db(
    "n8_jupiter_automation_orders?select=id,input_mint,output_mint,input_amount_raw,vault_address,status,order_metadata&jupiter_order_id=eq." +
      encodeURIComponent(depositRequestId) + "&wallet_connection_id=eq." +
      encodeURIComponent(wallet.id) + "&user_id=eq." + encodeURIComponent(row.id) + "&status=eq.deposit_pending&limit=1"
  );
  const deposit = Array.isArray(localRows) ? localRows[0] : null;
  if (!deposit) throw new Error("jupiter_trigger_deposit_not_found");
  const inputAmount = String(body?.inputAmount ?? deposit.input_amount_raw);
  if (inputAmount !== String(deposit.input_amount_raw)) throw new Error("jupiter_trigger_deposit_amount_mismatch");
  const orderMetadata = deposit.order_metadata ?? {};
  if (orderCount !== Number(orderMetadata.orderCount) ||
      intervalSeconds !== Number(orderMetadata.intervalSeconds) ||
      Number(orderMetadata.usdPerDcaRound) < 10) {
    throw new Error("jupiter_dca_parameters_mismatch");
  }

  let depositTx: VersionedTransaction;
  try { depositTx = VersionedTransaction.deserialize(base64ToBytes(depositSignedTx)); }
  catch { throw new Error("jupiter_trigger_deposit_signature_invalid"); }
  const depositMessageBytes = depositTx.message.serialize();
  if (depositTx.message.version !== 0 ||
      (depositTx.message.staticAccountKeys?.[0]?.toBase58?.() ?? "") !== wallet.public_address ||
      await sha256HexBytes(depositMessageBytes) !== String(orderMetadata.depositMessageHash ?? "")) {
    throw new Error("jupiter_trigger_deposit_message_changed");
  }
  const depositSignature = depositTx.signatures?.[0];
  if (!depositSignature || depositSignature.length !== 64 || depositSignature.every((v: number) => v === 0)) {
    throw new Error("jupiter_trigger_deposit_signature_missing");
  }
  const depositPub = new Uint8Array(new PublicKey(wallet.public_address).toBytes()).buffer;
  const depositSig = new Uint8Array(depositSignature).buffer;
  const depositMessage = new Uint8Array(depositMessageBytes).buffer;
  const depositKey = await crypto.subtle.importKey("raw", depositPub, { name: "Ed25519" }, false, ["verify"]);
  if (!await crypto.subtle.verify("Ed25519", depositKey, depositSig, depositMessage)) {
    throw new Error("jupiter_trigger_deposit_signature_invalid");
  }

  const claimed = await db(
    "n8_jupiter_automation_orders?id=eq." + encodeURIComponent(deposit.id) +
      "&user_id=eq." + encodeURIComponent(row.id) + "&status=eq.deposit_pending",
    "PATCH",
    { status: "creating", deposit_signature: base58Encode(depositSignature), updated_at: new Date().toISOString() },
    "return=representation"
  );
  if (!Array.isArray(claimed) || claimed.length !== 1) {
    throw new Error("jupiter_trigger_deposit_already_claimed");
  }
  const requestBody = {
    depositRequestId,
    depositSignedTx,
    userPubkey: wallet.public_address,
    inputMint: String(deposit.input_mint),
    outputMint: String(deposit.output_mint),
    inputAmount,
    orderCount,
    intervalSeconds,
  };
  try {
    const result = await triggerAccess(wallet, accessToken, "/trigger/v2/orders/dca", "POST", requestBody);
    const jupiterOrderId = String(result?.id ?? result?.orderId ?? "");
    if (!jupiterOrderId) throw new Error("jupiter_trigger_dca_response_invalid");
    await db(
      "n8_jupiter_automation_orders?id=eq." + encodeURIComponent(deposit.id) +
        "&user_id=eq." + encodeURIComponent(row.id) + "&status=eq.creating",
      "PATCH",
      {
        jupiter_order_id: jupiterOrderId,
        order_count: orderCount,
        interval_seconds: intervalSeconds,
        status: "active",
        order_metadata: {
          response: result, depositRequestId, orderType: "time_based",
          depositMessageHash: orderMetadata.depositMessageHash,
          usdPerDcaRound: orderMetadata.usdPerDcaRound,
        },
        updated_at: new Date().toISOString(),
      },
      "return=minimal"
    );
    await audit(row.id, null, "jupiter_dca_created", "ok", {
      jupiter_order_id: jupiterOrderId, wallet_connection_id: wallet.id,
      order_count: orderCount, interval_seconds: intervalSeconds,
    });
    return response({ ok: true, orderId: jupiterOrderId, result, vaultAddress: deposit.vault_address });
  } catch {
    // A lost response may occur after the provider accepted the signed deposit.
    // Mark unknown and reconcile through DCA history rather than creating a duplicate.
    await db(
      "n8_jupiter_automation_orders?id=eq." + encodeURIComponent(deposit.id) +
        "&user_id=eq." + encodeURIComponent(row.id) + "&status=eq.creating",
      "PATCH",
      { status: "unknown", updated_at: new Date().toISOString() },
      "return=minimal"
    );
    await audit(row.id, null, "jupiter_dca_create_unknown", "warning", {
      deposit_request_id: depositRequestId, wallet_connection_id: wallet.id,
    });
    return response({
      ok: true, status: "unknown", depositRequestId,
      message: "Jupiter's response is unknown. Check DCA history before attempting anything again. The deposit will not be retried.",
    }, 202);
  }
}

async function jupiterTriggerHistory(row: any, body: any): Promise<Response> {
  const address = clean(body?.address, 64);
  const wallet = await verifiedSolanaWallet(row.id, clean(body?.walletConnectionId, 64), address);
  const result = await triggerAccess(wallet, clean(body?.accessToken, 4096),
    "/trigger/v2/orders/history/dca?state=active&limit=20");
  return response({ ok: true, orders: result, walletConnectionId: wallet.id });
}
async function jupiterTriggerCancelDca(row: any, body: any): Promise<Response> {
  const address = clean(body?.address, 64);
  const wallet = await verifiedSolanaWallet(row.id, clean(body?.walletConnectionId, 64), address);
  const orderId = clean(body?.orderId, 128);
  if (!orderId) throw new Error("jupiter_trigger_order_id_required");
  const local = await db(
    "n8_jupiter_automation_orders?select=id,jupiter_order_id,status,order_metadata&user_id=eq." +
      encodeURIComponent(row.id) + "&wallet_connection_id=eq." + encodeURIComponent(wallet.id) +
      "&jupiter_order_id=eq." + encodeURIComponent(orderId) + "&limit=1"
  );
  if (!Array.isArray(local) || !local[0] || local[0].status !== "active") {
    throw new Error("jupiter_trigger_order_not_active");
  }
  const result = await triggerAccess(wallet, clean(body?.accessToken, 4096),
    "/trigger/v2/orders/dca/cancel/" + encodeURIComponent(orderId), "POST", {});
  if (!result?.transaction || !result?.requestId) throw new Error("jupiter_trigger_cancel_response_invalid");
  let cancelTx: VersionedTransaction;
  try { cancelTx = VersionedTransaction.deserialize(base64ToBytes(String(result.transaction))); }
  catch { throw new Error("jupiter_trigger_cancel_transaction_invalid"); }
  if (cancelTx.message.version !== 0 ||
      (cancelTx.message.staticAccountKeys?.[0]?.toBase58?.() ?? "") !== wallet.public_address) {
    throw new Error("jupiter_trigger_cancel_transaction_invalid");
  }
  const cancelMessageHash = await sha256HexBytes(cancelTx.message.serialize());
  const previousMeta = local[0].order_metadata ?? {};
  const claimed = await db(
    "n8_jupiter_automation_orders?id=eq." + encodeURIComponent(local[0].id) +
      "&user_id=eq." + encodeURIComponent(row.id) + "&status=eq.active",
    "PATCH",
    {
      status: "cancel_pending",
      order_metadata: {
        ...previousMeta,
        pendingCancelRequestId: String(result.requestId),
        pendingCancelMessageHash: cancelMessageHash,
        pendingCancelRefundAmount: result.refundAmount ?? null,
        pendingCancelRoundsRemaining: result.roundsRemaining ?? null,
      },
      updated_at: new Date().toISOString(),
    },
    "return=representation"
  );
  if (!Array.isArray(claimed) || claimed.length !== 1) throw new Error("jupiter_trigger_order_cancel_already_started");
  return response({
    ok: true,
    orderId,
    cancelRequestId: result.requestId,
    transaction: result.transaction,
    refundAmount: result.refundAmount ?? null,
    roundsRemaining: result.roundsRemaining ?? null,
    disclosure: "This is a one-way cancel. Review the refund amount, then sign the withdrawal transaction in your wallet. DCA cannot be paused and resumed.",
  });
}
async function jupiterTriggerConfirmCancelDca(row: any, body: any): Promise<Response> {
  const address = clean(body?.address, 64);
  const wallet = await verifiedSolanaWallet(row.id, clean(body?.walletConnectionId, 64), address);
  const orderId = clean(body?.orderId, 128);
  const cancelRequestId = clean(body?.cancelRequestId, 128);
  const signedTransaction = clean(body?.signedTransaction, 120000);
  if (!orderId || !cancelRequestId || !signedTransaction) throw new Error("jupiter_trigger_cancel_confirmation_invalid");
  const local = await db(
    "n8_jupiter_automation_orders?select=id,jupiter_order_id,status,order_metadata&user_id=eq." +
      encodeURIComponent(row.id) + "&wallet_connection_id=eq." + encodeURIComponent(wallet.id) +
      "&jupiter_order_id=eq." + encodeURIComponent(orderId) + "&status=eq.cancel_pending&limit=1"
  );
  if (!Array.isArray(local) || !local[0]) throw new Error("jupiter_trigger_cancel_not_pending");
  const metadata = local[0].order_metadata ?? {};
  if (String(metadata.pendingCancelRequestId ?? "") !== cancelRequestId) {
    throw new Error("jupiter_trigger_cancel_request_mismatch");
  }
  let tx: VersionedTransaction;
  try { tx = VersionedTransaction.deserialize(base64ToBytes(signedTransaction)); }
  catch { throw new Error("jupiter_trigger_cancel_transaction_invalid"); }
  const messageBytes = tx.message.serialize();
  if (tx.message.version !== 0 ||
      (tx.message.staticAccountKeys?.[0]?.toBase58?.() ?? "") !== wallet.public_address ||
      await sha256HexBytes(messageBytes) !== String(metadata.pendingCancelMessageHash ?? "")) {
    throw new Error("jupiter_trigger_cancel_transaction_changed");
  }
  const signature = tx.signatures?.[0];
  if (!signature || signature.length !== 64 || signature.every((v: number) => v === 0)) {
    throw new Error("jupiter_trigger_cancel_signature_missing");
  }
  const pub = new Uint8Array(new PublicKey(wallet.public_address).toBytes()).buffer;
  const sig = new Uint8Array(signature).buffer;
  const message = new Uint8Array(messageBytes).buffer;
  const key = await crypto.subtle.importKey("raw", pub, { name: "Ed25519" }, false, ["verify"]);
  if (!await crypto.subtle.verify("Ed25519", key, sig, message)) {
    throw new Error("jupiter_trigger_cancel_signature_invalid");
  }
  const claim = await db(
    "n8_jupiter_automation_orders?id=eq." + encodeURIComponent(local[0].id) +
      "&user_id=eq." + encodeURIComponent(row.id) + "&status=eq.cancel_pending",
    "PATCH",
    { status: "cancel_confirming", updated_at: new Date().toISOString() },
    "return=representation"
  );
  if (!Array.isArray(claim) || claim.length !== 1) throw new Error("jupiter_trigger_cancel_already_claimed");
  try {
    const result = await triggerAccess(wallet, clean(body?.accessToken, 4096),
      "/trigger/v2/orders/dca/confirm-cancel/" + encodeURIComponent(orderId), "POST",
      { signedTransaction, cancelRequestId });
    await db(
      "n8_jupiter_automation_orders?id=eq." + encodeURIComponent(local[0].id) +
        "&user_id=eq." + encodeURIComponent(row.id) + "&status=eq.cancel_confirming",
      "PATCH",
      {
        status: "cancelled",
        order_metadata: { cancelResult: result, cancelRequestId },
        updated_at: new Date().toISOString(),
      },
      "return=minimal"
    );
    await audit(row.id, null, "jupiter_dca_cancelled", "ok", { jupiter_order_id: orderId });
    return response({ ok: true, orderId, result });
  } catch {
    await db(
      "n8_jupiter_automation_orders?id=eq." + encodeURIComponent(local[0].id) +
        "&user_id=eq." + encodeURIComponent(row.id) + "&status=eq.cancel_confirming",
      "PATCH",
      { status: "cancel_unknown", updated_at: new Date().toISOString() },
      "return=minimal"
    );
    await audit(row.id, null, "jupiter_dca_cancel_unknown", "warning", { jupiter_order_id: orderId });
    return response({
      ok: true,
      status: "cancel_unknown",
      orderId,
      message: "Cancellation outcome is unknown. Check Jupiter DCA history and vault balance before attempting another cancellation.",
    }, 202);
  }
}

async function handleMiniApp(body: any): Promise<Response> {
  try {
    // Read-only account views, assessments and emergency pause remain usable while
    // the private Mini App is open. Credential changes and all future trade approvals
    // retain the strict five-minute Telegram initData window.
    const longLivedReadActions = new Set([
      "paper_terminal",
      "live_connections",
      "live_status",
      "live_assess",
      "live_pause",
      "binance_account",
      "okx_account",
      "bybit_account",
      "wallet_revoke",
      "jupiter_swap_status",
      "jupiter_trigger_history",
      "jupiter_live_config",
      "jupiter_trigger_vault",
    ]);
    const maxAge = longLivedReadActions.has(String(body?.action ?? "")) ? 24 * 60 * 60 : 300;
    const user = await verifyInitData(String(body?.initData ?? ""), maxAge);
    const row = await userRow(user);
    if (body.action === "connect_exchange") {
      const provider = String(body.provider) as Provider;
      const environment = String(body.environment);
      if (!["binance", "bybit", "okx"].includes(provider)) throw new Error("unsupported_provider");
      const requestedMarkets = provider === "binance" &&
        (body?.autoDetectMarkets === true || !Array.isArray(body?.requestedMarkets))
        ? undefined
        : normalizeRequestedMarkets(body?.requestedMarkets);
      const regionValue = clean(body?.credentials?.region, 16);
      const bybitRegionValue = clean(body?.credentials?.bybitRegion, 20);
      const credentials: Credentials = {
        apiKey: clean(body?.credentials?.apiKey, 512),
        apiSecret: clean(body?.credentials?.apiSecret, 1024),
        passphrase: clean(body?.credentials?.passphrase, 256),
        region: (["global", "us", "eea"].includes(regionValue) ? regionValue : "global") as OkxRegion,
        bybitRegion: (["global", "eea", "uae", "turkey", "kazakhstan", "georgia", "indonesia", "hongkong", "japan"].includes(bybitRegionValue)
          ? bybitRegionValue : "global") as BybitRegion,
      };
      const validation = await validateExchange(provider, environment, credentials, requestedMarkets);
      const id = await saveExchange(Number(user.id), provider, environment, validation.label, credentials,
        validation.permissions, validation.accountHint ?? null);
      await audit(row.id, null, "exchange_connected", "ok", {
        provider, environment, connection_id: id, permissions: validation.permissions,
      });
      return response({
        ok: true,
        label: validation.label,
        connectionId: id,
        selectedMarkets: validation.permissions.selectedMarkets ?? requestedMarkets ?? [],
        permissions: validation.permissions,
        accountHint: validation.accountHint ?? null,
        liveOrdersEnabled: false,
        message: "Account access checked and saved. 9N8 does not place an exchange order from this connection step.",
      });
    }
    if (body.action === "watch_wallet") {
      const provider = String(body.provider);
      const namespace = String(body.namespace);
      const address = clean(body.address, 128);
      const network = clean(body.network, 80);
      const chainId = clean(body.chainId, 40);
      if (!["trust_wallet", "phantom", "metamask", "walletconnect", "manual"].includes(provider) ||
          !walletAddressValid(namespace, address) || !network || !chainId) {
        throw new Error("wallet_address_or_network_invalid");
      }
      await db("n8_wallet_connections?on_conflict=user_id,chain_namespace,network,public_address", "POST", {
        user_id: row.id, provider, chain_namespace: namespace, chain_id: chainId,
        network, public_address: address, status: "watch_only",
      }, "resolution=merge-duplicates,return=minimal");
      await audit(row.id, null, "wallet_address_added", "ok", { provider, namespace, network });
      return response({ ok: true, address: mask(address) });
    }
    if (body.action === "paper_terminal") {
      return response({ ok: true, ...(await paperTerminalData(row.id)) });
    }
    if (body.action === "paper_start") {
      const started = await startCryptoPaper(row.id, Number(user.id));
      return response({ ok: started.ok, message: started.message });
    }
    if (body.action === "binance_account") {
      return response({ ok: true, ...(await liveBinanceAccount(user, row.id)) });
    }
    if (body.action === "okx_account") {
      return response({ ok: true, ...(await liveOkxAccount(user, row.id)) });
    }
    if (body.action === "bybit_account") {
      return response({ ok: true, ...(await liveBybitAccount(user, row.id)) });
    }
    if (body.action === "wallet_challenge") return await createWalletChallenge(user, body);
    if (body.action === "wallet_verify") return await verifyAndSaveWallet(user, body);
    if (body.action === "wallet_revoke") return await revokeWalletSession(user, body);
    if (body.action === "jupiter_live_config") return await getJupiterLiveConfig(row);
    if (body.action === "jupiter_swap_order") return await createJupiterSwapOrder(user, row, body);
    if (body.action === "jupiter_swap_execute") return await executeJupiterSwap(user, row, body);
    if (body.action === "jupiter_swap_status") return await getJupiterSwapStatus(user, row, body);
    if (body.action === "jupiter_trigger_challenge") return await jupiterTriggerChallenge(row, body);
    if (body.action === "jupiter_trigger_verify") return await jupiterTriggerVerify(row, body);
    if (body.action === "jupiter_trigger_refresh") return await jupiterTriggerRefresh(row, body);
    if (body.action === "jupiter_trigger_vault") return await jupiterTriggerVault(row, body);
    if (body.action === "jupiter_trigger_deposit_craft") return await jupiterTriggerDepositCraft(row, body);
    if (body.action === "jupiter_trigger_create_dca") return await jupiterTriggerCreateDca(row, body);
    if (body.action === "jupiter_trigger_history") return await jupiterTriggerHistory(row, body);
    if (body.action === "jupiter_trigger_cancel_dca") return await jupiterTriggerCancelDca(row, body);
    if (body.action === "jupiter_trigger_confirm_cancel_dca") return await jupiterTriggerConfirmCancelDca(row, body);
    if (body.action === "live_connections") {
      const exchangeRows = await db(
        "n8_exchange_connections?select=id,provider,environment,label,status&user_id=eq." +
          encodeURIComponent(row.id) + "&status=eq.active&order=created_at.desc"
      );
      const walletRows = await db(
        "n8_wallet_connections?select=id,provider,network,public_address,status,connection_type&user_id=eq." +
          encodeURIComponent(row.id) + "&status=eq.verified&order=created_at.desc"
      );
      return response({
        ok: true,
        exchanges: Array.isArray(exchangeRows) ? exchangeRows : [],
        wallets: Array.isArray(walletRows) ? walletRows.map((w: any) => ({
          ...w,
          public_address: mask(String(w.public_address)),
        })) : [],
        liveTradingEnabled: false,
        message: "Binance, Bybit and OKX API connections can be checked for the markets selected by each user. Account links do not place orders. Real exchange trading remains locked until each connector is separately certified and each exact order has owner approval.",
      });
    }
    if (body.action === "live_status") {
      const status = await getLiveStatus(row.id);
      return response({ ok: true, status });
    }
    if (body.action === "live_assess") {
      return await assessLiveMigration(user, row, body);
    }
    if (body.action === "live_pause") {
      const result = await applyEmergencyStop(row.id, "pause");
      await audit(row.id, null, "live_migration_pause", "ok", { source: "mini_app" });
      return response({ ok: true, ...result });
    }
    throw new Error("unsupported_action");
  } catch (error) {
    const code = error instanceof Error ? error.message : "request_failed";
    const messages: Record<string, string> = {
      telegram_auth_invalid: "Telegram identity could not be verified. Close the page and reopen it from the bot.",
      telegram_auth_expired: "This page expired. Reopen the secure form from 9N8.",
      private_chat_required: "Personal connections can only be created from a private Telegram chat.",
      backend_not_configured: "The secure backend is not configured yet. No credentials were saved.",
      select_at_least_one_market: "Choose at least one market to connect.",
      exchange_market_not_supported: "One of the selected markets is not supported by this connection.",
      binance_spot_account_read_failed: "Binance accepted the permission check but did not return the Spot account balances. Confirm the key has Reading access and IP restrictions allow 9N8.",
      binance_selected_product_not_enabled: "One or more selected Binance products are not enabled on both the API key and the Binance account. Use automatic detection or enable the required product in Binance.",
      binance_spot_margin_stock_permission_required: "On Binance, turn on “Spot & Margin & Stock Trading” for this API key.",
      binance_margin_permission_required: "Your Binance key does not have Margin access enabled.",
      binance_futures_permission_required: "Your Binance key does not have Futures access enabled.",
      binance_options_permission_required: "Your Binance key does not have Options access enabled.",
      bybit_api_key_is_read_only: "This Bybit key is read-only. Create a key with the trading permissions you selected.",
      bybit_spot_trade_permission_required: "Enable Spot Trading permission on this Bybit API key.",
      bybit_derivatives_trade_permission_required: "Enable Contract or Derivatives Trading permission on this Bybit API key.",
      bybit_options_trade_permission_required: "Enable Options Trading permission on this Bybit API key.",
      bybit_stock_products_not_supported_by_this_connection: "Stock-linked products are not available through this Bybit connection.",
      bybit_unified_account_balance_unavailable: "Bybit could not return Unified Account balances. Check that your account uses Unified Trading and try again.",
      okx_read_permission_required: "Enable Read permission on your OKX API key.",
      okx_trade_permission_required: "Enable Trade permission on your OKX API key for the markets you selected.",
      okx_stock_products_not_supported_by_this_connection: "Stock-linked products are not available through this OKX connection.",
      binance_read_permission_required: "Enable Read permission for this Binance API key.",
      binance_spot_permission_required: "On Binance, turn on “Spot & Margin & Stock Trading” for this API key when you select Spot, Margin or eligible stock-linked products.",
      binance_transfer_permission_detected: "Disable internal transfers and universal transfers on the Binance API key.",
      binance_non_spot_permission_detected: "The selected product permissions do not match this key. Review your market choices and exchange API settings.",
      binance_live_connection_missing: "Connect your Binance account first.",
      okx_live_connection_missing: "Connect your OKX account first.",
      okx_api_key_secret_and_passphrase_required: "Enter your OKX API key, secret and API passphrase.",
      okx_passphrase_required: "Enter the API passphrase configured when you created this OKX API key.",
      withdraw_permission_detected: "This API key has withdrawal permission. Revoke it and create a new key with withdrawals disabled.",
      wallet_transfer_or_withdraw_scope_detected: "This Bybit key has Wallet transfer/withdrawal permissions. Create a key without Wallet permissions.",
      exchange_credentials_rejected: "The exchange rejected the credentials or the key belongs to a different environment.",
      exchange_temporarily_unavailable: "The exchange is temporarily unavailable. Try again shortly.",
      unsupported_provider_environment: "Choose a supported live Binance, Bybit or OKX account.",
      wallet_address_or_network_invalid: "The public address format does not match the selected network.",
      credential_storage_failed: "Credentials were checked but could not be saved. No connection was created.",
      user_registration_failed: "Could not register your Telegram account. Try again.",
      jupiter_execution_certification_locked: "Live Jupiter swaps are still locked while execution certification tests are pending. You can request quotes, but no transaction will be submitted.",
      jupiter_delegated_execution_certification_locked: "Jupiter DCA automation is locked until vault deposit, delegated execution, cancellation and withdrawal checks have passed.",
      jupiter_api_key_not_configured: "Jupiter Trigger automation needs JUPITER_API_KEY in Supabase Edge Function Secrets. Manual Swap V2 quotes can work without it.",
      jupiter_trigger_auth_required: "Reconnect your verified Solana wallet to Jupiter automation.",
      jupiter_dca_round_below_minimum_usd: "Each Jupiter DCA round must be at least $10. Increase total SOL or reduce the number of rounds.",
      jupiter_dca_total_cap_exceeded: "9N8 limits each DCA deposit to 0.50 SOL.",
      jupiter_dca_daily_cap_exceeded: "This wallet would exceed the 0.50 SOL rolling 24-hour DCA deposit cap.",
      swap_per_order_cap_exceeded: "This swap exceeds the 0.05 SOL per-order limit.",
      swap_daily_cap_exceeded: "This swap would exceed the 0.10 SOL rolling 24-hour limit.",
      swap_previous_request_unresolved: "A previous Jupiter quote or transaction is unresolved. Refresh its status before creating another swap.",
      verified_solana_wallet_required: "Connect and verify your Solana-mainnet wallet before using the Jupiter terminal."
    };
    const friendly = code.startsWith("binance_selected_product_not_enabled:")
      ? messages.binance_selected_product_not_enabled
      : messages[code] ?? "Could not complete the request. Check the provider and try again.";
    return response({ ok: false, message: friendly }, 400);
  }
}
async function listConnections(userId: string): Promise<string> {
  const rows = await db("n8_exchange_connections?select=provider,environment,label,status&user_id=eq." +
    encodeURIComponent(userId) + "&status=neq.revoked&order=created_at.desc");
  if (!Array.isArray(rows) || rows.length === 0) return "No exchange accounts connected yet. Use /connect to inspect Binance API permissions across Spot, Margin, USDⓈ-M and COIN-M Futures, Options, or connect Bybit / OKX.";
  return "YOUR EXCHANGE CONNECTIONS\n\n" + rows.map((v: any) =>
    String(v.provider).toUpperCase() + " · " + v.environment + "\n" + String(v.label ?? "Account") + " · " + v.status
  ).join("\n\n");
}
async function listWallets(userId: string): Promise<string> {
  const rows = await db("n8_wallet_connections?select=provider,network,public_address,status,connection_type,execution_mode,auto_trade_enabled&user_id=eq." +
    encodeURIComponent(userId) + "&status=neq.revoked&order=created_at.desc");
  if (!Array.isArray(rows) || rows.length === 0) return "No wallet addresses linked. Use /connect to add a public watch-only address.";
  return "YOUR WATCH-ONLY ADDRESSES\n\n" + rows.map((v: any) =>
    String(v.provider).replace("_", " ") + " · " + v.network + "\n" + mask(String(v.public_address)) + " · " + v.status +
    " · " + (v.connection_type === "walletconnect_session" ? "wallet session" : "address link") +
    " · execution: " + String(v.execution_mode ?? "approval")
  ).join("\n\n");
}

function admin(member: any): boolean {
  return member?.status === "creator" || member?.status === "administrator";
}
async function member(chatId: number, userId: number): Promise<any> {
  return await telegram("getChatMember", { chat_id: chatId, user_id: userId });
}
async function groupAuth(chatId: number, actorId: number): Promise<any> {
  const me = await telegram("getMe", {});
  const bot = await member(chatId, Number(me.id));
  if (bot?.status !== "administrator") throw new Error("bot_must_be_admin");
  const actor = await member(chatId, actorId);
  if (!admin(actor)) throw new Error("human_admin_required");
  return bot;
}
async function saveGroup(chatId: number, title: string, creator: string | null, mode: string, bot: any) {
  const rights = {
    can_delete_messages: bot?.can_delete_messages === true,
    can_restrict_members: bot?.can_restrict_members === true,
    can_manage_chat: bot?.can_manage_chat === true,
    can_pin_messages: bot?.can_pin_messages === true,
  };
  await db("n8_group_settings?on_conflict=chat_id", "POST", {
    chat_id: chatId, title, created_by_user_id: creator, mode,
    bot_status: String(bot?.status ?? "member"), bot_rights: rights, updated_at: new Date().toISOString(),
  }, "resolution=merge-duplicates,return=minimal");
  return rights;
}
async function groupCommand(m: any, command: string, args: string[], actorRow: any) {
  const chat = m.chat;
  const chatId = Number(chat.id);
  const actorId = Number(m.from?.id);
  try {
    const bot = await groupAuth(chatId, actorId);
    if (command === "/n8setup") {
      const rights = await saveGroup(chatId, clean(chat.title, 120), actorRow.id, "market_only", bot);
      await send(chatId, "9N8 group setup complete.\nMode: MARKET ONLY\nPersonal exchange connections and wallets stay private.\nFor moderation grant only Delete Messages and Restrict Members, then run /n8moderator.\nCurrent bot rights: delete=" + rights.can_delete_messages + ", restrict=" + rights.can_restrict_members);
      return;
    }
    if (command === "/n8admin") {
      const rows = await db("n8_group_settings?select=mode,bot_rights&chat_id=eq." + encodeURIComponent(String(chatId)));
      const row = Array.isArray(rows) ? rows[0] : null;
      await send(chatId, "9N8 GROUP STATUS\nMode: " + String(row?.mode ?? "not configured") +
        "\nDelete messages: " + String(bot.can_delete_messages === true) +
        "\nRestrict members: " + String(bot.can_restrict_members === true) +
        "\nPersonal trading commands are disabled in groups.");
      return;
    }
    if (command === "/n8moderator") {
      if (bot.can_delete_messages !== true || bot.can_restrict_members !== true) {
        await send(chatId, "Moderator mode requires two bot rights: Delete Messages and Restrict Members. Grant them, then run /n8moderator again.");
        return;
      }
      await saveGroup(chatId, clean(chat.title, 120), actorRow.id, "moderator", bot);
      await send(chatId, "9N8 moderator mode enabled. Human group admins can use /n8delete and /n8ban by replying to a member message. Automatic bans are off.");
      return;
    }
    const settings = await db("n8_group_settings?select=mode&chat_id=eq." + encodeURIComponent(String(chatId)));
    if (!Array.isArray(settings) || settings[0]?.mode !== "moderator") {
      await send(chatId, "Moderator mode is not enabled. Ask a group administrator to run /n8moderator.");
      return;
    }
    if (command === "/n8delete") {
      if (bot.can_delete_messages !== true) {
        await send(chatId, "The bot does not have Delete Messages permission.");
        return;
      }
      const target = m.reply_to_message;
      if (!target?.message_id) {
        await send(chatId, "Reply to the message to remove, then send /n8delete.");
        return;
      }
      await telegram("deleteMessage", { chat_id: chatId, message_id: target.message_id });
      await audit(actorRow.id, chatId, "group_delete_message");
      return;
    }
    if (command === "/n8ban") {
      if (bot.can_restrict_members !== true) {
        await send(chatId, "The bot does not have Restrict Members permission.");
        return;
      }
      const target = m.reply_to_message?.from;
      if (!target?.id) {
        await send(chatId, "Reply to a regular member message and send /n8ban.");
        return;
      }
      const targetMember = await member(chatId, Number(target.id));
      if (admin(targetMember) || Number(target.id) === actorId) {
        await send(chatId, "Refusing to ban a group administrator, owner, or yourself.");
        return;
      }
      await telegram("banChatMember", { chat_id: chatId, user_id: Number(target.id) });
      await audit(actorRow.id, chatId, "group_ban_member", "ok", { target_user_id: Number(target.id) });
      await send(chatId, "Ban request sent by an authorized group administrator.");
      return;
    }
    if (command === "/n8unban") {
      if (bot.can_restrict_members !== true || !/^\d{5,20}$/.test(args[0] ?? "")) {
        await send(chatId, "Usage: /n8unban TELEGRAM_USER_ID. The bot needs Restrict Members permission.");
        return;
      }
      await telegram("unbanChatMember", { chat_id: chatId, user_id: Number(args[0]), only_if_banned: true });
      await audit(actorRow.id, chatId, "group_unban_member", "ok", { target_user_id: Number(args[0]) });
      await send(chatId, "Unban request sent.");
    }
  } catch (error) {
    const code = error instanceof Error ? error.message : "";
    await send(chatId, code === "bot_must_be_admin"
      ? "Promote 9N8 to group administrator first. It will request only the rights required for enabled features."
      : code === "human_admin_required"
      ? "Only the group owner or a current group administrator can configure 9N8."
      : "Group action failed. Check the bot permissions and try again.");
  }
}


async function getLiveStatus(userId: string): Promise<Obj> {
  const policies = await db(
    "n8_risk_policies?select=execution_mode,auto_execute_enabled,wallet_auto_execute_enabled,live_trading_enabled,require_confirmation,kill_switch,min_live_balance_usd,max_order_usd,max_daily_notional_usd,max_open_positions,max_slippage_bps,max_price_deviation_bps,minimum_liquidity_usd,paper_graduation_days&user_id=eq." +
      encodeURIComponent(userId) + "&limit=1"
  );
  const policy = Array.isArray(policies) ? policies[0] : null;
  const migrations = await db(
    "n8_live_migrations?select=id,account_type,market_type,evaluation_window_days,status,execution_mode,failed_checks,owner_confirmed_at,live_enabled_at,paused_at,revoked_at,created_at&user_id=eq." +
      encodeURIComponent(userId) + "&order=created_at.desc&limit=5"
  );
  return {
    liveTradingEnabled: policy?.live_trading_enabled === true,
    executionMode: String(policy?.execution_mode ?? "approval"),
    automaticExecutionEnabled: policy?.auto_execute_enabled === true,
    walletAutomaticExecutionEnabled: policy?.wallet_auto_execute_enabled === true,
    killSwitch: policy?.kill_switch === true,
    ownerApprovalRequired: policy?.require_confirmation !== false,
    minimumLiveBalanceUsd: Number(policy?.min_live_balance_usd ?? 10),
    maxOrderUsd: Number(policy?.max_order_usd ?? 10),
    maxDailyNotionalUsd: Number(policy?.max_daily_notional_usd ?? 25),
    maxOpenPositions: Number(policy?.max_open_positions ?? 3),
    maxSlippageBps: Number(policy?.max_slippage_bps ?? 100),
    maxPriceDeviationBps: Number(policy?.max_price_deviation_bps ?? 50),
    minimumLiquidityUsd: Number(policy?.minimum_liquidity_usd ?? 50000),
    paperGraduationDays: Number(policy?.paper_graduation_days ?? 30),
    migrations: Array.isArray(migrations) ? migrations : [],
    releaseState: "live_execution_not_activated",
  };
}

async function assessLiveMigration(user: any, row: any, body: any): Promise<Response> {
  const accountType = String(body?.accountType ?? "");
  const marketType = String(body?.marketType ?? "");
  const connectionId = clean(body?.connectionId, 64);
  const windowDays = Number(body?.windowDays ?? 30);
  if (!["exchange", "wallet", "broker"].includes(accountType) ||
      !["solana", "crypto", "stocks", "forex"].includes(marketType) ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(connectionId) ||
      ![7, 30].includes(windowDays)) {
    return response({ ok: false, message: "Choose a supported account, market, and 7- or 30-day review window." }, 400);
  }

  // Confirm connection ownership before calling the privileged database gate.
  if (accountType === "wallet") {
    const rows = await db(
      "n8_wallet_connections?select=id,provider,status,chain_namespace,chain_id,network&user_id=eq." +
        encodeURIComponent(row.id) + "&id=eq." + encodeURIComponent(connectionId) + "&status=eq.verified&limit=1"
    );
    if (!Array.isArray(rows) || rows.length !== 1) {
      return response({ ok: false, message: "This verified wallet does not belong to your Telegram account." }, 403);
    }
  } else if (accountType === "exchange") {
    const rows = await db(
      "n8_exchange_connections?select=id,provider,environment,status&user_id=eq." +
        encodeURIComponent(row.id) + "&id=eq." + encodeURIComponent(connectionId) + "&status=eq.active&limit=1"
    );
    if (!Array.isArray(rows) || rows.length !== 1) {
      return response({ ok: false, message: "This active exchange connection does not belong to your Telegram account." }, 403);
    }
    if (rows[0].environment !== "live") {
      return response({
        ok: false,
        message: "This exchange connection is " + String(rows[0].environment) + ". Demo/testnet keys cannot be migrated to live. A separate live-key onboarding and connector certification is required.",
      }, 400);
    }
  } else {
    return response({ ok: false, message: "A live broker connector is not yet certified for this market." }, 400);
  }

  if (windowDays === 30 && marketType === "crypto") {
    const finalized = await db("rpc/n8_finalize_mature_paper_session", "POST", {
      p_user_id: row.id,
      p_market_type: "crypto",
    });
    if (finalized?.ok === true && finalized?.already_completed !== true) {
      await audit(row.id, null, "paper_evaluation_finalized", "ok", {
        session_id: finalized.session_id,
        completed_at: finalized.completed_at,
      });
    }
  }

  let assessment = await db("rpc/n8_live_gate_assess", "POST", {
    p_user_id: row.id,
    p_window_days: windowDays,
    p_account_type: accountType,
    p_connection_id: connectionId,
    p_market_type: marketType,
  });

  if (!assessment || typeof assessment !== "object") {
    throw new Error("live_gate_assessment_unavailable");
  }

  // Seven days is a progress checkpoint only. The uploaded 9N8 specification
  // uses a 30-day forward-paper period before live eligibility.
  if (windowDays === 7) {
    assessment = {
      ...assessment,
      eligible: false,
      status: "progress_review_only",
      reasons: [
        ...(Array.isArray(assessment.reasons) ? assessment.reasons : []),
        "The 7-day review is a progress checkpoint only. Complete the 30-day forward-paper evaluation before live eligibility.",
      ],
    };
    await audit(row.id, null, "live_migration_progress_review", "ok", {
      account_type: accountType, market_type: marketType, window_days: 7,
    });
    return response({ ok: true, assessment, liveTradingEnabled: false });
  }

  const eligible = assessment.eligible === true;
  const migrationStatus = eligible ? "eligible_pending_setup" : "ineligible";
  const failedChecks = Array.isArray(assessment.reasons) ? assessment.reasons : [];
  const existing = await db(
    "n8_live_migrations?select=id&user_id=eq." + encodeURIComponent(row.id) +
      "&account_type=eq." + encodeURIComponent(accountType) +
      "&connection_id=eq." + encodeURIComponent(connectionId) +
      "&market_type=eq." + encodeURIComponent(marketType) +
      "&evaluation_window_days=eq.30&order=created_at.desc&limit=1"
  );
  let migrationId = "";
  if (Array.isArray(existing) && existing[0]?.id) {
    migrationId = String(existing[0].id);
    await db("n8_live_migrations?id=eq." + encodeURIComponent(migrationId), "PATCH", {
      status: migrationStatus,
      execution_mode: "approval",
      gate_snapshot: assessment,
      failed_checks: failedChecks,
      updated_at: new Date().toISOString(),
    }, "return=minimal");
  } else {
    const inserted = await db("n8_live_migrations", "POST", {
      user_id: row.id,
      account_type: accountType,
      connection_id: connectionId,
      market_type: marketType,
      evaluation_window_days: 30,
      status: migrationStatus,
      execution_mode: "approval",
      gate_snapshot: assessment,
      failed_checks: failedChecks,
    }, "return=representation");
    migrationId = Array.isArray(inserted) ? String(inserted[0]?.id ?? "") : "";
  }
  await audit(row.id, null, "live_migration_assessed", eligible ? "eligible_pending_manual_release" : "ineligible", {
    migration_id: migrationId || null,
    account_type: accountType,
    market_type: marketType,
    evaluation_window_days: 30,
    failed_check_count: failedChecks.length,
  });

  return response({
    ok: true,
    migrationId: migrationId || null,
    assessment,
    liveTradingEnabled: false,
    nextStep: eligible
      ? "The paper and connector gates passed. Live execution still requires a separately owner-signed trading-vault delegation and a manual connector release. No live order can be placed from this assessment."
      : "Continue genuine paper trading and resolve the listed checks. No live trading is enabled.",
  });
}

async function applyEmergencyStop(userId: string, mode: "pause" | "stop"): Promise<Obj> {
  const now = new Date().toISOString();
  await db("n8_risk_policies?user_id=eq." + encodeURIComponent(userId), "PATCH", {
    kill_switch: true,
    live_trading_enabled: false,
    auto_execute_enabled: false,
    wallet_auto_execute_enabled: false,
    execution_mode: "approval",
    updated_at: now,
  }, "return=minimal");
  const nextState = mode === "stop" ? "stopped" : "paused";
  await db(
    "n8_strategy_runs?user_id=eq." + encodeURIComponent(userId) + "&state=in.(running,paused)",
    "PATCH", { state: nextState, updated_at: now }, "return=minimal"
  );
  await db(
    "n8_paper_sessions?user_id=eq." + encodeURIComponent(userId) + "&status=eq.running",
    "PATCH", { status: mode === "stop" ? "stopped" : "paused", updated_at: now }, "return=minimal"
  );
  await db(
    "n8_live_migrations?user_id=eq." + encodeURIComponent(userId) + "&status=eq.active",
    "PATCH", { status: "paused", paused_at: now, updated_at: now }, "return=minimal"
  );
  return {
    stopped: true,
    liveTradingEnabled: false,
    killSwitch: true,
    openPositionsWereNotLiquidated: true,
    message: mode === "stop"
      ? "Emergency stop is active. New execution is blocked. Existing positions remain open and visible; 9N8 did not liquidate them."
      : "Automation is paused. Live trading is off. Existing positions remain open and visible.",
  };
}

async function liveStatusText(userId: string): Promise<string> {
  const state = await getLiveStatus(userId);
  const migrations = state.migrations as any[];
  const latest = migrations[0];
  const lines = [
    "9N8 LIVE TRADING STATUS",
    "",
    "Live execution: " + (state.liveTradingEnabled ? "ENABLED" : "LOCKED"),
    "Mode: " + String(state.executionMode).toUpperCase(),
    "Automatic execution: " + (state.automaticExecutionEnabled ? "ON" : "OFF"),
    "Wallet auto execution: " + (state.walletAutomaticExecutionEnabled ? "ON" : "OFF"),
    "Emergency stop: " + (state.killSwitch ? "ACTIVE" : "OFF"),
    "Minimum verified quote balance: $" + Number(state.minimumLiveBalanceUsd).toFixed(2),
    "Maximum order: $" + Number(state.maxOrderUsd).toFixed(2),
    "Daily notional cap: $" + Number(state.maxDailyNotionalUsd).toFixed(2),
    "Maximum open positions: " + String(state.maxOpenPositions),
    "Maximum slippage: " + String(state.maxSlippageBps) + " bps",
    "Minimum liquidity: $" + Number(state.minimumLiquidityUsd).toLocaleString("en-US"),
    "",
    "Live release: not activated in this build.",
  ];
  if (latest) {
    lines.push(
      "",
      "Latest migration: " + String(latest.status),
      "Market: " + String(latest.market_type),
      "Window: " + String(latest.evaluation_window_days) + " days",
    );
    const failed = Array.isArray(latest.failed_checks) ? latest.failed_checks : [];
    if (failed.length) lines.push("Blocking checks: " + failed.length);
  } else {
    lines.push("No live migration assessment yet. Use /migrate to assess your account.");
  }
  return lines.join("\n");
}

async function paperStatusText(userId: string): Promise<string> {
  const sessions = await db(
    "n8_paper_sessions?select=id,market_type,status,evaluation_window_days,started_at,last_activity_at,initial_balance_usd,cash_balance_usd,tick_count,last_tick_at,last_tick_error,config&user_id=eq." +
      encodeURIComponent(userId) + "&order=created_at.desc&limit=1"
  );
  if (!Array.isArray(sessions) || sessions.length === 0) {
    return "9N8 PAPER TRADING\n\nNo paper run yet. Send /start and tap Start crypto paper run. The engine starts with $1,000 virtual cash and uses public spot prices only. No real funds or API keys are involved.";
  }
  const session = sessions[0];
  const sessionId = String(session.id);
  const [ticks, positions, trades] = await Promise.all([
    db("n8_paper_ticks?select=tick_at,symbols_scanned,symbols_failed,signals_generated,opened_trades,closed_trades,cash_balance_usd,equity_usd,realized_pnl_usd,unrealized_pnl_usd,status,metadata&session_id=eq." +
      encodeURIComponent(sessionId) + "&order=tick_at.desc&limit=1"),
    db("n8_paper_positions?select=symbol,quantity,average_entry_price,mark_price,unrealized_pnl_usd,stop_loss_price,take_profit_price&session_id=eq." +
      encodeURIComponent(sessionId) + "&status=eq.open&order=opened_at.desc&limit=10"),
    db("n8_paper_trades?select=symbol,side,quantity,fill_price,notional_usd,fee_usd,realized_pnl_usd,filled_at&session_id=eq." +
      encodeURIComponent(sessionId) + "&order=filled_at.desc&limit=6"),
  ]);
  const num = (value: unknown): number => {
    const n = Number(value ?? 0);
    return Number.isFinite(n) ? n : 0;
  };
  const initial = num(session.initial_balance_usd) || 1000;
  const cash = num(session.cash_balance_usd);
  const tick = Array.isArray(ticks) ? ticks[0] : null;
  const equity = tick ? num(tick.equity_usd) : cash;
  const pnl = equity - initial;
  const lines = [
    "9N8 PAPER TRADING",
    "",
    "Status: " + String(session.status).toUpperCase(),
    "Market: " + String(session.market_type).toUpperCase() + " spot",
    "Engine: EMA 9 / EMA 21 crossover · RSI 14 · 1-minute candles",
    "Starting virtual balance: $" + initial.toFixed(2),
    "Cash balance: $" + cash.toFixed(2),
    "Latest equity: $" + equity.toFixed(2),
    "Net P&L: " + (pnl >= 0 ? "+$" : "-$") + Math.abs(pnl).toFixed(2),
    "Realized P&L: $" + num(tick?.realized_pnl_usd).toFixed(2),
    "Unrealized P&L: $" + num(tick?.unrealized_pnl_usd).toFixed(2),
    "Worker ticks: " + String(session.tick_count ?? 0),
    "Last scan: " + String(tick?.tick_at ?? session.last_tick_at ?? "waiting for first scan"),
  ];
  if (tick) {
    lines.push(
      "Symbols scanned: " + String(tick.symbols_scanned) +
        " · failed: " + String(tick.symbols_failed),
      "Signals: " + String(tick.signals_generated) +
        " · opened: " + String(tick.opened_trades) +
        " · closed: " + String(tick.closed_trades),
      "Latest worker result: " + String(tick.status).toUpperCase(),
    );
  } else {
    lines.push("First market scan is pending. The scheduled worker checks every minute.");
  }
  lines.push("", "OPEN PAPER POSITIONS");
  if (Array.isArray(positions) && positions.length) {
    for (const p of positions) {
      lines.push(
        String(p.symbol) + " · quantity " + num(p.quantity).toPrecision(6) +
        " · mark $" + num(p.mark_price).toPrecision(7) +
        " · P&L $" + num(p.unrealized_pnl_usd).toFixed(4)
      );
    }
  } else {
    lines.push("No open positions.");
  }
  lines.push("", "LATEST SIMULATED FILLS");
  if (Array.isArray(trades) && trades.length) {
    for (const t of trades) {
      lines.push(
        String(t.side).toUpperCase() + " " + String(t.symbol) +
        " · $" + num(t.notional_usd).toFixed(2) +
        " · fill $" + num(t.fill_price).toPrecision(7) +
        " · fee $" + num(t.fee_usd).toFixed(4)
      );
    }
  } else {
    lines.push("No simulated fills yet. The strategy waits for a real signal.");
  }
  if (session.last_tick_error) lines.push("", "Last scan issue: " + String(session.last_tick_error));
  lines.push(
    "",
    "PAPER ONLY: public market data, simulated fills, fees and slippage. No real orders, no wallet signing, and live trading remains locked.",
    "Use /pause to pause scans or /stop for the emergency stop."
  );
  return lines.join("\n");
}

async function startCryptoPaper(userId: string, chatId: number): Promise<{ ok: boolean; message: string }> {
  const policies = await db(
    "n8_risk_policies?select=kill_switch&user_id=eq." + encodeURIComponent(userId) + "&limit=1"
  );
  if (Array.isArray(policies) && policies[0]?.kill_switch === true) {
    return {
      ok: false,
      message: "Paper trading is blocked by the emergency stop. Send /resume and confirm the resume button before starting again.",
    };
  }
  const result = await db("rpc/n8_start_paper_session", "POST", {
    p_user_id: userId,
    p_market_type: "crypto",
    p_chat_id: chatId,
  });
  if (result?.ok !== true || !result?.session_id) throw new Error("paper_session_start_not_confirmed");
  await audit(userId, chatId, "paper_session_started", "ok", {
    session_id: result.session_id, market_type: "crypto",
  });
  return {
    ok: true,
    message: "PAPER RUN STARTED\n\n" +
      "Mode: PAPER ONLY · $1,000 virtual starting cash\n" +
      "Market: real Binance Spot 1-minute candles + best bid/ask depth\n" +
      "Universe: BTC, ETH, SOL, BNB, XRP, DOGE, SHIB, PEPE, BONK and WIF pairs where listed\n" +
      "Strategy: EMA 9 / EMA 21 crossover with RSI 14 confirmation\n" +
      "Execution model: ask/bid order-book VWAP · configurable fee estimate · spread/slippage gates\n" +
      "Risk: $50 maximum virtual entry · up to 3 open positions · virtual fees only\n\n" +
      "The simulator reads real Binance candles, ticker and order-book depth. Paper fills consume visible asks/bids and account for estimated fees and slippage. Signals may produce no trade; 9N8 will not invent market prices or fills.\n\n" +
      "Use /paper for the latest scan, virtual equity, positions and simulated fills. /pause pauses scans; /stop activates the emergency stop. Real trading remains OFF."
  };
}

async function setBotProfileAndCommands(): Promise<void> {
  // Keep the bot's public description and command list easy to understand.
  try {
    await telegram("setMyDescription", {
      description: "9N8 Solana Sniper: separate Paper and Live control panels, live launchpad monitoring, positions, risk rules and wallet controls. Paper uses virtual money; live execution remains gated."
    });
    await telegram("setMyShortDescription", {
      short_description: "Solana Sniper · Separate Paper & Live controls · Safety gates."
    });
    await telegram("setMyCommands", {
      scope: { type: "all_private_chats" },
      commands: [
        { command: "start", description: "Open the 9N8 Solana Sniper dashboard" },
        { command: "sniper", description: "Open the complete Solana Sniper control panel" },
        { command: "solana_paper", description: "Open separate Solana Paper Trading controls" },
        { command: "solana_live", description: "Open gated Solana Live Trading controls" },
        { command: "networks", description: "Show connected and unavailable blockchains" },
        { command: "positions", description: "View Paper and Live positions" },
        { command: "settings", description: "Open sniper trading settings" },
        { command: "safety", description: "Open anti-rug safety filters" },
        { command: "services", description: "See all 9N8 services" },
        { command: "simulator", description: "Open the exchange live-price simulator" },
        { command: "connect", description: "Connect Binance, Bybit or OKX" },
        { command: "connections", description: "See your connected accounts" },
        { command: "wallets", description: "See linked wallets" },
        { command: "live", description: "See live readiness and risk status" },
        { command: "risk", description: "See your safety limits" },
        { command: "approvals", description: "See requests waiting for approval" },
        { command: "pause", description: "Pause new activity" },
        { command: "stop", description: "Emergency stop" },
        { command: "help", description: "Get all commands and instructions" }
      ]
    });
  } catch {
    // Telegram metadata is secondary; never block the /start response.
  }
}

function serviceOverview(): string {
  return [
    "WELCOME TO 9N8",
    "Solana Sniper is the main control panel. Paper and Live are separate; open /sniper for launch monitoring, positions, settings and anti-rug filters.",
    "",
    "📊 FOLLOW THE MARKET",
    "See live prices, charts and market activity. The practice simulator uses real Binance market prices.",
    "",
    "🧪 SOLANA PAPER TRADING",
    "Use live Solana market data with virtual SOL. Paper fills, positions, settings and PnL are separate; no blockchain transaction is signed or broadcast.",
    "",
    "🔗 CONNECT YOUR EXCHANGE",
    "Connect Binance, Bybit or OKX. Choose the access you want to check: Spot, Margin, Futures or Options. Binance also has one combined Spot & Margin & Stock Trading permission for eligible products.",
    "Each exchange has its own permission rules. 9N8 checks the key, shows what was verified, and saves it securely. Keep Withdraw and account transfers turned off.",
    "",
    "🛡️ YOU STAY IN CONTROL",
    "Connecting an exchange does not place an order. Real-money exchange trading is still locked. Wallet links are separate and do not give 9N8 permission to move your funds.",
    "",
    "GET STARTED",
    "/sniper — open the Solana Sniper dashboard",
    "/solana_paper — open isolated virtual Solana trading",
    "/solana_live — open gated live-wallet controls",
    "/networks — show supported and unavailable chains",
    "/positions — open Paper and Live positions",
    "/settings — open sniper rules and exit settings",
    "/safety — open the anti-rug gatekeeper",
    "/live — check live-trading status",
    "/pause — pause new activity",
    "/stop — stop the bot",
    "/help — get help",
  ].join("\n");
}

async function handleMessage(update: any) {
  const m = update?.message ?? update?.edited_message;
  if (!m?.chat?.id || !m?.from?.id) return;
  const chatType = String(m.chat.type ?? "");
  const chatId = Number(m.chat.id);
  const text = String(m.text ?? "").trim();
  if (!text.startsWith("/")) {
    if (chatType !== "private") return;
    if (isEvmAddressCandidate(text)) {
      await userRow(m.from);
      await send(chatId,
        "EVM address format detected. Base, Ethereum and BNB Chain are not connected to 9N8 execution, so no wallet lookup, quote or trade was attempted. Use /networks to see the current chain status.",
        networkSelectorKeyboard());
      return;
    }
    if (isSolanaAddressCandidate(text)) {
      try {
        const address = new PublicKey(text);
        if (address.toBase58() === text) {
          await userRow(m.from);
          await send(chatId,
            "Solana address format detected. Pasting an address never starts a buy or signs a transaction. Open the Solana Sniper dashboard to review its supported scanner and keep Paper and Live controls separate.",
            solanaPanelKeyboard("dashboard"));
          return;
        }
      } catch {
        // Not a valid 32-byte Solana public key; ignore ordinary chat text.
      }
    }
    return;
  }
  const parts = text.split(/\s+/);
  const command = (parts[0].split("@")[0] ?? "").toLowerCase();
  const args = parts.slice(1);
  const actor = m.from;
  const row = await userRow(actor);

  if (chatType === "group" || chatType === "supergroup") {
    if (command.startsWith("/n8")) await groupCommand(m, command, args, row);
    else await send(chatId, "Personal accounts, wallets and trading are private-chat only. Group commands: /n8setup, /n8admin, /n8moderator.");
    return;
  }
  if (chatType !== "private") return;

  if (command === "/start" || command === "/menu") {
    await setBotProfileAndCommands();
    await audit(row.id, chatId, "private_start");
    await send(chatId,
      "🚀 WELCOME TO 9N8 — SOLANA SNIPER\n\n" +
      "Your Telegram control panel separates Solana Paper Trading from Live Sniper controls.\n\n" +
      "🧪 PAPER MODE\n" +
      "Uses live Solana market data with virtual SOL. Paper orders, balances, positions and PnL are separate and never sign or broadcast a blockchain transaction.\n\n" +
      "🔴 LIVE MODE\n" +
      "Uses a separate real-wallet control panel. Live trading stays locked until the server-side wallet, risk, operator and activation checks pass. Opening the panel does not place an order.\n\n" +
      "Use the buttons below for the complete dashboard, positions, settings and anti-rug filters. The exchange simulator and account connections remain available under /simulator and /connect.",
      mainKeyboard());
    return;
  }
    if (command === "/services") {
    await send(chatId, serviceOverview(), mainKeyboard());
    return;
  }
  if (command === "/networks" || command === "/chains" || command === "/network") {
    await send(chatId, networkSelectorText(), networkSelectorKeyboard());
    return;
  }
  if (command === "/help") {
    await send(chatId,
      "9N8 COMMANDS\n" +
      "/services — what 9N8 does and current feature status\n" +
      "/start — open private control panel\n" +
      "/status — inspect account and system state\n" +
      "/connect — connect Binance, Bybit, OKX or a wallet\n" +
      "/connections — list your exchange connections\n" +
      "/wallets — list your verified wallets\n" +
      "/migrate — assess live eligibility (does not enable live trading)\n" +
      "/live — live lock and risk status\n" +
      "/risk — current account risk limits\n" +
      "/approvals — pending trade proposals\n" +
      "/sniper — open complete Solana Sniper dashboard\n" +
      "/solana_paper — separate virtual Solana trading\n" +
      "/solana_live — gated Solana live-wallet controls\n" +
       "/networks — show supported and unavailable chains\n" +
      "/positions — open Paper and Live positions\n" +
      "/settings — open sniper trade settings\n" +
      "/safety — open anti-rug filters\n" +
      "/paper — open Solana Paper Trading\n" +
      "/pause — pause strategies and block new execution\n" +
      "/stop — emergency stop and kill switch\n" +
      "/resume — request to resume paper scanning; live stays off\n\n" +
      "GROUP COMMANDS\n/n8setup, /n8admin, /n8moderator\n/n8delete and /n8ban — reply to a member message\n/n8unban TELEGRAM_USER_ID\n\n" +
      "Never send exchange API secrets, private keys or seed phrases in chat."
    );
    return;
  }
  if (command === "/sniper" || command === "/dashboard") {
    await send(chatId,
      "🚀 SOLANA SNIPER CONTROL\n\nPaper and Live are separate. Use the dashboard for engine controls, positions, exit rules, safety filters and wallet actions.",
      solanaPanelKeyboard("dashboard"));
    return;
  }
  if (command === "/solana_live") {
    await send(chatId,
      "🔴 SOLANA LIVE CONTROLS\n\nLive trading is a separate real-wallet mode. The dashboard shows the current operator switch and activation, wallet, Paper-practice and balance readiness gates. The panel itself does not submit an order.",
      solanaPanelKeyboard("live"));
    return;
  }
  if (command === "/positions") {
    await send(chatId, "📊 SOLANA POSITIONS\n\nOpen the dashboard to inspect Paper positions and separately recorded Live positions and receipts.", solanaPanelKeyboard("positions"));
    return;
  }
  if (command === "/settings") {
    await send(chatId, "⚙️ SOLANA SNIPER SETTINGS\n\nOpen the dashboard to edit paper buy size, slippage, take-profit, trailing-stop, hard-stop and holding-time settings. Live controls remain subject to server-side limits.", solanaPanelKeyboard("settings"));
    return;
  }
  if (command === "/safety") {
    await send(chatId, "🛡️ SOLANA SAFETY FILTERS\n\nOpen the dashboard to review mint/freeze authority, liquidity-lock and holder-concentration gates. Unverified safety data must fail closed.", solanaPanelKeyboard("safety"));
    return;
  }
    if (command === "/connect") {
    await send(chatId, "Open this private secure form to connect Binance, Bybit or OKX. Choose the markets you want to use. Keep Withdraw and account transfers disabled. Never send API credentials, private keys or seed phrases in Telegram chat.", {
      inline_keyboard: [[{ text: "Open secure connections", web_app: { url: MINI_APP_URL } }]],
    });
    return;
  }
  if (command === "/migrate") {
    await send(chatId,
      "LIVE-TRADING READINESS\\n\\n" +
      await liveStatusText(row.id) +
      "\\n\\nThis is a status report only. Live execution remains locked; connecting a wallet or exchange does not enable real-money orders."
    );
    return;
  }
  if (command === "/live") {
    await send(chatId, await liveStatusText(row.id));
    return;
  }
  if (command === "/risk") {
    const state = await getLiveStatus(row.id);
    await send(chatId,
      "YOUR RISK POLICY\n\n" +
      "Execution mode: " + String(state.executionMode) + "\n" +
      "Owner approval: " + (state.ownerApprovalRequired ? "required" : "not required") + "\n" +
      "Minimum live balance: $" + Number(state.minimumLiveBalanceUsd).toFixed(2) + "\n" +
      "Maximum order: $" + Number(state.maxOrderUsd).toFixed(2) + "\n" +
      "Daily notional cap: $" + Number(state.maxDailyNotionalUsd).toFixed(2) + "\n" +
      "Maximum open positions: " + String(state.maxOpenPositions) + "\n" +
      "Maximum slippage: " + String(state.maxSlippageBps) + " bps\n" +
      "Minimum liquidity: $" + Number(state.minimumLiquidityUsd).toLocaleString("en-US") + "\n" +
      "Emergency kill switch: " + (state.killSwitch ? "ACTIVE" : "OFF") + "\n\n" +
      "No risk setting can override a hard reject or connector security failure."
    );
    return;
  }
  if (command === "/pause" || command === "/stop") {
    const mode = command === "/stop" ? "stop" : "pause";
    const result = await applyEmergencyStop(row.id, mode);
    await audit(row.id, chatId, mode === "stop" ? "emergency_stop" : "automation_paused", "ok");
    await send(chatId, String(result.message));
    return;
  }
  if (command === "/resume") {
    await send(chatId,
      "Resume is a separate owner action. Confirm only if you want to clear the emergency stop for paper scanning. Live trading will remain disabled.",
      { inline_keyboard: [[{ text: "Confirm resume paper scanning", callback_data: "n8:resume_confirm" }]] }
    );
    return;
  }
  if (command === "/status") {
    await send(chatId, await liveStatusText(row.id));
    return;
  }
  if (command === "/paper" || command === "/solana_paper") {
    await send(chatId,
      "🧪 SOLANA PAPER TRADING\n\n" +
      "This is the isolated virtual account. It reads live Solana market data but does not sign, submit or broadcast real transactions. Paper balance, fills, positions and PnL are kept separate from Live.\n\n" +
      "Open the Paper section to start or pause the paper engine, edit buy size and exit rules, inspect positions and review simulated fills.",
      solanaPanelKeyboard("paper"));
    return;
  }
  if (command === "/simulator") {
    await send(chatId, "LIVE-MARKET PAPER SIMULATOR\\n\\nReal Binance Spot candles, ticker and order-book data. Paper orders use virtual cash, visible depth VWAP and estimated fees; no real order is submitted.", {
      inline_keyboard: [[{ text: "📈 Open 9N8 simulator", web_app: { url: "https://9n8-secure-connect.vercel.app/?view=simulator" } }]]
    });
    return;
  }
  if (command === "/approvals") {
    const proposals = await db(
      "n8_trade_proposals?select=id,symbol,venue,side,environment,quote_amount_usd,status,expires_at&user_id=eq." +
        encodeURIComponent(row.id) + "&status=in.(pending,approved,executing,submitted,partially_filled)&order=created_at.desc&limit=10"
    );
    if (!Array.isArray(proposals) || proposals.length === 0) {
      await send(chatId, "APPROVALS\n\nNo pending trade proposals are recorded for your account.");
    } else {
      await send(chatId, "PENDING TRADE PROPOSALS\n\n" + proposals.map((v: any) =>
        String(v.symbol) + " · " + String(v.side).toUpperCase() + "\n" +
        String(v.venue) + " · " + String(v.environment) + "\n" +
        "Notional: " + String(v.quote_amount_usd ?? "not set") + "\n" +
        "Status: " + String(v.status) + "\nExpires: " + String(v.expires_at)
      ).join("\n\n"));
    }
    return;
  }
  if (command === "/connections") {
    await send(chatId, await listConnections(row.id));
    return;
  }
  if (command === "/wallets") {
    await send(chatId, await listWallets(row.id));
    return;
  }
  await send(chatId, "I did not recognize that command. Send /help.");
}

async function handleCallback(update: any) {
  const q = update.callback_query;
  if (!q) return;
  await telegram("answerCallbackQuery", { callback_query_id: q.id });
  const chat = q.message?.chat;
  if (!chat?.id || chat.type !== "private" || !q.from?.id) return;
  const row = await userRow(q.from);
  if (q.data === "n8:services") await send(Number(chat.id), serviceOverview(), mainKeyboard());
  if (q.data === "n8:networks") {
    await send(Number(chat.id), networkSelectorText(), networkSelectorKeyboard());
  }
  if (String(q.data ?? "").startsWith("n8:network:")) {
    const networkCode = String(q.data).slice("n8:network:".length);
    const network = getNetwork(networkCode);
    if (!network) {
      await send(Number(chat.id), "Unknown network. Solana Mainnet remains the only supported execution path.", networkSelectorKeyboard());
    } else if (!networkExecutionEnabled(network.code)) {
      await audit(row.id, Number(chat.id), "unsupported_network_selection_blocked", "rejected", { network: network.code });
      await send(Number(chat.id),
        network.label.toUpperCase() + " IS NOT CONNECTED\n\n" + network.note +
        "\n\nSolana Mainnet remains the only active execution network. No RPC, wallet, quote or transaction was attempted.",
        networkSelectorKeyboard());
    } else {
      await send(Number(chat.id),
        "✅ SOLANA MAINNET REMAINS ACTIVE\n\nPaper and Live remain separate. Live trading is still governed by the existing operator, wallet, risk and activation gates.",
        mainKeyboard());
    }
  }
  if (q.data === "n8:solana_paper") {
    await send(Number(chat.id), "🧪 SOLANA PAPER TRADING\n\nLive market data · virtual SOL · separate practice ledger · no blockchain transaction is signed or broadcast.", solanaPanelKeyboard("paper"));
  }
  if (q.data === "n8:solana_live") {
    await send(Number(chat.id), "🔴 SOLANA LIVE CONTROLS\n\nReal-wallet trading remains gated by the readiness checks displayed in the Live panel. Opening the panel does not enable live orders.", solanaPanelKeyboard("live"));
  }
  if (q.data === "n8:solana_positions") {
    await send(Number(chat.id), "📊 Open Paper and Live positions in the Solana dashboard.", solanaPanelKeyboard("positions"));
  }
  if (q.data === "n8:solana_settings") {
    await send(Number(chat.id), "⚙️ Open sniper settings and exit rules in the Solana dashboard.", solanaPanelKeyboard("settings"));
  }
  if (q.data === "n8:solana_safety") {
    await send(Number(chat.id), "🛡 Open the anti-rug safety gatekeeper in the Solana dashboard.", solanaPanelKeyboard("safety"));
  }
  if (q.data === "n8:paper_start_crypto") {
    const started = await startCryptoPaper(row.id, Number(chat.id));
    await send(Number(chat.id), started.message, {
      inline_keyboard: [[{ text: "📈 Open live-market simulator", web_app: { url: "https://9n8-secure-connect.vercel.app/?view=simulator" } }]]
    });
  }
  if (q.data === "n8:paper_status") await send(Number(chat.id), await paperStatusText(row.id));
  if (q.data === "n8:connections") await send(Number(chat.id), await listConnections(row.id));
  if (q.data === "n8:live_status") await send(Number(chat.id), await liveStatusText(row.id));
  if (q.data === "n8:resume_confirm") {
    const now = new Date().toISOString();
    await db("n8_risk_policies?user_id=eq." + encodeURIComponent(row.id), "PATCH", {
      kill_switch: false,
      live_trading_enabled: false,
      auto_execute_enabled: false,
      wallet_auto_execute_enabled: false,
      execution_mode: "approval",
      updated_at: now,
    }, "return=minimal");
    await db(
      "n8_strategy_runs?user_id=eq." + encodeURIComponent(row.id) + "&state=eq.paused",
      "PATCH", { state: "running", updated_at: now }, "return=minimal"
    );
    await db(
      "n8_paper_sessions?user_id=eq." + encodeURIComponent(row.id) + "&status=eq.paused",
      "PATCH", { status: "running", updated_at: now }, "return=minimal"
    );
    await audit(row.id, Number(chat.id), "paper_resume_confirmed", "ok");
    await send(Number(chat.id),
      "Paper scanning resume request recorded. Live trading remains OFF and automatic execution remains OFF."
    );
  }
}
async function handleMyChatMember(update: any) {
  const e = update.my_chat_member;
  const chat = e?.chat;
  if (!chat || (chat.type !== "group" && chat.type !== "supergroup")) return;
  const bot = e.new_chat_member ?? {};
  await db("n8_group_settings?on_conflict=chat_id", "POST", {
    chat_id: Number(chat.id),
    title: clean(chat.title, 120),
    bot_status: String(bot.status ?? "unknown"),
    bot_rights: {
      can_delete_messages: bot.can_delete_messages === true,
      can_restrict_members: bot.can_restrict_members === true,
      can_manage_chat: bot.can_manage_chat === true,
      can_pin_messages: bot.can_pin_messages === true,
    },
  }, "resolution=merge-duplicates,return=minimal");
}


async function registerWebhook(body: any): Promise<Response> {
  if (!TOKEN || !HOOK_SECRET) {
    return response({ ok: false, message: "Add TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET in Supabase Edge Function Secrets first." }, 503);
  }
  if (!same(String(body?.setupSecret ?? ""), HOOK_SECRET)) {
    return response({ ok: false, message: "Webhook setup secret did not match." }, 403);
  }
  try {
    await telegram("setWebhook", {
      url: FUNCTION_URL,
      secret_token: HOOK_SECRET,
      allowed_updates: ["message", "callback_query", "my_chat_member"],
      drop_pending_updates: false,
      max_connections: 20,
    });
    const info = await telegram("getWebhookInfo", {});
    if (String(info?.url ?? "") !== FUNCTION_URL) {
      return response({ ok: false, message: "Telegram did not confirm the new webhook URL. Try again after checking the bot settings." }, 502);
    }
    return response({
      ok: true,
      message: "Telegram webhook registered.",
      pendingUpdateCount: Number(info.pending_update_count ?? 0),
    });
  } catch {
    return response({ ok: false, message: "Telegram rejected webhook registration. Check the BotFather token and webhook secret in Supabase." }, 502);
  }
}

async function mainHandler(req: Request): Promise<Response> {
  const url = new URL(req.url);
  if (req.method === "GET" && url.searchParams.get("view") === "connect") return Response.redirect(MINI_APP_URL, 302);
  if (req.method === "GET" && url.searchParams.get("view") === "setup") return Response.redirect(SETUP_PAGE_URL, 302);
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS_HEADERS });
  if (req.method === "GET" && url.searchParams.get("view") === "wallet-config") {
    return response({
      ok: true,
      walletConnectConfigured: Boolean(REOWN_PROJECT_ID),
      reownProjectId: REOWN_PROJECT_ID || null,
      supportedNamespaces: ["eip155", "solana"],
      watchOnlyNamespaces: ["eip155", "solana"],
      liveWalletExecution: false,
    });
  }
  if (req.method === "GET" && url.searchParams.get("view") === "setup") return setupHtml();
  if (req.method === "GET") return response({
    ok: true, service: "9N8 trading intelligence", state: "live-market-paper / live-account-connect", environment: "binance-spot-live-connect",
    config: {
      telegramTokenConfigured: Boolean(TOKEN),
      webhookSecretConfigured: Boolean(HOOK_SECRET),
      databaseAdminConfigured: Boolean(SUPABASE_SERVICE_KEY),
      walletConnectConfigured: Boolean(REOWN_PROJECT_ID),
    },
  });
  if (req.method !== "POST") return response({ ok: false, error: "method_not_allowed" }, 405);
  let body: any;
  try { body = await req.json(); } catch { return response({ ok: false, error: "invalid_json" }, 400); }

  // Webhook setup is protected by the secret already stored in Supabase.
  if (body?.action === "register_webhook") return await registerWebhook(body);
  // Mini App traffic is authenticated with Telegram-signed initData.
  if (body?.action === "connect_exchange" || body?.action === "watch_wallet" ||
      ["wallet_challenge", "wallet_verify", "wallet_revoke", "live_connections", "live_status", "live_assess", "live_pause",
       "paper_terminal", "paper_start", "binance_account", "okx_account",
       "jupiter_live_config", "jupiter_swap_order", "jupiter_swap_execute", "jupiter_swap_status",
       "jupiter_trigger_challenge", "jupiter_trigger_verify", "jupiter_trigger_refresh",
       "jupiter_trigger_vault", "jupiter_trigger_deposit_craft", "jupiter_trigger_create_dca",
       "jupiter_trigger_history", "jupiter_trigger_cancel_dca",
       "jupiter_trigger_confirm_cancel_dca"].includes(String(body?.action))) {
    return await handleMiniApp(body);
  }

  if (!TOKEN || !HOOK_SECRET) return response({ ok: false, error: "telegram_secrets_not_configured" }, 503);
  const supplied = req.headers.get("x-telegram-bot-api-secret-token") ?? "";
  if (!same(supplied, HOOK_SECRET)) return new Response("Forbidden", { status: 401 });

  if (body?.update_id !== undefined) {
    const claimed = await db("rpc/n8_claim_telegram_update", "POST", {
      p_update_id: Number(body.update_id),
      p_lease_seconds: 60,
    });
    if (claimed !== true) return response({ ok: true, duplicate: true });
  }

  try {
    if (body?.my_chat_member) await handleMyChatMember(body);
    else if (body?.callback_query) await handleCallback(body);
    else await handleMessage(body);

    if (body?.update_id !== undefined) {
      await db("n8_telegram_updates?update_id=eq." + encodeURIComponent(String(body.update_id)),
        "PATCH", { processed_at: new Date().toISOString(), lease_until: null, outcome: "processed" }, "return=minimal");
    }
    return response({ ok: true });
  } catch {
    // Release the update lease so Telegram's retry can safely re-attempt transient failures.
    if (body?.update_id !== undefined) {
      try {
        await db("n8_telegram_updates?update_id=eq." + encodeURIComponent(String(body.update_id)),
          "PATCH", { lease_until: new Date().toISOString(), outcome: "retryable_error" }, "return=minimal");
      } catch {
        // Retry will become available when the lease expires.
      }
    }
    // Do not log Telegram updates, credentials, tokens, or secret values.
    return response({ ok: false, error: "update_processing_failed" }, 500);
  }
}

Deno.serve(async (req: Request) => {
  try { return await mainHandler(req); }
  catch { return response({ ok: false, error: "request_failed" }, 500); }
});
