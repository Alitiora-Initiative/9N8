#property strict
#property version "1.00"
input string SignalGateway = "https://your-9n8-api.example/v1/signals";
input int FastEMA = 20;
input int SlowEMA = 50;
input int ATRPeriod = 14;
input double MaxRiskFraction = 0.01;
int fastHandle, slowHandle, atrHandle;
datetime lastBar = 0;
int OnInit()
{
   if(MaxRiskFraction <= 0 || MaxRiskFraction > 0.02) return INIT_PARAMETERS_INCORRECT;
   fastHandle = iMA(_Symbol,_Period,FastEMA,0,MODE_EMA,PRICE_CLOSE);
   slowHandle = iMA(_Symbol,_Period,SlowEMA,0,MODE_EMA,PRICE_CLOSE);
   atrHandle = iATR(_Symbol,_Period,ATRPeriod);
   if(fastHandle==INVALID_HANDLE || slowHandle==INVALID_HANDLE || atrHandle==INVALID_HANDLE) return INIT_FAILED;
   return INIT_SUCCEEDED;
}
void OnDeinit(const int reason)
{
   if(fastHandle!=INVALID_HANDLE) IndicatorRelease(fastHandle);
   if(slowHandle!=INVALID_HANDLE) IndicatorRelease(slowHandle);
   if(atrHandle!=INVALID_HANDLE) IndicatorRelease(atrHandle);
}
void OnTick()
{
   MqlRates rates[];
   if(CopyRates(_Symbol,_Period,0,3,rates)<3 || rates[1].time==lastBar) return;
   lastBar=rates[1].time;
   double fast[],slow[],atr[];
   if(CopyBuffer(fastHandle,0,1,1,fast)!=1 || CopyBuffer(slowHandle,0,1,1,slow)!=1 ||
      CopyBuffer(atrHandle,0,1,1,atr)!=1 || atr[0]<=0) return;
   string side=fast[0]>slow[0]?"trend_up":(fast[0]<slow[0]?"trend_down":"neutral");
   string payload="{\"symbol\":\""+_Symbol+"\",\"timeframe\":\""+EnumToString(_Period)+
      "\",\"signal\":\""+side+"\",\"close\":\""+DoubleToString(rates[1].close,_Digits)+
      "\",\"atr\":\""+DoubleToString(atr[0],_Digits)+"\"}";
   char data[],result[];
   string headers;
   StringToCharArray(payload,data,0,WHOLE_ARRAY,CP_UTF8);
   // Signal relay only. No broker order-placement API is called.
   int status=WebRequest("POST",SignalGateway,"Content-Type: application/json\r\n",5000,data,result,headers);
   if(status<200 || status>=300) Print("9N8 signal relay failed. HTTP=",status);
}
