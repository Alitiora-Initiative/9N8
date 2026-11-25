from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
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
            c = OandaConnector(
                env,
                live_certified=bool(self.cfg["execution"].get("live_execution_certified", False)),
            )
            await c.connect()
            self.connectors["oanda"] = c
            for symbol in o.get("instruments", []):
                mt = MarketType.COMMODITY if symbol.startswith(
                    ("XAU", "XAG", "US30", "NAS", "SPX", "UK100", "DE", "JP225")
                ) else MarketType.FOREX
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

    @classmethod
    def _only_closed_candles(cls, candles: list, timeframe: str) -> list:
        interval_ms = cls._timeframe_ms(timeframe)
        if interval_ms <= 0:
            return []
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        closed = [
            candle for candle in candles
            if isinstance(candle.timestamp, (int, float))
            and int(candle.timestamp) + interval_ms <= now_ms
        ]
        return sorted(closed, key=lambda candle: candle.timestamp)

    async def _market_loop(self, spec: dict[str, Any]) -> None:
        connector = self.connectors[spec["connector"]]
        delay = 1.0
        last_candle_fetch = 0.0
        last_evaluated_candle = None
        cached_trend = []
        cached_entry = []
        candle_refresh = max(
            5.0, float(self.cfg["execution"].get("candle_refresh_seconds", 15))
        )
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

                loop_now = asyncio.get_running_loop().time()
                if loop_now - last_candle_fetch >= candle_refresh or not cached_entry:
                    trend_raw, entry_raw = await asyncio.gather(
                        connector.fetch_candles(spec["symbol"], spec["trend_tf"], 250),
                        connector.fetch_candles(spec["symbol"], spec["entry_tf"], 120),
                    )
                    cached_trend = self._only_closed_candles(trend_raw, spec["trend_tf"])
                    cached_entry = self._only_closed_candles(entry_raw, spec["entry_tf"])
                    last_candle_fetch = loop_now

                if cached_entry and cached_trend:
                    candle_time = cached_entry[-1].timestamp
                    if candle_time != last_evaluated_candle:
                        last_evaluated_candle = candle_time
                        intent = self.trend_strategy.evaluate(
                            spec["venue"], spec["symbol"], spec["market_type"],
                            cached_trend, cached_entry,
                        )
                        if intent is not None:
                            await self._consider_entry(
                                spec, connector, intent, snapshot, candle_time
                            )
                delay = 1.0
                await asyncio.sleep(
                    float(self.cfg["execution"].get("price_poll_seconds", 1.0))
                )
            except asyncio.CancelledError:
                raise
            except RiskRejected as exc:
                await self.store.event(
                    "WARNING", "risk_veto", str(exc), spec["venue"], spec["symbol"]
                )
                await asyncio.sleep(2)
            except Exception as exc:
                log.exception("Market loop error for %s %s", spec["venue"], spec["symbol"])
                await self.store.event(
                    "ERROR", "market_loop_error", str(exc), spec["venue"], spec["symbol"]
                )
                if self.notifier:
                    try:
                        await self.notifier.send(
                            "9N8 feed error: " + spec["venue"] + " "
                            + spec["symbol"] + ": " + str(exc)[:180]
                        )
                    except Exception:
                        log.warning("Could not deliver market-feed error notification.")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    def _forex_spread_pips(self, snapshot: MarketSnapshot) -> float:
        pip = 0.01 if "JPY" in snapshot.symbol.upper() else 0.0001
        return (snapshot.ask - snapshot.bid) / pip

    async def _consider_entry(
        self, spec: dict[str, Any], connector, intent: TradeIntent,
        snapshot: MarketSnapshot, candle_timestamp: int,
    ) -> None:
        """Create a short-lived Telegram approval proposal; never open on signal alone."""
        await self.risk.assert_entry_allowed()
        key = self._key(intent.venue, intent.symbol)
        open_rows = await self.store.positions()
        if any(row["position_key"] == key for row in open_rows):
            return
        if intent.market_type in {MarketType.SOLANA_DEX, MarketType.EVM_DEX}:
            raise RiskRejected(
                "DEX entries remain data-only until independent transaction simulation "
                "and durable position reconciliation are complete."
            )

        quote_age = (datetime.now(timezone.utc) - snapshot.timestamp).total_seconds()
        max_quote_age = max(
            1.0, min(30.0, float(self.cfg["execution"].get("max_quote_age_seconds", 5)))
        )
        if quote_age < -2 or quote_age > max_quote_age:
            raise RiskRejected(
                f"Market quote is stale or has an invalid timestamp ({quote_age:.2f}s old)."
            )

        account = await self._portfolio_account()
        exposure = sum(
            float(row["quantity"]) * float(row["mark_price"]) for row in open_rows
        )
        spread_slippage = max(snapshot.spread_pct / 2, 0.0)
        leverage = float(spec.get("leverage", 1))
        contract_multiplier = (
            connector.contract_multiplier(intent.symbol)
            if hasattr(connector, "contract_multiplier") else 1.0
        )
        spread_pips = (
            self._forex_spread_pips(snapshot)
            if intent.market_type is MarketType.FOREX else None
        )
        signal_entry = float(intent.entry_price)
        stop_distance = abs(signal_entry - float(intent.stop_loss))
        reward_distance = abs(float(intent.take_profit) - signal_entry)
        executable_entry = snapshot.ask if intent.side is Side.BUY else snapshot.bid
        if (
            not all(math.isfinite(value) for value in (
                signal_entry, stop_distance, reward_distance, executable_entry
            ))
            or signal_entry <= 0 or stop_distance <= 0
            or reward_distance <= 0 or executable_entry <= 0
        ):
            raise RiskRejected("The strategy signal contains invalid prices or risk distances.")

        if intent.side is Side.BUY:
            rebased_stop = executable_entry - stop_distance
            rebased_target = executable_entry + reward_distance
        else:
            rebased_stop = executable_entry + stop_distance
            rebased_target = executable_entry - reward_distance
        proposed = TradeIntent(
            intent.venue, intent.symbol, intent.market_type, intent.side,
            executable_entry, rebased_stop, rebased_target, stop_distance,
            intent.reason,
            {**intent.metadata, "signal_candle_ms": candle_timestamp},
        )
        if proposed.market_type in {MarketType.CRYPTO_SPOT, MarketType.CRYPTO_PERP}:
            minimum_volume = float(
                os.getenv(
                    "MINIMUM_24H_QUOTE_VOLUME_USD",
                    self.cfg["execution"].get("minimum_24h_quote_volume_usd", 100000),
                )
            )
            quote_volume = snapshot.quote_volume_24h_usd
            if (
                not math.isfinite(minimum_volume)
                or minimum_volume < 0
                or quote_volume is None
                or not math.isfinite(float(quote_volume))
                or float(quote_volume) < minimum_volume
            ):
                raise RiskRejected(
                    f"24h quote-volume filter failed for {proposed.symbol}: "
                    f"reported={quote_volume!r}, required={minimum_volume:,.0f} USD."
                )

        decision = await self.risk.evaluate(
            proposed, account, snapshot, exposure, spread_slippage,
            leverage=leverage, forex_spread_pips=spread_pips,
            contract_multiplier=contract_multiplier,
            current_open_positions=len(open_rows),
        )
        if not decision.approved or not decision.size:
            await self.store.event(
                "WARNING", "trade_veto", decision.reason, proposed.venue, proposed.symbol
            )
            return
        if hasattr(connector, "estimate_slippage_pct"):
            order_impact = await connector.estimate_slippage_pct(
                proposed.symbol, proposed.side, decision.size.quantity
            )
            limit = self.risk.slippage_limit_pct(proposed.market_type.value)
            if not math.isfinite(order_impact) or order_impact > limit:
                reason = (
                    f"Order-book impact {order_impact:.4%} exceeds "
                    f"the hard {limit:.4%} limit."
                )
                await self.store.event(
                    "WARNING", "market_impact_veto", reason,
                    proposed.venue, proposed.symbol,
                )
                return

        signal_key = (
            f"{proposed.venue}:{proposed.symbol}:"
            f"{proposed.side.value}:{candle_timestamp}"
        )
        approval_id = uuid.uuid4().hex[:12].upper()
        timeout_seconds = max(
            15, min(600, int(self.cfg["execution"].get("approval_timeout_seconds", 120)))
        )
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
        payload = {
            "intent": {
                "venue": proposed.venue,
                "symbol": proposed.symbol,
                "market_type": proposed.market_type.value,
                "side": proposed.side.value,
                "entry_price": proposed.entry_price,
                "stop_loss": proposed.stop_loss,
                "take_profit": proposed.take_profit,
                "risk_distance": proposed.risk_distance,
                "reason": proposed.reason,
                "metadata": proposed.metadata,
            },
            "size": {
                "quantity": decision.size.quantity,
                "notional_usd": decision.size.notional_usd,
                "risk_usd": decision.size.risk_usd,
            },
            "spec": {
                "connector": spec["connector"],
                "venue": spec["venue"],
                "symbol": spec["symbol"],
                "market_type": spec["market_type"].value,
                "entry_tf": spec.get("entry_tf"),
                "trend_tf": spec.get("trend_tf"),
                "leverage": leverage,
            },
            "mode": self.mode,
            "signal_key": signal_key,
            "signal_candle_ms": candle_timestamp,
            "reference_price": executable_entry,
            "risk_reason": decision.reason,
        }
        created = await self.store.create_pending_approval(
            approval_id, signal_key, expires_at.isoformat(), payload
        )
        if not created:
            return

        preview = (
            "9N8 TRADE APPROVAL REQUIRED\n"
            f"{proposed.side.value.upper()} {proposed.symbol}\n"
            f"Venue: {proposed.venue} · mode: {self.mode.upper()}\n"
            f"Reference entry: {proposed.entry_price:.10g}\n"
            f"Estimated quantity: {decision.size.quantity:.8g}\n"
            f"Notional: {decision.size.notional_usd:.2f} USD · "
            f"planned stop risk: {decision.size.risk_usd:.2f} USD\n"
            f"Stop loss: {proposed.stop_loss:.10g}\n"
            f"Take profit: {proposed.take_profit:.10g}\n"
            f"Strategy: {proposed.reason}\n"
            f"Approval expires in {timeout_seconds}s. "
            "The quote and risk limits are checked again after approval.\n"
            "No order has been placed."
        )
        await self.store.event(
            "INFO", "trade_approval_requested", preview,
            proposed.venue, proposed.symbol,
            {"approval_id": approval_id, "signal_key": signal_key},
        )
        if self.notifier:
            send_approval = getattr(self.notifier, "send_approval", None)
            if send_approval:
                await send_approval(approval_id, preview)
            else:
                await self.notifier.send(
                    preview + f"\nApproval ID: {approval_id}"
                )

    async def approve_pending(self, approval_id: str, approved_by: str) -> str:
        """Re-price and re-run all risk checks immediately before the confirmed order."""
        pending = await self.store.get_pending_approval(approval_id)
        if not pending:
            raise TradingError("Trade approval not found.")
        if pending["status"] != "PENDING":
            raise TradingError(f"This proposal is already {pending['status'].lower()}.")
        now = datetime.now(timezone.utc)
        expires = datetime.fromisoformat(pending["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if now >= expires:
            await self.store.decide_pending_approval(
                approval_id, "EXPIRED", approved_by, "Approval timeout elapsed; no order was sent."
            )
            raise TradingError("This trade proposal has expired. Wait for a fresh signal.")
        if not await self.store.claim_pending_approval(approval_id):
            raise TradingError("This trade proposal is already being processed or has been decided.")

        signal_key = pending["signal_key"]
        submission_started = False
        execution_returned = False
        approval_finalized = False
        message = ""
        try:
            await self.risk.assert_entry_allowed()
            payload = pending["payload"]
            raw = payload["intent"]
            intent = TradeIntent(
                raw["venue"], raw["symbol"], MarketType(raw["market_type"]),
                Side(raw["side"]), float(raw["entry_price"]), float(raw["stop_loss"]),
                float(raw["take_profit"]), float(raw["risk_distance"]),
                str(raw["reason"]), dict(raw.get("metadata") or {}),
            )
            spec = payload["spec"]
            connector_key = str(spec["connector"])
            connector = self.connectors.get(connector_key)
            if connector is None:
                raise ConnectorUnavailable("The proposal's market connector is no longer connected.")
            snapshot = await connector.fetch_snapshot(intent.symbol)
            old_reference = float(payload["reference_price"])
            current_entry = snapshot.ask if intent.side is Side.BUY else snapshot.bid
            if old_reference <= 0 or current_entry <= 0:
                raise RiskRejected("A valid fresh bid/ask quote is required.")
            drift_pct = abs(current_entry - old_reference) / old_reference * 100.0
            max_drift_pct = max(
                0.01,
                min(5.0, float(self.cfg["execution"].get("approval_max_price_drift_pct", 0.50))),
            )
            if drift_pct > max_drift_pct:
                raise RiskRejected(
                    f"Price moved {drift_pct:.3f}% since the proposal; limit is {max_drift_pct:.3f}%. Request a new signal."
                )

            stop_distance = abs(intent.entry_price - intent.stop_loss)
            reward_distance = abs(intent.take_profit - intent.entry_price)
            if intent.side is Side.BUY:
                stop = current_entry - stop_distance
                target = current_entry + reward_distance
            else:
                stop = current_entry + stop_distance
                target = current_entry - reward_distance
            intent = TradeIntent(
                intent.venue, intent.symbol, intent.market_type, intent.side,
                current_entry, stop, target, stop_distance, intent.reason,
                {**intent.metadata, "approved_by": str(approved_by)[:100], "approval_id": approval_id},
            )

            open_rows = await self.store.positions()
            position_key = self._key(intent.venue, intent.symbol)
            if any(row["position_key"] == position_key for row in open_rows):
                raise RiskRejected("A position is already open for this market.")
            account = await self._portfolio_account()
            exposure = sum(float(row["quantity"]) * float(row["mark_price"]) for row in open_rows)
            spread_slippage = max(snapshot.spread_pct / 2, 0)
            leverage = float(spec.get("leverage", 1))
            multiplier = (
                connector.contract_multiplier(intent.symbol)
                if hasattr(connector, "contract_multiplier") else 1.0
            )
            forex_spread = self._forex_spread_pips(snapshot) if intent.market_type is MarketType.FOREX else None
            decision = await self.risk.evaluate(
                intent, account, snapshot, exposure, spread_slippage,
                leverage=leverage, forex_spread_pips=forex_spread,
                contract_multiplier=multiplier, current_open_positions=len(open_rows),
            )
            if not decision.approved or not decision.size:
                raise RiskRejected(decision.reason)
            if hasattr(connector, "estimate_slippage_pct"):
                impact = await connector.estimate_slippage_pct(
                    intent.symbol, intent.side, decision.size.quantity
                )
                limit = self.risk.slippage_limit_pct(intent.market_type.value)
                if not math.isfinite(impact) or impact > limit:
                    raise RiskRejected(
                        f"Fresh order-book impact {impact:.4%} exceeds the {limit:.4%} hard limit."
                    )

            if not await self.store.claim_idempotency(signal_key, {
                "venue": intent.venue, "symbol": intent.symbol, "side": intent.side.value,
                "approval_id": approval_id,
            }):
                raise TradingError("This signal already has an execution record.")
            client_id = hashlib.sha256(signal_key.encode()).hexdigest()[:32]
            submission_started = True
            if self.mode == "paper":
                receipt = await self.paper.execute(intent, decision.size.quantity, snapshot, client_id)
            elif intent.side is Side.BUY:
                receipt = await connector.buy(intent, decision.size.quantity, client_id)
            else:
                receipt = await connector.sell(intent, decision.size.quantity, client_id)
            execution_returned = True
            if receipt.quantity <= 0 or receipt.average_price <= 0:
                raise ConnectorUnavailable("Provider returned no confirmed fill; manual reconciliation is required.")

            trade_record = {
                "client_order_id": signal_key, "venue": receipt.venue, "symbol": receipt.symbol,
                "market_type": intent.market_type.value, "side": receipt.side.value,
                "status": "open", "mode": self.mode, "quantity": receipt.quantity,
                "average_price": receipt.average_price, "entry_timestamp": receipt.timestamp.isoformat(),
                "stop_loss": intent.stop_loss, "take_profit": intent.take_profit,
                "slippage_pct": receipt.slippage_pct,
                "gas_fee_usd": float(receipt.raw.get("gas_fee_usd", 0)),
                "commission_usd": receipt.fee_usd, "metadata": receipt.raw,
            }
            position = {
                "position_key": position_key, "venue": intent.venue, "symbol": intent.symbol,
                "market_type": intent.market_type.value, "side": intent.side.value,
                "quantity": receipt.quantity, "entry_price": receipt.average_price,
                "mark_price": snapshot.last, "stop_loss": intent.stop_loss,
                "take_profit": intent.take_profit, "realized_pnl_usd": 0,
                "unrealized_pnl_usd": 0, "partial_closed": False,
                "breakeven_moved": False, "high_watermark": receipt.average_price,
                "low_watermark": receipt.average_price,
                "metadata": {
                    **intent.metadata, **receipt.raw, "order_id": receipt.order_id,
                    "tx_hash": receipt.tx_hash, "opened_at": receipt.timestamp.isoformat(),
                    "approval_id": approval_id,
                    "entry_fee_paid": float(receipt.fee_usd or 0),
                    "entry_fee_remaining": float(receipt.fee_usd or 0),
                    "paper_fees_paid": float(receipt.fee_usd or 0) if self.mode == "paper" else 0.0,
                },
                "opened_at": receipt.timestamp.isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if self.mode == "paper":
                await self._persist_paper_fill(trade_record, position=position)
            else:
                await self.store.record_trade(trade_record)
                await self.store.upsert_position(position)
            await self.store.update_idempotency(signal_key, "filled")
            message = (
                f"9N8 APPROVED ENTRY {receipt.side.value.upper()} {receipt.symbol}\n"
                f"Venue: {receipt.venue} · mode: {self.mode.upper()}\n"
                f"Quantity: {receipt.quantity:.8g} · fill: {receipt.average_price:.10g}\n"
                f"SL: {intent.stop_loss:.10g} · TP: {intent.take_profit:.10g}\n"
                f"Fee: {receipt.fee_usd:.6f} · slippage: {receipt.slippage_pct:.3%}\n"
                f"Approved by Telegram user {str(approved_by)[:32]}."
            )
            await self.store.decide_pending_approval(
                approval_id, "APPROVED", approved_by,
                f"Filled {receipt.quantity:.8g} at {receipt.average_price:.10g}.",
            )
            approval_finalized = True
            try:
                await self.store.event(
                    "INFO", "trade_entry", message, receipt.venue, receipt.symbol,
                    {"order_id": receipt.order_id, "tx_hash": receipt.tx_hash, "approval_id": approval_id},
                )
            except Exception as event_exc:
                log.error("Trade was recorded but trade event logging failed (%s).", type(event_exc).__name__)
            if self.notifier:
                try:
                    await self.notifier.send(message)
                except Exception as notify_exc:
                    log.warning("Trade fill notification failed (%s).", type(notify_exc).__name__)
            return message
        except Exception as exc:
            if approval_finalized:
                # The fill and approval are already committed. Do not reverse the
                # successful state because a later notification/logging step failed.
                log.error("Post-fill ancillary operation failed (%s).", type(exc).__name__)
                return message or (
                    f"9N8 trade {approval_id} was approved and recorded. "
                    "Check the positions dashboard for the confirmed fill."
                )
            if self.mode == "paper" and execution_returned:
                try:
                    durable_positions = await self.store.positions()
                    durable_trades = await self.store.query_trades(100)
                    has_durable_fill = any(
                        (row.get("metadata") or {}).get("approval_id") == approval_id
                        for row in durable_positions
                    ) or any(row.get("client_order_id") == signal_key for row in durable_trades)
                    if has_durable_fill:
                        await self.store.decide_pending_approval(
                            approval_id, "APPROVED", approved_by,
                            "Paper fill was durably committed and recovered from SQLite.",
                        )
                        await self.store.update_idempotency(signal_key, "filled")
                        return (
                            f"9N8 PAPER ENTRY RECOVERED\n{intent.symbol}\n"
                            "The simulated fill and virtual balance were committed. "
                            "Telegram received a late acknowledgement, but no live order was submitted."
                        )
                    await self.store.decide_pending_approval(
                        approval_id, "FAILED", approved_by,
                        "Paper fill was not durably committed; in-memory state was restored. No exchange order was submitted.",
                    )
                    await self.store.update_idempotency(signal_key, "failed")
                    raise TradingError(
                        "Paper entry was not durably committed. The simulator was restored from SQLite; no exchange order was submitted."
                    ) from exc
                except TradingError:
                    raise
                except Exception as paper_state_exc:
                    try:
                        await self.risk.pause()
                    except Exception:
                        pass
                    log.critical(
                        "Could not reconcile a paper approval after a storage error (%s).",
                        type(paper_state_exc).__name__,
                    )
                    raise TradingError(
                        "Paper storage state could not be verified. New entries are paused; inspect /paper before resuming."
                    ) from exc
            if execution_returned or (submission_started and self.mode != "paper"):
                # Once a live/testnet broadcast starts, a network error can leave the
                # actual provider result ambiguous. Never relabel it as a clean failure
                # and never retry it automatically.
                reconciliation = (
                    "Execution outcome may be unknown. Inspect provider open orders, "
                    "fills, wallet balances and transaction history manually before any retry. "
                    "9N8 has halted new entries."
                )
                try:
                    await self.store.decide_pending_approval(
                        approval_id, "RECONCILIATION_REQUIRED", approved_by,
                        f"{type(exc).__name__}: {str(exc)[:700]}",
                    )
                    await self.store.set_control("kill_switch", "true")
                    await self.store.set_control("execution_reconciliation_required", "true")
                    await self.risk.pause()
                    await self.store.event(
                        "CRITICAL", "execution_reconciliation_required",
                        reconciliation, intent.venue, intent.symbol,
                        {"approval_id": approval_id, "signal_key": signal_key},
                    )
                except Exception as state_exc:
                    log.critical(
                        "Could not persist uncertain execution state (%s). Manual provider reconciliation is mandatory.",
                        type(state_exc).__name__,
                    )
                if self.notifier:
                    try:
                        await self.notifier.send("9N8 EXECUTION WARNING\n" + reconciliation)
                    except Exception:
                        log.error("Could not send execution reconciliation alert.")
                raise TradingError(reconciliation) from exc
            try:
                await self.store.decide_pending_approval(
                    approval_id, "FAILED", approved_by, str(exc)[:1000]
                )
                await self.store.update_idempotency(signal_key, "failed")
            except Exception as state_exc:
                log.error("Could not persist failed approval state (%s).", type(state_exc).__name__)
            raise

    async def deny_pending(self, approval_id: str, denied_by: str) -> str:
        pending = await self.store.get_pending_approval(approval_id)
        if not pending:
            raise TradingError("Trade approval not found.")
        if pending["status"] != "PENDING":
            raise TradingError(f"This proposal is already {pending['status'].lower()}.")
        now = datetime.now(timezone.utc)
        expires = datetime.fromisoformat(pending["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if now >= expires:
            await self.store.decide_pending_approval(
                approval_id, "EXPIRED", denied_by, "Approval timeout elapsed; no order was sent."
            )
            raise TradingError("This trade proposal has expired.")
        if not await self.store.claim_pending_approval(approval_id):
            raise TradingError("This trade proposal is already being processed or has been decided.")
        message = f"Trade proposal {approval_id} denied by Telegram user {str(denied_by)[:32]}. No order was sent."
        await self.store.decide_pending_approval(approval_id, "DENIED", denied_by, message)
        await self.store.event("INFO", "trade_approval_denied", message)
        return message

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

    async def _maybe_daily_summary(self) -> None:
        now = datetime.now(timezone.utc)
        today = now.date()
        report_key = today.isoformat()
        if await self.store.get_control("daily_summary_sent_for", "") == report_key:
            return
        yesterday_start = datetime.combine(
            today.fromordinal(today.toordinal() - 1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        today_start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        trades = await self.store.closed_trades_between(
            yesterday_start.isoformat(), today_start.isoformat()
        )
        realized = sum(float(row.get("realized_pnl_usd") or 0) for row in trades)
        wins = sum(1 for row in trades if float(row.get("realized_pnl_usd") or 0) > 0)
        losses = sum(1 for row in trades if float(row.get("realized_pnl_usd") or 0) < 0)
        summary = (
            "9N8 DAILY PNL SUMMARY (UTC)\n"
            + yesterday_start.date().isoformat()
            + "\nClosed/partial exits: " + str(len(trades))
            + "\nWins: " + str(wins) + " · Losses: " + str(losses)
            + "\nRealized PnL: " + format(realized, "+.4f") + " USD"
        )
        await self.store.event(
            "INFO", "daily_pnl_summary", summary,
            metadata={"date_utc": yesterday_start.date().isoformat(),
                      "closed_exits": len(trades), "wins": wins,
                      "losses": losses, "realized_pnl_usd": realized},
        )
        if self.notifier:
            await self.notifier.send(summary)
        await self.store.set_control("daily_summary_sent_for", report_key)

    async def _equity_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                account = await self._portfolio_account()
                tripped = await self.risk.observe_equity(account)
                if tripped and self.notifier:
                    await self.notifier.send("9N8 DAILY DRAWDOWN: emergency flatten requested; trading halted for 24 hours.")
                await self.store.set_control("equity_monitor_failed", "false")
                await self._maybe_daily_summary()
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
                        exit_reason = "stop_loss" if stop_hit else "take_profit"
                        if self.mode == "paper":
                            await self._close_position_row(row, connector, exit_reason)
                        else:
                            # Live/testnet entries already carry native venue-side
                            # stop/TP instructions. Do not submit a second local
                            # market close at the same instant: that can race the
                            # bracket fill and create a reversed/oversized order.
                            marker = "native_bracket_breach:" + hashlib.sha256(
                                str(row["position_key"]).encode()
                            ).hexdigest()[:24]
                            if await self.store.get_control(marker, "false") != "true":
                                await self.store.set_control(marker, "true")
                                await self.store.set_control(
                                    "execution_reconciliation_required", "true"
                                )
                                await self.store.set_control("kill_switch", "true")
                                await self.risk.pause()
                                message = (
                                    "9N8 LIVE EXIT RECONCILIATION REQUIRED\n"
                                    f"{row['venue']} · {row['symbol']} · {exit_reason.upper()} threshold observed.\n"
                                    "The venue-side protective bracket was submitted with the entry. "
                                    "9N8 did not send a duplicate market close. Inspect the provider's "
                                    "open orders, fills and current position now; the bot remains paused "
                                    "until the position is reconciled."
                                )
                                await self.store.event(
                                    "CRITICAL", "native_bracket_breach_reconciliation",
                                    message, row["venue"], row["symbol"],
                                    {"position_key": row["position_key"], "reason": exit_reason},
                                )
                                if self.notifier:
                                    try:
                                        await self.notifier.send(message)
                                    except Exception:
                                        log.error("Could not deliver native bracket reconciliation alert.")
                            await self.store.upsert_position(row)
                        continue
                    if self.mode == "paper":
                        risk_per_unit = abs(float(row["entry_price"]) - stop)
                        favourable = (mark - float(row["entry_price"])) * direction
                        entry_price = max(float(row["entry_price"]), 1e-12)
                        pnl_pct = ((mark - entry_price) / entry_price) * 100.0 * direction
                        try:
                            opened_at = datetime.fromisoformat(str(row["opened_at"]))
                            if opened_at.tzinfo is None:
                                opened_at = opened_at.replace(tzinfo=timezone.utc)
                            elapsed_minutes = (
                                datetime.now(timezone.utc) - opened_at
                            ).total_seconds() / 60.0
                        except (TypeError, ValueError):
                            elapsed_minutes = 0.0
                        max_hold_minutes = max(
                            1,
                            min(
                                10080,
                                int(os.getenv(
                                    "PAPER_MAX_HOLD_MINUTES",
                                    self.cfg["execution"].get("paper_max_hold_minutes", 240),
                                )),
                            ),
                        )
                        stagnation_profit_pct = float(os.getenv(
                            "PAPER_MIN_STAGNATION_PROFIT_PCT",
                            self.cfg["execution"].get("paper_min_stagnation_profit_pct", 1.0),
                        ))
                        if (
                            elapsed_minutes >= max_hold_minutes
                            and pnl_pct < stagnation_profit_pct
                        ):
                            await self._close_position_row(row, connector, "time_kill_switch")
                            continue
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

    async def _persist_paper_fill(
        self,
        trade_record: dict[str, Any],
        *,
        position: dict[str, Any] | None = None,
        remove_position_key: str | None = None,
    ) -> None:
        """Persist paper balance, fill and position atomically; restore memory on failure."""
        try:
            await self.store.persist_paper_fill(
                trade_record,
                self.paper.cash,
                position=position,
                remove_position_key=remove_position_key,
            )
        except Exception as exc:
            try:
                persisted_cash = float(
                    await self.store.get_control(
                        "paper_cash", str(self.paper.starting_equity)
                    )
                )
                persisted_positions = await self.store.positions()
                await self.paper.restore_state(persisted_cash, persisted_positions)
            except Exception as restore_exc:
                log.critical(
                    "Paper persistence failed and in-memory restore also failed (%s).",
                    type(restore_exc).__name__,
                )
            try:
                await self.risk.pause()
                await self.store.event(
                    "CRITICAL",
                    "paper_persistence_failure",
                    "A simulated fill could not be persisted atomically. "
                    "Paper state was reloaded from the last durable snapshot; new entries are paused.",
                    trade_record.get("venue"),
                    trade_record.get("symbol"),
                    {"client_order_id": trade_record.get("client_order_id")},
                )
            except Exception as state_exc:
                log.error("Could not record paper persistence pause (%s).", type(state_exc).__name__)
            if self.notifier:
                try:
                    await self.notifier.send(
                        "9N8 PAPER STORAGE WARNING\n"
                        "A simulated fill could not be durably saved. The in-memory paper ledger "
                        "was restored from the last saved snapshot and new entries were paused. "
                        "Check /paper and /status before resuming."
                    )
                except Exception:
                    log.warning("Could not send paper persistence warning to Telegram.")
            raise TradingError(
                "Paper fill persistence failed. The in-memory ledger was restored from its last saved state; new entries are paused."
            ) from exc

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
        else:
            p = OpenPosition(
                row["venue"], row["symbol"], MarketType(row["market_type"]), Side(row["side"]),
                float(row["quantity"]), float(row["entry_price"]), float(row["mark_price"]),
                float(row["stop_loss"]), float(row["take_profit"]),
            )
            receipt = await connector.close_position(p, amount)
        direction = 1 if row["side"] == "buy" else -1
        gross_pnl = (
            receipt.average_price - float(row["entry_price"])
        ) * amount * direction
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        if self.mode == "paper":
            entry_fee_allocated = float(receipt.raw.get("entry_fee_allocated", 0.0) or 0.0)
            entry_fee_remaining = float(receipt.raw.get("remaining_entry_fee", 0.0) or 0.0)
        else:
            current_qty = max(float(row["quantity"]), 1e-12)
            remaining_entry_fee = float(
                metadata.get("entry_fee_remaining", metadata.get("entry_fee_paid", 0.0)) or 0.0
            )
            entry_fee_allocated = remaining_entry_fee * min(1.0, amount / current_qty)
            entry_fee_remaining = max(0.0, remaining_entry_fee - entry_fee_allocated)
        realized = gross_pnl - receipt.fee_usd - entry_fee_allocated
        metadata["entry_fee_remaining"] = entry_fee_remaining
        if self.mode == "paper":
            metadata["paper_fees_paid"] = entry_fee_remaining
        row["metadata"] = metadata
        trade_record = {
            "client_order_id": "partial:" + receipt.order_id, "venue": receipt.venue,
            "symbol": receipt.symbol, "market_type": row["market_type"],
            "side": receipt.side.value, "status": "partial_closed", "mode": self.mode,
            "quantity": receipt.quantity, "average_price": receipt.average_price,
            "entry_timestamp": row["opened_at"], "exit_timestamp": receipt.timestamp.isoformat(),
            "stop_loss": row["stop_loss"], "take_profit": row["take_profit"],
            "slippage_pct": receipt.slippage_pct,
            "commission_usd": receipt.fee_usd,
            "realized_pnl_usd": realized,
            "metadata": {
                "exit_reason": "partial_take_profit_1_5R",
                "order_id": receipt.order_id,
                "gross_pnl_usd": gross_pnl,
                "entry_fee_allocated_usd": entry_fee_allocated,
                "exit_fee_usd": receipt.fee_usd,
            },
        }
        row["realized_pnl_usd"] = float(row.get("realized_pnl_usd", 0)) + realized
        row["quantity"] = float(row["quantity"]) - amount
        if self.mode == "paper":
            await self._persist_paper_fill(trade_record, position=row)
        else:
            await self.store.record_trade(trade_record)
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
        else:
            receipt = await connector.close_position(p)
        direction = 1 if p.side is Side.BUY else -1
        gross_pnl = (
            receipt.average_price - p.entry_price
        ) * receipt.quantity * direction
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        if self.mode == "paper":
            entry_fee_allocated = float(receipt.raw.get("entry_fee_allocated", 0.0) or 0.0)
        else:
            entry_fee_allocated = float(
                metadata.get("entry_fee_remaining", metadata.get("entry_fee_paid", 0.0)) or 0.0
            )
        pnl = gross_pnl - receipt.fee_usd - entry_fee_allocated
        trade_record = {
            "client_order_id": "exit:" + receipt.order_id, "venue": receipt.venue,
            "symbol": receipt.symbol, "market_type": row["market_type"],
            "side": receipt.side.value, "status": "closed", "mode": self.mode,
            "quantity": receipt.quantity, "average_price": receipt.average_price,
            "entry_timestamp": row["opened_at"], "exit_timestamp": receipt.timestamp.isoformat(),
            "stop_loss": row["stop_loss"], "take_profit": row["take_profit"],
            "slippage_pct": receipt.slippage_pct,
            "commission_usd": receipt.fee_usd + entry_fee_allocated,
            "realized_pnl_usd": pnl,
            "metadata": {
                "exit_reason": reason,
                "order_id": receipt.order_id,
                "gross_pnl_usd": gross_pnl,
                "entry_fee_allocated_usd": entry_fee_allocated,
                "exit_fee_usd": receipt.fee_usd,
            },
        }
        if self.mode == "paper":
            await self._persist_paper_fill(
                trade_record,
                remove_position_key=row["position_key"],
            )
        else:
            await self.store.record_trade(trade_record)
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
                if self.mode != "paper":
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
            # Keep market/position monitoring and Telegram online. The kill switch
            # remains active, so no new entry can be approved until the owner resumes.
            if self.notifier:
                await self.notifier.send(
                    "PANIC complete: known positions were processed and new entries remain locked. "
                    "9N8 monitoring and Telegram controls are still online."
                )

    async def paper_status_text(self) -> str:
        if self.mode != "paper":
            return (
                "9N8 PAPER ACCOUNT\n"
                f"Current mode: {self.mode.upper()}\n"
                "Paper controls are read-only unless TRADING_MODE=paper."
            )
        account = await self.paper.account(self.snapshots)
        positions = await self.store.positions()
        now = datetime.now(timezone.utc)
        start = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
        trades = await self.store.closed_trades_between(
            start.isoformat(), now.isoformat()
        )
        realized = sum(float(row.get("realized_pnl_usd") or 0) for row in trades)
        unrealized = sum(
            float(row.get("unrealized_pnl_usd") or 0) for row in positions
        )
        daily_start = await self.store.get_daily_equity(now.date().isoformat())
        drawdown = (
            max(0.0, (daily_start - account.equity_usd) / daily_start * 100.0)
            if daily_start and daily_start > 0 else 0.0
        )
        paused, reason = await self.risk.is_paused()
        state = "PAUSED" if paused else "RUNNING"
        lines = [
            "9N8 PAPER ACCOUNT",
            "Market feed: live provider quotes/candles",
            "Execution: simulated fills only; no exchange orders",
            "Engine: " + state,
            "Virtual equity: $" + format(account.equity_usd, ",.2f"),
            "Available paper cash: $" + format(account.free_usd, ",.2f"),
            "Total paper PnL: " + format(account.equity_usd - self.paper.starting_equity, "+,.2f") + " USD",
            "Realized PnL today (UTC): " + format(realized, "+,.2f") + " USD",
            "Open-position unrealized PnL (gross): " + format(unrealized, "+,.2f") + " USD",
            "Open positions: " + str(len(positions)),
            "Daily equity drawdown: {:.2f}% / {:.2f}%".format(
                drawdown, float(self.cfg["risk"].get("maximum_daily_drawdown_pct", 0.05)) * 100
            ),
            "Paper fee rate: {:.3f}%".format(self.paper.fee_rate * 100),
            "Paper slippage: {:.1f} bps".format(self.paper.slippage_bps),
        ]
        if reason:
            lines.append("Risk lock: " + reason[:300])
        if await self.store.get_control("execution_reconciliation_required", "false") == "true":
            lines.append("CRITICAL: provider reconciliation lock is active.")
        return "\n".join(lines)

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
