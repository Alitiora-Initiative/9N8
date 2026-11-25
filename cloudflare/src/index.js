const DEFAULTS = Object.freeze({
  maxLots: 0.10,
  maxPositions: 3,
  sl: 500,
  tp: 1000,
  beTrigger: 150,
  beOffset: 0,
  paperCapital: 10000,
  watchlist: ["XAU/USD", "EUR/USD", "GBP/USD", "USD/JPY", "XAG/USD"],
  timeframe: "15min",
  reportDay: "MON",
  reportTime: "09:00",
  reportTimezone: "Africa/Nairobi"
});

const DAY_NAMES = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];
const META_API_DEFAULT = "https://mt-client-api-v1.new-york.agiliumtrade.ai";
const META_MARKET_DEFAULT = "https://mt-market-data-client-api-v1.new-york.agiliumtrade.ai";
const TWELVE_DATA_BASE = "https://api.twelvedata.com";

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" }
  });
}

function text(body, status = 200) {
  return new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8" } });
}

function isoNow() {
  return new Date().toISOString();
}

function parseCommand(input) {
  const match = String(input || "").trim().match(/^\/([a-z0-9_]+)(?:@[a-z0-9_]+)?(?:\s+([\s\S]*))?$/i);
  if (!match) return null;
  return { name: match[1].toLowerCase(), args: (match[2] || "").trim().split(/\s+/).filter(Boolean) };
}

function ema(values, period) {
  if (!Array.isArray(values) || period < 1 || values.length < period) {
    throw new Error("Not enough candles to calculate EMA(" + period + ").");
  }
  const alpha = 2 / (period + 1);
  let result = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
  for (const value of values.slice(period)) result = value * alpha + result * (1 - alpha);
  return result;
}

function rsi(values, period = 14) {
  if (!Array.isArray(values) || period < 2 || values.length <= period) {
    throw new Error("Not enough candles to calculate RSI(" + period + ").");
  }
  const changes = [];
  for (let i = 1; i < values.length; i++) changes.push(values[i] - values[i - 1]);
  let gain = 0;
  let loss = 0;
  for (let i = 0; i < period; i++) {
    gain += Math.max(changes[i], 0);
    loss += Math.max(-changes[i], 0);
  }
  let avgGain = gain / period;
  let avgLoss = loss / period;
  for (let i = period; i < changes.length; i++) {
    avgGain = (avgGain * (period - 1) + Math.max(changes[i], 0)) / period;
    avgLoss = (avgLoss * (period - 1) + Math.max(-changes[i], 0)) / period;
  }
  if (avgLoss === 0) return avgGain > 0 ? 100 : 50;
  const relativeStrength = avgGain / avgLoss;
  return 100 - 100 / (1 + relativeStrength);
}

function analyseCandles(symbol, candles) {
  const closes = candles.map((item) => Number(item.close));
  if (closes.length < 30 || closes.some((value) => !Number.isFinite(value))) {
    throw new Error("At least 30 valid candles are required for analysis.");
  }
  const fast = ema(closes, 9);
  const slow = ema(closes, 21);
  const momentum = rsi(closes, 14);
  const latest = candles[candles.length - 1];
  let label = "NO CLEAR SETUP";
  let explanation = "The configured trend and RSI conditions are not aligned.";
  if (fast > slow && latest.close > fast && momentum >= 50 && momentum <= 70) {
    label = "BULLISH SETUP";
    explanation = "EMA(9) is above EMA(21), price is above EMA(9), and RSI is between 50 and 70.";
  } else if (fast < slow && latest.close < fast && momentum >= 30 && momentum <= 50) {
    label = "BEARISH SETUP";
    explanation = "EMA(9) is below EMA(21), price is below EMA(9), and RSI is between 30 and 50.";
  }
  return {
    symbol,
    label,
    close: Number(latest.close),
    ema_fast: fast,
    ema_slow: slow,
    rsi: momentum,
    candle_time: String(latest.datetime || latest.time || ""),
    explanation
  };
}

function formatNumber(value, digits = 5) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString("en-US", { maximumFractionDigits: digits }) : "n/a";
}

async function telegram(env, method, payload = {}) {
  if (!env.TELEGRAM_BOT_TOKEN) throw new Error("TELEGRAM_BOT_TOKEN is not configured.");
  const response = await fetch("https://api.telegram.org/bot" + env.TELEGRAM_BOT_TOKEN + "/" + method, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(12000)
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("Telegram returned an unreadable response.");
  }
  if (!response.ok || !data.ok) {
    const description = String(data.description || "Telegram API request failed.");
    throw new Error("Telegram API: " + description.slice(0, 240));
  }
  return data.result;
}

async function sendMessage(env, chatId, message) {
  return telegram(env, "sendMessage", {
    chat_id: chatId,
    text: String(message).slice(0, 3900),
    disable_web_page_preview: true
  });
}

async function getUser(db, telegramId) {
  return db.prepare("SELECT * FROM users WHERE telegram_id = ?").bind(String(telegramId)).first();
}

async function getState(db, key, fallback = "") {
  const row = await db.prepare("SELECT value FROM app_state WHERE key = ?").bind(key).first();
  return row ? String(row.value) : fallback;
}

async function setState(db, key, value) {
  await db.prepare(
    "INSERT INTO app_state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
  ).bind(key, String(value)).run();
}

function isPrivate(message) {
  return Boolean(message.chat && message.chat.type === "private");
}

function isOwner(env, userId) {
  return Boolean(env.TELEGRAM_OWNER_ID && env.TELEGRAM_OWNER_ID !== "0" &&
    String(userId) === String(env.TELEGRAM_OWNER_ID));
}

async function requireAccount(env, message, user) {
  if (!isPrivate(message)) {
    await sendMessage(env, message.chat.id, "For account privacy, please use this command in your private chat with 9N8.");
    return null;
  }
  const account = await getUser(env.DB, user.id);
  if (!account) {
    await sendMessage(env, message.chat.id, "Welcome. Send /start first to create your private paper account.");
    return null;
  }
  return account;
}

async function requireOwner(env, message, user) {
  if (!isPrivate(message)) {
    await sendMessage(env, message.chat.id, "Run administrator commands in your private chat with 9N8.");
    return false;
  }
  if (!isOwner(env, user.id)) {
    await sendMessage(env, message.chat.id, "This command is restricted to the 9N8 platform administrator.");
    return false;
  }
  return true;
}

