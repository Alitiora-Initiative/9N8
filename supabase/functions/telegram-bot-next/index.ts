const TOKEN = (Deno.env.get("TELEGRAM_BOT_TOKEN") ?? "").trim();
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
const MINI_APP_URL = FUNCTION_URL + "?view=connect";
const enc = new TextEncoder();

type Provider = "okx" | "binance" | "bybit";
type Credentials = { apiKey: string; apiSecret: string; passphrase?: string };
type Obj = Record<string, unknown>;

function response(data: Obj, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
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
async function mac(keyBytes: Uint8Array, message: string): Promise<Uint8Array> {
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
    status: "active",
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
async function send(chatId: number, text: string, markup?: Obj) {
  const payload: Obj = { chat_id: chatId, text, disable_web_page_preview: true };
  if (markup) payload.reply_markup = markup;
  await telegram("sendMessage", payload);
}
function mainKeyboard(): Obj {
  return { inline_keyboard: [
    [{ text: "Connect exchange / link wallet", web_app: { url: MINI_APP_URL } }],
    [{ text: "My exchange connections", callback_data: "n8:connections" }],
  ] };
}

async function verifyInitData(raw: string): Promise<any> {
  if (!TOKEN || !raw || raw.length > 12000) throw new Error("telegram_auth_invalid");
  const p = new URLSearchParams(raw);
  const supplied = (p.get("hash") ?? "").toLowerCase();
  const authDate = Number(p.get("auth_date") ?? "0");
  const now = Math.floor(Date.now() / 1000);
  if (!/^[a-f0-9]{64}$/.test(supplied) || !Number.isSafeInteger(authDate)) throw new Error("telegram_auth_invalid");
  if (authDate > now + 30 || now - authDate > 300) throw new Error("telegram_auth_expired");
  const entries = Array.from(p.entries()).filter(([key]) => key !== "hash");
  entries.sort(([a], [b]) => a.localeCompare(b));
  const check = entries.map(([key, value]) => key + "=" + value).join("\n");
  const derived = await mac(enc.encode("WebAppData"), TOKEN);
  const calculated = hex(await mac(derived, check));
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
async function validateBinance(c: Credentials) {
  const query = "recvWindow=5000&timestamp=" + Date.now();
  const signature = hex(await mac(enc.encode(c.apiSecret), query));
  const data = await jsonGet("https://testnet.binance.vision/api/v3/account?" + query + "&signature=" + signature, {
    "X-MBX-APIKEY": c.apiKey, accept: "application/json",
  });
  if (!Array.isArray(data?.balances)) throw new Error("exchange_credentials_rejected");
  if (data.canWithdraw === true) throw new Error("withdraw_permission_detected");
  const scopes = Array.isArray(data.permissions) ? data.permissions.map(String) : [];
  return {
    label: "Binance Spot Testnet",
    permissions: { read: true, spotTrade: data.canTrade === true || scopes.includes("SPOT"), withdraw: false, scopes },
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
  if (c.apiKey.length > 512 || c.apiSecret.length > 1024 || (c.passphrase ?? "").length > 256) {
    throw new Error("credential_field_too_long");
  }
  if (provider === "okx" && environment === "demo") return await validateOKX(c);
  if (provider === "binance" && environment === "testnet") return await validateBinance(c);
  if (provider === "bybit" && environment === "demo") return await validateBybit(c);
  throw new Error("unsupported_provider_environment");
}
async function saveExchange(telegramId: number, provider: Provider, environment: string,
  label: string, credentials: Credentials, permissions: Obj): Promise<string> {
  const result = await db("rpc/n8_store_exchange_secret", "POST", {
    p_telegram_user_id: telegramId,
    p_provider: provider,
    p_environment: environment,
    p_label: label,
    p_credentials: credentials,
    p_permissions: permissions,
    p_account_hint: null,
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
"<form id='walletForm' autocomplete='off'><label for='walletProvider'>Wallet</label><select id='walletProvider'><option value='trust_wallet'>Trust Wallet</option><option value='phantom'>Phantom</option><option value='walletconnect'>WalletConnect-connected wallet</option><option value='manual'>Other / public address</option></select>",
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
async function handleMiniApp(body: any): Promise<Response> {
  try {
    const user = await verifyInitData(String(body?.initData ?? ""));
    const row = await userRow(user);
    if (body.action === "connect_exchange") {
      const provider = String(body.provider) as Provider;
      const environment = String(body.environment);
      if (!["okx", "binance", "bybit"].includes(provider)) throw new Error("unsupported_provider");
      const credentials: Credentials = {
        apiKey: clean(body?.credentials?.apiKey, 512),
        apiSecret: clean(body?.credentials?.apiSecret, 1024),
        passphrase: clean(body?.credentials?.passphrase, 256),
      };
      const validation = await validateExchange(provider, environment, credentials);
      const id = await saveExchange(Number(user.id), provider, environment, validation.label, credentials, validation.permissions);
      await audit(row.id, null, "exchange_connected", "ok", { provider, environment, connection_id: id, permissions: validation.permissions });
      return response({ ok: true, label: validation.label, connectionId: id });
    }
    if (body.action === "watch_wallet") {
      const provider = String(body.provider);
      const namespace = String(body.namespace);
      const address = clean(body.address, 128);
      const network = clean(body.network, 80);
      const chainId = clean(body.chainId, 40);
      if (!["trust_wallet", "phantom", "walletconnect", "manual"].includes(provider) ||
          !walletAddressValid(namespace, address) || !network || !chainId) {
        throw new Error("wallet_address_or_network_invalid");
      }
      await db("n8_wallet_connections", "POST", {
        user_id: row.id, provider, chain_namespace: namespace, chain_id: chainId,
        network, public_address: address, status: "watch_only",
      }, "resolution=merge-duplicates,return=minimal");
      await audit(row.id, null, "wallet_address_added", "ok", { provider, namespace, network });
      return response({ ok: true, address: mask(address) });
    }
    throw new Error("unsupported_action");
  } catch (error) {
    const code = error instanceof Error ? error.message : "request_failed";
    const messages: Record<string, string> = {
      telegram_auth_invalid: "Telegram identity could not be verified. Close the page and reopen it from the bot.",
      telegram_auth_expired: "This page expired. Reopen the secure form from 9N8.",
      private_chat_required: "Personal connections can only be created from a private Telegram chat.",
      backend_not_configured: "The secure backend is not configured yet. No credentials were saved.",
      okx_passphrase_required: "OKX requires its API passphrase as well as the key and secret.",
      withdraw_permission_detected: "This API key has withdrawal permission. Revoke it and create a new key with withdrawals disabled.",
      wallet_transfer_or_withdraw_scope_detected: "This Bybit key has Wallet transfer/withdrawal permissions. Create a key without Wallet permissions.",
      exchange_credentials_rejected: "The exchange rejected the credentials or the key belongs to a different environment.",
      exchange_temporarily_unavailable: "The exchange is temporarily unavailable. Try again shortly.",
      unsupported_provider_environment: "Choose the matching demo/testnet environment for this exchange.",
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
  if (!Array.isArray(rows) || rows.length === 0) return "No exchange connections yet. Use /connect to add a demo/testnet account.";
  return "YOUR EXCHANGE CONNECTIONS\n\n" + rows.map((v: any) =>
    String(v.provider).toUpperCase() + " · " + v.environment + "\n" + String(v.label ?? "Account") + " · " + v.status
  ).join("\n\n");
}
async function listWallets(userId: string): Promise<string> {
  const rows = await db("n8_wallet_connections?select=provider,network,public_address,status&user_id=eq." +
    encodeURIComponent(userId) + "&status=neq.revoked&order=created_at.desc");
  if (!Array.isArray(rows) || rows.length === 0) return "No wallet addresses linked. Use /connect to add a public watch-only address.";
  return "YOUR WATCH-ONLY ADDRESSES\n\n" + rows.map((v: any) =>
    String(v.provider).replace("_", " ") + " · " + v.network + "\n" + mask(String(v.public_address)) + " · " + v.status
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
    await audit(row.id, chatId, "private_start");
    await send(chatId,
      "9N8 UNIVERSAL ASSISTANT\n\nYour private account is ready.\nConnect OKX Demo, Binance Spot Testnet or Bybit Demo from the secure form. You can also save Trust Wallet and Phantom public addresses as watch-only links.\n\n" +
      "This candidate release is demo/testnet and read-only. No live orders, withdrawals, transfers or wallet signing.",
      mainKeyboard());
    return;
  }
  if (command === "/help") {
    await send(chatId, "9N8 COMMANDS\n/start — open private dashboard\n/connect — open secure exchange and wallet form\n/connections — list your exchange connections\n/wallets — list your watch-only addresses\n\nGROUP COMMANDS\n/n8setup, /n8admin, /n8moderator\n/n8delete and /n8ban — reply to a member message\n/n8unban TELEGRAM_USER_ID\n\nNever send API credentials in chat. Demo/testnet only. No trade execution is enabled in this candidate.");
    return;
  }
  if (command === "/connect") {
    await send(chatId, "Open this private secure form to connect an exchange or save a public wallet address. Never send API credentials in Telegram chat.", {
      inline_keyboard: [[{ text: "Open secure connections", web_app: { url: MINI_APP_URL } }]],
    });
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
  if (q.data === "n8:connections") await send(Number(chat.id), await listConnections(row.id));
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

async function mainHandler(req: Request): Promise<Response> {
  const url = new URL(req.url);
  if (req.method === "GET" && url.searchParams.get("view") === "connect") return connectHtml();
  if (req.method === "GET") return response({
    ok: true, service: "9N8 universal candidate", state: "staging", environment: "demo-testnet-only",
    config: {
      telegramTokenConfigured: Boolean(TOKEN),
      webhookSecretConfigured: Boolean(HOOK_SECRET),
      databaseAdminConfigured: Boolean(SUPABASE_SERVICE_KEY),
    },
  });
  if (req.method !== "POST") return response({ ok: false, error: "method_not_allowed" }, 405);
  let body: any;
  try { body = await req.json(); } catch { return response({ ok: false, error: "invalid_json" }, 400); }

  // Mini App traffic is authenticated with Telegram-signed initData.
  if (body?.action === "connect_exchange" || body?.action === "watch_wallet") return await handleMiniApp(body);

  if (!TOKEN || !HOOK_SECRET) return response({ ok: false, error: "telegram_secrets_not_configured" }, 503);
  const supplied = req.headers.get("x-telegram-bot-api-secret-token") ?? "";
  if (!same(supplied, HOOK_SECRET)) return new Response("Forbidden", { status: 401 });

  if (body?.update_id !== undefined) {
    const inserted = await db("n8_telegram_updates", "POST", { update_id: Number(body.update_id) },
      "resolution=ignore-duplicates,return=representation");
    if (!Array.isArray(inserted) || inserted.length === 0) return response({ ok: true, duplicate: true });
  }

  try {
    if (body?.my_chat_member) await handleMyChatMember(body);
    else if (body?.callback_query) await handleCallback(body);
    else await handleMessage(body);

    if (body?.update_id !== undefined) {
      await db("n8_telegram_updates?update_id=eq." + encodeURIComponent(String(body.update_id)),
        "PATCH", { processed_at: new Date().toISOString(), outcome: "processed" }, "return=minimal");
    }
    return response({ ok: true });
  } catch {
    // Do not log Telegram updates, credentials, tokens, or secret values.
    return response({ ok: false, error: "update_processing_failed" }, 500);
  }
}

Deno.serve(async (req: Request) => {
  try { return await mainHandler(req); }
  catch { return response({ ok: false, error: "request_failed" }, 500); }
});
