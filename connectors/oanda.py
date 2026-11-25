from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import aiohttp

from connectors.base import ExecutionConnector
from models import AccountState, Candle, ConnectorUnavailable, MarketSnapshot, MarketType, OpenPosition, Side, TradeIntent, TradeReceipt

log = logging.getLogger(__name__)


class OandaConnector(ExecutionConnector):
    venue = "oanda"

    def __init__(self, environment: str = "practice", token: str | None = None,
                 account_id: str | None = None, live_certified: bool = False):
        if environment not in {"practice","live"}:
            raise ValueError("OANDA environment must be practice or live.")
        self.environment = environment
        self.live_certified = bool(live_certified)
        self.base = "https://api-fxpractice.oanda.com" if environment == "practice" else "https://api-fxtrade.oanda.com"
        self.stream_base = "https://stream-fxpractice.oanda.com" if environment == "practice" else "https://stream-fxtrade.oanda.com"
        self.token = token or os.getenv("OANDA_API_TOKEN","").strip()
        self.account_id = account_id or os.getenv("OANDA_ACCOUNT_ID","").strip()
        self.session: aiohttp.ClientSession | None = None

    async def connect(self) -> None:
        if not self.token or not self.account_id:
            raise ConnectorUnavailable("OANDA_API_TOKEN and OANDA_ACCOUNT_ID are required.")
        self.session = aiohttp.ClientSession(
            headers={"Authorization":"Bearer "+self.token,"Content-Type":"application/json"},
            timeout=aiohttp.ClientTimeout(total=15,connect=5),
        )
        await self._request("GET",f"/v3/accounts/{quote(self.account_id,safe='')}/summary")

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session=None

    async def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict:
        if not self.session:
            raise ConnectorUnavailable("OANDA connector is not connected.")
        async with self.session.request(method,self.base+path,params=params,json=body) as response:
            payload = await response.json(content_type=None)
            if response.status >= 400:
                raise ConnectorUnavailable(f"OANDA HTTP {response.status}: {payload.get('errorMessage','request failed')}")
            return payload

    async def fetch_snapshot(self, symbol: str) -> MarketSnapshot:
        data=await self._request("GET",f"/v3/accounts/{self.account_id}/pricing",params={"instruments":symbol})
        prices=data.get("prices",[])
        if not prices:
            raise ConnectorUnavailable(f"OANDA returned no price for {symbol}.")
        p=prices[0]
        bid=float(p["bids"][0]["price"]); ask=float(p["asks"][0]["price"])
        return MarketSnapshot(self.venue,symbol,MarketType.FOREX,bid,ask,(bid+ask)/2)

    async def fetch_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        granularity = {"1h":"H1","5m":"M5","15m":"M15","1d":"D"}.get(timeframe,timeframe)
        data=await self._request("GET",f"/v3/instruments/{quote(symbol,safe='')}/candles",
                                 params={"granularity":granularity,"count":min(limit,500),"price":"M"})
        result=[]
        for c in data.get("candles",[]):
            if not c.get("complete") or "mid" not in c: continue
            m=c["mid"]
            result.append(Candle(int(datetime.fromisoformat(c["time"].replace("Z","+00:00")).timestamp()*1000),
                                 float(m["o"]),float(m["h"]),float(m["l"]),float(m["c"]),float(c.get("volume",0))))
        return result

    async def get_balance(self) -> AccountState:
        data=await self._request("GET",f"/v3/accounts/{self.account_id}/summary")
        account=data["account"]
        if str(account.get("currency", "")).upper() != "USD":
            raise ConnectorUnavailable(
                "OANDA risk sizing currently requires a USD-denominated account; "
                "account-currency conversion is not enabled."
            )
        return AccountState(self.venue,float(account["NAV"]),float(account["marginAvailable"]),
                            float(account["balance"]))

    async def get_open_positions(self) -> list[OpenPosition]:
        data=await self._request("GET",f"/v3/accounts/{self.account_id}/openTrades")
        result=[]
        for t in data.get("trades",[]):
            units=float(t["currentUnits"])
            if units == 0: continue
            side=Side.BUY if units>0 else Side.SELL
            result.append(OpenPosition(self.venue,t["instrument"],MarketType.FOREX,side,abs(units),
                float(t["price"]),float(t["price"]),float((t.get("stopLossOrder") or {}).get("price") or 0),
                float((t.get("takeProfitOrder") or {}).get("price") or 0),
                float(t.get("unrealizedPL") or 0),float(t.get("realizedPL") or 0),
                metadata={"trade_id":t["id"],"raw":t}))
        return result

    async def _market_order(self, intent: TradeIntent, quantity: float, client_order_id: str) -> TradeReceipt:
        if self.environment == "live":
            acknowledged = os.getenv("LIVE_ACKNOWLEDGEMENT", "") == "I_UNDERSTAND_REAL_FUNDS_CAN_BE_LOST"
            authorized = (
                os.getenv("TRADING_MODE", "").lower() == "live"
                and os.getenv("LIVE_TRADING", "false").lower() == "true"
                and os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true"
                and acknowledged and self.live_certified
            )
            if not authorized:
                raise ConnectorUnavailable("OANDA live order rejected by connector-level authorization gate.")
        signed_units=quantity if intent.side is Side.BUY else -quantity
        request={"order":{
            "type":"MARKET","instrument":intent.symbol,"units":str(signed_units),
            "timeInForce":"FOK","positionFill":"DEFAULT",
            "stopLossOnFill":{"timeInForce":"GTC","price":f"{intent.stop_loss:.10f}"},
            "takeProfitOnFill":{"timeInForce":"GTC","price":f"{intent.take_profit:.10f}"},
            "clientExtensions":{"id":client_order_id[:32],"tag":"9N8"},
        }}
        data=await self._request("POST",f"/v3/accounts/{self.account_id}/orders",body=request)
        fill=data.get("orderFillTransaction")
        if not fill:
            raise ConnectorUnavailable(f"OANDA order was not filled immediately: {data}")
        price=float(fill["price"]); units=abs(float(fill["units"]))
        commission=abs(float(fill.get("commission","0")))
        txid=str(fill.get("id",""))
        return TradeReceipt(self.venue,intent.symbol,intent.side,txid,units,price,commission,
                            abs(price-intent.entry_price)/max(intent.entry_price,1e-12),
                            datetime.now(timezone.utc),raw=data)

    async def buy(self,intent:TradeIntent,quantity:float,client_order_id:str)->TradeReceipt:
        if intent.side is not Side.BUY: raise ValueError("buy() requires buy intent")
        return await self._market_order(intent,quantity,client_order_id)

    async def sell(self,intent:TradeIntent,quantity:float,client_order_id:str)->TradeReceipt:
        if intent.side is not Side.SELL: raise ValueError("sell() requires sell intent")
        return await self._market_order(intent,quantity,client_order_id)

    async def close_position(self,position:OpenPosition,quantity:float|None=None)->TradeReceipt:
        trade_id=str(position.metadata.get("trade_id",""))
        if not trade_id: raise ConnectorUnavailable("OANDA trade ID missing; refusing ambiguous close.")
        units=min(abs(float(quantity or position.quantity)),position.quantity)
        data=await self._request("PUT",f"/v3/accounts/{self.account_id}/trades/{trade_id}/close",
                                 body={"units":str(units)})
        txn=data.get("orderFillTransaction") or {}
        return TradeReceipt(self.venue,position.symbol,Side.SELL if position.side is Side.BUY else Side.BUY,
            str(txn.get("id","")),abs(float(txn.get("units",0))),float(txn.get("price",position.mark_price)),
            abs(float(txn.get("commission",0))),0.0,raw=data)

    async def cancel_all_orders(self,symbol:str|None=None)->None:
        data=await self._request("GET",f"/v3/accounts/{self.account_id}/pendingOrders")
        for order in data.get("orders",[]):
            if symbol is None or order.get("instrument")==symbol:
                await self._request("PUT",f"/v3/accounts/{self.account_id}/orders/{order['id']}/cancel")

    async def provider_health(self)->dict[str,Any]:
        return {"venue":self.venue,"environment":self.environment,"healthy":self.session is not None}