async function metaRequest(env, base, path) {
  if (!env.METAAPI_TOKEN || !env.METAAPI_ACCOUNT_ID) {
    throw new Error("MT5 DEMO data is not configured. Add METAAPI_TOKEN and METAAPI_ACCOUNT_ID in Cloudflare Worker secrets.");
  }
  const root = (base || META_API_DEFAULT).replace(/\/+$/, "");
  const url = root + "/users/current/accounts/" + encodeURIComponent(env.METAAPI_ACCOUNT_ID) + "/" + path.replace(/^\/+/, "");
  const response = await fetch(url, {
    headers: { "auth-token": env.METAAPI_TOKEN, "accept": "application/json" },
    signal: AbortSignal.timeout(12000)
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("MetaApi returned an unreadable response.");
  }
  if (!response.ok || (data && data.error)) {
    if (response.status === 401) throw new Error("MetaApi rejected the API token. Check METAAPI_TOKEN.");
    if (response.status === 404) throw new Error("MetaApi account or requested symbol was not found.");
    if (response.status === 429) throw new Error("MetaApi rate limit reached. Try again later.");
    throw new Error("MetaApi request failed with HTTP " + response.status + ".");
  }
  return data;
}

async function verifyDemoAccount(env) {
  const base = env.METAAPI_API_BASE || META_API_DEFAULT;
  const account = await metaRequest(env, base, "account-information?refreshTerminalState=true");
  if (String(account.type || "").toUpperCase() !== "ACCOUNT_TRADE_MODE_DEMO") {
    throw new Error("9N8 refuses MT5 accounts that are live or not positively identified as DEMO.");
  }
  return account;
}

async function metaSymbolInfo(env, symbol) {
  const base = env.METAAPI_API_BASE || META_API_DEFAULT;
  const data = await metaRequest(env, base, "symbols/" + encodeURIComponent(symbol) + "/specification");
  const info = {
    name: String(data.symbol || symbol),
    digits: Number(data.digits || 5),
    point: Number(data.tickSize || 0),
    contractSize: Number(data.contractSize || 0),
    volumeMin: Number(data.minVolume || 0),
    volumeMax: Number(data.maxVolume || 0),
    volumeStep: Number(data.volumeStep || 0),
    stopsLevel: Number(data.stopsLevel || 0),
    currency: String(data.profitCurrency || "UNKNOWN")
  };
  if (!(info.point > 0 && info.contractSize > 0 && info.volumeMin > 0 &&
        info.volumeMax >= info.volumeMin && info.volumeStep > 0)) {
    throw new Error("MetaApi returned incomplete contract specifications for " + symbol + ".");
  }
  return info;
}

async function metaTick(env, symbol) {
  const base = env.METAAPI_API_BASE || META_API_DEFAULT;
  const data = await metaRequest(env, base, "symbols/" + encodeURIComponent(symbol) + "/current-price");
  const bid = Number(data.bid);
  const ask = Number(data.ask);
  const stamp = Date.parse(data.time || "");
  if (!(bid > 0 && ask >= bid && Number.isFinite(stamp))) {
    throw new Error("MetaApi returned an invalid quote for " + symbol + ".");
  }
  const ageSeconds = (Date.now() - stamp) / 1000;
  const maxAge = Number(env.PAPER_MAX_TICK_AGE_SECONDS || 900);
  if (ageSeconds < -60 || ageSeconds > maxAge) {
    throw new Error("The MT5 quote for " + symbol + " is stale (" + Math.round(ageSeconds) + " seconds old).");
  }
  return { bid, ask, time: new Date(stamp).toISOString() };
}

async function twelveDataRequest(env, path, params) {
  const key = env.TWELVEDATA_API_KEY || env.TWELVE_DATA_API_KEY;
  if (!key) throw new Error("Twelve Data is not configured. Add TWELVEDATA_API_KEY in Cloudflare Worker secrets.");
  const url = new URL(TWELVE_DATA_BASE + path);
  for (const [name, value] of Object.entries({ ...params, apikey: key, format: "JSON" })) {
    url.searchParams.set(name, String(value));
  }
  const response = await fetch(url, { signal: AbortSignal.timeout(12000) });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("Twelve Data returned an unreadable response.");
  }
  if (!response.ok || data.status === "error" || data.code) {
    throw new Error("Twelve Data: " + String(data.message || ("HTTP " + response.status)).slice(0, 220));
  }
  return data;
}

async function twelveDataCandles(env, symbol) {
  const data = await twelveDataRequest(env, "/time_series", {
    symbol,
    interval: env.TIMEFRAME || DEFAULTS.timeframe,
    outputsize: Number(env.OUTPUT_SIZE || 80)
  });
  if (!Array.isArray(data.values) || data.values.length < 30) {
    throw new Error("Twelve Data returned fewer than 30 candles for " + symbol + ".");
  }
  return data.values.slice().reverse().map((row) => ({
    datetime: String(row.datetime || ""),
    open: Number(row.open),
    high: Number(row.high),
    low: Number(row.low),
    close: Number(row.close),
    volume: row.volume == null || row.volume === "" ? null : Number(row.volume)
  }));
}

async function saveSignal(env, signal, notify = true) {
  const previous = await env.DB.prepare("SELECT label FROM signals WHERE symbol = ?").bind(signal.symbol).first();
  await env.DB.prepare(
    "INSERT INTO signals(symbol,label,close,ema_fast,ema_slow,rsi,candle_time,explanation,updated_at) " +
    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET label=excluded.label,close=excluded.close," +
    "ema_fast=excluded.ema_fast,ema_slow=excluded.ema_slow,rsi=excluded.rsi,candle_time=excluded.candle_time," +
    "explanation=excluded.explanation,updated_at=excluded.updated_at"
  ).bind(signal.symbol, signal.label, signal.close, signal.ema_fast, signal.ema_slow,
    signal.rsi, signal.candle_time, signal.explanation, isoNow()).run();

  if (notify && previous && previous.label !== signal.label) {
    const users = await env.DB.prepare("SELECT chat_id FROM users WHERE notifications = 1").all();
    const note = "9N8 SIGNAL UPDATE\n" + signal.symbol + "\n" + signal.label +
      "\nClose: " + formatNumber(signal.close) + "\nEMA 9: " + formatNumber(signal.ema_fast) +
      "\nEMA 21: " + formatNumber(signal.ema_slow) + "\nRSI 14: " + formatNumber(signal.rsi, 2) +
      "\n" + signal.explanation + "\nObservation only; not a trade instruction.";
    for (const user of users.results || []) {
      try { await sendMessage(env, user.chat_id, note); } catch (error) { console.log("Signal DM failed:", error.message); }
    }
  }
  return signal;
}

async function runTwelveDataScan(env, symbols) {
  const results = [];
  for (const symbol of symbols.slice(0, 5)) {
    try {
      const candles = await twelveDataCandles(env, symbol);
      const signal = analyseCandles(symbol, candles);
      results.push(await saveSignal(env, signal, true));
    } catch (error) {
      results.push({ symbol, error: error.message });
    }
  }
  return results;
}

function signalText(signal) {
  if (signal.error) return signal.symbol + ": " + signal.error;
  return signal.symbol + " — " + signal.label + "\nClose: " + formatNumber(signal.close) +
    " | EMA9: " + formatNumber(signal.ema_fast) + " | EMA21: " + formatNumber(signal.ema_slow) +
    " | RSI: " + formatNumber(signal.rsi, 2) + "\n" + signal.explanation;
}

