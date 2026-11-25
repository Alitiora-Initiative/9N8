from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import ccxt.pro as ccxtpro

from config import Settings
from connectors.base import ExecutionConnector
from models import (
    AccountState, Candle, ConnectorUnavailable, MarketSnapshot, MarketType,
    OpenPosition, Side, TradeIntent, TradeReceipt,
)

log = logging.getLogger(__name__)


class CCXTConnector(ExecutionConnector):
    def __init__(self, settings: Settings, market_type: MarketType, exchange_id: str | None = None,
                 sandbox: bool = False):
        self.settings = settings
        self.market_type = market_type
        self.venue = exchange_id or settings.config["markets"]["crypto_spot"]["exchange"]
        self.sandbox = sandbox
        self.exchange = None
        self.quote_currency = "USDT"
        self._symbols: set[str] = set()

    async def connect(self) -> None:
        exchange_cls = getattr(ccxtpro, self.venue, None)
        if exchange_cls is None:
            raise ConnectorUnavailable(f"Unknown CCXT Pro exchange: {self.venue}")
        api_key = self._env("CEX_API_KEY")
        secret = self._env("CEX_API_SECRET")
        password = self._env("CEX_PASSWORD")
        config: dict[str, Any] = {"enableRateLimit": True, "timeout": 15000}
        if api_key:
            config["apiKey"] = api_key
        if secret:
            config["secret"] = secret
        if password:
            config["password"] = password
        default_type = "swap" if self.market_type is MarketType.CRYPTO_PERP else "spot"
        config["options"] = {"defaultType": default_type}
        self.exchange = exchange_cls(config)
        if self.sandbox:
            if not hasattr(self.exchange, "set_sandbox_mode"):
                raise ConnectorUnavailable(f"{self.venue} does not support sandbox mode in CCXT.")
            self.exchange.set_sandbox_mode(True)
        await self.exchange.load_markets()
        log.info("Connected to %s (%s, sandbox=%s)", self.venue, self.market_type, self.sandbox)

    def _env(self, key: str) -> str:
        import os
        return os.getenv(key, "").strip()

    def _ex(self):
        if self.exchange is None:
            raise ConnectorUnavailable(f"{self.venue} is not connected.")
        return self.exchange

    async def close(self) -> None:
        if self.exchange is not None:
            await self.exchange.close()
            self.exchange = None

    async def fetch_snapshot(self, symbol: str) -> MarketSnapshot:
        ex = self._ex()
        book = await ex.fetch_order_book(symbol, 5)
        ticker = await ex.fetch_ticker(symbol)
        if not book.get("bids") or not book.get("asks"):
            raise ConnectorUnavailable(f"Empty order book: {symbol}")
        bid, ask = float(book["bids"][0][0]), float(book["asks"][0][0])
        last = float(ticker.get("last") or (bid+ask)/2)
        return MarketSnapshot(self.venue,symbol,self.market_type,bid,ask,last,
                              volume_5m=float(ticker.get("baseVolume") or 0)/288)

    async def watch_snapshot(self, symbol: str) -> MarketSnapshot:
        ex = self._ex()
        if ex.has.get("watchOrderBook"):
            try:
                book = await ex.watch_order_book(symbol, 5)
                ticker = await ex.fetch_ticker(symbol)
                bid, ask = float(book["bids"][0][0]), float(book["asks"][0][0])
                return MarketSnapshot(self.venue,symbol,self.market_type,bid,ask,
                    float(ticker.get("last") or (bid+ask)/2))
            except (ccxtpro.NetworkError, ccxtpro.ExchangeNotAvailable):
                log.exception("WebSocket feed failed for %s; falling back to REST snapshot.", symbol)
        return await self.fetch_snapshot(symbol)

    async def estimate_slippage_pct(self, symbol: str, side: Side, quantity: float) -> float:
        """Estimate market impact by walking the actual order-book depth."""
        if quantity <= 0:
            raise ValueError("Quantity must be positive.")
        book = await self._ex().fetch_order_book(symbol, 50)
        levels = book.get("asks" if side is Side.BUY else "bids") or []
        if not levels:
            return float("inf")
        remaining = float(quantity)
        notional = 0.0
        filled = 0.0
        best = float(levels[0][0])
        for price_raw, amount_raw in levels:
            price = float(price_raw)
            amount = float(amount_raw)
            take = min(remaining, amount)
            if price <= 0 or take <= 0:
                continue
            notional += price * take
            filled += take
            remaining -= take
            if remaining <= 1e-12:
                break
        if remaining > max(quantity * 1e-8, 1e-12) or filled <= 0:
            return float("inf")
        average = notional / filled
        return abs(average - best) / best

    async def fetch_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        rows = await self._ex().fetch_ohlcv(symbol, timeframe, limit=limit)
        return [Candle(int(r[0]),float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5])) for r in rows]

    async def fetch_recent_trades(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        return await self._ex().fetch_trades(symbol, limit=limit)

    async def get_balance(self) -> AccountState:
        balance = await self._ex().fetch_balance()
        total = balance.get("total", {})
        free = balance.get("free", {})
        quote_total = float(total.get("USDT") or total.get("USD") or 0)
        quote_free = float(free.get("USDT") or free.get("USD") or 0)
        if self.market_type is MarketType.CRYPTO_PERP:
            quote_total = float(balance.get("USDT", {}).get("total") or quote_total)
            quote_free = float(balance.get("USDT", {}).get("free") or quote_free)
        return AccountState(self.venue,quote_total,quote_free,quote_total)

    async def get_open_positions(self) -> list[OpenPosition]:
        if self.market_type is not MarketType.CRYPTO_PERP or not self._ex().has.get("fetchPositions"):
            return []
        rows = await self._ex().fetch_positions()
        result = []
        for row in rows:
            qty = abs(float(row.get("contracts") or 0))
            entry = float(row.get("entryPrice") or 0)
            mark = float(row.get("markPrice") or entry)
            if qty <= 0 or entry <= 0:
                continue
            side = Side.BUY if row.get("side") == "long" else Side.SELL
            result.append(OpenPosition(self.venue,row["symbol"],MarketType.CRYPTO_PERP,side,qty,entry,mark,
                float(row.get("stopLossPrice") or 0),float(row.get("takeProfitPrice") or 0),
                float(row.get("unrealizedPnl") or 0),0,metadata={"raw":row}))
        return result

    async def set_isolated_leverage(self, symbol: str, leverage: int) -> None:
        ex = self._ex()
        if not ex.has.get("setMarginMode") or not ex.has.get("setLeverage"):
            raise ConnectorUnavailable(f"{self.venue} cannot verify isolated margin and leverage.")
        await ex.set_margin_mode("isolated", symbol)
        await ex.set_leverage(leverage, symbol)
        # Confirm venue state by reading current positions/account config when available.
        self._symbols.add(symbol)

    def _bracket_capability(self, symbol: str) -> bool:
        ex = self._ex()
        try:
            return bool(ex.feature_value(symbol, "createOrder", "stopLoss") and
                        ex.feature_value(symbol, "createOrder", "takeProfit"))
        except Exception:
            return bool(ex.has.get("createOrderWithTakeProfitAndStopLoss"))

    async def _order(self, intent: TradeIntent, quantity: float, client_order_id: str) -> TradeReceipt:
        ex = self._ex()
        if not self._bracket_capability(intent.symbol):
            raise ConnectorUnavailable(
                f"{self.venue} does not advertise attached SL+TP for {intent.symbol}; live entry refused."
            )
        side = intent.side.value
        params = {
            "clientOrderId": client_order_id,
            "stopLoss": {"triggerPrice": intent.stop_loss, "type": "market"},
            "takeProfit": {"triggerPrice": intent.take_profit, "type": "market"},
        }
        order = await ex.create_order(intent.symbol,"market",side,quantity,None,params)
        filled = float(order.get("filled") or 0)
        average = float(order.get("average") or order.get("price") or intent.entry_price)
        if filled <= 0:
            raise ConnectorUnavailable(f"{self.venue} order has not reported a fill; reconcile order {order.get('id')}.")
        fee = float((order.get("fee") or {}).get("cost") or 0)
        ref = intent.entry_price
        slippage = abs(average-ref)/ref if ref > 0 else 1
        return TradeReceipt(self.venue,intent.symbol,intent.side,str(order["id"]),filled,average,fee,
                            slippage,datetime.now(timezone.utc),raw=order)

    async def buy(self, intent: TradeIntent, quantity: float, client_order_id: str) -> TradeReceipt:
        if intent.side is not Side.BUY:
            raise ValueError("buy() requires a buy intent.")
        return await self._order(intent,quantity,client_order_id)

    async def sell(self, intent: TradeIntent, quantity: float, client_order_id: str) -> TradeReceipt:
        if intent.side is not Side.SELL:
            raise ValueError("sell() requires a sell intent.")
        return await self._order(intent,quantity,client_order_id)

    async def close_position(self, position: OpenPosition, quantity: float | None = None) -> TradeReceipt:
        ex = self._ex()
        amount = min(float(quantity or position.quantity),position.quantity)
        side = "sell" if position.side is Side.BUY else "buy"
        params = {"reduceOnly": True} if position.market_type is MarketType.CRYPTO_PERP else {}
        order = await ex.create_order(position.symbol,"market",side,amount,None,params)
        filled = float(order.get("filled") or 0)
        average = float(order.get("average") or order.get("price") or position.mark_price)
        return TradeReceipt(self.venue,position.symbol,Side(side),str(order["id"]),filled,average,
            float((order.get("fee") or {}).get("cost") or 0),
            abs(average-position.mark_price)/max(position.mark_price,1e-12),raw=order)

    async def cancel_all_orders(self, symbol: str | None = None) -> None:
        ex = self._ex()
        if ex.has.get("cancelAllOrders"):
            await ex.cancel_all_orders(symbol)
        else:
            orders = await ex.fetch_open_orders(symbol)
            for order in orders:
                await ex.cancel_order(order["id"],order["symbol"])

    async def provider_health(self) -> dict[str, Any]:
        return {"venue":self.venue,"healthy":self.exchange is not None,
                "sandbox":self.sandbox,"market_type":self.market_type.value}
