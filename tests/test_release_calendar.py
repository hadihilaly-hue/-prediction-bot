"""v5 Phase 3-E: release calendar, event-clock plan targets, attribution, light tick."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import prediction_bot.cli as cli
from prediction_bot.clv_report import clv_report
from prediction_bot.config import Settings
from prediction_bot.odds import OddsPacer
from prediction_bot.paper.ledger import Ledger
from prediction_bot.pull_plan import (
    mark_pulled_ids,
    plan_daily_cap,
    select_plan_pull,
    tag_pull_rows,
)
from prediction_bot.release_calendar import ReleaseCalendar, plan_priority

UTC = timezone.utc
ROOT = Path(__file__).parents[1]
CALENDAR_PATH = ROOT / "data" / "release_calendar.json"
SETTINGS = Settings(_env_file=None)  # type: ignore[call-arg]
CAL = ReleaseCalendar.load(CALENDAR_PATH)


def _ledger(tmp_path: Path, name: str = "plan.sqlite") -> Ledger:
    return Ledger(tmp_path / name, Decimal("1000"))


def _eligible(ledger: Ledger, feeds: list[str], at: datetime) -> None:
    ledger.conn.executemany(
        "INSERT INTO feed_stats (computed_at,feed,lead_p50_h,lead_p90_h,pulls_14d,"
        "useful_pulls_14d,provisional,eligible) VALUES (?,?,100,200,0,0,0,1)",
        [(at.isoformat(), feed) for feed in feeds],
    )
    ledger.conn.commit()


def _game(ledger: Ledger, event: str, kickoff: datetime, offset_h: float = 3) -> None:
    ledger.conn.execute(
        "INSERT INTO market_snapshots (fetched_at,ticker,event_ticker,series,yes_bid,yes_ask,"
        "yes_bid_size,yes_ask_size,open_interest,volume,expected_expiration) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            (kickoff - timedelta(hours=30)).isoformat(),
            f"{event}-A",
            event,
            event.split("-", 1)[0],
            "0.48",
            "0.50",
            "100",
            "100",
            "10",
            "10",
            (kickoff + timedelta(hours=offset_h)).isoformat(),
        ),
    )
    ledger.conn.commit()


def _budget(ledger: Ledger, start: datetime, days: int, budget: float) -> None:
    for i in range(days):
        ledger.record_cadence_day(
            (start + timedelta(days=i)).date(), static_w=1, dyn_count=None, dyn_json=None,
            blended_w=1, budget=budget, mode="off", at=start,
        )  # fmt: skip


def _ticks(start: datetime, end: datetime, minutes: tuple[int, ...]) -> list[datetime]:
    at = start.replace(minute=0, second=0, microsecond=0)
    out = []
    while at <= end:
        out.extend(at.replace(minute=m) for m in minutes if start <= at.replace(minute=m) <= end)
        at += timedelta(hours=1)
    return out


TEN_MIN = (7, 17, 27, 37, 47, 57)  # the Cloudflare Worker
CRON = (17, 47)  # the GitHub fallback


def _run(
    ledger: Ledger,
    start: datetime,
    end: datetime,
    minutes: tuple[int, ...] = TEN_MIN,
    settings: Settings = SETTINGS,
    calendar: ReleaseCalendar = CAL,
) -> list[tuple[datetime, str]]:
    """Every tick: rebuild the plan as the CLI does, pull the funded feed."""
    pulls = []
    for now in _ticks(start, end, minutes):
        cli._refresh_pull_plan(ledger, now, settings, calendar)
        pick = select_plan_pull(ledger, settings, now, calendar=calendar)
        if pick is not None:
            mark_pulled_ids(ledger, pick.feed, now, 1.0)
            pulls.append((now, pick.feed))
    cli._refresh_pull_plan(ledger, end + timedelta(days=3), settings, calendar)
    return pulls


def _rows(ledger: Ledger, where: str = "1=1") -> list[dict[str, Any]]:
    cur = ledger.conn.execute(f"SELECT * FROM pull_plan WHERE {where} ORDER BY target_at,id")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


# --- calendar file ---------------------------------------------------------------


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(data))
    return path


def test_seed_calendar_loads_with_nba_reserved_off() -> None:
    assert not CAL.has_entries("basketball_nba")
    assert ReleaseCalendar.load(CALENDAR_PATH, enable=["basketball_nba"]).has_entries(
        "basketball_nba"
    )
    nfl = CAL.entries("americanfootball_nfl")
    assert {e.kind for e in nfl} == {"clock", "kickoff"}
    assert [e.minutes_before for e in CAL.kickoff_entries("soccer_epl")] == [70]
    assert [e.minutes_before for e in CAL.kickoff_entries("soccer_usa_mls")] == [55]
    assert plan_priority(CAL.close_priority) == 1 and plan_priority(2) == 4


def _one(feed: str, **entry: Any) -> dict[str, Any]:
    return {"feeds": {feed: {"entries": [entry]}}}


@pytest.mark.parametrize(
    "bad",
    [
        _one("not_a_feed", kind="kickoff", minutes_before=60, priority=3),
        _one("soccer_epl", kind="clock", days=["funday"], time="10:00", priority=3),
        _one("soccer_epl", kind="clock", days=["sun"], time="25:99", priority=3),
        _one("soccer_epl", kind="kickoff", minutes_before=60, priority=6),
        _one("soccer_epl", kind="kickoff", minutes_before=60, priority=3, typo=1),
        {"timezone": "Mars/Olympus", "feeds": {}},
    ],
)
def test_calendar_validation_rejects_bad_input(tmp_path: Path, bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ReleaseCalendar.load(_write(tmp_path, bad))


@pytest.mark.parametrize(
    ("local_day", "expected_utc"),
    [
        # Fall back, Sun Nov 1 2026: EDT (-4) before, EST (-5) after.
        ("2026-10-30", "2026-10-30T20:20:00+00:00"),
        ("2026-11-06", "2026-11-06T21:20:00+00:00"),
        # Spring forward, Sun Mar 14 2027: EST before, EDT after.
        ("2027-03-12", "2027-03-12T21:20:00+00:00"),
        ("2027-03-19", "2027-03-19T20:20:00+00:00"),
    ],
)
def test_clock_targets_keep_local_time_across_dst(local_day: str, expected_utc: str) -> None:
    day = datetime.fromisoformat(local_day).replace(tzinfo=UTC)
    targets = [
        t
        for t in CAL.clock_targets("americanfootball_nfl", day, day + timedelta(days=1, hours=6))
        if t.local_date.isoformat() == local_day
    ]
    assert [t.target_at.isoformat() for t in targets] == [expected_utc]
    assert targets[0].name == f"R-{local_day}T16:20"


def test_clock_targets_on_the_dst_change_days() -> None:
    for day, expected in (
        (datetime(2026, 11, 1, tzinfo=UTC), "2026-11-01T16:35:00+00:00"),
        (datetime(2027, 3, 14, tzinfo=UTC), "2027-03-14T15:35:00+00:00"),
    ):
        sunday = [
            t.target_at.isoformat()
            for t in CAL.clock_targets("americanfootball_nfl", day, day + timedelta(days=1))
            if t.entry.time == "11:35"
        ]
        assert sunday == [expected]


# --- planner -----------------------------------------------------------------------

SUNDAY = datetime(2026, 11, 8, tzinfo=UTC)  # EST: 1:00 pm ET = 18:00Z


def _nfl_sunday(ledger: Ledger) -> None:
    _eligible(ledger, ["americanfootball_nfl"], SUNDAY - timedelta(days=4))
    for i in range(6):
        _game(ledger, f"KXNFLGAME-26NOV08G{i}", SUNDAY.replace(hour=18))
    _game(ledger, "KXNFLGAME-26NOV08LATE1", SUNDAY.replace(hour=21, minute=5))
    _game(ledger, "KXNFLGAME-26NOV08LATE2", SUNDAY.replace(hour=21, minute=25))
    _game(ledger, "KXNFLGAME-26NOV08SNF", SUNDAY.replace(day=9, hour=1, minute=20))


def test_sunday_nfl_slate_clock_kickoff_and_close_grouped(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _nfl_sunday(ledger)
    _budget(ledger, SUNDAY - timedelta(days=2), 4, 100)
    pulls = _run(ledger, SUNDAY.replace(hour=12), SUNDAY.replace(day=9, hour=2))

    release = _rows(ledger, "source='release'")
    assert [r["target"] for r in release] == [
        "R-2026-11-08T11:35",
        "R-2026-11-08T14:55",
        "R-2026-11-08T18:50",
    ]
    assert all(r["status"] == "pulled" for r in release)
    assert all(r["status"] == "pulled" for r in _rows(ledger, "event_ticker LIKE 'KXNFL%'"))
    # No T-24h/T-6h/T-90m on a feed with calendar entries.
    assert not _rows(ledger, "target IN ('T-24h','T-6h','T-90m')")
    # The 1:00 pm cluster's -95 target (11:25) merges into the 11:35 clock pull, so the
    # six games cost one release credit plus one close credit.
    early = _rows(ledger, "event_ticker LIKE 'KXNFLGAME-26NOV08G%'")
    assert {r["target"] for r in early} == {"K-95m", "T-10m"}
    k95 = [r for r in early if r["target"] == "K-95m"]
    assert all(r["merged_into"] == release[0]["id"] for r in k95)
    assert all(r["pulled_at"] == release[0]["pulled_at"] for r in k95)
    assert all(r["pulled_at"].startswith("2026-11-08T16:37") for r in k95)
    # Every 18:00 close is taken inside the sharp-close window (17:40-17:58).
    closes = [r["pulled_at"] for r in early if r["target"] == "T-10m"]
    assert set(closes) == {"2026-11-08T17:47:00+00:00"}
    # 3 clock pulls + the 4:05 game's -95 pull (19:30, 25 min before the 14:55 clock)
    # + 4 kickoff-cluster closes.
    assert len(pulls) == 8
    total = sum(float(r["credits"]) for r in _rows(ledger))
    assert total == pytest.approx(8.0)


def test_midweek_ucl_slate_kickoff_55_grouped_and_the_17_45_close(tmp_path: Path) -> None:
    tuesday = datetime(2026, 11, 3, tzinfo=UTC)

    def slate(path: Path) -> Ledger:
        ledger = _ledger(path)
        _eligible(ledger, ["soccer_uefa_champs_league"], tuesday - timedelta(days=3))
        for i in range(2):
            _game(ledger, f"KXUCLGAME-26NOV03E{i}", tuesday.replace(hour=17, minute=45), 2)
        for i in range(6):
            _game(ledger, f"KXUCLGAME-26NOV03L{i}", tuesday.replace(hour=20), 2)
        _budget(ledger, tuesday - timedelta(days=1), 3, 100)
        return ledger

    (tmp_path / "ten").mkdir()
    ledger = slate(tmp_path / "ten")
    _run(ledger, tuesday.replace(hour=12), tuesday.replace(hour=21))
    late = _rows(ledger, "event_ticker LIKE 'KXUCLGAME-26NOV03L%'")
    assert {r["target"] for r in late} == {"K-55m", "T-10m"}
    assert all(r["status"] == "pulled" for r in late)
    assert sum(float(r["credits"]) for r in late) == pytest.approx(2.0)  # one per target time
    early_close = _rows(ledger, "event_ticker LIKE 'KXUCLGAME-26NOV03E%' AND target='T-10m'")
    # The 10-minute tick at 17:27 or 17:37 reaches the [17:25, 17:43] close window.
    assert {r["status"] for r in early_close} == {"pulled"}

    (tmp_path / "cron").mkdir()
    ledger = slate(tmp_path / "cron")
    _run(ledger, tuesday.replace(hour=12), tuesday.replace(hour=21), minutes=CRON)
    early_close = _rows(ledger, "event_ticker LIKE 'KXUCLGAME-26NOV03E%' AND target='T-10m'")
    assert {r["status"] for r in early_close} == {"missed"}


def test_same_feed_targets_within_20_minutes_share_one_pull(tmp_path: Path) -> None:
    saturday = datetime(2026, 11, 7, tzinfo=UTC)
    ledger = _ledger(tmp_path)
    _eligible(ledger, ["soccer_epl"], saturday - timedelta(days=3))
    _game(ledger, "KXEPLGAME-26NOV07A", saturday.replace(hour=15), 2)  # -70 at 13:50
    _game(ledger, "KXEPLGAME-26NOV07B", saturday.replace(hour=15, minute=15), 2)  # 14:05
    _game(ledger, "KXEPLGAME-26NOV07C", saturday.replace(hour=15, minute=40), 2)  # 14:30
    _budget(ledger, saturday - timedelta(days=1), 3, 100)
    # Stop before A's T-10m close window (14:40) so only kickoff pulls are counted.
    pulls = _run(ledger, saturday.replace(hour=13), saturday.replace(hour=14, minute=39))

    k70 = {r["event_ticker"][-1]: r for r in _rows(ledger, "target='K-70m'")}
    assert k70["B"]["merged_into"] == k70["A"]["id"]  # 15 min apart
    assert k70["C"]["merged_into"] is None  # 25 min after A
    assert k70["A"]["pulled_at"] == k70["B"]["pulled_at"]
    assert float(k70["A"]["credits"]) + float(k70["B"]["credits"]) == pytest.approx(1.0)
    assert [feed for _at, feed in pulls].count("soccer_epl") == 2


def test_release_target_expires_after_its_window(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _eligible(ledger, ["soccer_epl"], SUNDAY - timedelta(days=3))
    _game(ledger, "KXEPLGAME-26NOV08A", SUNDAY.replace(hour=16), 2)  # K-70m at 14:50
    before = SUNDAY.replace(hour=14)
    cli._refresh_pull_plan(ledger, before, SETTINGS, CAL)
    row = _rows(ledger, "target='K-70m'")[0]
    assert (row["window_start"], row["window_end"]) == (
        "2026-11-08T14:50:00+00:00",
        "2026-11-08T15:50:00+00:00",
    )
    cli._refresh_pull_plan(ledger, SUNDAY.replace(hour=15, minute=49), SETTINGS, CAL)
    assert _rows(ledger, "target='K-70m'")[0]["status"] == "planned"
    cli._refresh_pull_plan(ledger, SUNDAY.replace(hour=15, minute=51), SETTINGS, CAL)
    assert _rows(ledger, "target='K-70m'")[0]["status"] == "missed"


def test_kickoff_window_never_runs_past_kickoff_minus_two(tmp_path: Path) -> None:
    calendar = ReleaseCalendar.model_validate(
        _one("soccer_epl", kind="kickoff", minutes_before=30, priority=3)
    )
    ledger = _ledger(tmp_path)
    _eligible(ledger, ["soccer_epl"], SUNDAY - timedelta(days=3))
    _game(ledger, "KXEPLGAME-26NOV08A", SUNDAY.replace(hour=16), 2)
    cli._refresh_pull_plan(ledger, SUNDAY.replace(hour=12), SETTINGS, calendar)
    assert _rows(ledger, "target='K-30m'")[0]["window_end"] == "2026-11-08T15:58:00+00:00"


def test_lowest_priority_is_skipped_first_when_credits_are_short(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    friday = datetime(2026, 11, 6, tzinfo=UTC)
    _eligible(ledger, ["americanfootball_nfl", "baseball_mlb"], friday - timedelta(days=3))
    _game(ledger, "KXNFLGAME-26NOV08G0", SUNDAY.replace(hour=18))
    # First pitch 00:40Z: its -200 target lands at 21:20Z, on the NFL 16:20 ET clock.
    _game(ledger, "KXMLBGAME-26NOV06A", friday.replace(day=7, hour=0, minute=40))
    _budget(ledger, friday, 2, 2)  # floor(2 x 0.5) = 1 plan credit a day
    assert plan_daily_cap(ledger, SETTINGS, friday) == 1
    _run(ledger, friday.replace(hour=21), friday.replace(hour=22, minute=30))
    assert _rows(ledger, "source='release'")[0]["status"] == "pulled"
    mlb = _rows(ledger, "feed='baseball_mlb' AND target='K-200m'")[0]
    assert mlb["status"] == "skipped_budget"


def test_move_reserve_holds_back_plan_credits(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    now = SUNDAY.replace(hour=17, minute=47)
    _nfl_sunday(ledger)
    _budget(ledger, SUNDAY, 1, 100)
    for at, left in ((datetime(2026, 11, 1, 6, 17, tzinfo=UTC), 500), (now, 70)):
        ledger.conn.execute(
            "INSERT INTO runs (ran_at,strategies,scanned,signals,filled,settled,cash,open_cost,"
            "odds_remaining) VALUES (?,?,0,0,0,0,'1000','0',?)",
            ((at - timedelta(minutes=30)).isoformat(), "sportsbook_v3", left),
        )
    ledger.conn.commit()
    cli._refresh_pull_plan(ledger, now, SETTINGS, CAL)
    # 70 left: 70 - 1 < 25 hard reserve + 50 move reserve, so the close is held back.
    assert select_plan_pull(ledger, SETTINGS, now, calendar=CAL) is None
    for i in range(30):  # 30 move pulls this month leave 20 of the reserve
        ledger.conn.execute(
            "INSERT INTO odds_triggers (created_at,feed,ticker,ask,status) "
            "VALUES (?,?,?,?,'pulled')",
            ((SUNDAY - timedelta(days=1)).isoformat(), "soccer_epl", f"T{i}", "0.5"),
        )
    ledger.conn.commit()
    pick = select_plan_pull(ledger, SETTINGS, now, calendar=CAL)
    assert pick is not None and pick.source == "close"


def test_plan_allowance_accrues_by_day_with_two_days_borrowed(tmp_path: Path) -> None:
    from prediction_bot.pull_plan import plan_allowance, plan_allowance_left

    ledger = _ledger(tmp_path)
    assert plan_allowance(ledger, SETTINGS, SUNDAY) is None  # no balance recorded yet
    ledger.conn.execute(
        "INSERT INTO runs (ran_at,strategies,scanned,signals,filled,settled,cash,open_cost,"
        "odds_remaining) VALUES ('2026-11-01T00:47:00+00:00','sportsbook_v3',0,0,0,0,'1000',"
        "'0',500)"
    )
    ledger.conn.commit()
    # Usable = 500 - 25 hard reserve - 50 move reserve = 425; the plan's half is 212.5.
    first = datetime(2026, 11, 1, 12, tzinfo=UTC)
    assert plan_allowance(ledger, SETTINGS, first) == pytest.approx(212.5 * 3 / 30)
    assert plan_allowance(ledger, SETTINGS, datetime(2026, 11, 29, tzinfo=UTC)) == 212.5
    fallback = SETTINGS.model_copy(update={"odds_grid": "fallback"})
    assert plan_allowance(ledger, fallback, first) == pytest.approx(425 * 3 / 30)
    ledger.conn.execute(
        "INSERT INTO pull_plan (event_ticker,feed,target,kickoff,target_at,window_start,"
        "window_end,priority,status,credits,planned_at,pulled_at) VALUES ('E','soccer_epl',"
        "'T-10m','k','t','s','e',1,'pulled',5,'p','2026-11-01T10:00:00+00:00')"
    )
    assert plan_allowance_left(ledger, SETTINGS, first) == pytest.approx(212.5 * 3 / 30 - 5)


class FakeOdds:
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
        self.requests_remaining = str(self.left)
        return self.left

    def pull(self, feed: str) -> bool:
        if self.budget is None or self.requests_made >= self.budget or self.left <= 0:
            return False
        self.left -= 1
        self.requests_made += 1
        self.odds_requests_made += 1
        self.requests_last_by_sport[feed] = 1
        self.requests_remaining = str(self.left)
        return True


def test_pacer_move_reserve_blocks_plan_and_slows_the_rotation(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    plan = OddsPacer(FakeOdds(70), 12, ledger=ledger, plan_mode=True)  # type: ignore[arg-type]
    plan.move_reserve = 50
    assert plan.start_run(SUNDAY) == 0
    plan = OddsPacer(FakeOdds(80), 12, ledger=ledger, plan_mode=True)  # type: ignore[arg-type]
    plan.move_reserve = 50
    assert plan.start_run(SUNDAY) == 1
    rotation = OddsPacer(FakeOdds(80), 12, ledger=ledger, plan_mode=False)  # type: ignore[arg-type]
    plain = OddsPacer(FakeOdds(80), 12, ledger=ledger, plan_mode=False)  # type: ignore[arg-type]
    rotation.move_reserve = 50
    rotation.start_run(SUNDAY)
    plain.start_run(SUNDAY)
    assert rotation.credit < plain.credit
    # Trigger runs are what the reserve is for: they still get their credit.
    trigger = OddsPacer(FakeOdds(30), 12, ledger=ledger, trigger_mode=True)  # type: ignore[arg-type]
    trigger.move_reserve = 50
    assert trigger.start_run(SUNDAY) == 1


def test_soccer_weekend_cap_funds_the_two_busiest_feeds(tmp_path: Path) -> None:
    saturday = datetime(2026, 11, 7, tzinfo=UTC)
    ledger = _ledger(tmp_path)
    feeds = ["soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a"]
    _eligible(ledger, feeds, saturday - timedelta(days=3))
    for i in range(4):
        _game(ledger, f"KXEPLGAME-26NOV07E{i}", saturday.replace(hour=17), 2)
    for i in range(3):
        _game(ledger, f"KXLALIGAGAME-26NOV07L{i}", saturday.replace(hour=17), 2)
    _game(ledger, "KXSERIEAGAME-26NOV07S0", saturday.replace(hour=17), 2)
    _budget(ledger, saturday - timedelta(days=1), 3, 100)
    _run(ledger, saturday.replace(hour=14), saturday.replace(hour=18))
    funded = {
        str(r[0])
        for r in ledger.conn.execute("SELECT DISTINCT feed FROM pull_plan WHERE credits>0")
    }
    assert funded == {"soccer_epl", "soccer_spain_la_liga"}
    serie_a = _rows(ledger, "feed='soccer_italy_serie_a'")
    assert {r["status"] for r in serie_a} == {"skipped_budget"}
    assert {r["note"] for r in serie_a} == {"soccer_weekend_cap"}


def test_max_per_day_caps_an_entry(tmp_path: Path) -> None:
    calendar = ReleaseCalendar.model_validate(
        _one("soccer_usa_mls", kind="kickoff", minutes_before=55, priority=3, max_per_day=1)
    )
    wednesday = datetime(2026, 11, 4, tzinfo=UTC)
    ledger = _ledger(tmp_path)
    _eligible(ledger, ["soccer_usa_mls"], wednesday - timedelta(days=3))
    _game(ledger, "KXMLSGAME-26NOV04A", wednesday.replace(hour=19), 2)
    _game(ledger, "KXMLSGAME-26NOV04B", wednesday.replace(hour=23), 2)
    _budget(ledger, wednesday, 2, 100)
    _run(
        ledger, wednesday.replace(hour=16), wednesday.replace(hour=23, minute=50), calendar=calendar
    )
    k55 = {r["event_ticker"][-1]: r for r in _rows(ledger, "target='K-55m'")}
    assert k55["A"]["status"] == "pulled"
    # B's own -55 pull (22:05) is held back; it is only satisfied later by the 22:47
    # close pull of the same feed, whose window it still overlaps.
    assert k55["B"]["note"] == "max_per_day"
    assert k55["B"]["pulled_at"] == "2026-11-04T22:47:00+00:00"
    pulls = ledger.conn.execute(
        "SELECT COUNT(DISTINCT pulled_at) FROM pull_plan WHERE entry='kickoff:-55' AND credits>0 "
        "AND pulled_at<'2026-11-04T22:40'"
    ).fetchone()[0]
    assert pulls == 1
    # The close is not capped.
    assert {r["status"] for r in _rows(ledger, "target='T-10m'")} == {"pulled"}


def test_grid_fallback_plans_one_15_00_pull_on_quiet_days(tmp_path: Path) -> None:
    fallback = SETTINGS.model_copy(update={"odds_grid": "fallback"})
    monday = datetime(2026, 11, 9, 12, tzinfo=UTC)
    ledger = _ledger(tmp_path)
    _eligible(ledger, ["soccer_epl", "americanfootball_nfl"], monday - timedelta(days=3))
    _game(ledger, "KXEPLGAME-26NOV09A", monday.replace(hour=20), 2)  # targets today
    _game(ledger, "KXNFLGAME-26NOV15G0", monday.replace(day=15, hour=18))  # 6 days out
    _budget(ledger, monday, 2, 10)
    cli._refresh_pull_plan(ledger, monday, fallback, CAL)
    grid = _rows(ledger, "source='grid'")
    # NFL: no target today or tomorrow -> grid at 15:00 ET (20:00Z); EPL: games today
    # -> no grid today, but a grid tomorrow.
    assert {(r["feed"], r["target"], r["target_at"]) for r in grid} >= {
        ("americanfootball_nfl", "G-2026-11-09", "2026-11-09T20:00:00+00:00"),
        ("soccer_epl", "G-2026-11-10", "2026-11-10T20:00:00+00:00"),
    }
    assert not [r for r in grid if r["feed"] == "soccer_epl" and r["target"] == "G-2026-11-09"]
    # Rotation off: the plan may spend the whole day budget.
    assert plan_daily_cap(ledger, fallback, monday) == 10
    assert plan_daily_cap(ledger, SETTINGS, monday) == 5
    cli._refresh_pull_plan(ledger, monday, SETTINGS, CAL)  # default: no new grid rows
    assert len(_rows(ledger, "source='grid'")) == len(grid)


def test_calendar_off_and_rotation_are_the_defaults_that_keep_phase_3() -> None:
    assert SETTINGS.odds_grid == "rotation"
    assert SETTINGS.odds_release_calendar == "on"
    assert SETTINGS.odds_move_reserve == 50
    off = SETTINGS.model_copy(update={"odds_release_calendar": "off"})
    assert cli._release_calendar(off) is None


# --- light tick, dispatch target, attribution ----------------------------------------


def test_light_tick_runs_only_when_a_plan_target_is_due(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "tick.sqlite"
    ledger = Ledger(path, Decimal("1000"))
    now = datetime.now(UTC)
    ledger.conn.execute(
        "INSERT INTO runs (ran_at,strategies,scanned,signals,filled,settled,cash,open_cost) "
        "VALUES (?,?,0,0,0,0,'1000','0')",
        ((now - timedelta(minutes=10)).isoformat(), "book_scanner"),
    )
    ledger.conn.commit()
    settings = Settings(_env_file=None, rules_version="v3", paper_db_path=path)  # type: ignore[call-arg]
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    runs_before = ledger.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    cli.slot_guard(slot="scanner", within_minutes=25, plan_due=True)
    assert capsys.readouterr().out.strip() == "skip"
    _eligible(ledger, ["americanfootball_nfl"], now - timedelta(hours=1))
    _game(ledger, "KXNFLGAME-X", now + timedelta(minutes=12))  # its T-10m close is due
    _budget(ledger, now, 1, 10)
    cli.slot_guard(slot="scanner", within_minutes=25, plan_due=True)
    assert capsys.readouterr().out.strip() == "run"
    cli.slot_guard(slot="scanner", within_minutes=25)  # without --plan-due: unchanged
    assert capsys.readouterr().out.strip() == "skip"
    assert ledger.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == runs_before


def test_light_tick_skips_when_the_plan_check_fails(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "tick.sqlite"
    ledger = Ledger(path, Decimal("1000"))
    ledger.conn.execute(
        "INSERT INTO runs (ran_at,strategies,scanned,signals,filled,settled,cash,open_cost) "
        "VALUES (?,?,0,0,0,0,'1000','0')",
        ((datetime.now(UTC) - timedelta(minutes=10)).isoformat(), "book_scanner"),
    )
    ledger.conn.commit()
    settings = Settings(_env_file=None, rules_version="v3", paper_db_path=path)  # type: ignore[call-arg]
    monkeypatch.setattr(cli, "_settings", lambda: settings)

    def boom(*_args: object) -> None:
        raise RuntimeError("bad plan data")

    monkeypatch.setattr(cli, "_due_plan_feed", boom)
    cli.slot_guard(slot="scanner", within_minutes=25, plan_due=True)
    assert capsys.readouterr().out.strip() == "skip"
    # With no recent scanner run the tick still runs, whatever the plan check does.
    ledger.conn.execute("DELETE FROM runs")
    ledger.conn.commit()
    cli.slot_guard(slot="scanner", within_minutes=25, plan_due=True)
    assert capsys.readouterr().out.strip() == "run"


def test_pull_due_target_prints_that_targets_feed_only_while_due(
    tmp_path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "due.sqlite"
    ledger = Ledger(path, Decimal("1000"))
    now = datetime.now(UTC)
    _eligible(ledger, ["soccer_epl"], now - timedelta(hours=1))
    _game(ledger, "KXEPLGAME-X", now + timedelta(minutes=60), 2)  # K-70m due now
    _game(ledger, "KXEPLGAME-Y", now + timedelta(hours=10), 2)  # nothing due
    settings = Settings(_env_file=None, rules_version="v3", paper_db_path=path)  # type: ignore[call-arg]
    cli._refresh_pull_plan(ledger, now, settings, CAL)
    due = _rows(ledger, "event_ticker='KXEPLGAME-X' AND target='K-70m'")[0]["id"]
    later = _rows(ledger, "event_ticker='KXEPLGAME-Y' AND target='K-70m'")[0]["id"]
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    cli.pull_due(target=due)
    assert capsys.readouterr().out.strip() == "soccer_epl"
    cli.pull_due(target=later)
    assert capsys.readouterr().out == ""


def test_pull_rows_are_tagged_with_source_and_plan_id(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    since = SUNDAY.replace(hour=17, minute=47)
    before, after = (since - timedelta(minutes=5)).isoformat(), since.isoformat()
    for at in (before, after):
        for ticker in ("KXNFLGAME-26NOV08AB-A", "KXEPLGAME-26NOV08CD-C"):
            ledger.conn.execute(
                "INSERT INTO shadow_decisions (created_at,rules_version,window,strategy,ticker,"
                "side,would_trade,reason,fair,price,net_edge) "
                "VALUES (?,'v3','<2h','sportsbook_v3',?,'yes',0,'net_edge','0.50','0.52','-0.03')",
                (at, ticker),
            )
            ledger.conn.execute(
                "INSERT INTO signals (created_at,strategy,ticker,side,fair_prob,limit_price,edge,"
                "size,rationale) VALUES (?,'sportsbook_v3',?,'yes','0.5','0.5','0','1','')",
                (at, ticker),
            )
    ledger.conn.execute(
        "INSERT INTO feed_pulls (created_at,feed,rules_version,candidates,gate_candidates) "
        "VALUES (?,'americanfootball_nfl','v3',7,2)",
        (after,),
    )
    ledger.conn.commit()
    assert tag_pull_rows(ledger, "americanfootball_nfl", since, "close", 42) == (7, 2)
    tagged = ledger.conn.execute(
        "SELECT ticker,created_at FROM shadow_decisions WHERE pull_source='close' "
        "AND pull_plan_id=42"
    ).fetchall()
    assert tagged == [("KXNFLGAME-26NOV08AB-A", after)]
    assert (
        ledger.conn.execute("SELECT COUNT(*) FROM signals WHERE pull_source='close'").fetchone()[0]
        == 1
    )
    # A row is tagged once: a second pull of the feed does not re-tag it.
    tag_pull_rows(ledger, "americanfootball_nfl", since, "rotation", None)
    assert (
        ledger.conn.execute(
            "SELECT COUNT(*) FROM shadow_decisions WHERE pull_source='rotation'"
        ).fetchone()[0]
        == 0
    )


def test_clv_report_by_source_breaks_cohorts_down_by_pull_source(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    for i, source in enumerate(["release", "release", None]):
        ledger.conn.execute(
            "INSERT INTO shadow_decisions (created_at,rules_version,window,strategy,ticker,side,"
            "would_trade,reason,price,net_edge,clv_status,clv_kalshi,pull_source) "
            "VALUES (?,'v3','24-72h','sportsbook_v3',?,'yes',0,'net_edge','0.5','-0.01','ok',"
            "'1.5',?)",
            (f"2026-11-0{i + 1}T00:00:00+00:00", f"KXNFLGAME-{i}-A", source),
        )
    ledger.conn.commit()
    report = clv_report(ledger.conn, by_source=True)
    by = report["cohorts"]["v3_net_edge"]["metrics"]["clv_kalshi"]["by_pull_source"]
    assert by["release"]["n"] == 2 and by["untagged"]["n"] == 1
    assert (
        "by_pull_source"
        not in clv_report(ledger.conn)["cohorts"]["v3_net_edge"]["metrics"]["clv_kalshi"]
    )


def test_odds_budget_pull_source_section(tmp_path: Path) -> None:
    from scripts.odds_budget import pull_source_section

    ledger = _ledger(tmp_path)
    at = "2026-11-08T17:47:00+00:00"
    ledger.conn.execute(
        "INSERT INTO feed_pulls (created_at,feed,rules_version,candidates,gate_candidates,"
        "pull_source) VALUES (?,'americanfootball_nfl','v3',10,3,'release')",
        (at,),
    )
    for edge, price in (("0.01", "0.52"), ("-0.02", "0.53")):
        ledger.conn.execute(
            "INSERT INTO shadow_decisions (created_at,rules_version,window,strategy,ticker,side,"
            "would_trade,reason,fair,price,net_edge,pull_source) "
            "VALUES (?,'v3','<2h','sportsbook_v3','KXNFLGAME-X-A','yes',0,'net_edge','0.50',?,?,"
            "'release')",
            (at, price, edge),
        )
    ledger.conn.commit()
    text = pull_source_section(tmp_path / "plan.sqlite", datetime(2026, 11, 1, tzinfo=UTC))
    assert "release,1,10,3,2,50.0%,+2.50" in text
    assert "release,americanfootball_nfl,1,10,3,2,50.0%,+2.50" in text
    assert "release,2026-W45,1,10,3,2,50.0%,+2.50" in text


def test_pull_plan_migration_is_additive(tmp_path: Path) -> None:
    """A Phase 3 ledger gains the new columns without losing rows."""
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE pull_plan (id INTEGER PRIMARY KEY, event_ticker TEXT NOT NULL, "
        "feed TEXT NOT NULL, target TEXT NOT NULL, kickoff TEXT NOT NULL, "
        "target_at TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL, "
        "priority INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'planned', "
        "credits REAL NOT NULL DEFAULT 0, planned_at TEXT NOT NULL, pulled_at TEXT, "
        "budget_blocked_at TEXT, UNIQUE(event_ticker, target))"
    )
    conn.execute(
        "INSERT INTO pull_plan (event_ticker,feed,target,kickoff,target_at,window_start,"
        "window_end,priority,status,credits,planned_at) VALUES "
        "('E','soccer_epl','T-6h','k','t','s','e',2,'pulled',1,'p')"
    )
    conn.commit()
    conn.close()
    ledger = Ledger(path, Decimal("1000"))
    row = ledger.conn.execute("SELECT status,credits,source,merged_into FROM pull_plan").fetchone()
    assert row == ("pulled", 1.0, "default", None)


# --- simulated month -------------------------------------------------------------------


def test_simulated_month_default_flags(tmp_path: Path) -> None:
    """November 2026, real cadence and release calendars, the Phase 3 slate plus Monday
    night, three move-trigger pulls a day, default flags (rotation, 10-minute ticks)."""
    from scripts.simulate_pull_month import simulate

    result = simulate("rotation", "10min", tmp_path)
    assert result["spent_excluding_moves"] <= 450
    assert result["spent"] <= 500
    # OddsPacer's 25-credit hard reserve is never touched.
    assert result["lowest_balance"] >= OddsPacer.MEASURE_RESERVE
    # Every NFL Sunday and Monday status/inactives clock is pulled.
    assert result["nfl_sun_mon_clock"] == {"pulled": 20}
    assert result["targets"].get("close:pulled", 0) >= 160
    assert result["rotation_pulls"] > result["plan_pulls"] > 0
    assert result["move_pulls"] >= 50  # the reserve is spent by move pulls


def test_pacer_sees_feeds_pulled_before_the_engine_cleared_requests_last(tmp_path: Path) -> None:
    """Measurement and extra credits are judged after the cycle, when the engine has
    already cleared `requests_last_by_sport`; `pulled_feeds` keeps the answer."""
    from types import SimpleNamespace

    client = SimpleNamespace(
        requests_last_by_sport={}, pulled_feeds={"icehockey_nhl"}, budget=1,
        requests_remaining="400", requests_made=1,
    )  # fmt: skip
    pacer = OddsPacer(client, 12, ledger=_ledger(tmp_path))  # type: ignore[arg-type]
    pacer.measure_feed = "icehockey_nhl"
    pacer.extra_feeds = ["icehockey_nhl"]
    assert pacer.measure_pulled() and pacer.pulled("icehockey_nhl")
    assert not pacer.pulled("soccer_epl")