async function openPaperPosition(env, account, userId, args) {
  if (await getState(env.DB, "global_emergency_pause", "0") === "1") {
    throw new Error("The platform administrator has paused all new paper entries.");
  }
  if (!Number(account.paper_enabled)) throw new Error("Your paper entries are paused. Use /paper_enable first.");
  if (args.length < 3 || args.length > 5) {
    throw new Error("Usage: /paper_open XAUUSD BUY 0.01 [SL_POINTS TP_POINTS]");
  }
  const symbol = args[0].toUpperCase();
  const side = args[1].toUpperCase();
  const lots = Number(args[2]);
  const sl = args[3] == null ? Number(account.default_sl_points) : Number(args[3]);
  const tp = args[4] == null ? Number(account.default_tp_points) : Number(args[4]);
  if (!/^[A-Z0-9._-]{3,30}$/.test(symbol)) throw new Error("The symbol format is invalid.");
  if (side !== "BUY" && side !== "SELL") throw new Error("Side must be BUY or SELL.");
  if (!Number.isFinite(lots) || lots <= 0 || lots > Math.min(DEFAULTS.maxLots, Number(account.max_lots))) {
    throw new Error("Lot size must be positive and no greater than your 9N8 cap of " +
      Math.min(DEFAULTS.maxLots, Number(account.max_lots)).toFixed(2) + ".");
  }
  if (!Number.isInteger(sl) || !Number.isInteger(tp) || sl < 1 || tp < 1 || sl > 1000000 || tp > 1000000) {
    throw new Error("Stop-loss and take-profit distances must be whole numbers from 1 to 1,000,000 points.");
  }
  const count = await env.DB.prepare(
    "SELECT COUNT(*) AS n FROM paper_positions WHERE owner_id = ? AND status = 'OPEN'"
  ).bind(String(userId)).first();
  if (Number(count.n) >= Math.min(DEFAULTS.maxPositions, Number(account.max_open_positions))) {
    throw new Error("Your open-position limit has been reached.");
  }

  await verifyDemoAccount(env);
  const info = await metaSymbolInfo(env, symbol);
  if (lots < info.volumeMin - 1e-9 || lots > info.volumeMax + 1e-9 ||
      Math.abs(lots / info.volumeStep - Math.round(lots / info.volumeStep)) > 1e-6) {
    throw new Error("Lot size must follow the broker limits for " + symbol + ": " +
      info.volumeMin + " to " + Math.min(info.volumeMax, DEFAULTS.maxLots) +
      " in steps of " + info.volumeStep + ".");
  }
  if (sl < Math.max(1, info.stopsLevel) || tp < Math.max(1, info.stopsLevel)) {
    throw new Error("SL and TP must each be at least " + Math.max(1, info.stopsLevel) + " points for " + symbol + ".");
  }
  const tick = await metaTick(env, symbol);
  const slippagePoints = Number(env.PAPER_SLIPPAGE_POINTS || 2);
  const entry = side === "BUY" ? tick.ask + slippagePoints * info.point : tick.bid - slippagePoints * info.point;
  const stop = side === "BUY" ? entry - sl * info.point : entry + sl * info.point;
  const target = side === "BUY" ? entry + tp * info.point : entry - tp * info.point;
  const result = await env.DB.prepare(
    "INSERT INTO paper_positions(symbol,side,lots,entry_price,stop_loss,take_profit,opened_at,status," +
    "profit_currency,contract_size,point,owner_id,be_trigger_points,be_offset_points) " +
    "SELECT ?,?,?,?,?,?,?,'OPEN',?,?,?,?,?,? " +
    "WHERE (SELECT COUNT(*) FROM paper_positions WHERE owner_id = ? AND status = 'OPEN') < ? RETURNING ticket"
  ).bind(symbol, side, lots, entry, stop, target, isoNow(), info.currency, info.contractSize,
    info.point, String(userId), Number(account.be_trigger_points), Number(account.be_offset_points),
    String(userId), Math.min(DEFAULTS.maxPositions, Number(account.max_open_positions))).first();
  if (!result) throw new Error("The position limit was reached while this request was being processed. Try again.");
  return { ticket: result.ticket, symbol, side, lots, entry, stop, target, currency: info.currency };
}

async function closePaperPosition(env, ticket, ownerId, reason = "MANUAL", knownTick = null, knownPosition = null) {
  const position = knownPosition || await env.DB.prepare(
    "SELECT * FROM paper_positions WHERE ticket = ? AND owner_id = ? AND status = 'OPEN'"
  ).bind(Number(ticket), String(ownerId)).first();
  if (!position) throw new Error("Open paper ticket " + ticket + " was not found in your account.");
  const tick = knownTick || await metaTick(env, position.symbol);
  const slippage = Number(env.PAPER_SLIPPAGE_POINTS || 2) * Number(position.point);
  const closePrice = position.side === "BUY" ? tick.bid - slippage : tick.ask + slippage;
  const direction = position.side === "BUY" ? 1 : -1;
  const pnl = (closePrice - Number(position.entry_price)) * direction *
    Number(position.lots) * Number(position.contract_size);
  await env.DB.prepare(
    "UPDATE paper_positions SET status='CLOSED',close_price=?,closed_at=?,pnl_quote=?,close_reason=? " +
    "WHERE ticket=? AND owner_id=? AND status='OPEN'"
  ).bind(closePrice, isoNow(), pnl, reason, Number(ticket), String(ownerId)).run();
  return {
    ticket: Number(ticket), symbol: position.symbol, side: position.side, lots: Number(position.lots),
    entry: Number(position.entry_price), closePrice, pnl, currency: position.profit_currency, reason
  };
}

async function monitorPositions(env, ownerId = null) {
  if (!env.METAAPI_TOKEN || !env.METAAPI_ACCOUNT_ID) return { checked: 0, closed: 0 };
  const query = ownerId == null
    ? env.DB.prepare("SELECT * FROM paper_positions WHERE status='OPEN' ORDER BY ticket LIMIT 30")
    : env.DB.prepare("SELECT * FROM paper_positions WHERE owner_id=? AND status='OPEN' ORDER BY ticket LIMIT 30").bind(String(ownerId));
  const rows = await query.all();
  const positions = rows.results || [];
  if (!positions.length) return { checked: 0, closed: 0 };
  await verifyDemoAccount(env);
  const ticks = new Map();
  let closed = 0;
  for (const position of positions) {
    try {
      let tick = ticks.get(position.symbol);
      if (!tick) {
        tick = await metaTick(env, position.symbol);
        ticks.set(position.symbol, tick);
      }
      const side = position.side;
      const point = Number(position.point);
      let stop = Number(position.stop_loss);
      let activated = Number(position.be_activated);
      const trigger = Number(position.be_trigger_points);
      const offset = Number(position.be_offset_points);
      const favorable = side === "BUY"
        ? (tick.bid - Number(position.entry_price)) / point
        : (Number(position.entry_price) - tick.ask) / point;

      if (!activated && trigger > 0 && favorable >= trigger) {
        const newStop = side === "BUY"
          ? Number(position.entry_price) + offset * point
          : Number(position.entry_price) - offset * point;
        const improves = side === "BUY" ? newStop > stop : newStop < stop;
        if (improves) {
          stop = newStop;
          activated = 1;
          await env.DB.prepare(
            "UPDATE paper_positions SET stop_loss=?,be_activated=1 WHERE ticket=? AND status='OPEN'"
          ).bind(stop, Number(position.ticket)).run();
        }
      }

      const hitStop = side === "BUY" ? tick.bid <= stop : tick.ask >= stop;
      const hitTarget = side === "BUY"
        ? tick.bid >= Number(position.take_profit)
        : tick.ask <= Number(position.take_profit);
      if (hitStop || hitTarget) {
        let reason = hitTarget ? "TAKE_PROFIT" : "STOP_LOSS";
        if (hitStop && activated) {
          reason = offset === 0 ? "BREAK_EVEN" : "TRAILING_STOP";
        }
        const result = await closePaperPosition(env, position.ticket, position.owner_id, reason, tick, position);
        closed++;
        const user = await getUser(env.DB, position.owner_id);
        if (user && Number(user.notifications)) {
          const pnlText = (result.pnl >= 0 ? "+" : "") + formatNumber(result.pnl, 2) + " " + result.currency;
          try {
            await sendMessage(env, user.chat_id,
              "9N8 PAPER POSITION CLOSED\nTicket: " + result.ticket + "\n" + result.symbol + " " + result.side +
              "\nReason: " + result.reason + "\nClose: " + formatNumber(result.closePrice) +
              "\nSimulated P&L: " + pnlText);
          } catch (error) { console.log("Close notification failed:", error.message); }
        }
      }
    } catch (error) {
      console.log("Position monitor error for ticket " + position.ticket + ":", error.message);
    }
  }
  return { checked: positions.length, closed };
}

