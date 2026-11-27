"""Side-effect-free 9N8 v2 domain foundations.

Production services are composed by the application root only after their
provider and persistence adapters pass the explicit contracts in this package.
"""
from .contracts import Candle, TradeIntent, TradeSide, TradingMode
from .execution import ExecutionState, LiveReadiness, PaperQuoteAdapter, transition
from .market_data import MarketDataQualityError, normalize_ohlcv
from .quant import (
    atr, bollinger_bands, historical_var, kelly_fraction, max_drawdown_pct,
    monte_carlo_risk, position_size_for_risk, sharpe_ratio, sortino_ratio, vwap,
)
from .risk import QuoteRiskInput, RiskLimits, assess_quote

__all__ = [
    "Candle", "TradeIntent", "TradeSide", "TradingMode",
    "ExecutionState", "LiveReadiness", "PaperQuoteAdapter", "transition",
    "MarketDataQualityError", "normalize_ohlcv",
    "atr", "bollinger_bands", "historical_var", "kelly_fraction",
    "max_drawdown_pct", "monte_carlo_risk", "position_size_for_risk",
    "sharpe_ratio", "sortino_ratio", "vwap",
    "QuoteRiskInput", "RiskLimits", "assess_quote",
]
