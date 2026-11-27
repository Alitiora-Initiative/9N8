#property strict
#property version "1.00"
input string SignalGateway = "https://your-9n8-api.example/v1/signals";
input int FastEMA = 20;
input int SlowEMA = 50;
input int ATRPeriod = 14;
input double MaxRiskFraction = 0.01;
datetime lastBar = 0;
void OnTick()
{
   if(Bars < SlowEMA + 5 || MaxRiskFraction <= 0 || MaxRiskFraction > 0.02) return;
   if(Time[0] == lastBar) return;
   lastBar = Time[0];
   double fast = iMA(NULL,0,FastEMA,0,MODE_EMA,PRICE_CLOSE,1);
   double slow = iMA(NULL,0,SlowEMA,0,MODE_EMA,PRICE_CLOSE,1);
   double atr = iATR(NULL,0,ATRPeriod,1);
   if(atr <= 0) return;
   string side = fast > slow ? "trend_up" : (fast < slow ? "trend_down" : "neutral");
   string payload = "{\"symbol\":\""+Symbol()+"\",\"timeframe\":\""+IntegerToString(Period())+
      "\",\"signal\":\""+side+"\",\"close\":\""+DoubleToString(Close[1],Digits)+
      "\",\"atr\":\""+DoubleToString(atr,Digits)+"\"}";
   char data[], result[];
   string responseHeaders;
   StringToCharArray(payload,data,0,WHOLE_ARRAY,CP_UTF8);
   // Signal relay only. This adapter intentionally never places broker orders.
   int status = WebRequest("POST",SignalGateway,"Content-Type: application/json\r\n",5000,data,result,responseHeaders);
   if(status < 200 || status >= 300) Print("9N8 signal relay failed. HTTP=",status);
}