function previousCompleteWeek() {
  const now = new Date();
  const currentMonday = new Date(Date.UTC(
    now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - ((now.getUTCDay() + 6) % 7), 0, 0, 0
  ));
  const previousMonday = new Date(currentMonday.getTime() - 7 * 86400000);
  return { start: previousMonday.toISOString(), end: currentMonday.toISOString() };
}

async function weeklyReportText(env, start, end) {
  const rows = await env.DB.prepare(
    "SELECT p.pnl_quote,p.profit_currency,p.be_activated,p.owner_id " +
    "FROM paper_positions p INNER JOIN users u ON u.telegram_id=p.owner_id " +
    "WHERE p.status='CLOSED' AND p.closed_at>=? AND p.closed_at<? AND u.share_stats=1"
  ).bind(start, end).all();
  const items = rows.results || [];
  let wins = 0;
  let losses = 0;
  let breakevens = 0;
  let beMoves = 0;
  const byCurrency = new Map();
  const contributors = new Set();
  for (const item of items) {
    const pnl = Number(item.pnl_quote || 0);
    if (pnl > 1e-8) wins++;
    else if (pnl < -1e-8) losses++;
    else breakevens++;
    if (Number(item.be_activated)) beMoves++;
    contributors.add(String(item.owner_id));
    const key = item.profit_currency || "UNKNOWN";
    const bucket = byCurrency.get(key) || { pnl: 0, trades: 0 };
    bucket.pnl += pnl;
    bucket.trades++;
    byCurrency.set(key, bucket);
  }
  const rate = items.length ? (wins / items.length * 100).toFixed(1) : "0.0";
  const currencyLines = Array.from(byCurrency.entries()).map(([currency, value]) =>
    currency + ": " + (value.pnl >= 0 ? "+" : "") + formatNumber(value.pnl, 2) +
    " (" + value.trades + " trades)"
  );
  return "9N8 — WEEKLY PAPER PERFORMANCE\nPrevious complete week: " +
    start.slice(0, 10) + " to " + new Date(Date.parse(end) - 1).toISOString().slice(0, 10) +
    "\n\nOpted-in contributors: " + contributors.size +
    "\nClosed simulated trades: " + items.length +
    "\nWins: " + wins + " | Losses: " + losses + " | Break-even/flat: " + breakevens +
    "\nWin rate: " + rate + "%\nBreak-even stop activations: " + beMoves +
    "\n\nP&L by profit currency:\n" + (currencyLines.length ? currencyLines.join("\n") : "No opted-in closed trades this week.") +
    "\n\nAnonymous aggregate only. Paper-trading estimates, not audited returns or live account results.";
}

async function sendWeeklyReports(env) {
  const period = previousCompleteWeek();
  const report = await weeklyReportText(env, period.start, period.end);
  const chats = await env.DB.prepare("SELECT chat_id FROM report_chats ORDER BY chat_title").all();
  for (const chat of chats.results || []) {
    try { await sendMessage(env, chat.chat_id, report); }
    catch (error) { console.log("Weekly group report failed:", error.message); }
  }
  return { destinations: (chats.results || []).length, period };
}

async function scheduledWeeklyCheck(env, scheduledDate = new Date()) {
  const day = await getState(env.DB, "report_day", env.REPORT_DAY || DEFAULTS.reportDay);
  const time = await getState(env.DB, "report_time", env.REPORT_TIME || DEFAULTS.reportTime);
  const zone = await getState(env.DB, "report_timezone", env.REPORT_TIMEZONE || DEFAULTS.reportTimezone);
  let parts;
  try {
    parts = new Intl.DateTimeFormat("en-US", {
      timeZone: zone, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23"
    }).formatToParts(scheduledDate);
  } catch {
    console.log("Invalid report timezone configured:", zone);
    return;
  }
  const part = (type) => parts.find((p) => p.type === type)?.value || "";
  if (part("weekday").toUpperCase() !== day || (part("hour") + ":" + part("minute")) !== time) return;
  const localDate = new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" }).format(scheduledDate);
  if (await getState(env.DB, "last_weekly_report_date", "") === localDate) return;
  await setState(env.DB, "last_weekly_report_date", localDate);
  await sendWeeklyReports(env);
}

async function cronSignalScan(env) {
  const key = env.TWELVEDATA_API_KEY || env.TWELVE_DATA_API_KEY;
  if (!key) return;
  const last = Number(await getState(env.DB, "last_signal_scan_ms", "0"));
  if (Date.now() - last < 15 * 60 * 1000) return;
  await setState(env.DB, "last_signal_scan_ms", String(Date.now()));
  const symbols = String(env.WATCHLIST || DEFAULTS.watchlist.join(","))
    .split(",").map((s) => s.trim().toUpperCase()).filter(Boolean).slice(0, 5);
  const result = await runTwelveDataScan(env, symbols);
  console.log("Scheduled Twelve Data scan:", result.length, "symbols");
}

async function handleStart(env, message, user) {
  if (!isPrivate(message)) {
    await sendMessage(env, message.chat.id, "Open a private chat with 9N8 and send /start to create your own paper account.");
    return;
  }
  const now = isoNow();
  await env.DB.prepare(
    "INSERT INTO users(telegram_id,chat_id,username,first_name,created_at,last_seen,notifications,share_stats,paper_enabled," +
    "max_lots,max_open_positions,default_sl_points,default_tp_points,be_trigger_points,be_offset_points) " +
    "VALUES(?,?,?,?,?,?,1,0,0,?,?,?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET " +
    "chat_id=excluded.chat_id,username=excluded.username,first_name=excluded.first_name,last_seen=excluded.last_seen"
  ).bind(String(user.id), String(message.chat.id), user.username || null, user.first_name || null, now, now,
    DEFAULTS.maxLots, DEFAULTS.maxPositions, DEFAULTS.sl, DEFAULTS.tp, DEFAULTS.beTrigger, DEFAULTS.beOffset).run();
  await sendMessage(env, message.chat.id,
    "Welcome to 9N8" + (user.first_name ? ", " + user.first_name : "") + ".\n\n" +
    "Your private paper account is ready. Your positions and settings are isolated from other users.\n" +
    "Paper entries are PAUSED by default. Start with /settings, then /paper_enable when you are ready.\n\n" +
    "9N8 uses market data only. All entries, exits, stop-loss, take-profit and break-even are simulated. " +
    "No live broker orders, withdrawals or transfers are implemented.\n\nSend /help for commands.");
}

