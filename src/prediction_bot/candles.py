from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prediction_bot.venues.kalshi import KalshiClient


def candle_time(candle: dict[str, Any], field: str = "end_period_ts") -> datetime | None:
    value = candle.get(field)
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def candle_value(candle: dict[str, Any], book: str, field: str) -> Decimal | None:
    values = candle.get(book)
    if not isinstance(values, dict):
        return None
    value = values.get(f"{field}_dollars", values.get(field))
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def fetch_candles(
    kalshi: KalshiClient, ticker: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    return kalshi.get_candlesticks(
        ticker.split("-", 1)[0],
        ticker,
        int(start.timestamp()),
        int(end.timestamp()),
        period_interval=1,
    )
