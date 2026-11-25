const TOKEN = (Deno.env.get("TELEGRAM_BOT_TOKEN") ?? "").trim();
const OWNER = (Deno.env.get("TELEGRAM_OWNER_ID") ?? "").trim();
const HOOK_SECRET = (Deno.env.get("TELEGRAM_WEBHOOK_SECRET") ?? "").trim();
const OKX_KEY = (Deno.env.get("OKX_API_KEY") ?? "").trim();
const OKX_SECRET = (Deno.env.get("OKX_API_SECRET") ?? "").trim();
const OKX_PHRASE = (Deno.env.get("OKX_API_PASSPHRASE") ?? "").trim();
const OKX_BASE = (Deno.env.get("OKX_API_BASE") ?? "https://www.okx.com").trim().replace(/\/+$/, "");

const reply = (value: Record<string, unknown>, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

function equalSecret(a: string, b: string): boolean {
  if (!a || !b || a.length !== b.length) return false;
  let result = 0;
  for (let i = 0; i < a.length; i++) result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return result === 0;
}

async function tg(method: string, data: Record<string, unknown>): Promise<any> {
  if (!TOKEN) throw new Error("Telegram bot token is missing in Supabase Secrets.");
  const r = await fetch("https://api.telegram.org/bot" + TOKEN + "/" + method, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
  const j = await r.json();
  if (!r.ok || j.ok !== true) throw new Error(String(j.description ?? "Telegram API error").slice(0, 150));
  return j.result;
}

async function send(chatId: number, text: string, markup?: Record<string, unknown>) {
  const data: Record<string, unknown> = { chat_id: chatId, text: text, disable_web_page_preview: true };
  if (markup) data.reply_markup = markup;
  await tg("sendMessage", data);
}

function ownerCheck(userId: number, chatType: string): string | null {
  if (chatType !== "private") return "Use 9N8 in a private Telegram chat.";
  if (!OWNER) return "Owner lock is not configured. Send /id, then add the returned number as TELEGRAM_OWNER_ID in Supabase Edge Function Secrets.";
  if (String(userId) !== OWNER) return "This is your private 9N8 bot. This Telegram account is not authorized.";
  return null;
}

function n(value: unknown, places = 8): string {
  const valueNumber = Number(value);
  return Number.isFinite(valueNumber) ? valueNumber.toLocaleString("en-US", { maximumFractionDigits: places }) : String(value ?? "n/a");
}

function b64(bytes: Uint8Array): string {
  let s = "";
  for (const x of bytes) s += String.fromCharCode(x);
  return btoa(s);
}

async function okx(path: string, authenticated = false): Promise<any[]> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (!OKX_BASE.startsWith("https://")) throw new Error("OKX API URL must use HTTPS.");
  if (authenticated) {
    if (!OKX_KEY || !OKX_SECRET || !OKX_PHRASE) throw new Error("Add your OKX demo API key, secret and passphrase in Supabase Edge Function Secrets.");
    const timeResponse = await fetch(OKX_BASE + "/api/v5/public/time");
    const timeJson = await timeResponse.json();
    const ms = Number(timeJson?.data?.[0]?.ts);
    if (!timeResponse.ok || !Number.isFinite(ms)) throw new Error("Could not synchronize time with OKX.");
    const timestamp = new Date(ms).toISOString();
    const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(OKX_SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const signed = new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(timestamp + "GET" + path)));
    headers["OK-ACCESS-KEY"] = OKX_KEY;
    headers["OK-ACCESS-SIGN"] = b64(signed);
    headers["OK-ACCESS-TIMESTAMP"] = timestamp;
    headers["OK-ACCESS-PASSPHRASE"] = OKX_PHRASE;
    headers["x-simulated-trading"] = "1";
  }
  const r = await fetch(OKX_BASE + path, { method: "GET", headers });
  const j = await r.json();
  if (!r.ok || String(j?.code ?? "0") !== "0") throw new Error("OKX error " + String(j?.code ?? r.status) + ": " + String(j?.msg ?? "Request rejected").slice(0, 140));
  return Array.isArray(j?.data) ? j.data : [];
}

