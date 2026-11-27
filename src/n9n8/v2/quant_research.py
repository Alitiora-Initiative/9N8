from __future__ import annotations

import math
import random
from decimal import Decimal, InvalidOperation
from typing import Iterable, Sequence


def _values(items: Iterable[object], name: str) -> list[float]:
    result = []
    for item in items:
        try:
            value = float(item)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{name} must be numeric") from exc
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        result.append(value)
    if not result:
        raise ValueError(f"{name} cannot be empty")
    return result


def sample_autocorrelation(values: Sequence[object], lag: int = 1) -> float:
    x = _values(values, "values")
    if lag < 1 or lag >= len(x) - 1:
        raise ValueError("lag must be positive and leave at least two paired observations")
    left, right = x[:-lag], x[lag:]
    mean_left, mean_right = sum(left) / len(left), sum(right) / len(right)
    numerator = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    denominator = math.sqrt(sum((a - mean_left) ** 2 for a in left) * sum((b - mean_right) ** 2 for b in right))
    if denominator == 0:
        raise ValueError("autocorrelation is undefined for a constant window")
    return numerator / denominator


def scalar_kalman_filter(observations: Sequence[object], *, process_variance: object = "0.0001", measurement_variance: object = "0.01", initial_estimate: object | None = None) -> tuple[float, ...]:
    values = _values(observations, "observations")
    q, r = float(process_variance), float(measurement_variance)
    if not math.isfinite(q) or not math.isfinite(r) or q <= 0 or r <= 0:
        raise ValueError("process and measurement variances must be positive finite values")
    estimate = values[0] if initial_estimate is None else float(initial_estimate)
    if not math.isfinite(estimate):
        raise ValueError("initial_estimate must be finite")
    covariance, output = 1.0, []
    for observation in values:
        covariance += q
        gain = covariance / (covariance + r)
        estimate += gain * (observation - estimate)
        covariance *= 1.0 - gain
        output.append(estimate)
    return tuple(output)


def geometric_brownian_path(start_price: object, annual_drift: object, annual_volatility: object, *, steps: int, periods_per_year: int = 365, seed: int = 9108) -> tuple[Decimal, ...]:
    try:
        start, drift, volatility = Decimal(str(start_price)), Decimal(str(annual_drift)), Decimal(str(annual_volatility))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("GBM parameters must be Decimal-compatible") from exc
    if not all(x.is_finite() for x in (start, drift, volatility)):
        raise ValueError("GBM parameters must be finite")
    if start <= 0 or volatility < 0 or steps < 1 or periods_per_year < 1:
        raise ValueError("GBM requires positive start, non-negative volatility and positive steps")
    rng, dt = random.Random(seed), 1.0 / periods_per_year
    mu, sigma, price = float(drift), float(volatility), float(start)
    result = [start]
    for _ in range(steps):
        price *= math.exp((mu - 0.5 * sigma * sigma) * dt + sigma * math.sqrt(dt) * rng.gauss(0.0, 1.0))
        result.append(Decimal(str(price)))
    return tuple(result)


def mean_reversion_half_life(spread: Sequence[object]) -> float:
    values = _values(spread, "spread")
    if len(values) < 4:
        raise ValueError("at least four spread observations are required")
    lagged = values[:-1]
    changes = [values[i+1] - values[i] for i in range(len(values)-1)]
    mean_x, mean_y = sum(lagged)/len(lagged), sum(changes)/len(changes)
    variance = sum((x-mean_x)**2 for x in lagged)
    if variance == 0:
        raise ValueError("spread is constant")
    slope = sum((x-mean_x)*(y-mean_y) for x,y in zip(lagged,changes))/variance
    if slope >= 0:
        raise ValueError("estimated spread is not mean reverting")
    return -math.log(2.0)/slope


def walk_forward_splits(observations: int, *, train_size: int, test_size: int, step_size: int | None = None) -> tuple[tuple[range,range], ...]:
    step = test_size if step_size is None else step_size
    if observations < 1 or min(train_size,test_size,step) < 1:
        raise ValueError("observation and split sizes must be positive")
    result, start = [], 0
    while start + train_size + test_size <= observations:
        train, test = range(start,start+train_size), range(start+train_size,start+train_size+test_size)
        if train.stop > test.start:
            raise AssertionError("walk-forward leakage detected")
        result.append((train,test))
        start += step
    if not result:
        raise ValueError("not enough observations for one walk-forward split")
    return tuple(result)


