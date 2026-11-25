import { isAddress as isEvmAddress, verifyMessage as verifyEvmMessage } from "npm:viem@2.58.4";
import { validateOkxSpotRead, type OkxRegion } from "./okx-live.ts";
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

const TOKEN = (Deno.env.get("TELEGRAM_BOT_TOKEN") ?? "").trim();
const REOWN_PROJECT_ID = (Deno.env.get("REOWN_PROJECT_ID") ?? "").trim();
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

type Provider = "okx" | "binance";
type Credentials = { apiKey: string; apiSecret: string; passphrase?: string; region?: OkxRegion };
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
function mainKeyboard(): Obj {
  return { inline_keyboard: [
    [{ text: "▶ Start / view live-market paper run", callback_data: "n8:paper_start_crypto" }],
    [{ text: "📈 Open live-market simulator", web_app: { url: "https://9n8-secure-connect.vercel.app/?view=simulator" } }],
    [{ text: "Connect exchange · Binance / OKX", web_app: { url: MINI_APP_URL } }],
    [{ text: "Wallet watch / connect", web_app: { url: "https://9n8-secure-connect.vercel.app/connect?view=wallet" } }],
    [{ text: "📚 Services & features", callback_data: "n8:services" }],
    [{ text: "My exchange connections", callback_data: "n8:connections" }],
    [{ text: "Live status / risk", callback_data: "n8:live_status" }],
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
async function validateOKX(c: Credentials) {
  if (!c.passphrase) throw new Error("okx_passphrase_required");
  const path = "/api/v5/account/config";
  const timeResponse = await fetch("https://www.okx.com/api/v5/public/time");
  const timeJson = await timeResponse.json().catch(() => ({}));
  const ms = Number(timeJson?.data?.[0]?.ts);
  if (!timeResponse.ok || !Number.isFinite(ms)) throw new Error("exchange_temporarily_unavailable");
  const timestamp = new Date(ms).toISOString();
  const signature = b64(await mac(enc.encode(c.apiSecret), timestamp + "GET" + path));
  const data = await jsonGet("https://www.okx.com" + path, {
    "OK-ACCESS-KEY": c.apiKey,
    "OK-ACCESS-SIGN": signature,
    "OK-ACCESS-TIMESTAMP": timestamp,
    "OK-ACCESS-PASSPHRASE": c.passphrase,
    "x-simulated-trading": "1",
    accept: "application/json",
  });
  if (String(data?.code ?? "") !== "0" || !data?.data?.[0]) throw new Error("exchange_credentials_rejected");
  const permissions = String(data.data[0].perm ?? "").toLowerCase().split(",").map((v: string) => v.trim());
  if (permissions.includes("withdraw")) throw new Error("withdraw_permission_detected");
  return {
    label: "OKX Demo",
    permissions: {
      read: permissions.includes("read"),
      spotTrade: permissions.includes("trade"),
      withdraw: false,
      okxTradeScopeMayAlsoAllowFundingTransfers: permissions.includes("trade"),
    },
  };
}
async function binanceSignedGet(c: Credentials, path: string): Promise<any> {
  const query = "recvWindow=5000&timestamp=" + Date.now();
  const signature = hex(await mac(enc.encode(c.apiSecret), query));
  return await jsonGet("https://api.binance.com" + path + "?" + query + "&signature=" + signature, {
    "X-MBX-APIKEY": c.apiKey, accept: "application/json",
  });
}
async function validateBinanceLive(c: Credentials) {
  // Binance exposes the API key's own scope through this signed SAPI endpoint.
  // Fail closed unless read + Spot are enabled and withdrawal/transfer scopes are off.
  const restrictions = await binanceSignedGet(c, "/sapi/v1/account/apiRestrictions");
  if (restrictions?.enableReading !== true) throw new Error("binance_read_permission_required");
  if (restrictions?.enableSpotAndMarginTrading !== true) throw new Error("binance_spot_permission_required");
  if (restrictions?.enableWithdrawals !== false) throw new Error("withdraw_permission_detected");
  if (restrictions?.enableInternalTransfer !== false || restrictions?.permitsUniversalTransfer !== false) {
    throw new Error("binance_transfer_permission_detected");
  }
  if (restrictions?.enableFutures !== false || restrictions?.enableMargin !== false ||
      restrictions?.enableVanillaOptions !== false || restrictions?.enablePortfolioMarginTrading === true) {
    throw new Error("binance_non_spot_permission_detected");
  }
  const account = await binanceSignedGet(c, "/api/v3/account");
  if (!Array.isArray(account?.balances) || account?.canTrade !== true) {
    throw new Error("exchange_credentials_rejected");
  }
  if (account?.canWithdraw === true) throw new Error("withdraw_permission_detected");
  return {
    label: "Binance Spot LIVE",
    permissions: {
      read: true,
      spotTrade: true,
      withdraw: false,
      internalTransfer: false,
      universalTransfer: restrictions.permitsUniversalTransfer === true,
      margin: restrictions.enableMargin === true,
      futures: restrictions.enableFutures === true,
      options: restrictions.enableVanillaOptions === true,
      ipRestricted: restrictions.ipRestrict === true,
      accountCanTrade: true,
    },
    accountHint: account.uid == null ? null : "Binance UID · " + mask(String(account.uid)),
  };
}
async function validateBybit(c: Credentials) {
  const timestamp = String(Date.now());
  const recvWindow = "5000";
  const signature = hex(await mac(enc.encode(c.apiSecret), timestamp + c.apiKey + recvWindow));
  const data = await jsonGet("https://api-demo.bybit.com/v5/user/query-api", {
    "X-BAPI-API-KEY": c.apiKey,
    "X-BAPI-SIGN": signature,
    "X-BAPI-SIGN-TYPE": "2",
    "X-BAPI-TIMESTAMP": timestamp,
    "X-BAPI-RECV-WINDOW": recvWindow,
    accept: "application/json",
  });
  if (Number(data?.retCode) !== 0 || !data?.result) throw new Error("exchange_credentials_rejected");
  const permissions = data.result.permissions ?? {};
  const walletScopes = Array.isArray(permissions.Wallet) ? permissions.Wallet : [];
  if (walletScopes.length) throw new Error("wallet_transfer_or_withdraw_scope_detected");
  const spot = Array.isArray(permissions.Spot) ? permissions.Spot : [];
  return {
    label: "Bybit Demo",
    permissions: {
      read: true, spotTrade: spot.includes("SpotTrade"), withdraw: false,
      walletScopes: [], readOnly: Number(data.result.readOnly) === 1,
      derivativesScopes: (permissions.Derivatives?.length ?? 0) + (permissions.ContractTrade?.length ?? 0) > 0,
    },
  };
}
async function validateExchange(provider: Provider, environment: string, c: Credentials) {
  if (!c.apiKey || !c.apiSecret) throw new Error("api_key_and_secret_required");
  if (c.apiKey.length > 512 || c.apiSecret.length > 1024) throw new Error("credential_field_too_long");
  if (provider === "binance" && environment === "live") return await validateBinanceLive(c);
  if (provider === "okx" && environment === "live") return await validateOkxSpotRead({ apiKey: c.apiKey, apiSecret: c.apiSecret, passphrase: c.passphrase ?? "", region: c.region ?? "global" });
  throw new Error("unsupported_provider_environment");
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
"<style>:root{color-scheme:dark;font-family:system-ui,sans-serif;background:#0b1220;color:#edf2f7}*{box-sizing:border-box}body{margin:0;padding:18px;max-width:620px;margin:auto}h1{font-size:24px;margin:8px 0}p{color:#aab7ca;line-height:1.45}.card{background:#131e30;border:1px solid #26364d;border-radius:16px;padding:16px;margin:14px 0}label{display:block;font-size:13px;color:#afbdd0;margin:12px 0 6px}input,select{width:100%;padding:13px;border-radius:10px;border:1px solid #3a4b63;background:#0e1726;color:#fff;font-size:15px}button{width:100%;padding:14px;border:0;border-radius:10px;background:#2dbb80;color:#061a12;font-weight:700;font-size:15px;margin-top:15px}button:disabled{opacity:.55}small{color:#9aabc1}.warn{color:#f3c978}.status{min-height:24px;margin-top:10px;white-space:pre-wrap}.hidden{display:none}.pill{display:inline-block;background:#203b36;color:#8ce1bb;padding:5px 9px;border-radius:20px;font-size:12px}</style></head><body>",
"<div class='pill'>9N8 · Secure connection</div><h1>Connect your accounts</h1>",
"<p>Exchange credentials are sent directly over HTTPS to 9N8, verified against the selected demo/testnet account, and stored in encrypted server-side storage. Never enter credentials in Telegram chat.</p>",
"<div class='card'><h2>Exchange connection</h2><p class='warn'>Demo/testnet only. This page cannot submit trades and does not request withdrawal access.</p>",
"<form id='exchangeForm' autocomplete='off'><label for='provider'>Exchange</label><select id='provider' required><option value='okx'>OKX Demo Trading</option><option value='binance'>Binance Spot Testnet</option><option value='bybit'>Bybit Demo Trading</option></select>",
"<label for='environment'>Environment</label><select id='environment' required><option value='demo'>Demo</option></select>",
"<label for='apiKey'>API key</label><input id='apiKey' type='password' autocomplete='off' required maxlength='512'>",
"<label for='apiSecret'>API secret</label><input id='apiSecret' type='password' autocomplete='off' required maxlength='1024'>",
"<div id='passphraseRow'><label for='passphrase'>OKX API passphrase</label><input id='passphrase' type='password' autocomplete='off' maxlength='256'></div>",
"<button id='exchangeButton' type='submit'>Verify and save connection</button><div id='exchangeStatus' class='status' role='status'></div></form></div>",
"<div class='card'><h2>Wallet address watch</h2><p>Link a public address for future read-only portfolio monitoring. This does not connect to your wallet or authorize transactions. Never enter a recovery phrase or private key.</p>",
"<form id='walletForm' autocomplete='off'><label for='walletProvider'>Wallet</label><select id='walletProvider'><option value='trust_wallet'>Trust Wallet</option><option value='phantom'>Phantom</option><option value='walletconnect'>WalletConnect address (watch-only)</option><option value='manual'>Other / public address</option></select>",
"<label for='chain'>Network</label><select id='chain'><option value='eip155:1'>Ethereum mainnet</option><option value='eip155:8453'>Base mainnet</option><option value='eip155:56'>BNB Smart Chain</option><option value='solana:mainnet-beta'>Solana mainnet</option></select>",
"<label for='address'>Public wallet address</label><input id='address' type='text' autocomplete='off' spellcheck='false' maxlength='128' required>",
"<button id='walletButton' type='submit'>Save watch-only address</button><div id='walletStatus' class='status' role='status'></div></form></div>",
"<small>Closing this page clears the form. Connections belong only to the Telegram account that opened this page.</small>",
"<script>(function(){const tg=window.Telegram&&window.Telegram.WebApp;const exchangeForm=document.getElementById('exchangeForm');const walletForm=document.getElementById('walletForm');const provider=document.getElementById('provider');const environment=document.getElementById('environment');const passphraseRow=document.getElementById('passphraseRow');const passphrase=document.getElementById('passphrase');const exchangeStatus=document.getElementById('exchangeStatus');const walletStatus=document.getElementById('walletStatus');if(tg){tg.ready();tg.expand()}",
"function showProvider(){const v=provider.value;environment.innerHTML='';const o=document.createElement('option');o.value=v==='binance'?'testnet':'demo';o.textContent=v==='binance'?'Spot Testnet':'Demo Trading';environment.appendChild(o);passphraseRow.classList.toggle('hidden',v!=='okx');passphrase.required=v==='okx'}provider.addEventListener('change',showProvider);showProvider();",
"async function post(data){if(!tg||!tg.initData)throw new Error('Open this page from the 9N8 Telegram private chat.');const r=await fetch(location.pathname,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(Object.assign({initData:tg.initData},data))});const j=await r.json().catch(()=>({}));if(!r.ok||j.ok!==true)throw new Error(j.message||'Request failed.');return j}",
"exchangeForm.addEventListener('submit',async function(e){e.preventDefault();const button=document.getElementById('exchangeButton');button.disabled=true;exchangeStatus.textContent='Verifying credentials with the demo/testnet account…';const credentials={apiKey:document.getElementById('apiKey').value.trim(),apiSecret:document.getElementById('apiSecret').value.trim()};if(provider.value==='okx')credentials.passphrase=passphrase.value;try{const j=await post({action:'connect_exchange',provider:provider.value,environment:environment.value,credentials:credentials});exchangeStatus.textContent='Connected: '+j.label+'. The secret was saved securely and will not be displayed again.';exchangeForm.reset();showProvider()}catch(err){exchangeStatus.textContent=err.message||'Connection failed.'}finally{credentials.apiKey='';credentials.apiSecret='';credentials.passphrase='';document.getElementById('apiKey').value='';document.getElementById('apiSecret').value='';passphrase.value='';button.disabled=false}});",
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
  await validateBinanceLive(exchangeCredentials);
  const account = await binanceSignedRequest<any>(
    exchangeCredentials,
    "GET",
    "/api/v3/account",
    { omitZeroBalances: "true" },
  );
  if (!Array.isArray(account?.balances) || account?.canTrade !== true) {
    throw new Error("exchange_credentials_rejected");
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
    // USDT is the exchange's quote unit for this release; it is valued at its USD peg
    // for risk gating and the exact USDT quantity is preserved alongside this USD proxy.
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
    accountCanTrade: true,
    updatedAt: observedAt,
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
  const credentials = {
    apiKey: clean(stored?.apiKey, 512),
    apiSecret: clean(stored?.apiSecret, 1024),
    passphrase: clean(stored?.passphrase, 256),
    region: (["global", "us", "eea"].includes(regionValue) ? regionValue : "global") as OkxRegion,
  };
  const validation = await validateOkxSpotRead(credentials);
  const quote = validation.quoteBalance;
  const observedAt = new Date().toISOString();
  const evidence = {
    source: "okx_signed_spot_account",
    connectionId: connection.id,
    region: validation.region,
    quoteAsset: "USDT",
    availableQuote: quote.free,
    availableQuoteUsdValuation: "USDT nominal USD peg at 1.00",
    balances: validation.balances,
    accountLevel: validation.permissions.accountLevel,
    spotTradeScope: "not_verified",
    withdrawalScope: "not_verified",
    transferScope: "not_verified",
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
    source: "okx_signed_spot_account",
    evidence_hash: await sha256Hex(canonicalJson(evidence)),
    evidence,
  }, "return=minimal");
  await audit(userId, null, "okx_balance_refreshed", "ok", {
    connection_id: connection.id,
    quote_asset: "USDT",
    available_quote: quote.free,
    observed_at: observedAt,
    permission_scopes: "read validated; trade/withdraw/transfer not verified",
  });
  return {
    connection: { id: connection.id, label: connection.label, accountHint: connection.account_hint },
    balances: validation.balances,
    quoteBalance: { asset: "USDT", free: quote.free, locked: quote.locked, equity: quote.equity, availableUsdApprox: quote.free },
    permissions: validation.permissions,
    accountEquityUsdApprox: validation.accountEquityUsd,
    updatedAt: observedAt,
    liveOrdersEnabled: false,
  };
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
      "wallet_revoke",
    ]);
    const maxAge = longLivedReadActions.has(String(body?.action ?? "")) ? 24 * 60 * 60 : 300;
    const user = await verifyInitData(String(body?.initData ?? ""), maxAge);
    const row = await userRow(user);
    if (body.action === "connect_exchange") {
      const provider = String(body.provider) as Provider;
      const environment = String(body.environment);
      if (!["okx", "binance"].includes(provider)) throw new Error("unsupported_provider");
      const regionValue = clean(body?.credentials?.region, 16);
      const credentials: Credentials = {
        apiKey: clean(body?.credentials?.apiKey, 512),
        apiSecret: clean(body?.credentials?.apiSecret, 1024),
        passphrase: clean(body?.credentials?.passphrase, 256),
        region: (["global", "us", "eea"].includes(regionValue) ? regionValue : "global") as OkxRegion,
      };
      if (!["binance", "okx"].includes(provider) || environment !== "live") throw new Error("unsupported_provider_environment");
      const validation = await validateExchange(provider, environment, credentials);
      const id = await saveExchange(Number(user.id), provider, environment, validation.label, credentials,
        validation.permissions, validation.accountHint ?? null);
      await audit(row.id, null, "exchange_connected", "ok", { provider, environment, connection_id: id, permissions: validation.permissions });
      return response({
        ok: true, label: validation.label, connectionId: id,
        permissions: validation.permissions,
        accountHint: validation.accountHint ?? null,
        liveOrdersEnabled: false,
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
    }    if (body.action === "okx_account") {
      return response({ ok: true, ...(await liveOkxAccount(user, row.id)) });
    }
    if (body.action === "wallet_challenge") return await createWalletChallenge(user, body);
    if (body.action === "wallet_verify") return await verifyAndSaveWallet(user, body);
    if (body.action === "wallet_revoke") return await revokeWalletSession(user, body);
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
        message: "Binance Spot and OKX Spot accounts can be linked for account access. OKX read access is validated separately from trade and withdrawal scopes. Real orders remain locked until each connector passes release checks and each exact order has owner approval. A verified wallet still requires a separate owner-controlled trade permission.",
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
      binance_read_permission_required: "Enable Read permission for this Binance API key.",
      binance_spot_permission_required: "Enable Spot Trading permission only. Futures, margin and options are not supported here.",
      binance_transfer_permission_detected: "Disable internal transfers and universal transfers on the Binance API key.",
      binance_non_spot_permission_detected: "This key has margin, futures or options permission. Create a Spot-only key.",
      binance_live_connection_missing: "Connect your Binance Spot Live account first.",
      okx_live_connection_missing: "Connect your OKX Spot Live account first.",
      okx_api_key_secret_and_passphrase_required: "Enter your OKX API key, secret and API passphrase.",
      okx_passphrase_required: "Enter the API passphrase configured when you created this OKX API key.",
      withdraw_permission_detected: "This API key has withdrawal permission. Revoke it and create a new key with withdrawals disabled.",
      wallet_transfer_or_withdraw_scope_detected: "This Bybit key has Wallet transfer/withdrawal permissions. Create a key without Wallet permissions.",
      exchange_credentials_rejected: "The exchange rejected the credentials or the key belongs to a different environment.",
      exchange_temporarily_unavailable: "The exchange is temporarily unavailable. Try again shortly.",
      unsupported_provider_environment: "Choose Binance Spot or OKX Spot Live. Demo, testnet, margin and derivatives connectors are not accepted in this release.",
      wallet_address_or_network_invalid: "The public address format does not match the selected network.",
      credential_storage_failed: "Credentials were checked but could not be saved. No connection was created.",
      user_registration_failed: "Could not register your Telegram account. Try again.",
    };
    return response({ ok: false, message: messages[code] ?? "Could not complete the request. Check the provider and try again." }, 400);
  }
}
async function listConnections(userId: string): Promise<string> {
  const rows = await db("n8_exchange_connections?select=provider,environment,label,status&user_id=eq." +
    encodeURIComponent(userId) + "&status=neq.revoked&order=created_at.desc");
  if (!Array.isArray(rows) || rows.length === 0) return "No exchange accounts connected yet. Use /connect to link Binance Spot or OKX Spot.";
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
  // Keep Telegram's pre-Start description, profile bio and command menu aligned
  // with the actual deployed feature set. Metadata failure must never block /start.
  try {
    await telegram("setMyDescription", {
      description: "9N8 combines a real-market crypto paper simulator, Binance and OKX Spot account connections, self-custody wallet linking, trade approvals and risk controls. Paper fills are virtual; real order execution is separately gated."
    });
    await telegram("setMyShortDescription", {
      short_description: "Binance / OKX account connections, multi-wallet links, live market charts and paper trading."
    });
    await telegram("setMyCommands", {
      commands: [
        { command: "start", description: "Start 9N8 and show services" },
        { command: "services", description: "List features and limits" },
        { command: "paper", description: "Show virtual equity, positions and simulated fills" },
        { command: "simulator", description: "Open live Binance charts and paper simulator" },
        { command: "connect", description: "Connect Binance, OKX or wallet" },
        { command: "connections", description: "View exchange connections" },
        { command: "wallets", description: "View linked wallets" },
        { command: "live", description: "Show live-trading lock" },
        { command: "risk", description: "View risk limits" },
        { command: "approvals", description: "View pending approvals" },
        { command: "pause", description: "Pause strategies" },
        { command: "stop", description: "Emergency stop" },
        { command: "help", description: "Show all commands" }
      ]
    });
  } catch {
    // Telegram metadata is secondary; never block trading controls or /start.
  }
}

function serviceOverview(): string {
  return [
    "WHAT 9N8 PROVIDES",
    "",
    "📡 MARKET INTELLIGENCE",
    "Live public market observations, price and volume context, token/symbol lookups and strategy scans. The deployed paper pilot currently reads real Binance public spot candles for its supported USDT pairs.",
    "",
    "🧪 PAPER TRADING",
    "Use a $1,000 virtual account against real Binance Spot candles, ticker and order-book depth. Paper entries and exits use visible ask/bid VWAP, a configurable fee estimate, and spread/slippage gates. No exchange order is sent.",
    "",
    "📈 STRATEGY & SIGNALS",
    "The current crypto paper strategy uses EMA 9 / EMA 21 crossover with RSI 14 confirmation on closed one-minute candles. Signals are observations, not guaranteed predictions.",
    "",
    "🔗 EXCHANGE & WALLET CONNECTIONS",
    "Connect a Binance Spot Live API key with Read + Spot Trading permissions only; withdrawals, internal transfers, futures, margin and options must be disabled. Link EVM or Solana wallets for ownership verification or watch public addresses.",
    "",
    "🛡️ APPROVALS & RISK CONTROLS",
    "Review linked accounts, risk limits, pending proposals, pause controls and the emergency stop. Private connections are attached to the Telegram account that opened the bot.",
    "",
    "🚦 LIVE-TRADING READINESS",
    "9N8 includes a readiness assessment and a hard live-execution lock. Connecting Binance or verifying a wallet does not by itself authorize real-money orders.",
    "",
    "CURRENT LIMITS — PLEASE READ",
    "• Binance Spot Live account credentials can be connected and stored encrypted, but real-money order submission remains locked until the live connector is certified and the exact order is approved.",
    "• WalletConnect currently verifies wallet ownership only. No swap, token approval or background wallet trade-signing is enabled.",
    "• Stocks, forex, perps, unattended copy trading, launch sniping and DCA are not active in the deployed paper pilot.",
    "• Trojan is a feature reference, not a connected execution provider. 9N8 must use its own documented market-data and swap/exchange integrations.",
    "",
    "GET STARTED",
    "/paper — view virtual equity, positions and simulated fills",
    "/simulator — open live Binance charts and paper trading",
    "/connect — connect Binance Spot Live or wallet",
    "/connections — review exchange connections",
    "/wallets — review verified wallets and watch addresses",
    "/risk — view your configured risk limits",
    "/live — view live-trading readiness and lock status",
    "/pause — pause strategies and block new entries",
    "/stop — activate the emergency stop",
    "/help — view all commands"
  ].join("\n");
}

async function handleMessage(update: any) {
  const m = update?.message ?? update?.edited_message;
  if (!m?.chat?.id || !m?.from?.id) return;
  const chatType = String(m.chat.type ?? "");
  const chatId = Number(m.chat.id);
  const text = String(m.text ?? "").trim();
  if (!text.startsWith("/")) return;
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
    const started = await startCryptoPaper(row.id, chatId);
    const sessionLine = started.ok
      ? "PAPER SESSION: STARTED\n$1,000 virtual starting balance · Binance public spot candles · simulated fills only."
      : "PAPER SESSION: NOT STARTED\n" + started.message;
    await send(chatId,
      "⚡ WELCOME TO 9N8\n" +
      "Trading intelligence, paper testing and account controls in one private Telegram workspace.\n\n" +
      serviceOverview() +
      "\n\n" + sessionLine +
      "\n\nUse the buttons below to open your control panel. Send /services any time to review these features.",
      mainKeyboard());
    return;
  }
  if (command === "/services") {
    await send(chatId, serviceOverview(), mainKeyboard());
    return;
  }
  if (command === "/help") {
    await send(chatId,
      "9N8 COMMANDS\n" +
      "/services — what 9N8 does and current feature status\n" +
      "/start — open private control panel\n" +
      "/status — inspect account and system state\n" +
      "/connect — connect Binance Spot Live or wallet\n" +
      "/connections — list your exchange connections\n" +
      "/wallets — list your verified wallets\n" +
      "/migrate — assess live eligibility (does not enable live trading)\n" +
      "/live — live lock and risk status\n" +
      "/risk — current account risk limits\n" +
      "/approvals — pending trade proposals\n" +
      "/paper — paper-trading status and fills\n" +
      "/pause — pause strategies and block new execution\n" +
      "/stop — emergency stop and kill switch\n" +
      "/resume — request to resume paper scanning; live stays off\n\n" +
      "GROUP COMMANDS\n/n8setup, /n8admin, /n8moderator\n/n8delete and /n8ban — reply to a member message\n/n8unban TELEGRAM_USER_ID\n\n" +
      "Never send exchange API secrets, private keys or seed phrases in chat."
    );
    return;
  }
  if (command === "/connect") {
    await send(chatId, "Open this private secure form to connect Binance Spot or OKX Spot, or link a self-custody wallet. Use restricted API keys with withdrawals and transfers disabled. Never send API credentials, private keys or seed phrases in Telegram chat.", {
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
  if (command === "/paper") {
    await send(chatId, await paperStatusText(row.id), {
      inline_keyboard: [[{ text: "📈 Open live-market simulator", web_app: { url: "https://9n8-secure-connect.vercel.app/?view=simulator" } }]]
    });
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
       "paper_terminal", "paper_start", "binance_account", "okx_account"].includes(String(body?.action))) {
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
