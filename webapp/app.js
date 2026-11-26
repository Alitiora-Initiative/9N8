(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    if (tg.setHeaderColor) tg.setHeaderColor("#0B0E14");
    if (tg.setBackgroundColor) tg.setBackgroundColor("#0B0E14");
  }
  const initData = tg ? tg.initData : "";
  const headers = {"Content-Type":"application/json","X-Telegram-Init-Data":initData};
  const $ = (id) => document.getElementById(id);
  let state = null;
  let socket = null;
  let modalAction = null;
  const pnlByPosition = new Map();

  function haptic(style) {
    try {
      if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred(style || "medium");
    } catch (_) {}
  }
  function toast(message) {
    const el = $("toast");
    el.textContent = message;
    el.classList.add("show");
    window.setTimeout(() => el.classList.remove("show"), 2600);
  }
  async function api(path, options) {
    const opts = options || {};
    const response = await fetch(path, {
      method: opts.method || "GET",
      body: opts.body,
      headers: Object.assign({}, headers, opts.headers || {})
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || ("Request failed (" + response.status + ")"));
    return payload;
  }
  function fmt(value, places) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    return n.toLocaleString(undefined, {maximumFractionDigits: places === undefined ? 4 : places});
  }
  function shortAddress(address) {
    if (!address) return "Wallet not created";
    return address.slice(0, 7) + "…" + address.slice(-7);
  }
  function renderState(data) {
    state = data;
    $("solPrice").textContent = data.sol_usd ? "$" + fmt(data.sol_usd, 2) : "—";
    $("priceSource").textContent = data.price_source || "Oracle unavailable";
    $("walletBalance").textContent = fmt(data.balance_sol, 6) + " SOL";
    const pnl = Number(data.daily_realized_pnl_sol || 0);
    $("dailyPnl").textContent = (pnl >= 0 ? "+" : "") + fmt(pnl, 6) + " SOL";
    $("dailyPnl").className = "metric-value mono " + (pnl >= 0 ? "positive" : "negative");
    $("walletAddress").textContent = shortAddress(data.wallet_address);
    $("walletAddress").title = data.wallet_address || "";
    $("engineStatus").classList.toggle("running", !!data.enabled);
    $("engineStatus").innerHTML = '<span class="status-dot"></span> ' + (data.enabled ? "RUNNING" : "PAUSED");
    $("modeLabel").textContent = data.mode === "live"
      ? (data.live_enabled ? "LIVE · REAL FUNDS" : "LIVE LOCKED")
      : "PAPER · NO ORDERS";
    $("modePill").textContent = data.mode === "live" ? "LIVE" : "PAPER";
    $("engineToggle").innerHTML = data.enabled
      ? '<i data-lucide="pause"></i><span>STOP SNIPER</span>'
      : '<i data-lucide="zap"></i><span>START SNIPER</span>';
    const settings = data.settings || {};
    const form = $("settingsForm");
    Object.keys(settings).forEach((key) => {
      const field = form.elements.namedItem(key);
      if (!field) return;
      if (field.type === "checkbox") field.checked = !!settings[key];
      else field.value = settings[key];
    });
    renderPositions(data.positions || []);
    if (window.lucide) window.lucide.createIcons();
  }
  function renderPositions(positions) {
    $("positionCount").textContent = positions.length + " OPEN";
    if (!positions.length) {
      $("positionsList").innerHTML = '<div class="empty-state"><i data-lucide="layers-3"></i><span>No open positions</span></div>';
      if (window.lucide) window.lucide.createIcons();
      return;
    }
    $("positionsList").innerHTML = positions.map((position) => {
      const pnl = Number(pnlByPosition.get(position.id) || 0);
      const openedAt = new Date(position.opened_at).getTime();
      const elapsed = Number.isFinite(openedAt) ? Math.max(0, Math.floor((Date.now() - openedAt) / 1000)) + "s" : "—";
      return '<div class="position-row">' +
        '<div class="position-main"><strong>' + position.mint.slice(0, 8) + "…" + position.mint.slice(-5) +
        '</strong><small>' + position.token_amount_raw + ' raw tokens · entry ' +
        fmt(position.entry_price_usd, 10) + ' USD</small></div>' +
        '<div class="position-pnl ' + (pnl >= 0 ? "positive" : "negative") + '">' +
        (pnl >= 0 ? "+" : "") + fmt(pnl, 2) + '%</div>' +
        '<div class="position-time curve-value">' + elapsed + '</div>' +
        '<button class="panic-button" data-panic="' + position.id + '">PANIC SELL 100%</button>' +
        '</div>';
    }).join("");
  }
  function appendRadar(event) {
    if (event.type !== "launch_detected") return;
    const feed = $("radarFeed");
    const empty = feed.querySelector(".empty-state");
    if (empty) empty.remove();
    const progress = Number(event.curve_progress_pct || 0);
    const row = document.createElement("div");
    row.className = "radar-row";
    const left = document.createElement("div");
    left.innerHTML = '<span class="token-name"></span><span class="token-address"></span>';
    left.querySelector(".token-name").textContent = event.launchpad || "Solana";
    left.querySelector(".token-address").textContent = event.mint || "";
    const curve = document.createElement("div");
    curve.innerHTML = '<span class="curve-value"></span><div class="curve-track"><div class="curve-fill"></div></div>';
    curve.querySelector(".curve-value").textContent = progress > 0 ? fmt(progress, 1) + "%" : "—";
    curve.querySelector(".curve-fill").style.width = Math.min(100, Math.max(0, progress)) + "%";
    const tag = document.createElement("div");
    tag.className = "event-tag";
    tag.textContent = event.mode === "paper" ? "DETECTED" : "CANDIDATE";
    row.append(left, curve, tag);
    feed.prepend(row);
    while (feed.children.length > 30) feed.lastElementChild.remove();
  }
  async function refresh() {
    try {
      const data = await api("/api/state");
      renderState(data);
      $("connectionText").textContent = "CONNECTED";
      connectSocket();
    } catch (error) {
      $("connectionText").textContent = "AUTH ERROR";
      toast(error.message);
    }
  }
  function connectSocket() {
    if (socket || !initData) return;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const url = scheme + "://" + location.host + "/ws/telemetry";
    socket = new WebSocket(url);
    socket.onopen = () => {
      socket.send(JSON.stringify({initData:initData}));
      $("connectionText").textContent = "LIVE";
    };
    socket.onmessage = (message) => {
      let event;
      try { event = JSON.parse(message.data); } catch (_) { return; }
      if (event.type === "launch_detected") appendRadar(event);
      if (event.type === "position_update") {
        pnlByPosition.set(event.position_id, Number(event.pnl_pct || 0));
        if (state) renderPositions(state.positions || []);
      }
      if (event.type === "position_exit" || event.type === "position_opened") refresh();
    };
    socket.onclose = () => {
      socket = null;
      $("connectionText").textContent = "RECONNECTING";
      window.setTimeout(connectSocket, 2500);
    };
    socket.onerror = () => { if (socket) socket.close(); };
  }
  function openModal(action) {
    modalAction = action;
    $("actionModal").classList.add("open");
    $("actionModal").setAttribute("aria-hidden", "false");
    $("confirmCheck").checked = false;
    $("destinationAddress").value = "";
    $("modalMessage").textContent = "";
    if (action === "withdraw") {
      $("modalKicker").textContent = "SOLANA SETTLEMENT";
      $("modalTitle").textContent = "Withdraw all SOL";
      $("modalDescription").textContent = "A $1.00 USD platform fee and the actual network fee will be deducted. Open positions must be closed first.";
      $("confirmText").textContent = "I checked this destination and authorize this irreversible transfer.";
      $("modalSubmit").textContent = "CONFIRM WITHDRAWAL";
    } else {
      $("modalKicker").textContent = "PERMANENT KEY ROTATION";
      $("modalTitle").textContent = "Wipe & rotate wallet";
      $("modalDescription").textContent = "The old wallet pays a $1.00 USD termination fee, sends remaining SOL to your payout address, and is replaced with a new paused wallet. Open positions must be closed.";
      $("confirmText").textContent = "I understand the old wallet key will be retired and the transaction cannot be reversed.";
      $("modalSubmit").textContent = "CONFIRM WIPE & ROTATE";
    }
  }
  function closeModal() {
    $("actionModal").classList.remove("open");
    $("actionModal").setAttribute("aria-hidden", "true");
    modalAction = null;
  }

  $("engineToggle").addEventListener("click", async () => {
    haptic();
    try {
      if (state && state.enabled) {
        await api("/api/engine/stop", {method:"POST"});
        toast("Sniper paused");
      } else {
        await api("/api/engine/start", {method:"POST"});
        toast("Sniper start requested");
      }
      await refresh();
    } catch (error) { toast(error.message); }
  });
  $("copyAddress").addEventListener("click", async () => {
    haptic("light");
    const address = state && state.wallet_address;
    if (!address) return toast("Create a wallet in the Telegram bot first");
    try {
      await navigator.clipboard.writeText(address);
      toast("Deposit address copied");
    } catch (_) { toast("Clipboard permission unavailable"); }
  });
  $("withdrawOpen").addEventListener("click", () => { haptic(); openModal("withdraw"); });
  $("wipeOpen").addEventListener("click", () => { haptic("heavy"); openModal("wipe"); });
  $("modalClose").addEventListener("click", closeModal);
  $("actionModal").addEventListener("click", (event) => {
    if (event.target === $("actionModal")) closeModal();
  });
  $("modalSubmit").addEventListener("click", async () => {
    haptic("medium");
    if (!$("confirmCheck").checked) return toast("Tick the confirmation box first");
    const destination = $("destinationAddress").value.trim();
    if (!destination) return toast("Enter your external Solana address");
    $("modalSubmit").disabled = true;
    $("modalMessage").textContent = "Submitting wallet operation…";
    try {
      const path = modalAction === "withdraw" ? "/api/wallet/withdraw" : "/api/wallet/wipe";
      const result = await api(path, {
        method:"POST",
        body:JSON.stringify({confirm:true,destination:destination})
      });
      $("modalMessage").textContent = "Confirmed. Signature: " + (result.signature || "submitted");
      toast(modalAction === "withdraw" ? "Withdrawal confirmed" : "Wallet rotated. Check Telegram for the new key.");
      await refresh();
      window.setTimeout(closeModal, 3500);
    } catch (error) {
      $("modalMessage").textContent = error.message;
    } finally {
      $("modalSubmit").disabled = false;
    }
  });
  $("settingsForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    haptic("light");
    $("settingsState").textContent = "SAVING…";
    const values = {};
    const form = event.currentTarget;
    [
      "launchpad","pump_mode","buy_amount_sol","slippage_pct",
      "priority_fee_lamports","jito_tip_lamports","take_profit_pct",
      "stop_loss_pct","max_hold_seconds","moon_bag_pct",
      "daily_position_limit","max_daily_drawdown_pct",
      "pump_migration_progress_pct","min_liquidity_usd",
      "max_top10_concentration_pct"
    ].forEach((key) => {
      const input = form.elements.namedItem(key);
      if (!input) return;
      values[key] = (key === "launchpad" || key === "pump_mode") ? input.value : Number(input.value);
    });
    values.mev_shield = $("mevToggle").checked;
    try {
      await api("/api/settings", {method:"POST",body:JSON.stringify({values:values})});
      $("settingsState").textContent = "SAVED";
      toast("Sniper parameters saved");
      await refresh();
    } catch (error) {
      $("settingsState").textContent = "ERROR";
      toast(error.message);
    }
  });
  $("positionsList").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-panic]");
    if (!button) return;
    haptic("heavy");
    if (!window.confirm("Panic sell 100% of this token position?")) return;
    try {
      await api("/api/positions/" + button.dataset.panic + "/panic", {method:"POST"});
      toast("Panic sell submitted");
    } catch (error) { toast(error.message); }
  });
  if (tg) {
    refresh();
    window.setInterval(refresh, 15000);
  } else {
    $("connectionText").textContent = "OPEN INSIDE TELEGRAM";
    toast("Open this terminal from the 9N8 Telegram bot.");
  }
})();