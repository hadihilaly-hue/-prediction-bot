from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from prediction_bot.candles import candle_time, candle_value
from prediction_bot.models import OutcomeSide


def _first_fill(
    candles: list[dict[str, Any]],
    side: OutcomeSide | str,
    price: Decimal,
    start: datetime,
    end: datetime,
    *,
    optimistic: bool,
) -> datetime | None:
    outcome = OutcomeSide(side)
    start_utc = (
        start.replace(tzinfo=timezone.utc)
        if start.tzinfo is None
        else start.astimezone(timezone.utc)
    )
    end_utc = (
        end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end.astimezone(timezone.utc)
    )
    if end_utc <= start_utc:
        return None
    ordered = sorted(
        (
            (at, candle)
            for candle in candles
            if (at := candle_time(candle)) is not None and start_utc <= at < end_utc
        ),
        key=lambda item: item[0],
    )
    for at, candle in ordered:
        if optimistic:
            if outcome is OutcomeSide.yes:
                ask_low = candle_value(candle, "yes_ask", "low")
                filled = ask_low is not None and ask_low <= price
            else:
                bid_high = candle_value(candle, "yes_bid", "high")
                filled = bid_high is not None and bid_high >= Decimal(1) - price
        elif outcome is OutcomeSide.yes:
            traded_low = candle_value(candle, "price", "low")
            filled = traded_low is not None and traded_low < price
        else:
            traded_high = candle_value(candle, "price", "high")
            filled = traded_high is not None and traded_high > Decimal(1) - price
        if filled:
            return at
    return None


def trade_through_fill(
    candles: list[dict[str, Any]],
    side: OutcomeSide | str,
    price: Decimal,
    start: datetime,
    end: datetime,
) -> datetime | None:
    return _first_fill(candles, side, price, start, end, optimistic=False)


def optimistic_fill(
    candles: list[dict[str, Any]],
    side: OutcomeSide | str,
    price: Decimal,
    start: datetime,
    end: datetime,
) -> datetime | None:
    return _first_fill(candles, side, price, start, end, optimistic=True)
