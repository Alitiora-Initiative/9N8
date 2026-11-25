from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from config import Settings
from connectors.ccxt_connector import CCXTConnector
from connectors.evm_0x import EVM0xConnector
from connectors.oanda import OandaConnector
from connectors.paper_broker import PaperBroker
from connectors.solana_jupiter import SolanaJupiterConnector
from models import (
    AccountState, ConnectorUnavailable, MarketSnapshot, MarketType,
    OpenPosition, RiskRejected, Side, TradeIntent, TradingError,
)
from risk_manager.risk_manager import RiskManager
from storage import TradeStore
from strategies.indicators import atr
from strategies.multi_timeframe import MultiTimeframeStrategy

log = logging.getLogger(__name__)


class TradingEngine:
    """Async portfolio engine. RiskManager is the only entry authority."""

    def __init__(self, settings: Settings, store: TradeStore, notifier=None):
        self.settings = settings
        self.store = store
        self.notifier = notifier
        self.cfg = settings.config
        self.risk = RiskManager(store, self.cfg)
        self.trend_strategy = MultiTimeframeStrategy(self.cfg["strategy"])
        execution = self.cfg["execution"]
        self.paper = PaperBroker(
            float(execution.get("paper_starting_cash_usd", 10000)),
            float(execution.get("paper_fee_rate", 0.001)),
            float(execution.get("paper_slippage_bps", 5)),
        )
        self.connectors: dict[str, Any] = {}
        self.specs: list[dict[str, Any]] = []
        self.snapshots: dict[str, MarketSnapshot] = {}
        self.stop_event = asyncio.Event()
        self.tasks: list[asyncio.Task] = []
        self._panic_lock = asyncio.Lock()
        self._flatten_lock = asyncio.Lock()
        self._market_semaphore = asyncio.Semaphore(5)
        self.risk.register_flatten_callback(self._flatten_all)

    @property
    def mode(self) -> str:
        return self.settings.mode.value

    @staticmethod
    def _key(venue: str, symbol: str) -> str:
        return venue + ":" + symbol

    async def connect(self) -> None:
        if self.mode == "live" and not self.cfg["execution"].get("live_execution_certified", False):
            raise TradingError(
                "Live execution is locked: this branch has not completed its 48-hour testnet soak, "
                "native bracket reconciliation and connector security certification."
            )
        markets = self.cfg["markets"]
        if markets["crypto_spot"].get("enabled"):
            m = markets["crypto_spot"]
            c = CCXTConnector(self.settings, MarketType.CRYPTO_SPOT,
                              m.get("exchange"), sandbox=(self.mode == "testnet"))
            await c.connect()
            key = c.venue + ":spot"
            self.connectors[key] = c
            for symbol in m.get("symbols", []):
                self.specs.append({
                    "connector": key, "venue": c.venue, "symbol": symbol,
                    "market_type": MarketType.CRYPTO_SPOT,
                    "entry_tf": m.get("timeframes", {}).get("entry", "5m"),
                    "trend_tf": m.get("timeframes", {}).get("trend", "1h"),
                })

        if markets["crypto_perpetuals"].get("enabled"):
            m = markets["crypto_perpetuals"]
            if not self.cfg["execution"].get("allow_perpetuals", False):
                raise TradingError("Perpetuals are disabled by the execution safety setting.")
            c = CCXTConnector(self.settings, MarketType.CRYPTO_PERP,
                              m.get("exchange"), sandbox=(self.mode == "testnet"))
            await c.connect()
            key = c.venue + ":perp"
            self.connectors[key] = c
            for symbol in m.get("symbols", []):
                leverage = int(m.get("leverage", 1))
                if leverage > 3 or m.get("margin_mode") != "isolated":
                    raise TradingError("Perpetuals require isolated margin and a maximum 3x leverage.")
                await c.set_isolated_leverage(symbol, leverage)
                self.specs.append({
                    "connector": key, "venue": c.venue, "symbol": symbol,
                    "market_type": MarketType.CRYPTO_PERP, "entry_tf": "5m",
                    "trend_tf": "1h", "leverage": leverage,
                    "isolated_margin_confirmed": True,
                })

        o = markets["oanda"]
        if o.get("enabled"):
            env = os.getenv("OANDA_ENVIRONMENT", "practice").strip().lower()
            if self.mode != "live":
                env = "practice"
            elif not self.cfg["execution"].get("allow_oanda_live", False) or env != "live":
                raise TradingError("OANDA live requires config allow_oanda_live and OANDA_ENVIRONMENT=live.")
            c = OandaConnector(env)
            await c.connect()
            self.connectors["oanda"] = c
            for symbol in o.get("instruments", []):
                mt = MarketType.COMMODITY if symbol.startswith(("XAU", "XAG")) else MarketType.FOREX
                self.specs.append({
                    "connector": "oanda", "venue": "oanda", "symbol": symbol,
                    "market_type": mt,
                    "entry_tf": o.get("granularities", {}).get("entry", "M5"),
                    "trend_tf": o.get("granularities", {}).get("trend", "H1"),
                })

        s = markets["solana_dex"]
        if s.get("enabled"):
            if self.mode == "live":
                raise TradingError(
                    "Live Solana DEX entries are intentionally disabled until independent "
                    "buy-and-sell transaction simulation and position reconciliation are integrated."
                )
            c = SolanaJupiterConnector(cluster=s.get("cluster", "devnet"))
            await c.connect()
            self.connectors["solana-dex"] = c
            for token in s.get("tokens", []):
                self.specs.append({
                    "connector": "solana-dex", "venue": c.venue,
                    "symbol": token["mint"], "market_type": MarketType.SOLANA_DEX,
                    "entry_tf": "5m", "trend_tf": "1h", "dex_data_only": True,
                    "token_config": token,
                })

        e = markets["evm_dex"]
        if e.get("enabled"):
            if self.mode == "live":
                raise TradingError(
                    "Live EVM DEX entries are intentionally disabled until a supported pool OHLCV feed, "
                    "verified buy/sell simulator and full position reconciliation are configured."
                )
            c = EVM0xConnector()
            await c.connect()
            self.connectors[c.venue] = c
            for token in e.get("tokens", []):
                self.specs.append({
                    "connector": c.venue, "venue": c.venue,
                    "symbol": token["address"], "market_type": MarketType.EVM_DEX,
                    "entry_tf": "5m", "trend_tf": "1h", "dex_data_only": True,
                    "token_config": token,
                })

        if not self.specs:
            raise TradingError("No markets enabled. Enable at least one provider in config.json.")
        if self.mode == "paper":
            configured_cash = float(self.cfg["execution"].get("paper_starting_cash_usd", 10000))
            persisted_cash = await self.store.get_control("paper_cash", "")
            cash = float(persisted_cash) if persisted_cash else configured_cash
            await self.paper.restore_state(cash, await self.store.positions())
            await self.store.set_control("paper_cash", str(self.paper.cash))
        await self.store.event("INFO", "engine_connected",
                               f"Connected {len(self.specs)} instruments in {self.mode} mode.")

    async def start(self) -> None:
        if not self.connectors:
            await self.connect()
        await self.store.set_control("engine_mode", self.mode)
        await self.store.set_control("engine_started_at", datetime.now(timezone.utc).isoformat())
        self.tasks = [
            asyncio.create_task(self._market_loop(spec), name="market-" + spec["symbol"])
            for spec in self.specs
        ]
        self.tasks += [
            asyncio.create_task(self._position_loop(), name="position-watch"),
            asyncio.create_task(self._equity_loop(), name="portfolio-risk"),
        ]
        await asyncio.gather(*self.tasks)

    async def close(self) -> None:
        self.stop_event.set()
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        for connector in self.connectors.values():
            try:
                await connector.close()
            except Exception:
                log.exception("Connector shutdown failed.")
        self.tasks.clear()

    @staticmethod
    def _timeframe_ms(value: str) -> int:
        text = str(value).strip().lower()
        if text.startswith("m") and text[1:].isdigit():
            return int(text[1:]) * 60_000
        if text.startswith("h") and text[1:].isdigit():
            return int(text[1:]) * 3_600_000
        if text.endswith("m") and text[:-1].isdigit():
            return int(text[:-1]) * 60_000
        if text.endswith("h") and text[:-1].isdigit():
            return int(text[:-1]) * 3_600_000
        if text.endswith("d") and text[:-1].isdigit():
            return int(text[:-1]) * 86_400_000
        return 0

    def _only_closed_candles(self, candles: list, timeframe: str) -> list:
        duration = self._timeframe_ms(timeframe)
        if duration <= 0:
            return candles
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        return [c for c in candles if c.timestamp + duration <= now_ms]

    async def _market_loop(self, spec: dict[str, Any]) -> None:
        connector = self.connectors[spec["connector"]]
        delay = 1.0
        last_candle_fetch = 0.0
        last_evaluated_candle = None
        cached_trend = []
        cached_entry = []
        candle_refresh = max(5.0, float(self.cfg["execution"].get("candle_refresh_seconds", 15)))
        while not self.stop_event.is_set():
            try:
                async with self._market_semaphore:
                    if hasattr(connector, "watch_snapshot"):
                        snapshot = await connector.watch_snapshot(spec["symbol"])
                    else:
                        snapshot = await connector.fetch_snapshot(spec["symbol"])
                key = self._key(spec["venue"], spec["symbol"])
                self.snapshots[key] = snapshot
                if spec.get("dex_data_only"):
                    await asyncio.sleep(5)
                    continue
                now = asyncio.get_running_loop().time()
                if now - last_candle_fetch >= candle_refresh or not cached_entry:
                    trend_raw, entry_raw = await asyncio.gather(
                        connector.fetch_candles(spec["symbol"], spec["trend_tf"], 250),
                        connector.fetch_candles(spec["symbol"], spec["entry_tf"], 120),
                    )
                    cached_trend = self._only_closed_candles(trend_raw, spec["trend_tf"])
                    cached_entry = self._only_closed_candles(entry_raw, spec["entry_tf"])
                    last_candle_fetch = now
                if cached_entry:
                    candle_time = cached_entry[-1].timestamp
                    if candle_time != last_evaluated_candle and cached_trend:
                        last_evaluated_candle = candle_time
                        intent = self.trend_strategy.evaluate(
                            spec["venue"], spec["symbol"], spec["market_type"],
                            cached_trend, cached_entry,
                        )
                        if intent is not None:
                            await self._consider_entry(spec, connector, intent, snapshot, candle_time)
                delay = 1.0
                await asyncio.sleep(float(self.cfg["execution"].get("price_poll_seconds", 1.0)))
            except asyncio.CancelledError:
                raise
            except RiskRejected as exc:
                await self.store.event("WARNING", "risk_veto", str(exc), spec["venue"], spec["symbol"])
                await asyncio.sleep(2)
            except Exception as exc:
                log.exception("Market loop error for %s %s", spec["venue"], spec["symbol"])
                await self.store.event("ERROR", "market_loop_error", str(exc), spec["venue"], spec["symbol"])
                if self.notifier:
                    await self.notifier.send("9N8 feed error: " + spec["venue"] + " " + spec["symbol"] + ": " + str(exc)[:180])
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    def _forex_spread_pips(self, snapshot: MarketSnapshot) -> float:
        pip = 0.01 if "JPY" in snapshot.symbol else 0.0001
        return (snapshot.ask - snapshot.bid) / pip

    async def _consider_entry(self, spec: dict[str, Any], connector, intent: TradeIntent,
                              snapshot: MarketSnapshot, candle_timestamp: int) -> None:
        await self.risk.assert_entry_allowed()
        key = self._key(intent.venue, intent.symbol)
        open_rows = await self.store.positions()
        if any(row["position_key"] == key for row in open_rows):
            return
        account = await self._portfolio_account()
        exposure = sum(float(row["quantity"]) * float(row["mark_price"]) for row in open_rows)
        spread_slippage = max(snapshot.spread_pct / 2, 0)
        leverage = float(spec.get("leverage", 1))
        contract_multiplier = (
            connector.contract_multiplier(intent.symbol)
            if hasattr(connector, "contract_multiplier") else 1.0
        )
        spread_pips = self._forex_spread_pips(snapshot) if intent.market_type in {
            MarketType.FOREX, MarketType.COMMODITY
        } else None
        intent = TradeIntent(
            intent.venue, intent.symbol, intent.market_type, intent.side,
            snapshot.ask if intent.side is Side.BUY else snapshot.bid,
            intent.stop_loss, intent.take_profit, intent.risk_distance,
            intent.reason, {**intent.metadata, "signal_candle_ms": candle_timestamp},
        )
        if intent.market_type in {MarketType.SOLANA_DEX, MarketType.EVM_DEX}:
            raise RiskRejected("DEX entry is data-only until verified transaction simulations are wired.")
        decision = await self.risk.evaluate(
            intent, account, snapshot, exposure, spread_slippage,
            leverage=leverage, forex_spread_pips=spread_pips,
            contract_multiplier=contract_multiplier,
            current_open_positions=len(open_rows),
        )
        if not decision.approved or not decision.size:
            await self.store.event("WARNING", "trade_veto", decision.reason, intent.venue, intent.symbol)
            return
        if hasattr(connector, "estimate_slippage_pct"):
            order_impact = await connector.estimate_slippage_pct(
                intent.symbol, intent.side, decision.size.quantity
            )
            limit = self.risk.slippage_limit_pct(intent.market_type.value)
            if not math.isfinite(order_impact) or order_impact > limit:
                reason = f"Order-book impact {order_impact:.4%} exceeds the hard {limit:.4%} limit."
                await self.store.event("WARNING", "market_impact_veto", reason, intent.venue, intent.symbol)
                return

        material = f"{intent.venue}|{intent.symbol}|{intent.side.value}|{candle_timestamp}"
        client_id = hashlib.sha256(material.encode()).hexdigest()[:32]
        idempotency = f"{intent.venue}:{intent.symbol}:{intent.side.value}:{candle_timestamp}"
        if not await self.store.claim_idempotency(idempotency, {
            "venue": intent.venue, "symbol": intent.symbol, "side": intent.side.value,
        }):
            return

        try:
            if self.mode == "paper":
                receipt = await self.paper.execute(intent, decision.size.quantity, snapshot, client_id)
                await self.store.set_control("paper_cash", str(self.paper.cash))
            elif intent.side is Side.BUY:
                receipt = await connector.buy(intent, decision.size.quantity, client_id)
            else:
                receipt = await connector.sell(intent, decision.size.quantity, client_id)
            if receipt.quantity <= 0 or receipt.average_price <= 0:
                raise ConnectorUnavailable("Provider returned no confirmed fill; manual reconciliation is required.")
            await self.store.record_trade({
                "client_order_id": idempotency, "venue": receipt.venue, "symbol": receipt.symbol,
                "market_type": intent.market_type.value, "side": receipt.side.value,
                "status": "open", "mode": self.mode, "quantity": receipt.quantity,
                "average_price": receipt.average_price, "entry_timestamp": receipt.timestamp.isoformat(),
                "stop_loss": intent.stop_loss, "take_profit": intent.take_profit,
                "slippage_pct": receipt.slippage_pct,
                "gas_fee_usd": float(receipt.raw.get("gas_fee_usd", 0)),
                "commission_usd": receipt.fee_usd, "metadata": receipt.raw,
            })
            position = {
                "position_key": key, "venue": intent.venue, "symbol": intent.symbol,
                "market_type": intent.market_type.value, "side": intent.side.value,
                "quantity": receipt.quantity, "entry_price": receipt.average_price,
                "mark_price": snapshot.last, "stop_loss": intent.stop_loss,
                "take_profit": intent.take_profit, "realized_pnl_usd": 0,
                "unrealized_pnl_usd": 0, "partial_closed": False,
                "breakeven_moved": False, "high_watermark": receipt.average_price,
                "low_watermark": receipt.average_price,
                "metadata": {
                    **intent.metadata, **receipt.raw,
                    "order_id": receipt.order_id, "tx_hash": receipt.tx_hash,
                    "opened_at": receipt.timestamp.isoformat(),
                },
                "opened_at": receipt.timestamp.isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            await self.store.upsert_position(position)
            await self.store.update_idempotency(idempotency, "filled")
            message = (
                f"9N8 ENTRY {receipt.side.value.upper()} {receipt.symbol}\n"
                f"Venue: {receipt.venue} · mode: {self.mode.upper()}\n"
                f"Quantity: {receipt.quantity:.8g} · fill: {receipt.average_price:.8g}\n"
                f"SL: {intent.stop_loss:.8g} · TP: {intent.take_profit:.8g}\n"
                f"Fee: {receipt.fee_usd:.6f} · slippage: {receipt.slippage_pct:.3%}\n"
                f"Signal: {intent.reason}"
            )
            await self.store.event("INFO", "trade_entry", message, receipt.venue, receipt.symbol,
                                   {"order_id": receipt.order_id, "tx_hash": receipt.tx_hash})
            if self.notifier:
                await self.notifier.send(message)
        except Exception:
            await self.store.update_idempotency(idempotency, "failed")
            raise

    async def _portfolio_account(self) -> AccountState:
        if self.mode == "paper":
            return await self.paper.account(self.snapshots)
        # Spot and perpetual clients for the same exchange often report the same
        # unified account equity. Read each venue once to avoid double-counting.
        unique = {}
        for connector in self.connectors.values():
            unique.setdefault(connector.venue, connector)
        states = await asyncio.gather(
            *(connector.get_balance() for connector in unique.values()),
            return_exceptions=True,
        )
        valid = [state for state in states if isinstance(state, AccountState)]
        if len(valid) != len(states) or not valid:
            raise ConnectorUnavailable("A venue balance could not be verified; portfolio risk sizing is blocked.")
        equity = sum(state.equity_usd for state in valid)
        free = sum(state.free_usd for state in valid)
        return AccountState("portfolio", equity, free, equity)

    async def _equity_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                account = await self._portfolio_account()
                tripped = await self.risk.observe_equity(account)
                if tripped and self.notifier:
                    await self.notifier.send("9N8 DAILY DRAWDOWN: emergency flatten requested; trading halted for 24 hours.")
                await self.store.set_control("equity_monitor_failed", "false")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Equity monitor failed.")
                await self.store.event("ERROR", "equity_monitor_error", "Portfolio equity monitor failed.")
                await self.store.set_control("equity_monitor_failed", "true")
                if self.mode == "live":
                    await self.risk.pause()
            await asyncio.sleep(5)

    async def _position_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                for row in await self.store.positions():
                    spec = next((s for s in self.specs if s["venue"] == row["venue"] and s["symbol"] == row["symbol"]), None)
                    if not spec:
                        raise TradingError("Open position has no configured connector mapping.")
                    connector = self.connectors[spec["connector"]]
                    if hasattr(connector, "watch_snapshot"):
                        snapshot = await connector.watch_snapshot(row["symbol"])
                    else:
                        snapshot = await connector.fetch_snapshot(row["symbol"])
                    self.snapshots[row["position_key"]] = snapshot
                    side = Side(row["side"])
                    mark = snapshot.bid if side is Side.BUY else snapshot.ask
                    direction = 1 if side is Side.BUY else -1
                    row["mark_price"] = mark
                    row["unrealized_pnl_usd"] = (mark - float(row["entry_price"])) * float(row["quantity"]) * direction
                    row["updated_at"] = datetime.now(timezone.utc).isoformat()
                    stop, target = float(row["stop_loss"]), float(row["take_profit"])
                    stop_hit = mark <= stop if side is Side.BUY else mark >= stop
                    target_hit = mark >= target if side is Side.BUY else mark <= target
                    if stop_hit or target_hit:
                        await self._close_position_row(row, connector, "stop_loss" if stop_hit else "take_profit")
                        continue
                    if self.mode == "paper":
                        risk_per_unit = abs(float(row["entry_price"]) - stop)
                        favourable = (mark - float(row["entry_price"])) * direction
                        if not row["partial_closed"] and risk_per_unit > 0 and favourable >= 1.5 * risk_per_unit:
                            await self._partial_close(row, connector, snapshot)
                            row["partial_closed"] = True
                            row["breakeven_moved"] = True
                            row["stop_loss"] = float(row["entry_price"])
                        elif row["partial_closed"]:
                            candles = self._only_closed_candles(
                                await connector.fetch_candles(row["symbol"], "5m", 60), "5m"
                            )
                            current_atr = atr(candles, int(self.cfg["strategy"]["atr_period"]))
                            if current_atr:
                                distance = current_atr * float(self.cfg["strategy"]["trailing_atr_multiple"])
                                if side is Side.BUY:
                                    row["stop_loss"] = max(float(row["stop_loss"]), mark - distance)
                                else:
                                    row["stop_loss"] = min(float(row["stop_loss"]), mark + distance)
                    # Live trades keep the exchange-side hard bracket submitted on entry.
                    # Partial exits/trailing require venue-specific bracket amendment and
                    # are therefore deliberately restricted to the paper engine.
                    await self.store.upsert_position(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Position watcher iteration failed.")
                await self.store.event("ERROR", "position_watcher_error", "Position watcher iteration failed.")
            await asyncio.sleep(1)

    async def _partial_close(self, row: dict[str, Any], connector, snapshot: MarketSnapshot) -> None:
        amount = float(row["quantity"]) * float(self.cfg["strategy"]["partial_take_profit_fraction"])
        if amount <= 0:
            return
        if self.mode == "paper":
            side = Side.SELL if row["side"] == "buy" else Side.BUY
            intent = TradeIntent(
                row["venue"], row["symbol"], MarketType(row["market_type"]), side,
                snapshot.bid if side is Side.SELL else snapshot.ask,
                float(row["stop_loss"]), float(row["take_profit"]),
                abs(float(row["entry_price"]) - float(row["stop_loss"])),
                "TP1 partial close", {},
            )
            receipt = await self.paper.execute(intent, amount, snapshot, "tp1-" + uuid.uuid4().hex)
            await self.store.set_control("paper_cash", str(self.paper.cash))
        else:
            p = OpenPosition(
                row["venue"], row["symbol"], MarketType(row["market_type"]), Side(row["side"]),
                float(row["quantity"]), float(row["entry_price"]), float(row["mark_price"]),
                float(row["stop_loss"]), float(row["take_profit"]),
            )
            receipt = await connector.close_position(p, amount)
        direction = 1 if row["side"] == "buy" else -1
        realized = (receipt.average_price - float(row["entry_price"])) * amount * direction - receipt.fee_usd
        await self.store.record_trade({
            "client_order_id": "partial:" + receipt.order_id, "venue": receipt.venue,
            "symbol": receipt.symbol, "market_type": row["market_type"],
            "side": receipt.side.value, "status": "partial_closed", "mode": self.mode,
            "quantity": receipt.quantity, "average_price": receipt.average_price,
            "entry_timestamp": row["opened_at"], "exit_timestamp": receipt.timestamp.isoformat(),
            "stop_loss": row["stop_loss"], "take_profit": row["take_profit"],
            "slippage_pct": receipt.slippage_pct, "commission_usd": receipt.fee_usd,
            "realized_pnl_usd": realized,
            "metadata": {"exit_reason": "partial_take_profit_1_5R", "order_id": receipt.order_id},
        })
        row["realized_pnl_usd"] = float(row.get("realized_pnl_usd", 0)) + realized
        row["quantity"] = float(row["quantity"]) - amount
        await self.store.event(
            "INFO", "partial_take_profit",
            "50% closed at 1.5R; partial fill and realized PnL saved.",
            row["venue"], row["symbol"], {"realized_pnl_usd": realized},
        )

    async def _close_position_row(self, row: dict[str, Any], connector, reason: str) -> None:
        p = OpenPosition(
            row["venue"], row["symbol"], MarketType(row["market_type"]), Side(row["side"]),
            float(row["quantity"]), float(row["entry_price"]), float(row["mark_price"]),
            float(row["stop_loss"]), float(row["take_profit"]),
            float(row.get("unrealized_pnl_usd", 0)), float(row.get("realized_pnl_usd", 0)),
        )
        if self.mode == "paper":
            snapshot = self.snapshots.get(row["position_key"])
            if snapshot is None:
                raise ConnectorUnavailable("No snapshot to price paper exit.")
            exit_side = Side.SELL if p.side is Side.BUY else Side.BUY
            intent = TradeIntent(
                p.venue, p.symbol, p.market_type, exit_side,
                snapshot.bid if exit_side is Side.SELL else snapshot.ask,
                p.stop_loss, p.take_profit, abs(p.mark_price-p.stop_loss), reason, {},
            )
            receipt = await self.paper.execute(intent, p.quantity, snapshot, "exit-" + uuid.uuid4().hex)
            await self.store.set_control("paper_cash", str(self.paper.cash))
        else:
            receipt = await connector.close_position(p)
        direction = 1 if p.side is Side.BUY else -1
        pnl = (receipt.average_price - p.entry_price) * receipt.quantity * direction - receipt.fee_usd
        await self.store.record_trade({
            "client_order_id": "exit:" + receipt.order_id, "venue": receipt.venue,
            "symbol": receipt.symbol, "market_type": row["market_type"],
            "side": receipt.side.value, "status": "closed", "mode": self.mode,
            "quantity": receipt.quantity, "average_price": receipt.average_price,
            "entry_timestamp": row["opened_at"], "exit_timestamp": receipt.timestamp.isoformat(),
            "stop_loss": row["stop_loss"], "take_profit": row["take_profit"],
            "slippage_pct": receipt.slippage_pct, "commission_usd": receipt.fee_usd,
            "realized_pnl_usd": pnl, "metadata": {"exit_reason": reason, "order_id": receipt.order_id},
        })
        await self.store.remove_position(row["position_key"])
        message = f"9N8 EXIT {receipt.symbol} · {reason}\nRealized PnL: {pnl:+.4f} USD"
        await self.store.event("INFO", "trade_exit", message, receipt.venue, receipt.symbol, {"pnl": pnl})
        if self.notifier:
            await self.notifier.send(message)

    async def _flatten_all(self) -> None:
        async with self._flatten_lock:
            await self._flatten_all_unlocked()

    async def _flatten_all_unlocked(self) -> None:
        errors = []
        for row in await self.store.positions():
            try:
                spec = next((s for s in self.specs if s["venue"] == row["venue"] and s["symbol"] == row["symbol"]), None)
                if not spec:
                    raise TradingError("No connector for position.")
                connector = self.connectors[spec["connector"]]
                await connector.cancel_all_orders(row["symbol"])
                snapshot = await connector.fetch_snapshot(row["symbol"])
                self.snapshots[row["position_key"]] = snapshot
                row["mark_price"] = snapshot.bid if row["side"] == "buy" else snapshot.ask
                await self._close_position_row(row, connector, "emergency_flatten")
            except Exception as exc:
                log.exception("Emergency flatten failed for %s %s", row["venue"], row["symbol"])
                errors.append(f"{row['venue']} {row['symbol']}: {str(exc)[:160]}")
        if errors:
            await self.store.set_control("flatten_errors", "\n".join(errors))
            raise TradingError("Emergency flatten incomplete; manual account inspection required.")

    async def panic(self) -> None:
        async with self._panic_lock:
            await self.risk.pause()
            await self.store.set_control("kill_switch", "true")
            await self.store.event("CRITICAL", "panic", "Remote panic command received.")
            await self._flatten_all()
            await self.store.set_control("flatten_errors", "")
            self.stop_event.set()
            if self.notifier:
                await self.notifier.send("PANIC complete: known positions flattened; engine shutting down.")

    async def status_text(self) -> str:
        positions = await self.store.positions()
        paused, reason = await self.risk.is_paused()
        lines = ["9N8 MULTI-ASSET STATUS", "Mode: " + self.mode.upper(), "Paused: " + str(paused)]
        if reason:
            lines.append("Reason: " + reason)
        if await self.store.get_control("equity_monitor_failed", "false") == "true":
            lines.append("WARNING: equity monitor failed; new live entries should be stopped.")
        lines.append("")
        lines.append("ACCOUNTS")
        if self.mode == "paper":
            account = await self.paper.account(self.snapshots)
            lines.append("paper: equity %.2f USD · cash %.2f USD" % (account.equity_usd, account.free_usd))
        else:
            for key, connector in self.connectors.items():
                try:
                    a = await connector.get_balance()
                    lines.append("%s: equity %.2f USD · free %.2f USD" % (key, a.equity_usd, a.free_usd))
                except Exception as exc:
                    lines.append(key + ": unavailable (" + str(exc)[:100] + ")")
        lines += ["", "OPEN POSITIONS"]
        if positions:
            for p in positions:
                lines.append(
                    "%s %s qty=%.6g mark=%.8g PnL=%+.4f"
                    % (p["symbol"], p["side"], p["quantity"], p["mark_price"], p["unrealized_pnl_usd"])
                )
        else:
            lines.append("None")
        return "\n".join(lines)