def adf_stationarity_test(values: Sequence[object], *, regression: str = "c") -> dict[str,object]:
    """Research-only ADF test; statsmodels is required in the research environment."""
    x = _values(values,"values")
    try:
        from statsmodels.tsa.stattools import adfuller
    except ImportError as exc:
        raise RuntimeError("Install requirements-research.txt for the ADF test") from exc
    statistic,pvalue,used_lag,nobs,critical_values,_ = adfuller(x,regression=regression,autolag="AIC")
    return {"statistic":float(statistic),"pvalue":float(pvalue),"used_lag":int(used_lag),
            "observations":int(nobs),"critical_values":{k:float(v) for k,v in critical_values.items()},
            "stationary_at_5pct":bool(pvalue<0.05)}


def cointegration_test(dependent: Sequence[object], independent: Sequence[object]) -> dict[str,object]:
    """Research-only Engle-Granger test; never emits an execution signal."""
    y,x = _values(dependent,"dependent"),_values(independent,"independent")
    if len(y)!=len(x) or len(y)<30:
        raise ValueError("cointegration inputs must have equal length and at least 30 observations")
    try:
        from statsmodels.tsa.stattools import coint
    except ImportError as exc:
        raise RuntimeError("Install requirements-research.txt for cointegration tests") from exc
    statistic,pvalue,critical_values = coint(y,x,trend="c",autolag="aic")
    return {"statistic":float(statistic),"pvalue":float(pvalue),"critical_values":[float(v) for v in critical_values],
            "cointegrated_at_5pct":bool(pvalue<0.05)}


def random_forest_regime_holdout(features: Sequence[Sequence[object]], labels: Sequence[object], *, train_fraction: float = 0.8, random_state: int = 9108) -> dict[str,object]:
    """Chronological supervised research model. The holdout is never shuffled."""
    matrix = [[float(value) for value in row] for row in features]
    target = [str(value) for value in labels]
    if not 0.5 <= train_fraction < 1 or len(matrix) != len(target) or len(matrix) < 30:
        raise ValueError("invalid model inputs or train_fraction")
    width = len(matrix[0]) if matrix else 0
    if width == 0 or any(len(row) != width or any(not math.isfinite(value) for value in row) for row in matrix):
        raise ValueError("feature matrix must be rectangular and finite")
    split = int(len(matrix) * train_fraction)
    if split < 10 or len(set(target[:split])) < 2 or len(set(target[split:])) < 1:
        raise ValueError("training and test partitions do not contain enough labels")
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import accuracy_score
    except ImportError as exc:
        raise RuntimeError("Install requirements-research.txt for scikit-learn") from exc
    model = RandomForestClassifier(n_estimators=200, max_depth=6, random_state=random_state, class_weight="balanced")
    model.fit(matrix[:split], target[:split])
    predicted = model.predict(matrix[split:])
    return {
        "accuracy": float(accuracy_score(target[split:], predicted)),
        "predictions": [str(item) for item in predicted],
        "feature_importances": [float(item) for item in model.feature_importances_],
        "chronological_holdout": True,
        "research_only": True,
    }


def gaussian_hmm_regimes(features: Sequence[Sequence[object]], *, components: int = 3, random_state: int = 9108) -> dict[str,object]:
    """Optional HMM research experiment. It cannot approve or place live trades."""
    rows = [[float(value) for value in row] for row in features]
    if components<2 or len(rows)<components*10 or not rows or not rows[0]:
        raise ValueError("HMM input is too small for the requested component count")
    width=len(rows[0])
    if any(len(row)!=width or any(not math.isfinite(value) for value in row) for row in rows):
        raise ValueError("HMM feature matrix must be rectangular and finite")
    try:
        from hmmlearn.hmm import GaussianHMM
    except ImportError as exc:
        raise RuntimeError("Install requirements-research.txt for HMM experiments") from exc
    model=GaussianHMM(n_components=components,covariance_type="diag",n_iter=200,random_state=random_state)
    model.fit(rows)
    return {"regimes":model.predict(rows).tolist(),"log_likelihood":float(model.score(rows)),
            "components":int(components),"research_only":True}


def finbert_sentiment(texts: Sequence[str]) -> tuple[dict[str,object],...]:
    """Optional FinBERT sentiment scoring. Do not send private Telegram content."""
    if not texts or any(not str(text).strip() for text in texts):
        raise ValueError("non-empty text items are required")
    try:
        from transformers import pipeline
    except ImportError as exc:
        raise RuntimeError("Install requirements-research-ml.txt for FinBERT") from exc
    classifier=pipeline("text-classification",model="ProsusAI/finbert",tokenizer="ProsusAI/finbert")
    results=classifier(list(texts),truncation=True)
    return tuple({"label":item["label"],"score":float(item["score"])} for item in results)
