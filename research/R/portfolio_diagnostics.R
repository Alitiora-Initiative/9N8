n8_metrics <- function(returns, periods_per_year=365, confidence=0.95) {
  x <- as.numeric(returns)
  if (length(x)<2 || any(!is.finite(x)) || any(x<=-1)) stop("invalid return series")
  if (periods_per_year<=0 || confidence<=0 || confidence>=1) stop("invalid settings")
  equity <- cumprod(1+x)
  peak <- cummax(c(1,head(equity,-1)))
  drawdown <- (peak-equity)/peak
  downside <- pmin(x,0)
  downside_deviation <- sqrt(mean(downside^2))
  sharpe <- if(sd(x)==0) NA_real_ else mean(x)/sd(x)*sqrt(periods_per_year)
  sortino <- if(downside_deviation==0) NA_real_ else mean(x)/downside_deviation*sqrt(periods_per_year)
  list(sharpe=sharpe, sortino=sortino, max_drawdown_pct=100*max(drawdown),
       historical_var=max(0,-as.numeric(quantile(x,1-confidence,type=1))), observations=length(x))
}
if (identical(Sys.getenv("N8_SELF_TEST"),"1")) {
  result <- n8_metrics(c(0.02,-0.01,0.03,-0.02))
  stopifnot(result$observations==4, result$max_drawdown_pct>0)
  cat("9N8 R portfolio diagnostics: PASS\n")
}
