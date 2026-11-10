from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from prediction_bot.cadence import CadenceCalendar
from prediction_bot.config import Settings
from prediction_bot.odds import OddsPacer
from prediction_bot.paper.ledger import Ledger
from prediction_bot.pull_plan import (
    eligible_feeds,
    mark_pulled,
    plan_credits_today,
    plan_daily_cap,
    plan_kickoffs,
    plan_pulls,
    select_plan_pull,
)
from scripts.simulate_pull_month import team_code

UTC = timezone.utc
CALENDAR = Path(__file__).parents[1] / "data" / "cadence_calendar.json"
SETTINGS = Settings(_env_file=None)  # type: ignore[call-arg]


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
            f"{event}-{team_code(event)}",
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


def _budget(ledger: Ledger, day: datetime, budget: float) -> None:
    ledger.record_cadence_day(
        day.date(), static_w=1, dyn_count=None, dyn_json=None, blended_w=1,
        budget=budget, mode="off", at=day,
    )  # fmt: skip


def _slots(start: datetime, end: datetime) -> list[datetime]:
    """The workflow's :17 and :47 cron slots."""
    at = start.replace(minute=17, second=0, microsecond=0)
    out = []
    while at <= end:
        out.append(at)
        at += timedelta(minutes=30)
    return out


def _run_day(ledger: Ledger, feeds: list[str], start: datetime, end: datetime) -> list[str]:
    """Step every slot: refresh the plan, pull the funded feed. Returns pulled feeds."""
    pulls = []
    for now in _slots(start, end):
        plan_pulls(ledger, plan_kickoffs(ledger.conn, eligible_feeds(ledger.conn), now), now)
        pick = select_plan_pull(ledger, SETTINGS, now)
        if pick is not None:
            mark_pulled(ledger, pick.feed, now, 1.0)
            pulls.append(pick.feed)
    plan_pulls(ledger, {}, end + timedelta(days=1))  # close out expired targets
    return pulls


def _status(ledger: Ledger) -> dict[tuple[str, str], str]:
    return {
        (str(e), str(t)): str(s)
        for e, t, s in ledger.conn.execute("SELECT event_ticker,target,status FROM pull_plan")
    }


SUNDAY = datetime(2026, 11, 8, tzinfo=UTC)


