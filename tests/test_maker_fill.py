from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from prediction_bot.maker_fill import optimistic_fill, trade_through_fill
from prediction_bot.models import OutcomeSide

START = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
PRICE = Decimal("0.40")


def candle(
    minute: int,
    *,
    traded_low: str = "0.50",
    traded_high: str = "0.50",
    yes_ask_low: str = "0.50",
    yes_bid_high: str = "0.50",
) -> dict[str, object]:
    return {
        "end_period_ts": int((START + timedelta(minutes=minute)).timestamp()),
        "price": {"low_dollars": traded_low, "high_dollars": traded_high},
        "yes_ask": {"low_dollars": yes_ask_low},
        "yes_bid": {"high_dollars": yes_bid_high},
    }


@pytest.mark.parametrize(
    ("function", "side", "candles", "expected_minute"),
    [
        (
            trade_through_fill,
            OutcomeSide.yes,
            [
                candle(1, traded_low="0.40"),
                candle(2, traded_low="0.39"),
            ],
            2,
        ),
        (optimistic_fill, OutcomeSide.yes, [candle(1, yes_ask_low="0.40")], 1),
        (
            trade_through_fill,
            OutcomeSide.no,
            [candle(1, traded_high="0.60"), candle(2, traded_high="0.61")],
            2,
        ),
        (optimistic_fill, OutcomeSide.no, [candle(1, yes_bid_high="0.60")], 1),
    ],
)
def test_maker_fill_rules_for_yes_and_no(function, side, candles, expected_minute) -> None:  # type: ignore[no-untyped-def]
    result = function(candles, side, PRICE, START, START + timedelta(minutes=5))
    assert result == START + timedelta(minutes=expected_minute)


def test_maker_fill_rules_respect_the_half_open_time_window() -> None:
    candles = [
        candle(0, traded_low="0.39"),
        candle(5, traded_low="0.39"),
        candle(2, traded_low="0.39"),
    ]
    assert trade_through_fill(
        candles,
        OutcomeSide.yes,
        PRICE,
        START + timedelta(minutes=1),
        START + timedelta(minutes=5),
    ) == START + timedelta(minutes=2)
