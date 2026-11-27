namespace N9N8.BrokerBridge;
public sealed record BrokerQuote(decimal Bid,decimal Ask,DateTimeOffset ObservedAtUtc);
public sealed record OrderRisk(decimal Equity,decimal RiskFraction,decimal StopDistanceFraction,decimal MaxExposureFraction);
public static class BrokerRisk{
 public static decimal SpreadBps(BrokerQuote q){if(q.Bid<=0||q.Ask<=q.Bid)throw new ArgumentException("Invalid bid/ask.");var mid=(q.Bid+q.Ask)/2m;return(q.Ask-q.Bid)/mid*10000m;}
 public static decimal MaximumNotional(OrderRisk r){if(r.Equity<=0||r.StopDistanceFraction<=0||r.RiskFraction<=0||r.RiskFraction>0.02m||r.MaxExposureFraction<=0||r.MaxExposureFraction>1m)throw new ArgumentOutOfRangeException(nameof(r));return Math.Min(r.Equity*r.MaxExposureFraction,r.Equity*r.RiskFraction/r.StopDistanceFraction);}
 public static bool IsQuoteFresh(BrokerQuote q,DateTimeOffset now,TimeSpan maxAge)=>q.ObservedAtUtc<=now&&now-q.ObservedAtUtc<=maxAge;
}
internal static class Program{
 static void Main(){var q=new BrokerQuote(100m,100.1m,DateTimeOffset.UtcNow);if(BrokerRisk.SpreadBps(q)<=0)throw new Exception("spread failed");if(BrokerRisk.MaximumNotional(new OrderRisk(1000m,.01m,.1m,.1m))!=100m)throw new Exception("sizing failed");if(!BrokerRisk.IsQuoteFresh(q,DateTimeOffset.UtcNow,TimeSpan.FromSeconds(5)))throw new Exception("freshness failed");Console.WriteLine("9N8 broker risk: PASS");}
}
