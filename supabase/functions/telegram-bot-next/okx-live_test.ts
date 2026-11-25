import {
  okxSignedRead,
  signOkxRequest,
  validateOkxSpotRead,
  type OkxCredentials,
} from "./okx-live.ts";

Deno.test("OKX signatures are Base64 HMAC-SHA256 and change with the signed path", async () => {
  const first = await signOkxRequest(
    "test-secret",
    "2026-11-25T18:00:00.000Z",
    "GET",
    "/api/v5/account/balance",
  );
  const second = await signOkxRequest(
    "test-secret",
    "2026-11-25T18:00:00.000Z",
    "GET",
    "/api/v5/account/config",
  );
  if (!/^[A-Za-z0-9+/]{43}=$/.test(first)) throw new Error("signature_not_base64_sha256");
  if (first === second) throw new Error("signature_did_not_bind_request_path");
});

Deno.test("OKX adapter refuses non-read and unapproved endpoints before fetch", async () => {
  const credentials: OkxCredentials = {
    apiKey: "key",
    apiSecret: "secret",
    passphrase: "pass",
    region: "global",
  };
  let called = false;
  try {
    await okxSignedRead(credentials, "/api/v5/trade/order", async () => {
      called = true;
      return new Response("{}", { status: 200 });
    });
    throw new Error("trade_endpoint_was_not_rejected");
  } catch (error) {
    if (!(error instanceof Error) || error.message !== "okx_endpoint_not_allowed") throw error;
  }
  if (called) throw new Error("network_called_for_unapproved_endpoint");
});

Deno.test("OKX adapter signs and sends the allowed balance-read request", async () => {
  const credentials: OkxCredentials = {
    apiKey: "test-api-key",
    apiSecret: "test-api-secret",
    passphrase: "test-passphrase",
    region: "global",
  };
  let requestedUrl = "";
  let requestedHeaders = new Headers();
  const timestamp = "2026-11-25T18:00:00.000Z";
  const payload = {
    code: "0",
    data: [{ totalEq: "20.5", details: [{ ccy: "USDT", availBal: "10", frozenBal: "1", eq: "11" }] }],
  };
  const result = await okxSignedRead<any>(
    credentials,
    "/api/v5/account/balance",
    async (input, init) => {
      requestedUrl = String(input);
      requestedHeaders = new Headers(init?.headers);
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
    timestamp,
  );
  if (requestedUrl !== "https://openapi.okx.com/api/v5/account/balance") {
    throw new Error("wrong_global_api_host");
  }
  if (requestedHeaders.get("OK-ACCESS-KEY") !== credentials.apiKey ||
      requestedHeaders.get("OK-ACCESS-PASSPHRASE") !== credentials.passphrase ||
      requestedHeaders.get("OK-ACCESS-TIMESTAMP") !== timestamp ||
      !requestedHeaders.get("OK-ACCESS-SIGN")) {
    throw new Error("signed_okx_headers_missing");
  }
  if (result.data[0].details[0].availBal !== "10") throw new Error("balance_response_not_returned");
});

Deno.test("OKX validation identifies read access without claiming unverified trade/withdraw scopes", async () => {
  const credentials: OkxCredentials = { apiKey: "test-api-key", apiSecret: "test-api-secret", passphrase: "test-passphrase", region: "us" };
  const urls: string[] = [];
  const fetcher = async (input: RequestInfo | URL) => {
    const url = String(input); urls.push(url);
    const data = url.endsWith("/api/v5/account/config")
      ? { code: "0", data: [{ acctLv: "2" }] }
      : { code: "0", data: [{ totalEq: "25", details: [{ ccy: "USDT", availBal: "15", frozenBal: "0", eq: "15" }] }] };
    return new Response(JSON.stringify(data), { status: 200, headers: { "content-type": "application/json" } });
  };
  const result = await validateOkxSpotRead(credentials, fetcher, "2026-11-25T18:00:00.000Z");
  if (urls.length !== 2 || !urls.every(url => url.startsWith("https://us.okx.com/"))) throw new Error("wrong_regional_api_host_or_validation_call_count");
  if (result.permissions.read !== true || result.permissions.spotTrade !== "not_verified" || result.permissions.withdraw !== "not_verified") throw new Error("okx_scope_claimed_without_evidence");
  if (result.quoteBalance.free !== 15 || result.region !== "us") throw new Error("okx_account_data_not_normalized");
});
