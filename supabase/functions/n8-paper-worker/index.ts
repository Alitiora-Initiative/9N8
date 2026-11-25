import {
  decidePaperAction,
  simulatedFillPrice,
  type PaperCandle,
} from "../_shared/paper-engine.ts";

const SUPABASE_URL = (Deno.env.get("SUPABASE_URL") ?? "").replace(/\/+$/, "");
const SERVICE_KEY = (() => {
  const legacy = (Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "").trim();
  if (legacy) return legacy;
  try {
    const parsed = JSON.parse(Deno.env.get("SUPABASE_SECRET_KEYS") ?? "{}");
    return String(parsed.default ?? parsed.service_role ?? "").trim();
  } catch { return ""; }
})();
const BINANCE_BASE = "https://api.binance.com";
const SOURCE = "binance-spot-paper";
const HEADER = "x-9n8-worker-key";

type Obj = Record<string, unknown>;
type Instrument = {
  id: string; symbol: string; base_asset: string | null; quote_asset: string | null;
  metadata: Record<string, unknown>;
};
type Session = {
  id: string; user_id: string; market_type: string; config: Record<string, unknown>;
  initial_balance_usd: number; cash_balance_usd: number; tick_count: number;
  worker_lock_token: string;
};
type Position = {
  id: string; instrument_id: string; symbol: string; quantity: number;
  average_entry_price: number; mark_price: number | null; realized_pnl_usd: number;
  unrealized_pnl_usd: number; entry_fees_usd: number; stop_loss_price: number;
  take_profit_price: number; status: string;
};

function json(data: Obj, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}
async function db(path: string, method = "GET", body?: unknown, prefer?: string): Promise<any> {
  if (!SUPABASE_URL || !SERVICE_KEY) throw new Error("paper_worker_backend_not_configured");
  const headers: Record<string, string> = {
    apikey: SERVICE_KEY, authorization: "Bearer " + SERVICE_KEY, accept: "application/json",
  };
  if (body !== undefined) headers["content-type"] = "application/json";
  if (prefer) headers.prefer = prefer;
  const result = await fetch(SUPABASE_URL + "/rest/v1/" + path, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(12_000),
  });
  const text = await result.text();
  if (!result.ok) throw new Error("paper_worker_database_" + result.status);
  if (!text) return null;
  try { return JSON.parse(text); } catch { return text; }
}
async function rpc(name: string, args: Obj): Promise<any> {
  return await db("rpc/" + name, "POST", args);
}
function n(value: unknown, fallback = 0): number {
  const result = Number(value);
  return Number.isFinite(result) ? result : fallback;
}
function iso(ms: number): string { return new Date(ms).toISOString(); }

async function fetchCandles(symbol: string): Promise<PaperCandle[]> {
  if (!/^[A-Z0-9]{3,20}USDT$/.test(symbol)) throw new Error("unsupported_market_symbol");
  const url = BINANCE_BASE + "/api/v3/klines?symbol=" + encodeURIComponent(symbol) + "&interval=1m&limit=60";
  const result = await fetch(url, { signal: AbortSignal.timeout(8_000) });
  if (!result.ok) throw new Error("market_data_http_" + result.status);
  const raw = await result.json();
  if (!Array.isArray(raw)) throw new Error("market_data_invalid_response");
  const candles = raw.map((row: any[]) => ({
    openTime: n(row[0]), open: n(row[1]), high: n(row[2]), low: n(row[3]),
    close: n(row[4]), volume: n(row[5]), closeTime: n(row[6]),
    quoteVolume: n(row[7]), trades: n(row[8]),
  })).filter((c: PaperCandle) => c.closeTime > 0 && c.closeTime < Date.now() - 500 &&
    c.close > 0 && c.open > 0 && c.high > 0 && c.low > 0);
  if (candles.length < 23) throw new Error("market_history_not_ready");
  return candles;
}
async function persistMarketData(instrument: Instrument, candles: PaperCandle[]): Promise<void> {
  const latest = candles[candles.length - 1];
  const observedAt = new Date().toISOString();
  await db("n8_market_snapshots", "POST", [{
    instrument_id: instrument.id, source: SOURCE, observed_at: observedAt,
    price: latest.close, volume_24h: null, liquidity_usd: null, change_24h_pct: null,
    metadata: { timeframe: "1m", candle_open_time: iso(latest.openTime), provider: "Binance public spot market data" },
  }], "return=minimal");
  const payload = candles.map((candle) => ({
    instrument_id: instrument.id, source: SOURCE, timeframe: "1m",
    open_time: iso(candle.openTime), close_time: iso(candle.closeTime),
    open: candle.open, high: candle.high, low: candle.low, close: candle.close,
    volume: candle.volume, quote_volume: candle.quoteVolume ?? null,
    trades_count: candle.trades ?? null, metadata: {},
  }));
  await db("n8_market_candles?on_conflict=instrument_id,source,timeframe,open_time", "POST", payload,
    "resolution=merge-duplicates,return=minimal");
}
function idempotencyKey(sessionId: string, symbol: string, side: string, candleOpenTime: number): string {
  return sessionId + ":" + symbol + ":" + side + ":" + String(candleOpenTime);
}

