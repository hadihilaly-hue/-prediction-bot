"""Credit cadence: static/dyn day weights, budget share, floors/caps,
under-spend breadth — and that PBOT_CADENCE_MODE=legacy reproduces the old
pacer numbers exactly."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx

from prediction_bot.cadence import (
    CadenceCalendar,
    blend,
    build_plan,
    dynamic_counts,
    parse_day_overrides,
)
from prediction_bot.odds import OddsClient, OddsPacer, run_share
from prediction_bot.paper.ledger import Ledger

FIXTURE_CALENDAR = {
    "credits_per_month": 500,
    "entry_window_hours": [72, 168],
    "seasons": {
        "sport_a": [{"start": "2026-10-01", "end": "2026-12-31"}],
        "sport_b": [{"start": "2026-11-01", "end": "2026-11-30"}],
        "sport_off": [{"start": "2027-03-01", "end": "2027-10-01"}],
        "golf_majors": [{"dates": ["2026-10-30"]}],
    },
    "blackouts": [
        {
            "sports": ["sport_a"],
            "start": "2026-10-20",
            "end": "2026-10-22",
            "note": "all-star break",
        }
    ],
    "weekly_profile": {
        "sport_a": {"Mon": 2.0, "Tue": 1.0},
        "sport_b": {"Mon": 1.0},
    },
    "clusters": [
        {"pull_start": "2026-10-12", "pull_end": "2026-10-14", "weight": 2.0, "why": "week"}
    ],
    "dead_zones": [{"start": "2026-10-19", "end": "2026-10-23", "why": "quiet"}],
}


def cal() -> CadenceCalendar:
    return CadenceCalendar(FIXTURE_CALENDAR)


def _client(left: int, used: int = 0) -> OddsClient:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[],
            headers={"x-requests-remaining": str(left), "x-requests-used": str(used)},
        )

    return OddsClient("k", client=httpx.Client(transport=httpx.MockTransport(handler)))


# --- legacy parity: the old path must reproduce today's numbers exactly ---


def test_legacy_mode_reproduces_run_share(tmp_path) -> None:
    ledger = Ledger(tmp_path / "l.sqlite", Decimal("100"))
    for left in (0, 1, 10, 123, 500):
        for now in (
            datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 5, 11, 30, tzinfo=timezone.utc),
            datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc),
        ):
            client = _client(left)
            pacer = OddsPacer(client, runs_per_day=12, ledger=ledger, cadence_mode="legacy")
            got = pacer.start_run(now)
            expected = run_share(left, 12, now)
            if expected < 1:
                # fractions accumulate across runs before a request is earned
                acc = expected
                assert got == (0 if acc < 1 else int(acc))
            else:
                assert got == int(expected), (left, now, got, expected)


def test_calendar_missing_file_falls_back_to_legacy_semantics(tmp_path) -> None:
    # OddsPacer without a calendar behaves exactly like legacy.
    client = _client(120)
    pacer = OddsPacer(client, runs_per_day=12, cadence_mode="calendar", calendar=None)
    now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
    assert pacer.start_run(now) == int(run_share(120, 12, now))


# --- static weight math on the fixture calendar ---


def test_static_weight_sums_active_sport_profiles() -> None:
    c = cal()
    mon = date(2026, 11, 2)  # Monday; window Nov 5-9 inside both seasons
    assert "sport_a" in c.active_sports(mon) and "sport_b" in c.active_sports(mon)
    assert c.base_weight(mon) == 2.0 + 1.0
    assert c.static_weight(mon) == 3.0


def test_blackout_days_excluded() -> None:
    c = cal()
    # Pull Sat Oct 17: game window Oct 20-24 -> Oct 20-22 blacked out but
    # Oct 23-24 still in season, so sport_a stays active.
    assert "sport_a" in c.active_sports(date(2026, 10, 17))
    # Pull Mon Oct 12: game window Oct 15-19, entirely before the blackout.
    assert "sport_a" in c.active_sports(date(2026, 10, 12))
    # Blackout covering the whole window kills the sport: pull Oct 15 has
    # window Oct 18-22; with blackout Oct 18-22 there is no in-season day.
    c2 = CadenceCalendar(
        {
            **FIXTURE_CALENDAR,
            "blackouts": [
                {
                    "sports": ["sport_a"],
                    "start": "2026-10-18",
                    "end": "2026-10-22",
                    "note": "full",
                }
            ],
        }
    )
    assert "sport_a" not in c2.active_sports(date(2026, 10, 15))


def test_cluster_and_deadzone_factors() -> None:
    c = cal()
    assert c.cluster_weight(date(2026, 10, 13)) == 2.0
    assert c.cluster_weight(date(2026, 10, 5)) == 1.0
    assert c.deadzone_factor(date(2026, 10, 21)) == 0.5
    assert c.deadzone_factor(date(2026, 10, 5)) == 1.0


def test_golf_dates_active_only_on_major_week() -> None:
    c = cal()
    # golf_majors game Oct 30 -> pullable Oct 23-27
    assert "golf_majors" in c.active_sports(date(2026, 10, 26))
    assert "golf_majors" not in c.active_sports(date(2026, 10, 5))


# --- dynamic blend and the 7-day horizon fallback ---


def test_dynamic_counts_bucketing() -> None:
    c = cal()
    today = date(2026, 10, 5)
    events = [
        # commence Oct 9 -> in window of pull days Oct 2-6 -> counts for Oct 5,6
        {
            "sport_key": "sport_a",
            "home_team": "Alpha",
            "away_team": "Beta",
            "commence_time": "2026-10-09T20:00:00Z",
        },
        # unmapped names never count
        {
            "sport_key": "sport_a",
            "home_team": "Nope",
            "away_team": "Nada",
            "commence_time": "2026-10-09T20:00:00Z",
        },
    ]
    names = {"Alpha", "Beta"}
    counts = dynamic_counts(events, today, c, lambda _sport: names)
    assert counts[today] == 1 and counts[today + timedelta(days=1)] == 1
    assert counts[today + timedelta(days=4)] == 0


def test_blend_bands() -> None:
    assert blend(4.0, 6, 6.0) == 4.0  # at median: no change
    assert blend(4.0, 12, 6.0) == 6.0  # +50% cap behaviour of the formula
    assert blend(4.0, 0, 6.0) == 2.0  # zero mapped: half static share
    assert blend(4.0, None, 6.0) == 4.0  # no dyn layer: static alone
    assert blend(4.0, 0, 0.0) == 4.0  # degenerate median: static alone


def test_horizon_fallback_uses_static_only(tmp_path) -> None:
    c = cal()
    today = date(2026, 10, 5)
    now = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    dyn = {today + timedelta(days=i): 3 for i in range(7)}
    plan = build_plan(now, 300, 12, c, dyn=dyn)
    # Every remaining day has a weight; days beyond today+6 keep static only.
    for d, w in plan.weights.items():
        if d > today + timedelta(days=6):
            assert w == c.static_weight(d)
    assert plan.day == today and plan.dyn_count == 3


def test_mode_off_never_calls_events(tmp_path) -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(
            200, json=[], headers={"x-requests-remaining": "300", "x-requests-used": "1"}
        )

    client = OddsClient("k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    ledger = Ledger(tmp_path / "off.sqlite", Decimal("100"))
    pacer = OddsPacer(
        client,
        runs_per_day=12,
        ledger=ledger,
        cadence_mode="off",
        calendar=cal(),
    )
    pacer.start_run(datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
    assert not any("/events" in url for url in calls)


# --- floors, caps, breadth, overrides ---


def test_day_floor_two_pulls_when_active() -> None:
    c = cal()
    now = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    # Tiny left-over: raw share would be far below 2 pulls/day.
    plan = build_plan(now, 1, 12, c)
    assert plan.budget == 2.0


def test_day_cap_forty_pulls() -> None:
    c = cal()
    now = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    plan = build_plan(now, 450, 12, c)
    assert plan.budget <= 40.0


def test_breadth_multiplier_on_underspend() -> None:
    c = cal()
    # Oct 21 with ~90% of quota left: the capped remainder cannot absorb it.
    now = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
    plan = build_plan(now, 460, 12, c)
    assert plan.breadth == 2.0
    # Early in the month the check never fires.
    early = build_plan(datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), 460, 12, c)
    assert early.breadth == 1.0


def test_day_override_replaces_weight() -> None:
    c = cal()
    day = date(2026, 10, 5)
    overrides = {day: 100.0}
    now = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    plan = build_plan(now, 300, 12, c, overrides=overrides)
    assert plan.blended_w == 100.0
    assert parse_day_overrides("2026-10-05=100,2026-10-06=0.5") == {
        date(2026, 10, 5): 100.0,
        date(2026, 10, 6): 0.5,
    }


# --- pacer integration ---


def test_calendar_mode_rounds_share_and_records_day(tmp_path) -> None:
    ledger = Ledger(tmp_path / "cad.sqlite", Decimal("100"))
    client = _client(300)
    pacer = OddsPacer(
        client,
        runs_per_day=12,
        ledger=ledger,
        cadence_mode="calendar",
        calendar=cal(),
    )
    now = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    budget = pacer.start_run(now)
    assert 0 <= budget <= 3
    row = ledger.cadence_day(date(2026, 10, 5))
    assert row is not None and row["mode"] == "calendar"
    pacer.finish_run(now, feeds=["sport_a"])
    row = ledger.cadence_day(date(2026, 10, 5))
    assert row["feeds_json"] == '["sport_a"]'


def test_events_free_check_disables_dyn(tmp_path) -> None:
    # When x-requests-used rises after an events call, dyn is dropped and
    # the plan falls back to static only.
    state = {"used": 10}

    def handler(req: httpx.Request) -> httpx.Response:
        state["used"] += 1  # every call costs quota -> events is not free
        return httpx.Response(
            200,
            json=[],
            headers={
                "x-requests-remaining": "300",
                "x-requests-used": str(state["used"]),
            },
        )

    client = OddsClient("k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    ledger = Ledger(tmp_path / "nofree.sqlite", Decimal("100"))
    real_cal = CadenceCalendar(
        {
            **FIXTURE_CALENDAR,
            "seasons": {"americanfootball_nfl": [{"start": "2026-09-01", "end": "2027-01-31"}]},
        }
    )
    pacer = OddsPacer(
        client,
        runs_per_day=12,
        ledger=ledger,
        cadence_mode="calendar",
        calendar=real_cal,
    )
    pacer.start_run(datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc))
    assert not client.events_free
    row = ledger.cadence_day(date(2026, 10, 5))
    assert row is not None and row["dyn_json"] is None