async function handleUserCommand(env, message, user, command) {
  const name = command.name;
  const args = command.args;
  if (name === "id") {
    await sendMessage(env, message.chat.id, "Your Telegram user ID: " + user.id +
      "\nUse this numeric value for TELEGRAM_OWNER_ID. Never share your bot token.");
    return;
  }
  if (name === "start") return handleStart(env, message, user);
  if (name === "help") {
    await sendMessage(env, message.chat.id,
      "9N8 COMMANDS\n/settings — your settings\n/setlots 0.05 — per-trade lot cap\n" +
      "/setmaxpositions 3 — simultaneous position cap\n/setsl 500 and /settp 1000 — default SL/TP\n" +
      "/setbe 150 0 or /setbe off — break-even\n/paper_enable or /paper_pause\n" +
      "/paper_open XAUUSD BUY 0.01 [SL_POINTS TP_POINTS]\n" +
      "/paper_positions, /paper_close TICKET, /paper_check, /paper_history, /paper_balance, /stats\n" +
      "/mt5_status, /mt5_symbols, /mt5_price SYMBOL, /mt5_scan SYMBOL\n" +
      "/price SYMBOL, /scan [SYMBOL], /signals\n" +
      "/notifications on|off and /share_stats on|off\n/id\n\n" +
      "All positions are simulated. No live MT5 orders are sent.");
    return;
  }
  if (name === "help") {
    await sendMessage(env, message.chat.id,
      "9N8 COMMANDS\n" +
      "/settings — your settings\n/setlots 0.05 — per-trade lot cap\n/setmaxpositions 3 — simultaneous position cap\n" +
      "/setsl 500 — default stop distance\n/settp 1000 — default target distance\n/setbe 150 0 or /setbe off — break-even trigger and offset\n" +
      "/paper_enable or /paper_pause — allow/pause paper entries\n" +
      "/paper_open XAUUSD BUY 0.01 [SL_POINTS TP_POINTS] — simulated entry\n" +
      "/paper_positions, /paper_close TICKET, /paper_check, /paper_history, /paper_balance, /stats\n" +
      "/mt5_status, /mt5_symbols [FILTER], /mt5_price SYMBOL, /mt5_scan SYMBOL — read-only MT5 DEMO data\n" +
      "/price SYMBOL, /scan [SYMBOL], /signals — Twelve Data prices and EMA/RSI observations\n" +
      "/notifications on|off — private trade and signal messages\n/share_stats on|off — anonymous group-statistics consent\n/id — your Telegram ID\n\nPaper results are estimates, not audited or guaranteed performance.");
    return;
  }

  if (name === "settings" || name === "status") {
    const account = await requireAccount(env, message, user);
    if (!account) return;
    const be = Number(account.be_trigger_points) === 0
      ? "OFF"
      : "after " + account.be_trigger_points + " points, offset " + account.be_offset_points;
    const paused = await getState(env.DB, "global_emergency_pause", "0") === "1";
    await sendMessage(env, message.chat.id,
      "YOUR 9N8 PAPER SETTINGS\nPaper entries: " + (Number(account.paper_enabled) ? "ENABLED" : "PAUSED") +
      "\nPlatform emergency pause: " + (paused ? "ON" : "OFF") +
      "\nMax lots per trade: " + account.max_lots +
      "\nMax open positions: " + account.max_open_positions +
      "\nDefault stop: " + account.default_sl_points + " points" +
      "\nDefault target: " + account.default_tp_points + " points" +
      "\nBreak-even: " + be +
      "\nNotifications: " + (Number(account.notifications) ? "ON" : "OFF") +
      "\nAnonymous group statistics: " + (Number(account.share_stats) ? "OPTED IN" : "OPTED OUT") +
      "\nPlatform hard caps: 0.10 lots and 3 open positions.");
    return;
  }

  if (name === "setlots" || name === "setmaxpositions" || name === "setsl" || name === "settp" || name === "setbe" ||
      name === "notifications" || name === "share_stats" || name === "paper_enable" || name === "paper_pause" ||
      name.startsWith("paper_") || name === "stats" || name === "mt5_status" || name === "mt5_symbols" ||
      name === "mt5_price" || name === "mt5_scan" || name === "price" || name === "scan" || name === "signals") {
    const account = await requireAccount(env, message, user);
    if (!account) return;

    if (name === "setlots") {
      const value = Number(args[0]);
      if (args.length !== 1 || !Number.isFinite(value) || value < 0.01 || value > DEFAULTS.maxLots) {
        throw new Error("Usage: /setlots 0.05 (allowed range 0.01–0.10 lots).");
      }
      await env.DB.prepare("UPDATE users SET max_lots=? WHERE telegram_id=?").bind(value, String(user.id)).run();
      await sendMessage(env, message.chat.id, "Your per-trade paper lot cap is now " + value + ".");
      return;
    }
    if (name === "setmaxpositions") {
      const value = Number(args[0]);
      if (args.length !== 1 || !Number.isInteger(value) || value < 1 || value > DEFAULTS.maxPositions) {
        throw new Error("Usage: /setmaxpositions 2 (allowed range 1–3).");
      }
      await env.DB.prepare("UPDATE users SET max_open_positions=? WHERE telegram_id=?").bind(value, String(user.id)).run();
      await sendMessage(env, message.chat.id, "Your simultaneous paper-position cap is now " + value + ".");
      return;
    }
    if (name === "setsl" || name === "settp") {
      const value = Number(args[0]);
      if (args.length !== 1 || !Number.isInteger(value) || value < 1 || value > 1000000) {
        throw new Error("Usage: /" + name + " 500 (whole points from 1 to 1,000,000).");
      }
      const column = name === "setsl" ? "default_sl_points" : "default_tp_points";
      await env.DB.prepare("UPDATE users SET " + column + "=? WHERE telegram_id=?").bind(value, String(user.id)).run();
      await sendMessage(env, message.chat.id, "Your default " + (name === "setsl" ? "stop-loss" : "take-profit") +
        " distance is now " + value + " points.");
      return;
    }
    if (name === "setbe") {
      if (args.length === 1 && args[0].toLowerCase() === "off") {
        await env.DB.prepare("UPDATE users SET be_trigger_points=0,be_offset_points=0 WHERE telegram_id=?").bind(String(user.id)).run();
        await sendMessage(env, message.chat.id, "Break-even is disabled for new paper positions. Existing positions keep their saved settings.");
        return;
      }
      const trigger = Number(args[0]);
      const offset = args[1] == null ? 0 : Number(args[1]);
      if (args.length < 1 || args.length > 2 || !Number.isInteger(trigger) || !Number.isInteger(offset) ||
          trigger < 1 || trigger > 1000000 || offset < 0 || offset >= trigger) {
        throw new Error("Usage: /setbe 150 0 or /setbe off. Offset must be smaller than the trigger.");
      }
      await env.DB.prepare("UPDATE users SET be_trigger_points=?,be_offset_points=? WHERE telegram_id=?")
        .bind(trigger, offset, String(user.id)).run();
      await sendMessage(env, message.chat.id, "Break-even will move the simulated stop after " + trigger +
        " favorable points, with an offset of " + offset + " points.");
      return;
    }
    if (name === "notifications" || name === "share_stats") {
      const value = args[0] === "on" ? 1 : args[0] === "off" ? 0 : -1;
      if (args.length !== 1 || value < 0) throw new Error("Usage: /" + name + " on|off.");
      const column = name === "notifications" ? "notifications" : "share_stats";
      await env.DB.prepare("UPDATE users SET " + column + "=? WHERE telegram_id=?").bind(value, String(user.id)).run();
      await sendMessage(env, message.chat.id, name === "notifications"
        ? "Your private 9N8 notifications are now " + (value ? "ON." : "OFF.")
        : (value ? "You opted in to anonymous aggregate weekly group reports." :
          "You opted out. Your trades will not be included in future group reports."));
      return;
    }
    if (name === "paper_enable" || name === "paper_pause") {
      const value = name === "paper_enable" ? 1 : 0;
      await env.DB.prepare("UPDATE users SET paper_enabled=? WHERE telegram_id=?").bind(value, String(user.id)).run();
      await sendMessage(env, message.chat.id, value
        ? "Paper entries enabled. This only enables simulated entries; no live broker orders are sent."
        : "New paper entries paused. Existing paper positions continue to be monitored.");
      return;
    }
    if (name === "paper_open") {
      const opened = await openPaperPosition(env, account, user.id, args);
      await sendMessage(env, message.chat.id,
        "9N8 PAPER POSITION OPENED\nTicket: " + opened.ticket + "\n" + opened.symbol + " " + opened.side +
        "\nLots: " + opened.lots + "\nSimulated entry: " + formatNumber(opened.entry) +
        "\nStop-loss: " + formatNumber(opened.stop) + "\nTake-profit: " + formatNumber(opened.target) +
        "\nQuote currency: " + opened.currency + "\nNo MT5 order was placed.");
      return;
    }
    if (name === "paper_positions") {
      const rows = await env.DB.prepare(
        "SELECT ticket,symbol,side,lots,entry_price,stop_loss,take_profit,be_activated FROM paper_positions " +
        "WHERE owner_id=? AND status='OPEN' ORDER BY ticket"
      ).bind(String(user.id)).all();
      const items = rows.results || [];
      await sendMessage(env, message.chat.id, items.length
        ? "YOUR OPEN PAPER POSITIONS\n" + items.map((p) =>
          "#" + p.ticket + " " + p.symbol + " " + p.side + " " + p.lots +
          "\nEntry " + formatNumber(p.entry_price) + " | SL " + formatNumber(p.stop_loss) +
          " | TP " + formatNumber(p.take_profit) + " | BE " + (Number(p.be_activated) ? "moved" : "not moved")
        ).join("\n\n")
        : "You have no open paper positions.");
      return;
    }
    if (name === "paper_close") {
      const ticket = Number(args[0]);
      if (args.length !== 1 || !Number.isInteger(ticket) || ticket <= 0) {
        throw new Error("Usage: /paper_close TICKET.");
      }
      const result = await closePaperPosition(env, ticket, user.id);
      await sendMessage(env, message.chat.id,
        "9N8 PAPER POSITION CLOSED\nTicket: " + result.ticket + "\n" + result.symbol + " " + result.side +
        "\nExit: " + formatNumber(result.closePrice) + "\nReason: MANUAL\nSimulated P&L: " +
        (result.pnl >= 0 ? "+" : "") + formatNumber(result.pnl, 2) + " " + result.currency);
      return;
    }
    if (name === "paper_check") {
      const result = await monitorPositions(env, user.id);
      await sendMessage(env, message.chat.id,
        "Paper monitor checked " + result.checked + " position(s) and closed " + result.closed +
        ". Monitoring requires configured MT5 DEMO market data.");
      return;
    }
    if (name === "paper_history") {
      const rows = await env.DB.prepare(
        "SELECT ticket,symbol,side,lots,entry_price,close_price,pnl_quote,profit_currency,status,close_reason " +
        "FROM paper_positions WHERE owner_id=? ORDER BY ticket DESC LIMIT 10"
      ).bind(String(user.id)).all();
      const items = rows.results || [];
      await sendMessage(env, message.chat.id, items.length
        ? "LAST 10 PAPER TRADES\n" + items.map((p) =>
          "#" + p.ticket + " " + p.symbol + " " + p.side + " " + p.status +
          (p.status === "CLOSED" ? "\nP&L " + formatNumber(p.pnl_quote, 2) + " " + p.profit_currency + " (" + p.close_reason + ")" :
            "\nEntry " + formatNumber(p.entry_price))
        ).join("\n\n")
        : "No paper trades yet.");
      return;
    }
    if (name === "paper_balance") {
      const summary = await env.DB.prepare(
        "SELECT profit_currency,SUM(CASE WHEN status='CLOSED' THEN pnl_quote ELSE 0 END) AS pnl," +
        "SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) AS closed," +
        "SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS opened " +
        "FROM paper_positions WHERE owner_id=? GROUP BY profit_currency"
      ).bind(String(user.id)).all();
      const rows = summary.results || [];
      await sendMessage(env, message.chat.id,
        "PAPER ACCOUNT SUMMARY\nReference capital: " + formatNumber(Number(env.PAPER_INITIAL_BALANCE || DEFAULTS.paperCapital), 2) +
        " " + (env.PAPER_CURRENCY || "USD") + " (simulation reference only)\n" +
        (rows.length ? rows.map((r) => r.profit_currency + ": realized P&L " +
          (Number(r.pnl) >= 0 ? "+" : "") + formatNumber(r.pnl, 2) +
          " | open " + r.opened + " | closed " + r.closed).join("\n") : "No paper trading activity yet.") +
        "\nP&L is stored in each instrument's quote currency and is not converted.");
      return;
    }
    if (name === "stats") {
      const start = new Date(Date.now() - 7 * 86400000).toISOString();
      const stats = await env.DB.prepare(
        "SELECT COUNT(*) AS trades," +
        "SUM(CASE WHEN pnl_quote>0.00000001 THEN 1 ELSE 0 END) AS wins," +
        "SUM(CASE WHEN pnl_quote< -0.00000001 THEN 1 ELSE 0 END) AS losses," +
        "SUM(CASE WHEN be_activated=1 THEN 1 ELSE 0 END) AS be_moves " +
        "FROM paper_positions WHERE owner_id=? AND status='CLOSED' AND closed_at>=?"
      ).bind(String(user.id), start).first();
      const trades = Number(stats.trades || 0);
      const wins = Number(stats.wins || 0);
      await sendMessage(env, message.chat.id,
        "YOUR LAST 7 DAYS — PAPER STATISTICS\nClosed trades: " + trades +
        "\nWins: " + wins + " | Losses: " + Number(stats.losses || 0) +
        "\nBreak-even activations: " + Number(stats.be_moves || 0) +
        "\nWin rate: " + (trades ? (wins / trades * 100).toFixed(1) : "0.0") + "%\nP&L remains in the position's quote currency.");
      return;
    }
    if (name === "mt5_status") {
      const info = await verifyDemoAccount(env);
      await sendMessage(env, message.chat.id,
        "METAAPI MT5 CONNECTION\nStatus: connected\nAccount type: DEMO\nLogin: " + (info.login || "unavailable") +
        "\nServer: " + (info.server || "unavailable") + "\nCurrency: " + (info.currency || "unknown") +
        "\nBalance: " + formatNumber(info.balance, 2) + "\nEquity: " + formatNumber(info.equity, 2) +
        "\n9N8 uses this connection for market data only.");
      return;
    }
    if (name === "mt5_symbols") {
      await verifyDemoAccount(env);
      const data = await metaRequest(env, env.METAAPI_API_BASE || META_API_DEFAULT, "symbols");
      if (!Array.isArray(data)) throw new Error("MetaApi returned an invalid symbol list.");
      const filter = (args[0] || "").toUpperCase();
      const symbols = data.filter((s) => typeof s === "string" && (!filter || s.toUpperCase().includes(filter))).slice(0, 60);
      await sendMessage(env, message.chat.id, symbols.length ? "MT5 DEMO SYMBOLS\n" + symbols.join(", ") : "No matching MT5 symbols found.");
      return;
    }
    if (name === "mt5_price") {
      const symbol = args[0];
      if (!symbol || args.length !== 1) throw new Error("Usage: /mt5_price XAUUSD.");
      await verifyDemoAccount(env);
      const tick = await metaTick(env, symbol.toUpperCase());
      await sendMessage(env, message.chat.id, symbol.toUpperCase() + " MT5 DEMO QUOTE\nBid: " +
        formatNumber(tick.bid) + "\nAsk: " + formatNumber(tick.ask) + "\nTime: " + tick.time);
      return;
    }
    if (name === "mt5_scan") {
      const symbol = args[0];
      if (!symbol || args.length !== 1) throw new Error("Usage: /mt5_scan XAUUSD.");
      await verifyDemoAccount(env);
      const tf = "15m";
      const base = env.METAAPI_MARKET_DATA_BASE || META_MARKET_DEFAULT;
      const data = await metaRequest(env, base, "historical-market-data/symbols/" +
        encodeURIComponent(symbol.toUpperCase()) + "/timeframes/" + tf + "/candles?limit=80");
      if (!Array.isArray(data) || data.length < 31) throw new Error("MetaApi returned insufficient MT5 candle history.");
      const candles = data.slice().map((item) => ({
        datetime: String(item.time || ""), close: Number(item.close)
      })).filter((item) => Number.isFinite(item.close)).sort((a, b) => Date.parse(a.datetime) - Date.parse(b.datetime));
      const signal = analyseCandles(symbol.toUpperCase(), candles.slice(0, -1));
      await sendMessage(env, message.chat.id, signalText(signal));
      return;
    }
    if (name === "price") {
      const symbol = args[0];
      if (!symbol || args.length !== 1) throw new Error("Usage: /price EUR/USD.");
      const data = await twelveDataRequest(env, "/quote", { symbol });
      await sendMessage(env, message.chat.id,
        String(data.symbol || symbol).toUpperCase() + " — TWELVE DATA\nPrice: " +
        formatNumber(data.close || data.price) + "\nChange: " + (data.percent_change || "n/a") +
        "\nProvider timestamp: " + (data.datetime || "unavailable"));
      return;
    }
    if (name === "scan") {
      const symbols = args.length ? [args[0].toUpperCase()] :
        String(env.WATCHLIST || DEFAULTS.watchlist.join(",")).split(",").map((s) => s.trim().toUpperCase()).filter(Boolean).slice(0, 5);
      const results = await runTwelveDataScan(env, symbols);
      await sendMessage(env, message.chat.id, "9N8 MARKET OBSERVATIONS\n\n" +
        results.map(signalText).join("\n\n") +
        "\n\nRules-based observations only; not trade instructions.");
      return;
    }
    if (name === "signals") {
      const rows = await env.DB.prepare("SELECT * FROM signals ORDER BY updated_at DESC LIMIT 10").all();
      const items = rows.results || [];
      await sendMessage(env, message.chat.id, items.length
        ? "LATEST SAVED SIGNAL OBSERVATIONS\n\n" + items.map((s) => signalText(s)).join("\n\n")
        : "No signal observations saved yet. Configure Twelve Data and run /scan.");
      return;
    }
  }

  await sendMessage(env, message.chat.id, "Unknown command. Use /help to see available 9N8 commands.");
}