async function processSession(session: Session): Promise<Obj> {
  const started = Date.now();
  const token = session.worker_lock_token;
  let cash = n(session.cash_balance_usd, n(session.initial_balance_usd, 1000));
  const config = session.config ?? {};
  const maxPositionUsd = Math.min(250, Math.max(5, n(config.max_position_usd, 50)));
  const maxOpenPositions = Math.max(1, Math.min(10, Math.floor(n(config.max_open_positions, 3))));
  const feeRate = Math.min(0.02, Math.max(0, n(config.fee_rate, 0.001)));
  const slippageBps = Math.min(100, Math.max(0, n(config.slippage_bps, 5)));
  const stopLossPct = Math.min(0.20, Math.max(0.005, n(config.stop_loss_pct, 0.02)));
  const takeProfitPct = Math.min(0.50, Math.max(0.01, n(config.take_profit_pct, 0.04)));
  const [instruments, positions, states] = await Promise.all([
    db("n8_market_instruments?select=id,symbol,base_asset,quote_asset,metadata&active=eq.true&market_type=eq.crypto&order=symbol.asc&limit=30"),
    db("n8_paper_positions?select=*&session_id=eq." + encodeURIComponent(session.id) + "&status=eq.open"),
    db("n8_paper_asset_state?select=instrument_id,last_candle_open_time&session_id=eq." + encodeURIComponent(session.id)),
  ]);
  if (!Array.isArray(instruments) || instruments.length === 0) throw new Error("paper_market_universe_empty");
  const openByInstrument = new Map<string, Position>();
  for (const position of (Array.isArray(positions) ? positions : []) as Position[]) {
    openByInstrument.set(position.instrument_id, position);
  }
  const stateByInstrument = new Map<string, any>();
  for (const state of (Array.isArray(states) ? states : [])) stateByInstrument.set(state.instrument_id, state);

  const metrics = {
    symbolsScanned: 0, symbolsFailed: 0, signalsGenerated: 0,
    openedTrades: 0, closedTrades: 0, status: "ok" as string,
    details: [] as Obj[], error: null as string | null,
  };
  const prices = new Map<string, number>();
  const candleTimes = new Map<string, number>();
  for (const instrument of instruments as Instrument[]) {
    try {
      const providerSymbol = String(instrument.metadata?.provider_symbol ?? instrument.symbol).toUpperCase();
      const candles = await fetchCandles(providerSymbol);
      const latest = candles[candles.length - 1];
      prices.set(instrument.id, latest.close);
      candleTimes.set(instrument.id, latest.openTime);
      await persistMarketData(instrument, candles);
      metrics.symbolsScanned++;

      const priorState = stateByInstrument.get(instrument.id);
      const priorTime = priorState?.last_candle_open_time ? Date.parse(priorState.last_candle_open_time) : null;
      const position = openByInstrument.get(instrument.id) ?? null;
      const decision = decidePaperAction(candles, position ? {
        stopLossPrice: n(position.stop_loss_price), takeProfitPrice: n(position.take_profit_price),
      } : null, Number.isFinite(priorTime) ? priorTime : null);
      let actualAction = decision.action;
      let tradeResult: any = null;

      if (decision.action === "buy" || decision.action === "sell") {
        metrics.signalsGenerated++;
        if (decision.action === "buy" && openByInstrument.size >= maxOpenPositions) {
          actualAction = "hold";
        } else {
          const fillPrice = simulatedFillPrice(latest.close, decision.action, slippageBps);
          tradeResult = await rpc("n8_apply_paper_trade", {
            p_session_id: session.id,
            p_worker_lock_token: token,
            p_instrument_id: instrument.id,
            p_symbol: instrument.symbol,
            p_side: decision.action,
            p_fill_price: fillPrice,
            p_notional_usd: decision.action === "buy" ? maxPositionUsd : 0,
            p_fee_rate: feeRate,
            p_stop_loss_pct: stopLossPct,
            p_take_profit_pct: takeProfitPct,
            p_idempotency_key: idempotencyKey(session.id, instrument.symbol, decision.action, latest.openTime),
            p_signal_reason: decision.reason,
            p_reference_price: latest.close,
            p_candle_open_time: iso(latest.openTime),
          });
          if (tradeResult?.ok === true) {
            cash = n(tradeResult.cash_balance_usd, cash);
            if (decision.action === "buy") {
              metrics.openedTrades++;
              const created = tradeResult.position as Position | undefined;
              if (created) openByInstrument.set(instrument.id, created);
              else openByInstrument.set(instrument.id, {
                id: "", instrument_id: instrument.id, symbol: instrument.symbol,
                quantity: n(tradeResult.quantity), average_entry_price: fillPrice,
                mark_price: latest.close, realized_pnl_usd: 0, unrealized_pnl_usd: 0,
                entry_fees_usd: n(tradeResult.fee_usd), stop_loss_price: fillPrice * (1-stopLossPct),
                take_profit_price: fillPrice * (1+takeProfitPct), status: "open",
              });
            } else {
              metrics.closedTrades++;
              openByInstrument.delete(instrument.id);
            }
          } else {
            actualAction = "hold";
          }
        }
      }

      await db("n8_paper_asset_state?on_conflict=session_id,instrument_id", "POST", [{
        session_id: session.id, user_id: session.user_id, instrument_id: instrument.id,
        symbol: instrument.symbol, last_candle_open_time: iso(latest.openTime),
        last_signal: actualAction, updated_at: new Date().toISOString(),
      }], "resolution=merge-duplicates,return=minimal");

      metrics.details.push({
        symbol: instrument.symbol, price: latest.close, ema9: decision.emaFast,
        ema21: decision.emaSlow, rsi14: decision.rsi, signal: actualAction,
        reason: actualAction === "hold" ? decision.reason : decision.reason,
        trade: tradeResult?.ok === true ? tradeResult.trade_id ?? null : null,
      });
    } catch (error) {
      metrics.symbolsFailed++;
      metrics.status = "partial";
      metrics.details.push({ symbol: instrument.symbol, error: String(error).slice(0, 120) });
    }
  }

  const finalPositions = await db("n8_paper_positions?select=*&session_id=eq." +
    encodeURIComponent(session.id));
  let unrealized = 0;
  let realized = 0;
  if (Array.isArray(finalPositions)) {
    for (const position of finalPositions as Position[]) {
      realized += n(position.realized_pnl_usd);
      if (position.status === "open") {
        const mark = prices.get(position.instrument_id) ?? n(position.mark_price);
        const pnl = (mark - n(position.average_entry_price)) * n(position.quantity);
        unrealized += pnl;
        if (position.id) {
          await db("n8_paper_positions?id=eq." + encodeURIComponent(position.id), "PATCH", {
            mark_price: mark, unrealized_pnl_usd: pnl, updated_at: new Date().toISOString(),
          }, "return=minimal");
        }
      }
    }
  }
  const equity = cash + Array.from(openByInstrument.entries()).reduce((sum, [id, position]) => {
    return sum + n(position.quantity) * n(prices.get(id), n(position.mark_price));
  }, 0);
  const finish = await rpc("n8_finish_paper_tick", {
    p_session_id: session.id, p_worker_lock_token: token,
    p_metrics: {
      symbols_scanned: metrics.symbolsScanned, symbols_failed: metrics.symbolsFailed,
      signals_generated: metrics.signalsGenerated, opened_trades: metrics.openedTrades,
      closed_trades: metrics.closedTrades, cash_balance_usd: cash,
      equity_usd: equity, realized_pnl_usd: realized, unrealized_pnl_usd: unrealized,
      status: metrics.status, duration_ms: Date.now() - started,
      details: metrics.details,
    },
  });
  if (finish?.ok !== true) throw new Error("paper_tick_finish_not_confirmed");
  return {
    session_id: session.id, symbols_scanned: metrics.symbolsScanned,
    symbols_failed: metrics.symbolsFailed, signals_generated: metrics.signalsGenerated,
    opened_trades: metrics.openedTrades, closed_trades: metrics.closedTrades,
    equity_usd: equity, realized_pnl_usd: realized, unrealized_pnl_usd: unrealized,
    status: metrics.status,
  };
}