async function action(chat: number, name: string, args: string[] = []) {
  try {
    if (name === "status") {
      if (!OKX_KEY || !OKX_SECRET || !OKX_PHRASE) {
        await send(chat, "OKX connection: NOT CONFIGURED\nAdd the three OKX demo credentials in Supabase Edge Function Secrets.\nTrading mode: DEMO only.");
        return;
      }
      const rows = await okx("/api/v5/account/config", true);
      const account = rows[0] ?? {};
      const permissions = String(account.perm ?? "not returned");
      const warning = permissions.toLowerCase().split(",").map((x: string) => x.trim()).includes("withdraw")
        ? "\nWARNING: Withdraw permission is enabled. Revoke this key and create a new one with Withdraw OFF."
        : "";
      await send(chat, "OKX PRIVATE CONNECTION\nAPI: authenticated\nEnvironment: DEMO — simulated funds\nAccount level: " + String(account.acctLv ?? "n/a") + "\nPermissions: " + permissions + warning + "\nThis hosted release is read-only; it does not place orders.");
      return;
    }
    if (name === "balance") {
      const rows = await okx("/api/v5/account/balance", true);
      const a = rows[0] ?? {};
      const details = Array.isArray(a.details) ? a.details : [];
      const currencies = details.filter((x: any) => Number(x.availBal ?? 0) > 0 || Number(x.eq ?? 0) > 0)
        .slice(0, 20).map((x: any) => String(x.ccy) + ": available " + n(x.availBal) + " | equity " + n(x.eq));
      await send(chat, "OKX DEMO BALANCE\nTotal equity: " + n(a.totalEq, 4) + "\nAdjusted equity: " + n(a.adjEq, 4) + "\n\n" + (currencies.join("\n") || "No non-zero balances.") + "\n\nSimulated funds only.");
      return;
    }
    if (name === "ticker") {
      const symbol = (args[0] ?? "BTC-USDT").toUpperCase();
      if (!/^[A-Z0-9-]{3,30}$/.test(symbol)) throw new Error("Usage: /ticker BTC-USDT");
      const rows = await okx("/api/v5/market/ticker?instId=" + encodeURIComponent(symbol));
      const x = rows[0];
      if (!x) throw new Error("No OKX spot ticker for " + symbol);
      await send(chat, "OKX SPOT — " + symbol + "\nLast: " + n(x.last) + "\nBid: " + n(x.bidPx) + " | Ask: " + n(x.askPx) + "\n24h high: " + n(x.high24h) + " | low: " + n(x.low24h) + "\n24h volume: " + n(x.vol24h));
      return;
    }
    if (name === "scan") {
      const symbol = (args[0] ?? "BTC-USDT").toUpperCase();
      const bar = args[1] ?? "15m";
      const allowed = ["1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H", "6H", "12H", "1D", "1W"];
      if (!/^[A-Z0-9-]{3,30}$/.test(symbol) || !allowed.includes(bar)) throw new Error("Usage: /scan BTC-USDT 15m");
      const path = "/api/v5/market/candles?instId=" + encodeURIComponent(symbol) + "&bar=" + encodeURIComponent(bar) + "&limit=100";
      const rows = await okx(path);
      const candles = rows.filter((x: any[]) => x.length >= 9 && String(x[8]) === "1").reverse();
      if (candles.length < 30) throw new Error("Not enough closed candles returned; retry shortly.");
      const closes = candles.map((x: any[]) => Number(x[4]));
      const ema = (v: number[], p: number) => {
        let result = v.slice(0, p).reduce((a, b) => a + b, 0) / p;
        const alpha = 2 / (p + 1);
        for (const item of v.slice(p)) result = item * alpha + result * (1 - alpha);
        return result;
      };
      const changes = closes.slice(1).map((x, i) => x - closes[i]);
      const gains = changes.map((x) => Math.max(x, 0));
      const losses = changes.map((x) => Math.max(-x, 0));
      let gain = gains.slice(0, 14).reduce((a, b) => a + b, 0) / 14;
      let loss = losses.slice(0, 14).reduce((a, b) => a + b, 0) / 14;
      for (let i = 14; i < gains.length; i++) {
        gain = (gain * 13 + gains[i]) / 14;
        loss = (loss * 13 + losses[i]) / 14;
      }
      const rsi = loss === 0 ? (gain > 0 ? 100 : 50) : 100 - 100 / (1 + gain / loss);
      const fast = ema(closes, 9);
      const slow = ema(closes, 21);
      const close = closes[closes.length - 1];
      const label = fast > slow && close > fast && rsi >= 50 && rsi <= 70 ? "BULLISH SETUP"
        : fast < slow && close < fast && rsi >= 30 && rsi <= 50 ? "BEARISH SETUP" : "NO CLEAR SETUP";
      await send(chat, "9N8 MARKET OBSERVATION\n" + symbol + " · " + bar + "\n" + label + "\nLast close: " + n(close) + "\nEMA 9: " + n(fast) + "\nEMA 21: " + n(slow) + "\nRSI 14: " + n(rsi, 2) + "\nClosed candle: " + new Date(Number(candles[candles.length - 1][0])).toISOString() + "\n\nObservation only; no order is placed.");
    }
  } catch (error) {
    await send(chat, "9N8: " + (error instanceof Error ? error.message : "Request failed."));
  }
}

