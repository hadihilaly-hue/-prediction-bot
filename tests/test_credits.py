"""Credits fix (Part 2)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from prediction_bot.cadence import CadenceCalendar, build_plan
from prediction_bot.odds import OddsPacer
from prediction_bot.paper.ledger import Ledger

UTC = timezone.utc
CALENDAR = CadenceCalendar.load(Path(__file__).parents[1] / "data" / "cadence_calendar.json")
NOV29 = date(2026, 11, 29)
# 100 credits left over Nov 29-30 weighted 0.038 / 0.962: a day budget of
# 0.95 x 100 x 0.038 = 3.61, which at rotation_share 0.7 is 2.527 credits.
OVERRIDES = {NOV29: 0.038, date(2026, 11, 30): 0.962}
SLOTS = [datetime(2026, 11, 29, 2 * i, 17, tzinfo=UTC) for i in range(12)]


def _ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "credits.sqlite", Decimal("1000"))


# --- 2.1 the pacer hands out the day's target, not ~3x it ---------------------------------


def _accrue(slots: list[datetime]) -> float:
    granted = 0.0
    for now in slots:
        plan = build_plan(now, 100, 12, CALENDAR, overrides=OVERRIDES, granted_today=granted)
        assert plan.budget == pytest.approx(3.61)
        granted += plan.share
    return granted * 0.7


def test_twelve_slots_sum_to_the_day_target() -> None:
    assert _accrue(SLOTS) == pytest.approx(2.527)
    # The old share (target / slots left, recomputed every slot) summed to ~3.1x.
    old = sum(build_plan(now, 100, 12, CALENDAR, overrides=OVERRIDES).share for now in SLOTS)
    assert old * 0.7 == pytest.approx(2.527 * sum(1 / k for k in range(1, 13)))


def test_missed_slots_are_caught_up_by_later_ones() -> None:
    kept = [slot for i, slot in enumerate(SLOTS, start=1) if not 3 <= i <= 8]
    assert _accrue(kept) == pytest.approx(2.527)


def test_a_repeated_slot_cannot_push_the_day_past_its_target() -> None:
    twice = [*SLOTS[:5], SLOTS[4] + timedelta(minutes=40), *SLOTS[5:]]
    assert _accrue(twice) == pytest.approx(2.527)


class _Quota:
    def __init__(self, left: int) -> None:
        self.left = left
        self.budget: int | None = None
        self.requests_remaining: str | None = str(left)
        self.requests_used: str | None = "0"
        self.requests_made = 0
        self.odds_requests_made = 0
        self.requests_last_by_sport: dict[str, int | None] = {}
        self.events_free = True
        self._events_requests_used_baseline: int | None = None

    def quota_remaining(self) -> int:
        return self.left


def test_pacer_records_granted_share_per_day(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    for now in [*SLOTS, SLOTS[4] + timedelta(minutes=40)]:
        pacer = OddsPacer(
            _Quota(100),  # type: ignore[arg-type]
            12,
            ledger=ledger,
            cadence_mode="off",
            calendar=CALENDAR,
            day_overrides=OVERRIDES,
        )
        pacer.rotation_share = 0.7
        pacer.start_run(now)
        pacer.finish_run(now)
    assert ledger.cadence_granted(NOV29) * 0.7 == pytest.approx(2.527)
    assert ledger.cadence_granted(NOV29 + timedelta(days=1)) == 0.0
