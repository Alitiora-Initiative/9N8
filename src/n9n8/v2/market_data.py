from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import Candle, decimal_value


class MarketDataQualityError(ValueError):
    pass


_INTERVAL = re.compile(r"^(\d+)(s|m|h|d|w)$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def interval_seconds(interval: str) -> int:
    match = _INTERVAL.fullmatch(str(interval).strip().lower())
    if not match:
        raise MarketDataQualityError(f"Unsupported candle interval: {interval}")
    amount = int(match.group(1))
    seconds = amount * _UNIT_SECONDS[match.group(2)]
    if amount <= 0 or seconds > 31_536_000:
        raise MarketDataQualityError("Candle interval is outside the supported range")
    return seconds


def _timestamp(value: Any, source_timezone: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        numeric = Decimal(str(value))
        if not numeric.is_finite():
            raise MarketDataQualityError("Timestamp must be finite")
        epoch = numeric / Decimal(1000) if abs(numeric) >= Decimal("100000000000") else numeric
        try:
            return datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise MarketDataQualityError("Timestamp is outside the supported epoch range") from exc
    elif isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            return _timestamp(int(raw), source_timezone)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MarketDataQualityError("Timestamp must be ISO-8601 or an epoch value") from exc
    else:
        raise MarketDataQualityError("Unsupported timestamp type")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        try:
            zone = ZoneInfo(source_timezone)
        except ZoneInfoNotFoundError as exc:
            raise MarketDataQualityError(f"Unknown source timezone: {source_timezone}") from exc
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(timezone.utc)


def _row_values(row: Any) -> tuple[Any, Any, Any, Any, Any, Any]:
    if isinstance(row, Mapping):
        values = (
            row.get("timestamp", row.get("time", row.get("ts"))),
            row.get("open", row.get("o")),
            row.get("high", row.get("h")),
            row.get("low", row.get("l")),
            row.get("close", row.get("c")),
            row.get("volume", row.get("v")),
        )
        if any(value is None for value in values):
            raise MarketDataQualityError("OHLCV mapping is missing a required field")
        return values
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)) and len(row) >= 6:
        return row[0], row[1], row[2], row[3], row[4], row[5]
    raise MarketDataQualityError("OHLCV row must be a mapping or a six-element sequence")


def normalize_ohlcv(
    rows: Sequence[Any],
    *,
    symbol: str,
    source: str,
    interval: str,
    source_timezone: str = "UTC",
    now: datetime | None = None,
    only_closed: bool = True,
) -> tuple[Candle, ...]:
    """Normalize provider bars, sort in UTC and reject ambiguous timestamps.

    Duplicate candle-open times are rejected rather than silently overwritten.
    Unfinished bars are excluded by default to prevent look-ahead bias.
    """
    duration = interval_seconds(interval)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise MarketDataQualityError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)

    candles: list[Candle] = []
    seen: set[datetime] = set()
    for row in rows:
        timestamp, raw_open, raw_high, raw_low, raw_close, raw_volume = _row_values(row)
        opened_at = _timestamp(timestamp, source_timezone)
        if opened_at > current:
            raise MarketDataQualityError("Future-dated candle received")
        if opened_at in seen:
            raise MarketDataQualityError("Duplicate candle-open timestamp received")
        seen.add(opened_at)
        closed = opened_at + timedelta(seconds=duration) <= current
        if only_closed and not closed:
            continue
        try:
            candle = Candle(
                symbol=symbol,
                source=source,
                interval=interval,
                opened_at=opened_at,
                open=decimal_value(raw_open, "open"),
                high=decimal_value(raw_high, "high"),
                low=decimal_value(raw_low, "low"),
                close=decimal_value(raw_close, "close"),
                volume=decimal_value(raw_volume, "volume"),
                is_closed=closed,
            )
        except (TypeError, ValueError) as exc:
            raise MarketDataQualityError(str(exc)) from exc
        candles.append(candle)

    return tuple(sorted(candles, key=lambda item: item.opened_at))
