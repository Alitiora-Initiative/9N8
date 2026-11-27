const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? Deno.env.get("SUPABASE_SERVICE_KEY") ?? "";
const BOT_TOKEN = Deno.env.get("TELEGRAM_BOT_TOKEN") ?? "";
const RPC_URL = Deno.env.get("SOLANA_RPC_URL") ?? "https://api.mainnet-beta.solana.com";
const encoder = new TextEncoder();
const ALLOWED_ORIGINS = new Set([
  "https://9n8-secure-connect.vercel.app",
  "https://9n8-secure-connect-eckfkaenf-jolse.vercel.app",
  "http://localhost:5173",
  ...(Deno.env.get("WALLET_VAULT_ALLOWED_ORIGINS") ?? "").split(",").map((value) => value.trim()).filter(Boolean),
]);
const CORS_HEADERS = {
  "access-control-allow-methods": "POST, OPTIONS",
  "access-control-allow-headers": "content-type",
  "access-control-max-age": "600",
  "vary": "Origin",
};

function response(body: unknown, status = 200, origin = ""): Response {
  const headers = new Headers({ "content-type": "application/json; charset=utf-8", "cache-control": "no-store, max-age=0", ...CORS_HEADERS });
  if (origin && ALLOWED_ORIGINS.has(origin)) headers.set("access-control-allow-origin", origin);
  return new Response(JSON.stringify(body), { status, headers });
}
function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "request_failed";
}
function isBase64(value: unknown, min: number, max: number): value is string {
  return typeof value === "string" && value.length >= min && value.length <= max &&
    value.length % 4 === 0 && /^[A-Za-z0-9+/]+={0,2}$/.test(value);
}
function decodeBase64(value: string): Uint8Array {
  const binary = atob(value);
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}
function equalConstantTime(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let diff = 0;
  for (let i = 0; i < left.length; i++) diff |= left.charCodeAt(i) ^ right.charCodeAt(i);
  return diff === 0;
}
async function hmac(keyBytes: Uint8Array, message: string): Promise<Uint8Array> {
  // Copy into an ArrayBuffer-backed view to satisfy Deno's WebCrypto BufferSource types.
  const keyMaterial = new Uint8Array(keyBytes.length);
  keyMaterial.set(keyBytes);
  const key = await crypto.subtle.importKey("raw", keyMaterial, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  try {
    return new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(message)));
  } finally {
    keyMaterial.fill(0);
  }
}
function hex(value: Uint8Array): string {
  return Array.from(value, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
async function verifyTelegramInitData(raw: string, maxAgeSeconds: number): Promise<any> {
  if (!BOT_TOKEN || !raw || raw.length > 12000) throw new Error("telegram_auth_invalid");
  const params = new URLSearchParams(raw);
  const suppliedHash = (params.get("hash") ?? "").toLowerCase();
  const authDate = Number(params.get("auth_date") ?? "0");
  const now = Math.floor(Date.now() / 1000);
  if (!/^[a-f0-9]{64}$/.test(suppliedHash) || !Number.isSafeInteger(authDate) ||
      authDate > now + 30 || now - authDate > maxAgeSeconds) throw new Error("telegram_auth_expired");
  const entries = Array.from(params.entries()).filter(([key]) => key !== "hash");
  entries.sort(([left], [right]) => left.localeCompare(right));
  const checkString = entries.map(([key, value]) => key + "=" + value).join("\n");
  const secret = await hmac(encoder.encode("WebAppData"), BOT_TOKEN);
  const calculated = hex(await hmac(secret, checkString));
  if (!equalConstantTime(calculated, suppliedHash)) throw new Error("telegram_auth_invalid");
  let user: any;
  try { user = JSON.parse(params.get("user") ?? "null"); } catch { throw new Error("telegram_auth_invalid"); }
  if (!Number.isSafeInteger(Number(user?.id)) || Number(user.id) <= 0) throw new Error("telegram_identity_invalid");
  const chatType = params.get("chat_type");
  if (chatType && chatType !== "private") throw new Error("private_chat_required");
  return user;
}
async function db(path: string, method = "GET", body?: unknown, prefer?: string): Promise<any> {
  if (!SUPABASE_URL || !SERVICE_KEY) throw new Error("wallet_vault_database_not_configured");
  const headers = new Headers({ apikey: SERVICE_KEY, authorization: "Bearer " + SERVICE_KEY });
  if (body !== undefined) headers.set("content-type", "application/json");
  if (prefer) headers.set("prefer", prefer);
  const result = await fetch(SUPABASE_URL.replace(/\/+$/, "") + "/rest/v1/" + path, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store", signal: AbortSignal.timeout(10000),
  });
  const text = await result.text();
  if (!result.ok) throw new Error("wallet_vault_database_error_" + result.status);
  return text ? JSON.parse(text) : null;
}
async function requireUser(telegramUser: any): Promise<any> {
  const rows = await db("n8_users?on_conflict=telegram_user_id", "POST", {
    telegram_user_id: Number(telegramUser.id),
    telegram_username: String(telegramUser.username ?? "").slice(0, 64) || null,
    first_name: String(telegramUser.first_name ?? "").slice(0, 80) || null,
  }, "resolution=merge-duplicates,return=representation");
  const row = Array.isArray(rows) ? rows[0] : null;
  if (!row?.id) throw new Error("wallet_vault_user_registration_failed");
  return row;
}
function validateEnvelope(input: any) {
  if (input?.formatVersion !== 1 || input?.cipher !== "AES-256-GCM" ||
      input?.kdf !== "PBKDF2-SHA256" || input?.iterations !== 600000 ||
      !isBase64(input?.salt, 24, 64) || !isBase64(input?.iv, 16, 32) ||
      !isBase64(input?.ciphertext, 40, 65536)) throw new Error("wallet_vault_envelope_invalid");
  if (decodeBase64(input.salt).length !== 16 || decodeBase64(input.iv).length !== 12 ||
      decodeBase64(input.ciphertext).length < 32) throw new Error("wallet_vault_envelope_invalid");
  return {
    format_version: 1, cipher: "AES-256-GCM", kdf: "PBKDF2-SHA256",
    kdf_iterations: 600000, salt_b64: input.salt, iv_b64: input.iv, ciphertext_b64: input.ciphertext,
  };
}
function validateAccounts(value: unknown, requirePrimary = true): any[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 25) throw new Error("wallet_accounts_invalid");
  const accounts = value.map((entry: any) => {
    const namespace = String(entry?.chainNamespace ?? "");
    const chainId = String(entry?.chainId ?? "");
    const address = String(entry?.address ?? "");
    const network = String(entry?.network ?? "").slice(0, 50);
    const path = String(entry?.derivationPath ?? "");
    const index = Number(entry?.walletIndex);
    const label = String(entry?.label ?? "Wallet").slice(0, 40);
    if (!Number.isInteger(index) || index < 0 || index > 1000 || !network) throw new Error("wallet_account_fields_invalid");
    if (namespace === "solana") {
      if (chainId !== "mainnet-beta" || !/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(address) ||
          !/^m\/44'\/501'\/\d+'\/0'$/.test(path)) throw new Error("solana_wallet_account_invalid");
    } else if (namespace === "eip155") {
      if (!["1", "8453", "56", "42161"].includes(chainId) ||
          !/^0x[a-fA-F0-9]{40}$/.test(address) || path !== "m/44'/60'/0'/0/0") {
        throw new Error("evm_wallet_account_invalid");
      }
    } else {
      throw new Error("wallet_namespace_unsupported");
    }
    return {
      wallet_index: index, chain_namespace: namespace, chain_id: chainId,
      network, public_address: address, derivation_path: path, label,
    };
  });
  const keys = new Set(accounts.map((row) => [row.wallet_index, row.chain_namespace, row.chain_id].join(":")));
  if (keys.size !== accounts.length) throw new Error("wallet_accounts_duplicate");
  if (requirePrimary && (!accounts.some((row) => row.chain_namespace === "solana" && row.wallet_index === 0) ||
      !accounts.some((row) => row.chain_namespace === "eip155" && row.chain_id === "8453"))) {
    throw new Error("wallet_primary_accounts_missing");
  }
  return accounts;
}
async function getVault(userId: string): Promise<any> {
  const vaults = await db("n8_wallet_vaults?select=id,format_version,cipher,kdf,kdf_iterations,salt_b64,iv_b64,ciphertext_b64,created_at,updated_at&user_id=eq." + encodeURIComponent(userId));
  const accounts = await db("n8_wallet_accounts?select=wallet_index,chain_namespace,chain_id,network,public_address,derivation_path,label,is_selected&user_id=eq." +
    encodeURIComponent(userId) + "&order=wallet_index.asc,chain_namespace.asc,chain_id.asc");
  return { vault: Array.isArray(vaults) ? (vaults[0] ?? null) : null, accounts: Array.isArray(accounts) ? accounts : [] };
}
async function handle(request: Request): Promise<Response> {
  const origin = request.headers.get("origin") ?? "";
  if (request.method === "OPTIONS") {
    if (origin && !ALLOWED_ORIGINS.has(origin)) return response({ ok: false, error: "origin_not_allowed" }, 403);
    return new Response(null, { status: 204, headers: { ...CORS_HEADERS, ...(origin ? { "access-control-allow-origin": origin } : {}) } });
  }
  if (request.method !== "POST") return response({ ok: false, error: "method_not_allowed" }, 405, origin);
  if (origin && !ALLOWED_ORIGINS.has(origin)) return response({ ok: false, error: "origin_not_allowed" }, 403, origin);
  try {
    const body = await request.json();
    const action = String(body?.action ?? "");
    const maxAge = action === "status" || action === "load" || action === "balance" ? 86400 : 300;
    const telegramUser = await verifyTelegramInitData(String(body?.initData ?? ""), maxAge);
    const owner = await requireUser(telegramUser);
    const ownerId = String(owner.id);

    if (action === "status") {
      const data = await getVault(ownerId);
      return response({ ok: true, exists: Boolean(data.vault), accounts: data.accounts }, 200, origin);
    }
    if (action === "load") {
      const data = await getVault(ownerId);
      if (!data.vault) return response({ ok: false, error: "wallet_vault_not_found" }, 404, origin);
      return response({
        ok: true,
        envelope: {
          formatVersion: data.vault.format_version, cipher: data.vault.cipher, kdf: data.vault.kdf,
          iterations: data.vault.kdf_iterations, salt: data.vault.salt_b64, iv: data.vault.iv_b64,
          ciphertext: data.vault.ciphertext_b64,
        },
        accounts: data.accounts,
      }, 200, origin);
    }
    if (action === "save") {
      const existing = await db("n8_wallet_vaults?select=id&user_id=eq." + encodeURIComponent(ownerId));
      if (Array.isArray(existing) && existing.length) return response({ ok: false, error: "wallet_vault_already_exists" }, 409, origin);
      const envelope = validateEnvelope(body?.envelope);
      const accounts = validateAccounts(body?.accounts);
      const inserted = await db("n8_wallet_vaults", "POST", { user_id: ownerId, ...envelope }, "return=representation");
      const vault = Array.isArray(inserted) ? inserted[0] : null;
      if (!vault?.id) throw new Error("wallet_vault_save_failed");
      try {
        await db("n8_wallet_accounts?on_conflict=user_id,chain_namespace,chain_id,public_address", "POST",
          accounts.map((account) => ({ user_id: ownerId, ...account, is_selected: account.chain_namespace === "solana" && account.wallet_index === 0 })),
          "resolution=ignore-duplicates,return=minimal");
      } catch (error) {
        await db("n8_wallet_vaults?user_id=eq." + encodeURIComponent(ownerId), "DELETE");
        throw error;
      }
      return response({ ok: true, message: "Encrypted wallet backup stored. The server cannot decrypt it." }, 200, origin);
    }
    if (action === "add_accounts") {
      const vaults = await db("n8_wallet_vaults?select=id&user_id=eq." + encodeURIComponent(ownerId));
      if (!Array.isArray(vaults) || !vaults[0]?.id) return response({ ok: false, error: "wallet_vault_not_found" }, 404, origin);
      const accounts = validateAccounts(body?.accounts, false);
      await db("n8_wallet_accounts?on_conflict=user_id,chain_namespace,chain_id,public_address", "POST",
        accounts.map((account) => ({ user_id: ownerId, ...account, is_selected: false })),
        "resolution=ignore-duplicates,return=minimal");
      return response({ ok: true, message: "Public wallet addresses added." }, 200, origin);
    }
    if (action === "balance") {
      const data = await getVault(ownerId);
      const solana = data.accounts.filter((account: any) => account.chain_namespace === "solana");
      if (!solana.length) return response({ ok: true, balances: [] }, 200, origin);
      const balances = await Promise.all(solana.map(async (account: any) => {
        const rpc = await fetch(RPC_URL, {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "getBalance", params: [account.public_address, { commitment: "confirmed" }] }),
          cache: "no-store", signal: AbortSignal.timeout(8000),
        });
        const payload = await rpc.json().catch(() => ({}));
        if (!rpc.ok || payload?.error || !Number.isSafeInteger(payload?.result?.value)) {
          return { address: account.public_address, ok: false, error: "rpc_unavailable" };
        }
        return { address: account.public_address, ok: true, lamports: payload.result.value, sol: payload.result.value / 1_000_000_000 };
      }));
      return response({ ok: true, balances }, 200, origin);
    }
    return response({ ok: false, error: "action_not_supported" }, 400, origin);
  } catch (error) {
    const message = errorMessage(error);
    const status = message === "telegram_auth_invalid" || message === "telegram_auth_expired" ||
      message === "private_chat_required" ? 401 : 400;
    return response({ ok: false, error: message }, status, origin);
  }
}

Deno.serve(handle);