async function adminCommand(env, message, user, command) {
  const name = command.name;
  const args = command.args;
  const adminCommands = ["users", "pause_all", "resume_all", "report_addchat", "report_removechat",
    "report_chats", "reportschedule", "report_now", "announce"];
  if (!adminCommands.includes(name)) return false;
  if (!await requireOwner(env, message, user)) return true;

  if (name === "users") {
    const result = await env.DB.prepare("SELECT COUNT(*) AS n FROM users").first();
    await sendMessage(env, message.chat.id, "Registered 9N8 users: " + Number(result.n || 0));
    return true;
  }
  if (name === "pause_all" || name === "resume_all") {
    await setState(env.DB, "global_emergency_pause", name === "pause_all" ? "1" : "0");
    await sendMessage(env, message.chat.id, name === "pause_all"
      ? "Emergency pause enabled. New paper entries are blocked for everyone; existing paper positions continue to be monitored."
      : "Emergency pause cleared. Each user's personal paper-entry setting still applies.");
    return true;
  }
  if (name === "report_addchat") {
    if (args.length !== 1) throw new Error("Usage: /report_addchat CHAT_ID_OR_@USERNAME.");
    const chat = await telegram(env, "getChat", { chat_id: args[0] });
    const me = await telegram(env, "getMe", {});
    const member = await telegram(env, "getChatMember", { chat_id: chat.id, user_id: me.id });
    if (!["administrator", "creator"].includes(member.status)) {
      throw new Error("Promote 9N8 to administrator in that group or channel before adding it.");
    }
    if (chat.type === "channel" && member.status === "administrator" &&
        member.can_post_messages === false) {
      throw new Error("9N8 needs the channel permission named Post Messages.");
    }
    const title = chat.title || chat.username || String(chat.id);
    await env.DB.prepare(
      "INSERT INTO report_chats(chat_id,chat_title,chat_type,added_by,added_at) VALUES(?,?,?,?,?) " +
      "ON CONFLICT(chat_id) DO UPDATE SET chat_title=excluded.chat_title,chat_type=excluded.chat_type," +
      "added_by=excluded.added_by,added_at=excluded.added_at"
    ).bind(String(chat.id), title, String(chat.type), String(user.id), isoNow()).run();
    await sendMessage(env, message.chat.id, "Verified and added " + title + " for anonymous weekly reports.");
    return true;
  }
  if (name === "report_removechat") {
    if (args.length !== 1) throw new Error("Usage: /report_removechat CHAT_ID.");
    const result = await env.DB.prepare("DELETE FROM report_chats WHERE chat_id=?").bind(String(args[0])).run();
    await sendMessage(env, message.chat.id, Number(result.meta?.changes || 0)
      ? "Report destination removed." : "That report destination was not registered.");
    return true;
  }
  if (name === "report_chats") {
    const result = await env.DB.prepare("SELECT chat_id,chat_title,chat_type FROM report_chats ORDER BY chat_title").all();
    const rows = result.results || [];
    await sendMessage(env, message.chat.id, rows.length
      ? "WEEKLY REPORT DESTINATIONS\n" + rows.map((r) => r.chat_title + " (" + r.chat_type + "): " + r.chat_id).join("\n")
      : "No weekly report destinations configured.");
    return true;
  }
  if (name === "reportschedule") {
    if (args.length < 2 || args.length > 3) {
      throw new Error("Usage: /reportschedule MON 09:00 [Africa/Nairobi].");
    }
    const day = args[0].toUpperCase();
    const time = args[1];
    const zone = args[2] || "Africa/Nairobi";
    if (!DAY_NAMES.includes(day) || !/^\d{2}:\d{2}$/.test(time)) {
      throw new Error("Use a weekday such as MON and time HH:MM.");
    }
    const [hour, minute] = time.split(":").map(Number);
    if (hour > 23 || minute > 59) throw new Error("The report time must be a valid 24-hour HH:MM value.");
    try { new Intl.DateTimeFormat("en-US", { timeZone: zone }).format(new Date()); }
    catch { throw new Error("Unknown timezone. Use an IANA name such as Africa/Nairobi or UTC."); }
    await setState(env.DB, "report_day", day);
    await setState(env.DB, "report_time", time);
    await setState(env.DB, "report_timezone", zone);
    await sendMessage(env, message.chat.id, "Weekly reports scheduled for " + day + " at " + time + " " + zone + ".");
    return true;
  }
  if (name === "report_now") {
    const result = await sendWeeklyReports(env);
    await sendMessage(env, message.chat.id, "Weekly report sent to " + result.destinations + " configured destination(s).");
    return true;
  }
  if (name === "announce") {
    const announcement = args.join(" ").trim();
    if (!announcement) throw new Error("Usage: /announce Your update text.");
    const result = await env.DB.prepare("SELECT chat_id FROM users WHERE notifications=1").all();
    let sent = 0;
    for (const row of result.results || []) {
      try { await sendMessage(env, row.chat_id, "9N8 ANNOUNCEMENT\n\n" + announcement); sent++; }
      catch (error) { console.log("Announcement delivery failed:", error.message); }
    }
    await sendMessage(env, message.chat.id, "Announcement delivered to " + sent + " registered user chat(s).");
    return true;
  }
  return true;
}

