from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from prediction_bot.fees import SeriesFeeCache, maker_fee, taker_fee, taker_fee_rate
from prediction_bot.paper.ledger import Ledger


def test_series_fee_math_and_rounding() -> None:
    assert taker_fee(Decimal("0.5"), Decimal(1), Decimal(1)) == Decimal("0.02")
    assert taker_fee_rate(Decimal("0.5"), Decimal(1)) == Decimal("0.0175")
    assert maker_fee(
        Decimal("0.5"), Decimal(1), Decimal(1), "quadratic_with_maker_fees", "centicent"
    ) == Decimal("0.0044")
    assert maker_fee(Decimal("0.5"), Decimal(1), Decimal("0.5"), "quadratic") == Decimal("0.00")


def test_fee_functions_reject_unknown_rounding_and_fee_type() -> None:
    with pytest.raises(ValueError, match="unsupported fee rounding"):
        taker_fee(Decimal("0.5"), Decimal(1), Decimal(1), "mill")
    with pytest.raises(ValueError, match="unknown fee type"):
        maker_fee(Decimal("0.5"), Decimal(1), Decimal(1), "unknown")


class FakeKalshi:
    def __init__(self, series: list[dict[str, object]] | None = None, fail: bool = False):
        self.series = series or []
        self.fail = fail
        self.calls = 0

    def get_series(self) -> list[dict[str, object]]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("offline")
        return self.series


def test_series_fee_cache_refreshes_once_and_persists(tmp_path) -> None:
    now = datetime(2026, 10, 23, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "fees.sqlite", Decimal("100"))
    kalshi = FakeKalshi(
        [{"ticker": "KXNHLGAME", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}]
    )
    cache = SeriesFeeCache(ledger, kalshi, clock=lambda: now)  # type: ignore[arg-type]

    assert cache.get("KXNHLGAME") == ("quadratic_with_maker_fees", Decimal(1))
    assert cache.get("KXNHLGAME") == ("quadratic_with_maker_fees", Decimal(1))
    assert kalshi.calls == 1


def test_series_fee_cache_fails_closed_for_unknown_and_uses_stale_value(tmp_path) -> None:
    now = datetime(2026, 10, 23, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "fees.sqlite", Decimal("100"))
    cache = SeriesFeeCache(
        ledger,  # type: ignore[arg-type]
        FakeKalshi([{"ticker": "KXNHLGAME", "fee_type": "unknown", "fee_multiplier": 1}]),
        clock=lambda: now,
    )
    assert cache.get("KXNHLGAME") == ("unknown", Decimal(1))

    ledger.save_series_fee(
        "KXNHLGAME",
        "quadratic_with_maker_fees",
        Decimal("0.5"),
        now - timedelta(hours=25),
    )
    stale_cache = SeriesFeeCache(
        ledger,  # type: ignore[arg-type]
        FakeKalshi(fail=True),
        clock=lambda: now,
    )
    assert stale_cache.get("KXNHLGAME") == ("quadratic_with_maker_fees", Decimal("0.5"))