Deno.serve(async (req: Request) => {
  if (req.method === "GET") return reply({ ok: true, service: "9N8 Telegram webhook", environment: "demo-only" });
  if (req.method !== "POST") return reply({ ok: false, error: "Method not allowed" }, 405);
  if (!TOKEN || !HOOK_SECRET) return reply({ ok: false, error: "Telegram secrets not configured." }, 503);
  const received = req.headers.get("x-telegram-bot-api-secret-token") ?? "";
  if (!equalSecret(received, HOOK_SECRET)) return new Response("Forbidden", { status: 401 });

  let update: any;
  try { update = await req.json(); } catch { return reply({ ok: false, error: "Invalid JSON" }, 400); }

  try {
    if (update?.callback_query) {
      const q = update.callback_query;
      await tg("answerCallbackQuery", { callback_query_id: q.id });
      const chat = q.message?.chat;
      if (!chat?.id) return reply({ ok: true });
      const denied = ownerCheck(Number(q.from?.id), String(chat.type ?? ""));
      if (denied) await send(Number(chat.id), denied);
      else if (["status", "balance"].includes(String(q.data))) await action(Number(chat.id), String(q.data));
      else if (String(q.data).startsWith("scan:")) {
        const p = String(q.data).split(":");
        await action(Number(chat.id), "scan", [p[1] ?? "BTC-USDT", p[2] ?? "15m"]);
      }
      return reply({ ok: true });
    }

    const m = update?.message ?? update?.edited_message;
    if (!m?.chat?.id) return reply({ ok: true });
    const text = String(m.text ?? "").trim();
    if (!text.startsWith("/")) return reply({ ok: true });
    const parts = text.split(/\s+/);
    const command = (parts[0].split("@")[0] ?? "").toLowerCase();
    const args = parts.slice(1);
    const chatId = Number(m.chat.id);
    const userId = Number(m.from?.id);

    if (command === "/id") {
      await send(chatId, "Your Telegram user ID: " + userId + "\nAdd this number as TELEGRAM_OWNER_ID in Supabase Edge Function Secrets. Never share API credentials in chat.");
      return reply({ ok: true });
    }

    const denied = ownerCheck(userId, String(m.chat.type ?? ""));
    if (denied) { await send(chatId, denied); return reply({ ok: true }); }

    if (command === "/start") {
      await send(chatId,
        "9N8 IS ONLINE\n\nHosted on Supabase — your cousin's laptop can be closed.\nExchange: OKX spot\nEnvironment: DEMO — simulated funds\n\nThis first hosted release is read-only. It can check your account, show balances, fetch spot prices and scan indicators. It does not place or cancel orders.",
        { inline_keyboard: [
          [{ text: "OKX connection", callback_data: "status" }, { text: "Demo balance", callback_data: "balance" }],
          [{ text: "Scan BTC-USDT", callback_data: "scan:BTC-USDT:15m" }]
        ] }
      );
    } else if (command === "/help") {
      await send(chatId, "9N8 PRIVATE OKX DEMO BOT\n/id — Telegram user ID\n/start — open menu\n/okx_status — test private OKX connection\n/balance — demo balances\n/ticker BTC-USDT — spot quote\n/scan BTC-USDT 15m — EMA(9), EMA(21), RSI(14)\n\nNo live trading, withdrawals, futures, leverage or automatic order execution.");
    } else if (command === "/okx_status") await action(chatId, "status");
    else if (command === "/balance") await action(chatId, "balance");
    else if (command === "/ticker") await action(chatId, "ticker", args);
    else if (command === "/scan") await action(chatId, "scan", args);
    else await send(chatId, "Unknown command. Send /help.");
  } catch {
    // Do not log webhook bodies, bot tokens, or exchange credentials.
    return reply({ ok: false, error: "Webhook processing failed." }, 500);
  }
  return reply({ ok: true });
});
