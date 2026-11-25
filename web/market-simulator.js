import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createSeriesMarkers,
} from "lightweight-charts";

const API = "https://qlfcazdfxexcnzdgpqqm.supabase.co/functions/v1/telegram-bot-next";
const BINANCE_REST = "https://data-api.binance.vision";
const BINANCE_WS = "wss://data-stream.binance.vision/stream?streams=";
const $ = (id) => document.getElementById(id);
const tg = window.Telegram?.WebApp;
const panel = $("simulatorPanel");
const symbolSelect = $("marketSymbol");
const timeframeSelect = $("marketTimeframe");
const chartHost = $("marketChart");

if (panel && chartHost && symbolSelect && timeframeSelect) {
  const state = {
    active: false,
    started: false,
    busy: false,
    symbol: symbolSelect.value || "BTCUSDT",
    timeframe: timeframeSelect.value || "1m",
    chart: null,
    candles: null,
    volume: null,
    ema9: null,
    ema21: null,
    markers: null,
    rows: [],
    paper: null,
    book: null,
    ticker: null,
    socket: null,
    reconnectTimer: null,
    pollTimer: null,
    resizeObserver: null,
    destroyed: false,
    lastMarketMessage: 0,
  };

  const text = (id, value) => {
    const node = $(id);
    if (node) node.textContent = value;
  };
  const money = (value, digits = 2) => Number.isFinite(Number(value))
    ? new Intl.NumberFormat("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(value))
    : "—";
  const compact = (value, digits = 5) => Number.isFinite(Number(value))
    ? new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(Number(value))
    : "—";
  const usd = (value) => Number.isFinite(Number(value))
    ? new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(Number(value))
    : "—";
  const pct = (value) => Number.isFinite(Number(value)) ? Number(value).toFixed(2) + "%" : "—";
  const timeLabel = (value) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  };
  const cleanText = (value) => String(value ?? "").replace(/[&<>"]/g, "");
  const initData = () => String(tg?.initData ?? "");

  async function apiPost(body) {
    if (!initData()) throw new Error("Open the simulator from your 9N8 Telegram private chat.");
    const response = await fetch(API, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ...body, initData: initData() }),
      cache: "no-store",
      credentials: "omit",
      signal: AbortSignal.timeout(12000),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok !== true) {
      throw new Error(payload.message || payload.error || ("9N8 API HTTP " + response.status));
    }
    return payload;
  }

  async function binanceJson(path) {
    const response = await fetch(BINANCE_REST + path, {
      cache: "no-store",
      credentials: "omit",
      signal: AbortSignal.timeout(10000),
      headers: { accept: "application/json" },
    });
    if (!response.ok) throw new Error("Binance market data HTTP " + response.status);
    return await response.json();
  }

  function ensureChart() {
    if (state.chart) return;
    const width = Math.max(280, chartHost.clientWidth || 640);
    state.chart = createChart(chartHost, {
      width,
      height: 390,
      layout: {
        background: { color: "#0b1524" },
        textColor: "#9db0c5",
        fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
        attributionLogo: true,
      },
      grid: {
        vertLines: { color: "#1b2b40" },
        horzLines: { color: "#1b2b40" },
      },
      rightPriceScale: { borderColor: "#2a3c54" },
      timeScale: { borderColor: "#2a3c54", timeVisible: true, secondsVisible: false },
      crosshair: { vertLine: { color: "#56718c" }, horzLine: { color: "#56718c" } },
      handleScroll: true,
      handleScale: true,
    });
    state.candles = state.chart.addSeries(CandlestickSeries, {
      upColor: "#00e676",
      downColor: "#ff1744",
      borderUpColor: "#00e676",
      borderDownColor: "#ff1744",
      wickUpColor: "#00e676",
      wickDownColor: "#ff1744",
      priceLineVisible: true,
      lastValueVisible: true,
    });
    state.volume = state.chart.addSeries(HistogramSeries, {
      priceScaleId: "volume",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    state.chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
      visible: false,
    });
    state.ema9 = state.chart.addSeries(LineSeries, {
      color: "#f4c95d",
      lineWidth: 1,
      lastValueVisible: false,
      priceLineVisible: false,
      title: "EMA 9",
    });
    state.ema21 = state.chart.addSeries(LineSeries, {
      color: "#7ba8ff",
      lineWidth: 1,
      lastValueVisible: false,
      priceLineVisible: false,
      title: "EMA 21",
    });
    state.markers = createSeriesMarkers(state.candles, []);
    state.resizeObserver = new ResizeObserver(() => {
      if (state.chart && chartHost.clientWidth > 0) {
        state.chart.applyOptions({ width: chartHost.clientWidth });
      }
    });
    state.resizeObserver.observe(chartHost);
  }

  function ema(values, period) {
    if (values.length < period) return [];
    const result = [];
    const multiplier = 2 / (period + 1);
    let current = values.slice(0, period).reduce((sum, value) => sum + value, 0) / period;
    result.push({ value: current, index: period - 1 });
    for (let i = period; i < values.length; i++) {
      current = (values[i] - current) * multiplier + current;
      result.push({ value: current, index: i });
    }
    return result;
  }

  function rsi(values, period = 14) {
    if (values.length <= period) return null;
    let gain = 0, loss = 0;
    for (let i = 1; i <= period; i++) {
      const diff = values[i] - values[i - 1];
      gain += Math.max(diff, 0);
      loss += Math.max(-diff, 0);
    }
    let avgGain = gain / period, avgLoss = loss / period;
    for (let i = period + 1; i < values.length; i++) {
      const diff = values[i] - values[i - 1];
      avgGain = (avgGain * (period - 1) + Math.max(diff, 0)) / period;
      avgLoss = (avgLoss * (period - 1) + Math.max(-diff, 0)) / period;
    }
    if (avgLoss === 0) return avgGain === 0 ? 50 : 100;
    return 100 - 100 / (1 + avgGain / avgLoss);
  }

  function updateIndicators() {
    if (!state.rows.length || !state.ema9 || !state.ema21) return;
    const closes = state.rows.map(row => row.close);
    const e9 = ema(closes, 9).map(item => ({ time: state.rows[item.index].time, value: item.value }));
    const e21 = ema(closes, 21).map(item => ({ time: state.rows[item.index].time, value: item.value }));
    state.ema9.setData(e9);
    state.ema21.setData(e21);
    const latest9 = e9.at(-1)?.value;
    const latest21 = e21.at(-1)?.value;
    const latestRsi = rsi(closes);
    text("ema9Value", latest9 == null ? "—" : compact(latest9, 8));
    text("ema21Value", latest21 == null ? "—" : compact(latest21, 8));
    text("rsi14Value", latestRsi == null ? "—" : latestRsi.toFixed(2));
  }

  function renderBook(book) {
    if (!book?.bids?.length || !book?.asks?.length) return;
    const asks = book.asks.slice(0, 8).reverse();
    const bids = book.bids.slice(0, 8);
    const row = (item, side) => '<div class="book-row ' + side + '"><span>@ ' +
      compact(item.price, 8) + '</span><span>' + compact(item.quantity, 5) + '</span></div>';
    $("bookAsks").innerHTML = asks.map(item => row(item, "ask")).join("");
    $("bookBids").innerHTML = bids.map(item => row(item, "bid")).join("");
    text("bookUpdated", "Depth snapshot · " + timeLabel(Date.now()));
  }

  function renderTicker(ticker, bid, ask) {
    const last = Number(ticker?.lastPrice);
    const spread = bid > 0 && ask > 0 ? (ask - bid) / ((ask + bid) / 2) * 100 : null;
    text("marketPrice", "Last @ " + (last > 0 ? compact(last, 8) : "—"));
    text("marketBid", "Bid @ " + (bid > 0 ? compact(bid, 8) : "—"));
    text("marketAsk", "Ask @ " + (ask > 0 ? compact(ask, 8) : "—"));
    text("marketSpread", "Spread " + (spread == null ? "—" : pct(spread)));
    text("marketChange", "24h " + (ticker?.change == null ? "—" : pct(ticker.change)));
  }

  async function loadInitialMarket() {
    ensureChart();
    const symbol = state.symbol;
    const interval = state.timeframe;
    text("marketFeedStatus", "● Loading Binance public data…");
    const [rawKlines, rawBook, rawTicker] = await Promise.all([
      binanceJson("/api/v3/klines?symbol=" + encodeURIComponent(symbol) + "&interval=" +
        encodeURIComponent(interval) + "&limit=400"),
      binanceJson("/api/v3/depth?symbol=" + encodeURIComponent(symbol) + "&limit=20"),
      binanceJson("/api/v3/ticker/24hr?symbol=" + encodeURIComponent(symbol)),
    ]);
    if (!Array.isArray(rawKlines) || !rawKlines.length) throw new Error("Binance returned no candles for " + symbol);
    state.rows = rawKlines.map(row => ({
      time: Math.floor(Number(row[0]) / 1000),
      open: Number(row[1]), high: Number(row[2]), low: Number(row[3]), close: Number(row[4]),
      volume: Number(row[5]), closeTime: Number(row[6]),
    })).filter(row => Number.isFinite(row.time) && row.close > 0);
    state.candles.setData(state.rows.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
    state.volume.setData(state.rows.map(row => ({
      time: row.time, value: row.volume,
      color: row.close >= row.open ? "rgba(0,230,118,.35)" : "rgba(255,23,68,.35)",
    })));
    renderBook({
      bids: rawBook.bids.map(level => ({ price: Number(level[0]), quantity: Number(level[1]) })),
      asks: rawBook.asks.map(level => ({ price: Number(level[0]), quantity: Number(level[1]) })),
    });
    state.book = rawBook;
    state.ticker = {
      lastPrice: Number(rawTicker.lastPrice),
      change: Number(rawTicker.priceChangePercent),
    };
    const bestBid = Number(rawBook.bids?.[0]?.[0]);
    const bestAsk = Number(rawBook.asks?.[0]?.[0]);
    renderTicker(state.ticker, bestBid, bestAsk);
    updateIndicators();
    state.chart.timeScale().fitContent();
    renderMarkers();
    text("marketFeedStatus", "● LIVE · Binance public Spot");
  }

  function paperMarkers() {
    if (!state.paper?.trades?.length) return [];
    const selected = state.symbol.toUpperCase();
    return state.paper.trades
      .filter(trade => String(trade.symbol || "").replace("/", "").toUpperCase() === selected)
      .map(trade => ({
        time: Math.floor(new Date(trade.filled_at).getTime() / 1000),
        position: trade.side === "buy" ? "belowBar" : "aboveBar",
        color: trade.side === "buy" ? "#00e676" : "#ff1744",
        shape: trade.side === "buy" ? "arrowUp" : "arrowDown",
        text: "PAPER " + String(trade.side || "").toUpperCase(),
      }))
      .filter(marker => Number.isFinite(marker.time) && marker.time > 0)
      .sort((a, b) => a.time - b.time);
  }

  function renderMarkers() {
    if (!state.markers) return;
    state.markers.setMarkers(paperMarkers());
  }

  function renderPaper() {
    const data = state.paper;
    if (!data?.session) {
      text("paperEquity", "—");
      text("paperCash", "—");
      text("paperPnl", "—");
      text("paperPnlSplit", "Start a virtual paper run to begin");
      text("paperOpenCount", "0");
      text("paperSessionMeta", "No paper session yet");
      $("paperPositionsBody").innerHTML = '<tr><td colspan="6">No open virtual positions.</td></tr>';
      $("paperTradesBody").innerHTML = '<tr><td colspan="8">No simulated fills yet.</td></tr>';
      renderMarkers();
      return;
    }
    const session = data.session;
    const tick = data.tick || {};
    const initial = Number(session.initial_balance_usd ?? 1000);
    const cash = Number(session.cash_balance_usd ?? initial);
    const equity = Number(tick.equity_usd ?? cash);
    const realized = Number(tick.realized_pnl_usd ?? 0);
    const unrealized = Number(tick.unrealized_pnl_usd ?? 0);
    const net = equity - initial;
    text("paperEquity", usd(equity));
    text("paperCash", usd(cash));
    text("paperPnl", (net >= 0 ? "+" : "") + usd(net));
    text("paperPnlSplit", "Realized " + usd(realized) + " · Unrealized " + usd(unrealized));
    text("paperOpenCount", String(data.positions?.length ?? 0));
    text("paperSessionMeta", String(session.status || "unknown").toUpperCase() + " · " +
      Number(session.tick_count || 0) + " ticks · Started " + timeLabel(session.started_at));
    $("paperPositionsBody").innerHTML = data.positions?.length ? data.positions.map(p => {
      const pnl = Number(p.unrealized_pnl_usd || 0);
      return '<tr><td>' + cleanText(p.symbol) + '</td><td>' + compact(p.quantity, 8) +
        '</td><td>@ ' + compact(p.average_entry_price, 8) + '</td><td>@ ' +
        compact(p.mark_price, 8) + '</td><td><span class="green">TP @ ' +
        compact(p.take_profit_price, 8) + '</span><br><span class="red">SL @ ' +
        compact(p.stop_loss_price, 8) + '</span></td><td class="' + (pnl >= 0 ? "green" : "red") + '">' +
        (pnl >= 0 ? "+" : "") + usd(pnl) + '</td></tr>';
    }).join("") : '<tr><td colspan="6">No open virtual positions.</td></tr>';
    $("paperTradesBody").innerHTML = data.trades?.length ? data.trades.slice(0, 35).map(t => {
      const pnl = t.realized_pnl_usd == null ? null : Number(t.realized_pnl_usd);
      return '<tr><td class="' + (t.side === "buy" ? "green" : "red") + '">' +
        (t.side === "buy" ? "▲ BUY" : "▼ SELL") + '</td><td>' + cleanText(t.symbol) +
        '</td><td>' + compact(t.quantity, 8) + '</td><td>@ ' + compact(t.fill_price, 8) +
        '</td><td>@ ' + compact(t.reference_price, 8) + '</td><td>' + usd(t.fee_usd) +
        '</td><td>' + (pnl == null ? "—" : (pnl >= 0 ? "+" : "") + usd(pnl)) +
        '</td><td>' + timeLabel(t.filled_at) + '</td></tr>';
    }).join("") : '<tr><td colspan="8">No simulated fills yet.</td></tr>';
    renderMarkers();
  }

  async function refreshPaper() {
    if (!initData()) {
      text("paperEngineStatus", "Open this Web App from your 9N8 Telegram private chat to load your paper account.");
      return;
    }
    try {
      const data = await apiPost({ action: "paper_terminal" });
      state.paper = { session: data.session, tick: data.tick, positions: data.positions || [], trades: data.trades || [] };
      renderPaper();
      if (data.session) {
        text("paperEngineStatus", "● " + String(data.session.status).toUpperCase() +
          " · Real Binance data · virtual $1,000 starting account · no real orders");
      }
    } catch (error) {
      text("paperEngineStatus", error.message || "Could not refresh the paper ledger.");
    }
  }

  async function startPaper() {
    if (state.busy) return;
    if (!initData()) {
      text("paperEngineStatus", "Open this Web App from your 9N8 Telegram private chat first.");
      return;
    }
    state.busy = true;
    const button = $("startPaperButton");
    button.disabled = true;
    text("paperEngineStatus", "Starting / resuming the virtual paper session…");
    try {
      const result = await apiPost({ action: "paper_start" });
      text("paperEngineStatus", result.message || "Paper session started.");
      await refreshPaper();
    } catch (error) {
      text("paperEngineStatus", error.message || "Could not start paper trading.");
    } finally {
      state.busy = false;
      button.disabled = false;
    }
  }

  function closeSocket() {
    if (state.reconnectTimer) clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
    if (state.socket) {
      state.socket.onclose = null;
      state.socket.close();
      state.socket = null;
    }
  }

  function connectSocket() {
    closeSocket();
    if (!state.active || document.hidden) return;
    const symbol = state.symbol.toLowerCase();
    const interval = state.timeframe;
    const streams = [
      symbol + "@kline_" + interval,
      symbol + "@bookTicker",
      symbol + "@ticker",
    ].join("/");
    const socket = new WebSocket(BINANCE_WS + streams);
    state.socket = socket;
    socket.onopen = () => {
      state.lastMarketMessage = Date.now();
      text("marketFeedStatus", "● LIVE · Binance public Spot stream");
    };
    socket.onmessage = (event) => {
      try {
        const packet = JSON.parse(event.data);
        const data = packet.data || packet;
        state.lastMarketMessage = Date.now();
        if (data.e === "kline" && data.k) {
          const k = data.k;
          const candle = {
            time: Math.floor(Number(k.t) / 1000),
            open: Number(k.o), high: Number(k.h), low: Number(k.l), close: Number(k.c),
            volume: Number(k.v), closeTime: Number(k.T),
          };
          const previous = state.rows.at(-1);
          if (previous && previous.time === candle.time) state.rows[state.rows.length - 1] = candle;
          else if (!previous || candle.time > previous.time) state.rows.push(candle);
          if (state.rows.length > 450) state.rows.shift();
          state.candles.update({ time: candle.time, open: candle.open, high: candle.high, low: candle.low, close: candle.close });
          state.volume.update({
            time: candle.time, value: candle.volume,
            color: candle.close >= candle.open ? "rgba(0,230,118,.35)" : "rgba(255,23,68,.35)",
          });
          updateIndicators();
          if (k.x) refreshPaper().catch(() => {});
        } else if (data.e === "bookTicker") {
          const bid = Number(data.b), ask = Number(data.a);
          if (bid > 0 && ask > 0) {
            const current = state.book || { bids: [], asks: [] };
            current.bids = [[String(bid), String(data.B)]];
            current.asks = [[String(ask), String(data.A)]];
            state.book = current;
            renderBook({
              bids: current.bids.map(level => ({ price: Number(level[0]), quantity: Number(level[1]) })),
              asks: current.asks.map(level => ({ price: Number(level[0]), quantity: Number(level[1]) })),
            });
            renderTicker(state.ticker, bid, ask);
          }
        } else if (data.e === "24hrTicker") {
          state.ticker = {
            lastPrice: Number(data.c),
            change: Number(data.P),
          };
          const bid = Number(state.book?.bids?.[0]?.[0]);
          const ask = Number(state.book?.asks?.[0]?.[0]);
          renderTicker(state.ticker, bid, ask);
        }
      } catch (error) {
        console.warn("9N8 Binance stream message rejected", error);
      }
    };
    socket.onerror = () => text("marketFeedStatus", "● Stream issue · reconnecting");
    socket.onclose = () => {
      state.socket = null;
      if (state.active && !document.hidden) {
        text("marketFeedStatus", "● Stream disconnected · reconnecting");
        state.reconnectTimer = setTimeout(connectSocket, 2500);
      }
    };
  }

  async function activate() {
    if (state.destroyed || state.started) return;
    state.started = true;
    state.active = true;
    if (tg) { tg.ready(); tg.expand(); }
    ensureChart();
    try {
      await loadInitialMarket();
    } catch (error) {
      text("marketFeedStatus", "● Binance data unavailable");
      text("marketEngineStatus", error.message || "Market data unavailable.");
    }
    connectSocket();
    await refreshPaper();
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(() => {
      if (state.active && !document.hidden) refreshPaper();
    }, 12000);
  }

  async function changeMarket() {
    state.symbol = symbolSelect.value.toUpperCase();
    state.timeframe = timeframeSelect.value;
    closeSocket();
    try {
      await loadInitialMarket();
      connectSocket();
    } catch (error) {
      text("marketFeedStatus", "● Binance data unavailable");
      text("marketEngineStatus", error.message || "Could not load selected market.");
    }
  }

  $("startPaperButton")?.addEventListener("click", startPaper);
  $("refreshPaperButton")?.addEventListener("click", async () => {
    await changeMarket();
    await refreshPaper();
  });
  symbolSelect.addEventListener("change", changeMarket);
  timeframeSelect.addEventListener("change", changeMarket);
  window.addEventListener("n8:simulator-visible", activate);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      closeSocket();
    } else if (state.active) {
      connectSocket();
      refreshPaper();
    }
  });

  if (!panel.classList.contains("hidden")) activate();
}
