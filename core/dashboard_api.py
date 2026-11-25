from __future__ import annotations

import hmac
import json
import logging
import math
import os
import uuid
from datetime import datetime, timezone

from aiohttp import web

from models import ConnectorUnavailable, MarketType, RiskRejected, Side, TradeIntent

log = logging.getLogger(__name__)


def _epoch(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return int(stamp.timestamp())
    except (TypeError, ValueError):
        return None


def _metadata(row):
    raw = row.get("metadata_json", "{}")
    try:
        row["metadata"] = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, json.JSONDecodeError):
        row["metadata"] = {}
    row.pop("metadata_json", None)
    return row


class DashboardAPIServer:
    """Private bearer-authenticated API for the 9N8 terminal."""

    def __init__(self, engine):
        self.engine = engine
        self.token = os.getenv("DASHBOARD_API_TOKEN", "").strip()
        self.host = os.getenv("DASHBOARD_API_HOST", "127.0.0.1").strip()
        self.port = int(os.getenv("DASHBOARD_API_PORT", "8787"))
        raw = os.getenv("DASHBOARD_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
        self.origins = {x.strip().rstrip("/") for x in raw.split(",") if x.strip()}
        self.enabled = len(self.token) >= 32
        self.runner = None

    async def start(self):
        if not self.enabled:
            log.warning("Dashboard API disabled: set DASHBOARD_API_TOKEN to 32+ random characters.")
            return
        app = web.Application(client_max_size=65536, middlewares=[self.middleware])
        app.router.add_route("OPTIONS", "/{tail:.*}", self.options)
        app.router.add_get("/api/health", self.health)
        app.router.add_get("/api/dashboard", self.dashboard)
        app.router.add_get("/api/trades", self.trades)
        app.router.add_get("/api/events", self.events)
        app.router.add_get("/api/candles", self.candles)
        app.router.add_post("/api/orders", self.orders)
        app.router.add_post("/api/control/pause", self.pause)
        app.router.add_post("/api/control/resume", self.resume)
        app.router.add_post("/api/control/panic", self.panic)
        app.router.add_post("/api/control/migrate-live", self.migrate_live)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.host, self.port).start()
        log.warning("🔐 9N8 dashboard API listening on %s:%s in %s mode", self.host, self.port, self.engine.mode)

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    @web.middleware
    async def middleware(self, request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        except RiskRejected as exc:
            response = web.json_response({"ok": False, "error": str(exc)}, status=422)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            response = web.json_response({"ok": False, "error": str(exc)[:240]}, status=400)
        except Exception:
            log.exception("Dashboard API error for %s %s", request.method, request.path)
            response = web.json_response({"ok": False, "error": "Internal dashboard API error."}, status=500)
        origin = request.headers.get("Origin", "").rstrip("/")
        if origin and origin in self.origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Authorization,Content-Type"
            response.headers["Access-Control-Max-Age"] = "600"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def authorize(self, request):
        origin = request.headers.get("Origin", "").rstrip("/")
        if origin and origin not in self.origins:
            raise web.HTTPForbidden(text="Origin is not allowlisted.")
        header = request.headers.get("Authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not self.enabled or not supplied or not hmac.compare_digest(supplied, self.token):
            raise web.HTTPUnauthorized(text="Dashboard access token is invalid.")

    async def options(self, request):
        origin = request.headers.get("Origin", "").rstrip("/")
        if origin and origin not in self.origins:
            raise web.HTTPForbidden()
        return web.Response(status=204)

    async def health(self, request):
        return web.json_response({
            "ok": True, "service": "9N8 dashboard API", "mode": self.engine.mode,
            "liveExecutionCertified": bool(self.engine.cfg["execution"].get("live_execution_certified", False)),
            "authRequired": True,
        })

    async def dashboard(self, request):
        await self.authorize(request)
        account = await self.engine._portfolio_account()
        positions = await self.engine.store.positions()
        now = datetime.now(timezone.utc)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_open = await self.engine.store.set_daily_equity(midnight.date().isoformat(), account.equity_usd)
        trades = await self.engine.store.query_trades(300)
        closed = []
        for row in trades:
            stamp = _epoch(row.get("exit_timestamp"))
            if row.get("status") in {"closed", "partial_closed"} and stamp is not None and stamp >= int(midnight.timestamp()):
                closed.append(row)
        wins = sum(1 for row in closed if float(row.get("realized_pnl_usd") or 0) > 0)
        markets = []
        for spec in self.engine.specs:
            snap = self.engine.snapshots.get(self.engine._key(spec["venue"], spec["symbol"]))
            markets.append({
                "venue": spec["venue"], "symbol": spec["symbol"],
                "marketType": spec["market_type"].value,
                "price": snap.last if snap else None,
                "bid": snap.bid if snap else None,
                "ask": snap.ask if snap else None,
                "spreadPct": snap.spread_pct if snap else None,
                "timestamp": snap.timestamp.isoformat() if snap else None,
            })
        paused, reason = await self.engine.risk.is_paused()
        return web.json_response({
            "ok": True, "updatedAt": now.isoformat(), "mode": self.engine.mode,
            "liveExecutionCertified": bool(self.engine.cfg["execution"].get("live_execution_certified", False)),
            "liveTradingEnabled": bool(self.engine.settings.live_enabled and self.engine.cfg["execution"].get("live_execution_certified", False)),
            "riskGuard": {"active": True, "paused": paused, "pauseReason": reason,
                          "maxRiskPerTradePct": 2, "dailyDrawdownLimitPct": 5, "maxLeverage": 3},
            "account": {
                "equityUsd": account.equity_usd, "freeUsd": account.free_usd,
                "cashUsd": account.balance_usd, "dailyPnlUsd": account.equity_usd - day_open,
                "dailyPnlPct": ((account.equity_usd - day_open) / day_open * 100) if day_open else None,
                "realizedTodayUsd": sum(float(x.get("realized_pnl_usd") or 0) for x in closed),
                "openPositions": len(positions),
                "winRatePct": wins / len(closed) * 100 if closed else None,
                "closedTradesToday": len(closed),
            },
            "markets": markets,
            "positions": [_metadata(dict(row)) for row in positions],
        })

    async def trades(self, request):
        await self.authorize(request)
        limit = min(max(int(request.query.get("limit", "50")), 1), 200)
        rows = await self.engine.store.query_trades(limit, request.query.get("symbol") or None)
        return web.json_response({"ok": True, "trades": [_metadata(dict(row)) for row in rows]})

    async def events(self, request):
        await self.authorize(request)
        limit = min(max(int(request.query.get("limit", "50")), 1), 200)
        return web.json_response({"ok": True, "events": await self.engine.store.query_events(limit)})

    async def candles(self, request):
        await self.authorize(request)
        venue, symbol = request.query.get("venue", ""), request.query.get("symbol", "")
        timeframe = request.query.get("timeframe", "5m")
        if not venue or not symbol or timeframe not in {"1m", "5m", "15m", "1h", "4h", "1d", "M5", "H1"}:
            raise web.HTTPBadRequest(text="venue, symbol and a supported timeframe are required.")
        spec = next((s for s in self.engine.specs if s["venue"] == venue and s["symbol"] == symbol), None)
        if not spec:
            raise web.HTTPNotFound(text="Instrument is not enabled.")
        connector = self.engine.connectors[spec["connector"]]
        rows = await connector.fetch_candles(symbol, timeframe, 250)
        candles = [{"time": int(x.timestamp / 1000), "open": x.open, "high": x.high,
                    "low": x.low, "close": x.close, "volume": x.volume}
                   for x in rows if x.open > 0 and x.close > 0]
        valid_times = [x["time"] for x in candles]
        markers = []
        if valid_times:
            for trade in await self.engine.store.query_trades(200, symbol):
                stamp = _epoch(trade.get("exit_timestamp") or trade.get("entry_timestamp"))
                if stamp is None:
                    continue
                nearest = min(valid_times, key=lambda t: abs(t - stamp))
                if abs(nearest - stamp) > 3600:
                    continue
                is_buy = str(trade.get("side", "")).lower() == "buy"
                markers.append({"time": nearest, "position": "belowBar" if is_buy else "aboveBar",
                                "color": "#00E676" if is_buy else "#FF1744",
                                "shape": "arrowUp" if is_buy else "arrowDown",
                                "text": ("BUY" if is_buy else "SELL") + " @ " + format(float(trade.get("average_price") or 0), ".8g")})
        return web.json_response({"ok": True, "venue": venue, "symbol": symbol,
                                  "timeframe": timeframe, "candles": candles, "markers": markers})

    async def orders(self, request):
        await self.authorize(request)
        payload = await request.json()
        await self.engine.risk.assert_entry_allowed()
        venue, symbol = str(payload.get("venue", "")), str(payload.get("symbol", ""))
        side = Side(str(payload.get("side", "")).lower())
        requested = float(payload.get("notionalUsd", 0))
        stop_pct, target_r = float(payload.get("stopLossPct", 0.015)), float(payload.get("takeProfitR", 3))
        if not math.isfinite(requested) or requested <= 0 or not 0.002 <= stop_pct <= 0.05 or not 1 <= target_r <= 5:
            raise RiskRejected("Invalid notional, stop loss or take-profit.")
        spec = next((s for s in self.engine.specs if s["venue"] == venue and s["symbol"] == symbol), None)
        if not spec:
            raise RiskRejected("Instrument is not configured.")
        market_type = spec["market_type"]
        if market_type in {MarketType.SOLANA_DEX, MarketType.EVM_DEX}:
            raise RiskRejected("DEX execution is locked pending independent sell simulation and position reconciliation.")
        if market_type is MarketType.CRYPTO_SPOT and side is Side.SELL and self.engine.mode != "paper":
            raise RiskRejected("Spot short selling is blocked. Use a derivatives or forex market.")
        connector = self.engine.connectors[spec["connector"]]
        snapshot = await (connector.watch_snapshot(symbol) if hasattr(connector, "watch_snapshot") else connector.fetch_snapshot(symbol))
        if not all(math.isfinite(v) and v > 0 for v in (snapshot.bid, snapshot.ask, snapshot.last)) or snapshot.ask < snapshot.bid:
            raise ConnectorUnavailable("Invalid or crossed provider quote.")
        self.engine.snapshots[self.engine._key(venue, symbol)] = snapshot
        entry = snapshot.ask if side is Side.BUY else snapshot.bid
        distance = entry * stop_pct
        stop = entry - distance if side is Side.BUY else entry + distance
        target = entry + distance * target_r if side is Side.BUY else entry - distance * target_r
        intent = TradeIntent(venue, symbol, market_type, side, entry, stop, target, distance,
                             "Manual dashboard order · risk manager approved", {"source": "dashboard"})
        open_rows = await self.engine.store.positions()
        position_key = self.engine._key(venue, symbol)
        if any(x["position_key"] == position_key for x in open_rows):
            raise RiskRejected("A position is already open for this instrument.")
        account = await self.engine._portfolio_account()
        exposure = sum(float(x["quantity"]) * float(x["mark_price"]) for x in open_rows)
        multiplier = connector.contract_multiplier(symbol) if hasattr(connector, "contract_multiplier") else 1.0
        leverage = float(spec.get("leverage", 1))
        spread = self.engine._forex_spread_pips(snapshot) if market_type is MarketType.FOREX else None
        decision = await self.engine.risk.evaluate(
            intent, account, snapshot, exposure, max(snapshot.spread_pct / 2, 0),
            contract_multiplier=multiplier, leverage=leverage, forex_spread_pips=spread,
            current_open_positions=len(open_rows),
        )
        if not decision.approved or not decision.size:
            raise RiskRejected(decision.reason)
        notional = min(requested, decision.size.notional_usd)
        quantity = notional / (entry * multiplier)
        actual_risk = quantity * distance * multiplier
        if quantity <= 0 or not math.isfinite(quantity):
            raise RiskRejected("Calculated order size is invalid.")
        if hasattr(connector, "estimate_slippage_pct"):
            impact = await connector.estimate_slippage_pct(symbol, side, quantity)
            limit = self.engine.risk.slippage_limit_pct(market_type.value)
            if not math.isfinite(impact) or impact > limit:
                raise RiskRejected("Order-book impact exceeds the hard slippage limit.")
        client_id = str(payload.get("clientOrderId") or uuid.uuid4().hex)
        if not 8 <= len(client_id) <= 64:
            raise RiskRejected("Invalid client order ID.")
        ledger_key = "dashboard:" + venue + ":" + symbol + ":" + client_id
        if not await self.engine.store.claim_idempotency(ledger_key, {"venue": venue, "symbol": symbol, "side": side.value}):
            raise RiskRejected("This order request was already accepted.")
        try:
            if self.engine.mode == "paper":
                receipt = await self.engine.paper.execute(intent, quantity, snapshot, client_id)
                await self.engine.store.set_control("paper_cash", str(self.engine.paper.cash))
            elif side is Side.BUY:
                receipt = await connector.buy(intent, quantity, client_id)
            else:
                receipt = await connector.sell(intent, quantity, client_id)
            if receipt.quantity <= 0 or receipt.average_price <= 0:
                raise ConnectorUnavailable("The provider did not confirm a valid fill.")
            opened = receipt.timestamp.isoformat()
            await self.engine.store.record_trade({
                "client_order_id": ledger_key, "venue": venue, "symbol": symbol,
                "market_type": market_type.value, "side": side.value, "status": "open",
                "mode": self.engine.mode, "quantity": receipt.quantity, "average_price": receipt.average_price,
                "entry_timestamp": opened, "stop_loss": stop, "take_profit": target,
                "slippage_pct": receipt.slippage_pct, "gas_fee_usd": float(receipt.raw.get("gas_fee_usd", 0)),
                "commission_usd": receipt.fee_usd, "metadata": {"manual": True, "risk_usd": actual_risk,
                    "order_id": receipt.order_id, "tx_hash": receipt.tx_hash},
            })
            await self.engine.store.upsert_position({
                "position_key": position_key, "venue": venue, "symbol": symbol, "market_type": market_type.value,
                "side": side.value, "quantity": receipt.quantity, "entry_price": receipt.average_price,
                "mark_price": snapshot.last, "stop_loss": stop, "take_profit": target,
                "realized_pnl_usd": 0, "unrealized_pnl_usd": 0, "partial_closed": False, "breakeven_moved": False,
                "high_watermark": receipt.average_price, "low_watermark": receipt.average_price,
                "metadata": {"manual": True, "risk_usd": actual_risk, "order_id": receipt.order_id},
                "opened_at": opened, "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            await self.engine.store.update_idempotency(ledger_key, "filled")
            message = (f"⚡ {side.value.upper()} {symbol} @ {receipt.average_price:.8g}\n"
                       f"🎯 TP @ {target:.8g} · 🛡️ SL @ {stop:.8g}\n"
                       f"Size {notional:.2f} USD · Risk {actual_risk:.2f} USD · {self.engine.mode.upper()}")
            await self.engine.store.event("INFO", "dashboard_order", message, venue, symbol,
                                          {"order_id": receipt.order_id, "risk_usd": actual_risk})
            if self.engine.notifier:
                await self.engine.notifier.send(message)
            return web.json_response({"ok": True, "venue": venue, "symbol": symbol, "side": side.value,
                "mode": self.engine.mode, "quantity": receipt.quantity, "averagePrice": receipt.average_price,
                "notionalUsd": notional, "riskUsd": actual_risk, "stopLoss": stop,
                "takeProfit": target, "feeUsd": receipt.fee_usd, "orderId": receipt.order_id})
        except Exception:
            await self.engine.store.update_idempotency(ledger_key, "failed")
            raise

    async def pause(self, request):
        await self.authorize(request)
        await self.engine.risk.pause()
        return web.json_response({"ok": True, "paused": True})

    async def resume(self, request):
        await self.authorize(request)
        await self.engine.risk.resume()
        return web.json_response({"ok": True, "paused": False})

    async def panic(self, request):
        await self.authorize(request)
        try:
            await self.engine.panic()
        except Exception:
            log.exception("Emergency flatten incomplete.")
            return web.json_response({"ok": False, "error": "Emergency flatten incomplete. Inspect provider accounts manually."}, status=500)
        return web.json_response({"ok": True, "message": "Tracked positions flattened; engine stop requested."})

    async def migrate_live(self, request):
        await self.authorize(request)
        return web.json_response({"ok": False, "locked": True,
            "message": "Live execution remains locked until 48-hour testnet soak and bracket/security review pass."}, status=409)
