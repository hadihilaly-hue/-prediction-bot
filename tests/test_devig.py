from __future__ import annotations

from decimal import Decimal

import pytest

from prediction_bot.devig import devig_all, longshot_haircut


@pytest.mark.parametrize(
    "odds",
    [
        {"home": Decimal("1.91"), "away": Decimal("1.91")},
        {
            "home": Decimal("2.25"),
            "draw": Decimal("3.20"),
            "away": Decimal("3.40"),
        },
    ],
)
def test_devig_methods_return_normalized_probabilities(odds) -> None:  # type: ignore[no-untyped-def]
    methods = devig_all(odds)
    assert {"multiplicative", "power", "shin"} <= set(methods)
    for probabilities in methods.values():
        assert sum(probabilities.values(), Decimal(0)) == pytest.approx(
            Decimal(1), abs=Decimal("1e-25")
        )


def test_additive_devig_omitted_when_it_would_create_nonpositive_probability() -> None:
    methods = devig_all(
        {
            "heavy_a": Decimal("1.02"),
            "heavy_b": Decimal("1.05"),
            "longshot": Decimal("100"),
        }
    )
    assert "additive" not in methods


def test_haircut_modes_and_time_bands() -> None:
    fair = Decimal("0.1")
    near = longshot_haircut(fair, Decimal("24"), "slope")
    mid = longshot_haircut(fair, Decimal("100"), "slope")
    far = longshot_haircut(fair, Decimal("200"), "slope")
    assert near == fair
    assert mid < near
    assert far < mid
    assert longshot_haircut(fair, Decimal("100"), "flat") == Decimal("0.09")
    assert longshot_haircut(fair, Decimal("100"), "none") == fair
    with pytest.raises(ValueError, match="unknown longshot haircut"):
        longshot_haircut(fair, Decimal("100"), "bad")
