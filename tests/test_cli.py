from __future__ import annotations

from decimal import Decimal

import pytest
import typer

from prediction_bot.cli import _validate_order


@pytest.mark.parametrize(
    ("price", "count"),
    [
        ("abc", "1"),
        ("NaN", "1"),
        ("0.5", "Infinity"),
        ("0", "1"),
        ("-0.5", "1"),
        ("1.0", "1"),
        ("0.5", "0"),
        ("0.5", "-3"),
        ("0.01", "101"),  # notional 1.01 <= 10 but count > max_position_contracts (100)
        ("0.5", "40"),  # notional 20 > 10
    ],
)
def test_validate_order_rejects_bad_values(settings, price: str, count: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(typer.BadParameter):
        _validate_order(price, count, settings)


def test_validate_order_accepts_sane_values(settings) -> None:  # type: ignore[no-untyped-def]
    assert _validate_order("0.40", "5", settings) == (Decimal("0.40"), Decimal(5))