Deno.serve(async (req: Request) => {
  if (req.method === "GET") return json({
    ok: true, service: "9N8 paper-only market worker", execution: "simulated-only",
    realOrdersEnabled: false, marketData: "Binance public spot candles",
  });
  if (req.method !== "POST") return json({ ok: false, error: "method_not_allowed" }, 405);
  const secret = req.headers.get(HEADER) ?? "";
  if (!secret || secret.length < 32) return json({ ok: false, error: "unauthorized" }, 401);
  try {
    const valid = await rpc("n8_verify_paper_worker_key", { p_key: secret });
    if (valid !== true) return json({ ok: false, error: "unauthorized" }, 401);
    const claimed = await rpc("n8_claim_paper_sessions", { p_limit: 20 });
    const sessions = Array.isArray(claimed) ? claimed as Session[] : [];
    const results: Obj[] = [];
    for (const session of sessions) {
      try {
        results.push(await processSession(session));
      } catch (error) {
        try {
          await rpc("n8_finish_paper_tick", {
            p_session_id: session.id, p_worker_lock_token: session.worker_lock_token,
            p_metrics: {
              symbols_scanned: 0, symbols_failed: 0, signals_generated: 0,
              opened_trades: 0, closed_trades: 0,
              cash_balance_usd: n(session.cash_balance_usd, 1000),
              equity_usd: n(session.cash_balance_usd, 1000),
              realized_pnl_usd: 0, unrealized_pnl_usd: 0,
              status: "failed", error: String(error).slice(0, 200), details: [],
            },
          });
        } catch { /* The lock expires automatically if the database is unavailable. */ }
        results.push({ session_id: session.id, status: "failed", error: String(error).slice(0, 120) });
      }
    }
    return json({ ok: true, sessions_claimed: sessions.length, results });
  } catch {
    return json({ ok: false, error: "paper_worker_failed" }, 500);
  }
});
