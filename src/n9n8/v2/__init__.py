"""9N8 v2 side-effect-free domain, risk and research foundations.

Provider adapters are loaded only by their owning process. This package does
not start Telegram, open network connections, read secrets or sign transactions.
"""
from .contracts import Candle, TradeIntent, TradeSide, TradingMode
from .execution import ExecutionState, LiveReadiness, PaperQuoteAdapter, transition
from .market_data import MarketDataQualityError, normalize_ohlcv
from .microstructure import (
    BookLevel, BookMetrics, ExecutionEstimate, analyze_order_book,
    estimate_maker_taker_fees, estimate_market_execution, impermanent_loss_fraction,
)
from .macro_features import (
    average_pairwise_correlation, elasticity, funding_pressure,
    market_session, open_interest_divergence, supply_to_fdv_ratio,
)
from .quant import (
    atr, bollinger_bands, historical_var, kelly_fraction, max_drawdown_pct,
    monte_carlo_risk, position_size_for_risk, sharpe_ratio, sortino_ratio, vwap,
)
from .quant_research import (
    adf_stationarity_test, cointegration_test, finbert_sentiment,
    gaussian_hmm_regimes, geometric_brownian_path, mean_reversion_half_life,
    random_forest_regime_holdout, sample_autocorrelation, scalar_kalman_filter,
    walk_forward_splits,
)
from .risk import QuoteRiskInput, RiskLimits, assess_quote

__all__ = [
    "Candle", "TradeIntent", "TradeSide", "TradingMode",
    "ExecutionState", "LiveReadiness", "PaperQuoteAdapter", "transition",
    "MarketDataQualityError", "normalize_ohlcv",
    "BookLevel", "BookMetrics", "ExecutionEstimate", "analyze_order_book",
    "estimate_maker_taker_fees", "estimate_market_execution", "impermanent_loss_fraction",
    "average_pairwise_correlation", "elasticity", "funding_pressure",
    "market_session", "open_interest_divergence", "supply_to_fdv_ratio",
    "atr", "bollinger_bands", "historical_var", "kelly_fraction", "max_drawdown_pct",
    "monte_carlo_risk", "position_size_for_risk", "sharpe_ratio", "sortino_ratio", "vwap",
    "adf_stationarity_test", "cointegration_test", "finbert_sentiment",
    "gaussian_hmm_regimes", "geometric_brownian_path", "mean_reversion_half_life",
    "random_forest_regime_holdout", "sample_autocorrelation", "scalar_kalman_filter",
    "walk_forward_splits", "QuoteRiskInput", "RiskLimits", "assess_quote",
]