def test_sunday_nfl_slate_costs_one_credit_per_target_per_kickoff_cluster(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = _ledger(tmp_path)
    _eligible(ledger, ["americanfootball_nfl"], SUNDAY - timedelta(days=2))
    early = [f"KXNFLGAME-26NOV08G{i}" for i in range(6)]
    for event in early:
        _game(ledger, event, SUNDAY.replace(hour=18))  # 1:00 pm ET
    _game(ledger, "KXNFLGAME-26NOV08LATE1", SUNDAY.replace(hour=21, minute=5))
    _game(ledger, "KXNFLGAME-26NOV08LATE2", SUNDAY.replace(hour=21, minute=25))
    _budget(ledger, SUNDAY - timedelta(days=1), 100)
    _budget(ledger, SUNDAY, 100)

    pulls = _run_day(
        ledger, ["americanfootball_nfl"], SUNDAY - timedelta(days=1), SUNDAY.replace(hour=23)
    )
    status = _status(ledger)
    assert len(status) == 8 * 4
    assert all(state == "pulled" for state in status.values()), status
    # Six 1pm games share one pull per target: 4 credits for 24 targets.
    early_credits = ledger.conn.execute(
        "SELECT SUM(credits) FROM pull_plan WHERE event_ticker LIKE 'KXNFLGAME-26NOV08G%'"
    ).fetchone()[0]
    assert early_credits <= 4.0 + 1e-9
    assert len(pulls) < 8 * 4
    # The sharp close for a 18:00 kickoff lands on the 17:47 slot.
    pulled_at = ledger.conn.execute(
        "SELECT pulled_at FROM pull_plan WHERE event_ticker=? AND target='T-10m'", (early[0],)
    ).fetchone()[0]
    assert pulled_at.startswith("2026-11-08T17:47")


def test_low_priority_targets_yield_to_the_close_when_credits_are_short(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = _ledger(tmp_path)
    _eligible(ledger, ["americanfootball_nfl"], SUNDAY - timedelta(days=2))
    _game(ledger, "KXNFLGAME-26NOV08ONE", SUNDAY.replace(hour=18))
    _game(ledger, "KXNFLGAME-26NOV09MNF", SUNDAY.replace(day=10, hour=1, minute=15))
    _budget(ledger, SUNDAY, 2)  # cap = floor(2 x (1 - 0.5)) = 1 plan credit today
    assert plan_daily_cap(ledger, SETTINGS, SUNDAY) == 1

    _run_day(
        ledger, ["americanfootball_nfl"], SUNDAY.replace(hour=0, minute=30), SUNDAY.replace(hour=23)
    )
    status = _status(ledger)
    # The one credit went to the close; earlier, lower-priority targets were held back.
    assert status[("KXNFLGAME-26NOV08ONE", "T-10m")] == "pulled"
    assert status[("KXNFLGAME-26NOV08ONE", "T-6h")] == "skipped_budget"
    assert status[("KXNFLGAME-26NOV08ONE", "T-90m")] == "skipped_budget"
    assert plan_credits_today(ledger, SUNDAY.replace(hour=20)) == 1.0


TUESDAY = datetime(2026, 11, 3, tzinfo=UTC)


def test_midweek_ucl_slate_and_the_17_45_close_gap(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = _ledger(tmp_path)
    _eligible(ledger, ["soccer_uefa_champs_league"], TUESDAY - timedelta(days=2))
    _game(ledger, "KXUCLGAME-26NOV03EARLY1", TUESDAY.replace(hour=17, minute=45), offset_h=3)
    _game(ledger, "KXUCLGAME-26NOV03EARLY2", TUESDAY.replace(hour=17, minute=45), offset_h=3)
    late = [f"KXUCLGAME-26NOV03LATE{i}" for i in range(6)]
    for event in late:
        _game(ledger, event, TUESDAY.replace(hour=20), offset_h=3)
    for day in (TUESDAY - timedelta(days=1), TUESDAY):
        _budget(ledger, day, 100)

    _run_day(ledger, [], TUESDAY - timedelta(days=1), TUESDAY.replace(hour=22))
    status = _status(ledger)
    assert all(
        status[(e, t)] == "pulled" for e in late for t in ("T-24h", "T-6h", "T-90m", "T-10m")
    )
    # No :17/:47 slot falls in [17:25, 17:43]: the 17:45 close is missed, not mis-timed.
    assert status[("KXUCLGAME-26NOV03EARLY1", "T-10m")] == "missed"
    assert status[("KXUCLGAME-26NOV03EARLY1", "T-90m")] == "pulled"
    late_credits = ledger.conn.execute(
        "SELECT SUM(credits) FROM pull_plan WHERE event_ticker LIKE 'KXUCLGAME-26NOV03LATE%'"
    ).fetchone()[0]
    assert late_credits <= 4.0 + 1e-9


def test_plan_kickoff_prefers_linked_odds_commence_and_skips_ineligible(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = _ledger(tmp_path)
    now = SUNDAY.replace(hour=6)
    _eligible(ledger, ["americanfootball_nfl"], now - timedelta(days=1))
    _game(ledger, "KXNFLGAME-26NOV08AB", SUNDAY.replace(hour=18))
    _game(ledger, "KXNHLGAME-26NOV08CD", SUNDAY.replace(hour=23))  # NHL not eligible
    ledger.conn.execute(
        "INSERT INTO odds_snapshots (created_at,feed,event_id,home_team,away_team,"
        "commence_time,team,prob,books_used,event_ticker) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now.isoformat(), "americanfootball_nfl", "x", "A", "B", "2026-11-08T18:20:00Z",
         "A", "0.5", 2, "KXNFLGAME-26NOV08AB"),
    )  # fmt: skip
    kickoffs = plan_kickoffs(ledger.conn, eligible_feeds(ledger.conn), now)
    assert kickoffs == {
        "KXNFLGAME-26NOV08AB": ("americanfootball_nfl", datetime(2026, 11, 8, 18, 20, tzinfo=UTC))
    }
    assert select_plan_pull(ledger, Settings(_env_file=None, odds_pull_plan="off"), now) is None  # type: ignore[call-arg]


# --- pacer -------------------------------------------------------------------------


class FakeOdds:
    """Counts credits like the Odds API: every pull costs one, budget enforced."""

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


def test_plan_mode_pacer_spends_one_credit_and_never_touches_the_carry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = _ledger(tmp_path)
    ledger.record_odds_pacer_carry(0.4)
    client = FakeOdds(100)
    pacer = OddsPacer(client, 12, ledger=ledger, plan_mode=True)  # type: ignore[arg-type]
    assert pacer.start_run(SUNDAY) == 1
    assert client.pull("americanfootball_nfl") and not client.pull("soccer_epl")
    assert not pacer.grant_extra_credit("soccer_epl")
    pacer.finish_run(SUNDAY)
    assert ledger.odds_pacer_carry() == 0.4
    assert OddsPacer(FakeOdds(25), 12, ledger=ledger, plan_mode=True).start_run(SUNDAY) == 0  # type: ignore[arg-type]


def test_simulated_november_never_exceeds_the_monthly_budget(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Real cadence calendar, every :17/:47 slot of November, rotation always spending
    its full share, and a dense slate of plan targets competing for the rest."""
    ledger = _ledger(tmp_path, "month.sqlite")
    start = datetime(2026, 11, 1, tzinfo=UTC)
    feeds = [
        "americanfootball_nfl",
        "americanfootball_ncaaf",
        "soccer_epl",
        "soccer_uefa_champs_league",
    ]
    _eligible(ledger, feeds, start - timedelta(days=1))
    for day in range(30):
        date = start + timedelta(days=day)
        weekday = date.weekday()
        if weekday == 6:  # Sunday NFL
            for i, (h, m) in enumerate([(18, 0)] * 6 + [(21, 5), (21, 25), (1, 20)]):
                _game(ledger, f"KXNFLGAME-{day:02d}N{i}", date.replace(hour=h, minute=m))
        if weekday == 5:  # Saturday college football and EPL, spread kickoffs
            for i, h in enumerate([16, 16, 17, 19, 20, 20, 23, 23, 2]):
                _game(
                    ledger, f"KXNCAAFGAME-{day:02d}C{i}", date.replace(hour=h, minute=30 * (i % 2))
                )
            for i, h in enumerate([12, 15, 15, 15, 17]):
                _game(ledger, f"KXEPLGAME-{day:02d}E{i}", date.replace(hour=h), offset_h=3)
        if weekday in (1, 2):  # UCL Tuesday/Wednesday
            for i, (h, m) in enumerate([(17, 45), (17, 45)] + [(20, 0)] * 6):
                _game(
                    ledger, f"KXUCLGAME-{day:02d}U{i}", date.replace(hour=h, minute=m), offset_h=3
                )

    client = FakeOdds(500)
    calendar = CadenceCalendar.load(CALENDAR)
    rotation = OddsPacer(client, 12, ledger=ledger, cadence_mode="off", calendar=calendar)  # type: ignore[arg-type]
    rotation.rotation_share = SETTINGS.odds_entry_share
    plan_pulled = rotation_pulled = 0
    lowest_before_plan = 500
    for now in _slots(start, start + timedelta(days=30) - timedelta(minutes=1)):
        plan_pulls(ledger, plan_kickoffs(ledger.conn, eligible_feeds(ledger.conn), now), now)
        if now.minute == 17 and now.hour % 2 == 0:  # sportsbook slot
            budget = rotation.start_run(now)
            for i in range(budget):
                rotation_pulled += client.pull(f"rotation_{i}")
            pick = select_plan_pull(ledger, SETTINGS, now, exclude=[])
            left_before = client.left
            if (
                pick is not None
                and rotation.grant_extra_credit(pick.feed)
                and client.pull(pick.feed)
            ):
                lowest_before_plan = min(lowest_before_plan, left_before)
                mark_pulled(ledger, pick.feed, now, 1.0)
                plan_pulled += 1
            rotation.finish_run(now)
        else:
            pick = select_plan_pull(ledger, SETTINGS, now)
            if pick is None:
                continue
            planner = OddsPacer(client, 12, ledger=ledger, plan_mode=True)  # type: ignore[arg-type]
            left_before = client.left
            if planner.start_run(now) and client.pull(pick.feed):
                lowest_before_plan = min(lowest_before_plan, left_before)
                mark_pulled(ledger, pick.feed, now, 1.0)
                plan_pulled += 1
            planner.finish_run(now)
        assert client.left >= 0
    spent = 500 - client.left
    assert spent <= 500
    # Plan credits never dip into the 25-credit reserve (the rotation's own cadence
    # pacer may still spend toward zero by month end, as it always has).
    assert lowest_before_plan - 1 >= OddsPacer.MEASURE_RESERVE
    closes = ledger.conn.execute(
        "SELECT COUNT(*) FROM pull_plan WHERE target='T-10m' AND status='pulled'"
    ).fetchone()[0]
    assert closes > 0
    assert rotation_pulled > 0 and plan_pulled > 0
    # The rotation keeps at least its half; days with no plan targets hand their
    # unused share back to the month, which the rotation then spends.
    assert plan_pulled <= rotation_pulled


def test_pull_due_prints_the_funded_feed(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import prediction_bot.cli as cli

    path = tmp_path / "due.sqlite"
    ledger = Ledger(path, Decimal("1000"))
    now = datetime.now(UTC)
    _eligible(ledger, ["americanfootball_nfl"], now - timedelta(hours=1))
    ledger.conn.execute(
        "INSERT INTO market_snapshots (fetched_at,ticker,event_ticker,series,yes_bid,yes_ask,"
        "yes_bid_size,yes_ask_size,open_interest,volume,expected_expiration) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            (now - timedelta(hours=1)).isoformat(), "KXNFLGAME-X-KC", "KXNFLGAME-X", "KXNFLGAME",
            "0.48", "0.50", "100", "100", "10", "10",
            (now + timedelta(minutes=12) + timedelta(hours=3)).isoformat(),
        ),
    )  # fmt: skip
    _budget(ledger, now, 10)
    settings = Settings(_env_file=None, rules_version="v3", paper_db_path=path)  # type: ignore[call-arg]
    monkeypatch.setattr(cli, "_settings", lambda: settings)

    cli.pull_due()
    assert capsys.readouterr().out.strip() == "americanfootball_nfl"
    monkeypatch.setattr(
        cli, "_settings", lambda: settings.model_copy(update={"odds_pull_plan": "off"})
    )
    cli.pull_due()
    assert capsys.readouterr().out == ""
