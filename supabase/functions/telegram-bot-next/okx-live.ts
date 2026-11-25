export type OkxRegion = "global" | "us" | "eea";

export type OkxCredentials = {
  apiKey: string;
  apiSecret: string;
  passphrase: string;
  region?: OkxRegion;
};

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

const OKX_ORIGINS: Record<OkxRegion, string> = {
  global: "https://openapi.okx.com",
  us: "https://us.okx.com",
  eea: "https://eea.okx.com",
};

// This connection adapter is deliberately read-only. No order, transfer, withdrawal,
// asset, or sub-account mutation endpoint is allowlisted here.
const READ_PATHS = new Set([
  "/api/v5/account/config",
  "/api/v5/account/balance",
]);

function base64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

export async function signOkxRequest(
  apiSecret: string,
  timestamp: string,
  method: string,
  requestPath: string,
  body = "",
): Promise<string> {
  const normalizedMethod = method.toUpperCase();
  if (normalizedMethod !== "GET" || !READ_PATHS.has(requestPath)) {
    throw new Error("okx_endpoint_not_allowed");
  }
  const prehash = timestamp + normalizedMethod + requestPath + body;
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(apiSecret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = new Uint8Array(
    await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(prehash)),
  );
  return base64(signature);
}

export async function okxSignedRead<T = any>(
  credentials: OkxCredentials,
  path: string,
  fetcher: FetchLike = fetch,
  timestamp = new Date().toISOString(),
): Promise<T> {
  const apiKey = String(credentials.apiKey ?? "").trim();
  const apiSecret = String(credentials.apiSecret ?? "").trim();
  const passphrase = String(credentials.passphrase ?? "").trim();
  const region = credentials.region ?? "global";

  if (!apiKey || !apiSecret || !passphrase) {
    throw new Error("okx_api_key_secret_and_passphrase_required");
  }
  if (!Object.prototype.hasOwnProperty.call(OKX_ORIGINS, region)) {
    throw new Error("okx_region_unsupported");
  }
  if (!READ_PATHS.has(path)) throw new Error("okx_endpoint_not_allowed");

  const signature = await signOkxRequest(apiSecret, timestamp, "GET", path);
  const response = await fetcher(OKX_ORIGINS[region] + path, {
    method: "GET",
    headers: {
      accept: "application/json",
      "OK-ACCESS-KEY": apiKey,
      "OK-ACCESS-SIGN": signature,
      "OK-ACCESS-TIMESTAMP": timestamp,
      "OK-ACCESS-PASSPHRASE": passphrase,
    },
    cache: "no-store",
  });

  const payload = await response.json().catch(() => ({})) as any;
  if (!response.ok) throw new Error("okx_http_" + response.status);
  if (String(payload?.code ?? "") !== "0") {
    const code = String(payload?.code ?? "unknown").replace(/[^0-9A-Za-z_-]/g, "").slice(0, 32);
    throw new Error("okx_api_" + code);
  }
  return payload as T;
}

export async function validateOkxSpotRead(
  credentials: OkxCredentials,
  fetcher: FetchLike = fetch,
  timestamp?: string,
) {
  const region = credentials.region ?? "global";
  const config = await okxSignedRead<any>(credentials, "/api/v5/account/config", fetcher, timestamp);
  const balance = await okxSignedRead<any>(credentials, "/api/v5/account/balance", fetcher, timestamp);

  if (!Array.isArray(config?.data) || !config.data[0] ||
      !Array.isArray(balance?.data) || !balance.data[0]) {
    throw new Error("okx_account_response_invalid");
  }

  const accountLevel = String(config.data[0].acctLv ?? "unknown");
  const details = Array.isArray(balance.data[0].details) ? balance.data[0].details : [];
  const balances = details.map((entry: any) => ({
    asset: String(entry.ccy ?? ""),
    free: Number(entry.availBal ?? 0),
    locked: Number(entry.frozenBal ?? 0),
    equity: Number(entry.eq ?? 0),
  })).filter((entry: any) =>
    entry.asset &&
    Number.isFinite(entry.free) &&
    Number.isFinite(entry.locked) &&
    Number.isFinite(entry.equity) &&
    entry.free + entry.locked + entry.equity !== 0
  ).slice(0, 150);

  return {
    label: "OKX Spot LIVE · " + region.toUpperCase(),
    accountHint: "OKX account · private read access validated",
    permissions: {
      read: true,
      spotTrade: "not_verified",
      withdraw: "not_verified",
      transfer: "not_verified",
      accountLevel,
      permissionScopeSource: "not_exposed_by_account_read_endpoints",
      ownerMustConfirmReadTradeOnlyAndWithdrawDisabled: true,
    },
    balances,
    accountEquityUsd: Number(balance.data[0].totalEq ?? 0),
    quoteBalance: balances.find((entry: any) => entry.asset === "USDT") ?? {
      asset: "USDT", free: 0, locked: 0, equity: 0,
    },
    region,
  };
}
