"""Small shared market candle data type used by OKX technical analysis."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Candle:
    datetime: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
