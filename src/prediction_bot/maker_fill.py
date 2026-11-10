from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from prediction_bot.candles import candle_time, candle_value
from prediction_bot.models import OutcomeSide, Trade


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


def queue_fill(
    trades: Sequence[Trade],
    side: OutcomeSide | str,
    price: Decimal,
    queue_ahead: Decimal,
    size: Decimal,
    start: datetime,
    end: datetime,
) -> datetime | None:
    """First time public volume at or through our resting bid clears the queue and us.

    A resting buy of `side` at `price` (in that side's own dollars) trades against
    takers buying the other side: a YES bid at p is hit by `no`-taker trades with
    yes_price <= p; a NO bid at p by `yes`-taker trades with no price (1 - yes_price)
    <= p. The order counts as filled once cumulative matching volume since placement
    reaches `queue_ahead + size` (FIFO at the level; trades through it count too).
    """
    outcome = OutcomeSide(side)
    start_utc = _utc(start)
    end_utc = _utc(end)
    needed = queue_ahead + size
    traded = Decimal(0)
    for trade in sorted(trades, key=lambda t: t.created_at):
        at = _utc(trade.created_at)
        if at < start_utc or at >= end_utc:
            continue
        if outcome is OutcomeSide.yes:
            hits = trade.taker_side is OutcomeSide.no and trade.yes_price <= price
        else:
            hits = trade.taker_side is OutcomeSide.yes and Decimal(1) - trade.yes_price <= price
        if not hits:
            continue
        traded += trade.count
        if traded >= needed:
            return at
    return None


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def run_over_fill(
    trades: Sequence[Trade],
    candles: list[dict[str, Any]],
    quotes: Sequence[tuple[datetime, Decimal | None, Decimal | None]],
    side: OutcomeSide | str,
    price: Decimal,
    start: datetime,
    end: datetime,
) -> datetime | None:
    """First time the market ran over a resting buy of `side` at `price`, whether or not
    the queue ahead of it traded (orders ahead can cancel while price runs through).

    Run-over: a public trade strictly through the price (YES bid: yes_price < price;
    NO bid: 1 - yes_price < price), a 1-minute candle trading strictly through it, or
    the side's ask at or below the price in a candle or a quote snapshot. `quotes` are
    (time, yes_bid, yes_ask) rows. None when nothing in [start, end) qualifies.
    """
    outcome = OutcomeSide(side)
    start_utc, end_utc = _utc(start), _utc(end)
    if end_utc <= start_utc:
        return None
    found: list[datetime] = []
    for trade in trades:
        at = _utc(trade.created_at)
        if not start_utc <= at < end_utc:
            continue
        side_price = trade.yes_price if outcome is OutcomeSide.yes else 1 - trade.yes_price
        if side_price < price:
            found.append(at)
            break
    for rule in (
        trade_through_fill(candles, outcome, price, start_utc, end_utc),
        optimistic_fill(candles, outcome, price, start_utc, end_utc),
    ):
        if rule is not None:
            found.append(rule)
    for at_raw, yes_bid, yes_ask in sorted(quotes, key=lambda q: q[0]):
        at = _utc(at_raw)
        if not start_utc <= at < end_utc:
            continue
        ask = yes_ask if outcome is OutcomeSide.yes else (None if yes_bid is None else 1 - yes_bid)
        if ask is not None and ask <= price:
            found.append(at)
            break
    return min(found) if found else None


def candle_volume(candles: list[dict[str, Any]], start: datetime, end: datetime) -> Decimal:
    """Contracts traded in [start, end) according to 1-minute candles."""
    start_utc, end_utc = _utc(start), _utc(end)
    total = Decimal(0)
    for candle in candles:
        at = candle_time(candle)
        if at is None or not start_utc <= at < end_utc:
            continue
        raw = candle.get("volume_fp", candle.get("volume"))
        try:
            total += Decimal(str(raw)) if raw not in (None, "") else Decimal(0)
        except ArithmeticError:
            continue
    return total