async function handleUpdate(env, update) {
  const message = update.message || update.edited_message;
  if (!message || !message.text || !message.from || !message.chat) return;
  const command = parseCommand(message.text);
  if (!command) return;
  const user = message.from;

  if (command.name === "start") {
    await handleStart(env, message, user);
    return;
  }
  if (command.name === "id") {
    await sendMessage(env, message.chat.id, "Your Telegram user ID: " + user.id +
      "\nUse this numeric value for TELEGRAM_OWNER_ID. Never share your bot token.");
    return;
  }
  if (command.name === "help") {
    await handleUserCommand(env, message, user, command);
    return;
  }
  if (await adminCommand(env, message, user, command)) return;
  await handleUserCommand(env, message, user, command);
}

async function handleWebhook(request, env) {
  if (!env.TELEGRAM_WEBHOOK_SECRET) return text("Webhook secret is not configured.", 503);
  const supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
  if (supplied !== env.TELEGRAM_WEBHOOK_SECRET) return text("Forbidden", 403);
  let update;
  try {
    update = await request.json();
  } catch {
    return text("Invalid JSON", 400);
  }
  if (!env.DB) return text("D1 binding is unavailable.", 503);
  if (Number.isInteger(update.update_id)) {
    const result = await env.DB.prepare(
      "INSERT OR IGNORE INTO processed_updates(update_id,processed_at) VALUES(?,?)"
    ).bind(update.update_id, isoNow()).run();
    if (Number(result.meta?.changes || 0) === 0) return text("OK");
  }
  try {
    await handleUpdate(env, update);
  } catch (error) {
    console.log("Telegram update handling failed:", error.message);
    const message = update.message;
    if (message?.chat?.id) {
      try { await sendMessage(env, message.chat.id, "9N8 could not complete that command: " + error.message); }
      catch (sendError) { console.log("Error reply failed:", sendError.message); }
    }
  }
  return text("OK");
}

async function registerWebhook(request, env) {
  const secret = env.TELEGRAM_WEBHOOK_SECRET;
  if (!secret || request.headers.get("Authorization") !== "Bearer " + secret) {
    return text("Forbidden", 403);
  }
  const url = new URL(request.url);
  url.pathname = "/webhook";
  url.search = "";
  const result = await telegram(env, "setWebhook", {
    url: url.toString(),
    secret_token: secret,
    allowed_updates: ["message", "edited_message"],
    drop_pending_updates: false,
    max_connections: 10
  });
  return json({ ok: true, webhook_url: url.toString(), telegram_result: result });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/health")) {
      if (!env.DB) return json({ status: "misconfigured", missing: ["D1 binding"] }, 503);
      try {
        await env.DB.prepare("SELECT 1 AS ready").first();
        return json({
          service: "9N8",
          status: "online",
          mode: "paper-only",
          database: "connected",
          telegram_configured: Boolean(env.TELEGRAM_BOT_TOKEN),
          market_data_configured: Boolean(env.TWELVEDATA_API_KEY || env.TWELVE_DATA_API_KEY),
          mt5_demo_data_configured: Boolean(env.METAAPI_TOKEN && env.METAAPI_ACCOUNT_ID)
        });
      } catch {
        return json({ service: "9N8", status: "database-not-ready" }, 503);
      }
    }
    if (request.method === "POST" && url.pathname === "/setup") {
      try { return await registerWebhook(request, env); }
      catch (error) { return json({ ok: false, error: error.message }, 500); }
    }
    if (request.method === "POST" && url.pathname === "/webhook") {
      try { return await handleWebhook(request, env); }
      catch (error) {
        console.log("Webhook request failed:", error.message);
        return text("Webhook processing error", 500);
      }
    }
    return text("Not found", 404);
  },

  async scheduled(controller, env) {
    if (!env.DB) return;
    try {
      const result = await monitorPositions(env);
      if (result.checked || result.closed) console.log("Paper monitor:", JSON.stringify(result));
    } catch (error) {
      console.log("Paper monitor failed:", error.message);
    }
    try {
      await cronSignalScan(env);
    } catch (error) {
      console.log("Scheduled signal scan failed:", error.message);
    }
    try {
      await scheduledWeeklyCheck(env, new Date(controller.scheduledTime || Date.now()));
    } catch (error) {
      console.log("Weekly report scheduler failed:", error.message);
    }
  }
};

export { ema, rsi, analyseCandles, parseCommand, previousCompleteWeek };
