"""Credits fix (Part 2)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

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


# --- 2.2 no credits on feeds v3 cannot use ----------------------------------------------


def _eligible(ledger: Ledger, feeds: list[str], at: datetime) -> None:
    ledger.conn.executemany(
        "INSERT INTO feed_stats (computed_at,feed,lead_p50_h,lead_p90_h,pulls_14d,"
        "useful_pulls_14d,provisional,eligible) VALUES (?,?,100,200,0,0,0,1)",
        [(at.isoformat(), feed) for feed in feeds],
    )
    ledger.conn.commit()


def _legs(ledger: Ledger, event: str, codes: list[str], kickoff: datetime, at: datetime) -> None:
    for code in codes:
        ledger.conn.execute(
            "INSERT INTO market_snapshots (fetched_at,ticker,event_ticker,series,yes_bid,"
            "yes_ask,yes_bid_size,yes_ask_size,open_interest,volume,expected_expiration) "
            "VALUES (?,?,?,?,'0.48','0.50','100','100','10','10',?)",
            (at.isoformat(), f"{event}-{code}", event, event.split("-", 1)[0],
             (kickoff + timedelta(hours=3)).isoformat()),
        )  # fmt: skip
    ledger.conn.commit()


def test_evaluable_feed_and_event_mapping() -> None:
    from prediction_bot.teams import evaluable_feed, event_teams_mapped

    assert evaluable_feed("americanfootball_nfl") and evaluable_feed("soccer_epl")
    assert not evaluable_feed("mma_mixed_martial_arts")
    assert not evaluable_feed("basketball_ncaab")  # teams.json has no NCAAB teams
    assert not evaluable_feed("golf_masters_tournament_winner")
    event = "KXEPLGAME-26NOV22ARSTOT"
    assert event_teams_mapped(event, [f"{event}-ARS", f"{event}-TOT", f"{event}-TIE"])
    assert not event_teams_mapped(event, [f"{event}-ARS", f"{event}-ZZZ"])
    assert not event_teams_mapped(event, [f"{event}-TIE"])


def test_no_unmapped_feed_or_game_reaches_the_plan_or_measure_pulls(tmp_path: Path) -> None:
    from prediction_bot.cli import _refresh_pull_plan, _release_calendar
    from prediction_bot.config import Settings
    from prediction_bot.feed_selection import select_measure_pull
    from prediction_bot.odds_planner import PlannedGame

    settings = Settings(_env_file=None, rules_version="v3")  # type: ignore[call-arg]
    ledger = _ledger(tmp_path)
    now = datetime(2026, 11, 14, 6, tzinfo=UTC)
    feeds = ["americanfootball_ncaaf", "mma_mixed_martial_arts", "basketball_ncaab"]
    _eligible(ledger, feeds, now - timedelta(days=1))
    kickoff = now + timedelta(hours=12)
    _legs(ledger, "KXNCAAFGAME-26NOV14TEXLSU", ["TEX", "LSU"], kickoff, now)
    _legs(ledger, "KXNCAAFGAME-26NOV14FCSA", ["TEX", "ZZFCS"], kickoff, now)  # FCS opponent
    _legs(ledger, "KXUFCFIGHT-26NOV14AB", ["A", "B"], kickoff, now)
    _legs(ledger, "KXNCAAMBGAME-26NOV14XY", ["X", "Y"], kickoff, now)
    # A UFC target planned before this fix is retired, never funded.
    ledger.conn.execute(
        "INSERT INTO pull_plan (event_ticker,feed,target,kickoff,target_at,window_start,"
        "window_end,priority,status,credits,planned_at) VALUES "
        "('KXUFCFIGHT-26NOV14AB','mma_mixed_martial_arts','T-10m',?,?,?,?,0,'planned',0,?)",
        (kickoff.isoformat(), now.isoformat(), now.isoformat(),
         (now + timedelta(hours=1)).isoformat(), now.isoformat()),
    )  # fmt: skip
    ledger.conn.commit()
    _refresh_pull_plan(ledger, now, settings, _release_calendar(settings))
    planned = {
        (str(e), str(f))
        for e, f in ledger.conn.execute(
            "SELECT event_ticker,feed FROM pull_plan WHERE status='planned'"
        )
    }
    assert planned and all(f == "americanfootball_ncaaf" for _e, f in planned)
    assert not any(e.endswith("FCSA") for e, _f in planned)
    assert ledger.conn.execute(
        "SELECT status,note FROM pull_plan WHERE feed='mma_mixed_martial_arts'"
    ).fetchall() == [("superseded", "unevaluable_feed")]
    games = {
        "basketball_ncaab": [PlannedGame(now + timedelta(hours=6), False, "KXNCAAMBGAME-X")],
        "basketball_nba": [PlannedGame(now + timedelta(minutes=90), False, "KXNBAGAME-Y")],
    }
    pick = select_measure_pull(ledger, games, settings, now)
    assert pick is not None and pick.feed == "basketball_nba"
    del games["basketball_nba"]
    assert select_measure_pull(ledger, games, settings, now) is None


def test_every_credit_spent_in_a_cycle_has_a_feed_pulls_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shadow strategy's fetch (no primary feed_pulls row) is still recorded, and the
    credits in feed_pulls match the move in x-requests-used across runs."""
    import httpx

    import prediction_bot.cli as cli
    from prediction_bot.config import Settings
    from prediction_bot.engine import CycleResult
    from prediction_bot.odds import OddsClient

    path = tmp_path / "acct.sqlite"
    ledger = Ledger(path, Decimal("100"))
    used = {"n": 100}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/odds"):
            used["n"] += 1
            headers = {"x-requests-last": "1"}
        else:
            headers = {}
        headers.update(
            {"x-requests-used": str(used["n"]), "x-requests-remaining": str(500 - used["n"])}
        )
        return httpx.Response(200, json=[], headers=headers)

    odds = OddsClient("key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    pulls = {"feeds": ["americanfootball_nfl"]}

    class EngineStub:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def settle_open_positions(self):  # type: ignore[no-untyped-def]
            return {}

        def run_cycle(self, markets):  # type: ignore[no-untyped-def]
            for feed in pulls["feeds"]:
                odds.h2h(feed)  # as a shadow strategy would: no feed_pulls row
            odds.requests_last_by_sport.clear()
            return CycleResult()

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, rules_version="v3", v3_execution="taker", paper_db_path=path,
        odds_api_key="key", odds_runs_per_day=12,
    )  # fmt: skip
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_client", lambda _settings: object())
    monkeypatch.setattr(cli, "_odds_client", lambda _settings: odds)
    monkeypatch.setattr(cli, "_fetch", lambda _client, _series: [])
    monkeypatch.setattr(cli, "Engine", EngineStub)
    monkeypatch.setattr(cli, "_plan_games_listed", lambda *args, **kwargs: True)
    for name in ("score_maker_shadows", "backfill_clv_commence_times", "record_clv",
                 "refresh_clv_statuses", "reanchor_kickoffs"):  # fmt: skip
        monkeypatch.setattr(cli, name, lambda *args, **kwargs: 0)

    cli.paper(
        strategy=["sportsbook_v3"], series=["KXNFLGAME"], cycles=1, plan="americanfootball_nfl"
    )
    # A plan run on a feed v3 cannot evaluate spends nothing.
    pulls["feeds"] = ["mma_mixed_martial_arts"]
    cli.paper(strategy=["sportsbook_v3"], series=["KXUFCFIGHT"], cycles=1,
              plan="mma_mixed_martial_arts")  # fmt: skip

    rows = (
        Ledger(path, Decimal("100"))
        .conn.execute("SELECT feed,candidates,requests_last,shadow_only FROM feed_pulls")
        .fetchall()
    )
    assert rows == [("americanfootball_nfl", 0, 1, 1)]
    run_used = [int(u) for (u,) in ledger.conn.execute("SELECT odds_used FROM runs ORDER BY id")]
    assert sum(r[2] for r in rows) == run_used[-1] - 100


# --- 2.3 move triggers fire on real moves, not noise -------------------------------------

NFL = "americanfootball_nfl"
EVENT = "KXNFLGAME-26NOV22KCBUF"


def _snap(ledger: Ledger, ticker: str, at: datetime, bid: str, ask: str) -> None:
    ledger.conn.execute(
        "INSERT INTO market_snapshots (fetched_at,ticker,event_ticker,series,yes_bid,yes_ask,"
        "yes_bid_size,yes_ask_size,open_interest,volume,expected_expiration) "
        "VALUES (?,?,?,'KXNFLGAME',?,?,'10','10','0','0',?)",
        (at.isoformat(), ticker, EVENT, bid, ask, (at + timedelta(days=5)).isoformat()),
    )
    ledger.conn.commit()


def _trigger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: Ledger, now: datetime) -> str:
    import prediction_bot.cli as cli
    from prediction_bot.config import Settings
    from prediction_bot.odds_planner import PlannedGame

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, rules_version="v3", min_lead_hours=72, paper_db_path=ledger.path,
        odds_trigger_daily_cap=2,
    )  # fmt: skip
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_client", lambda _settings: object())
    monkeypatch.setattr(cli, "_fetch", lambda _client, _series: [])
    monkeypatch.setattr(
        cli,
        "_planner_games",
        lambda *_args: {NFL: [PlannedGame(now + timedelta(hours=100), event_ticker=EVENT)]},
    )
    out: list[str] = []
    monkeypatch.setattr(cli.typer, "echo", lambda text: out.append(str(text)))
    cli.odds_trigger(series=["KXNFLGAME"])
    return out[0] if out else ""


def test_one_cent_is_noise_two_cents_is_a_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    now = datetime.now(UTC)
    last_pull = now - timedelta(hours=2)
    _snap(ledger, f"{EVENT}-KC", last_pull - timedelta(minutes=5), "0.47", "0.49")
    ledger.record_feed_pulls({NFL: 1}, "v3", last_pull)
    _snap(ledger, f"{EVENT}-KC", now - timedelta(minutes=1), "0.47", "0.50")  # 1c
    # A market listed since the last pull is not a move either.
    _snap(ledger, f"{EVENT}-BUF", now - timedelta(minutes=1), "0.31", "0.34")
    assert _trigger(tmp_path, monkeypatch, ledger, now) == ""
    _snap(ledger, f"{EVENT}-KC", now, "0.47", "0.51")  # 2c since the last pull
    assert _trigger(tmp_path, monkeypatch, ledger, now + timedelta(seconds=1)) == NFL
    assert ledger.conn.execute("SELECT ticker,ask,move FROM odds_triggers").fetchall() == [
        (f"{EVENT}-KC", "0.51", "0.02")
    ]


def test_the_largest_move_is_taken_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = _ledger(tmp_path)
    now = datetime.now(UTC)
    last_pull = now - timedelta(hours=2)
    for leg in ("KC", "BUF"):
        _snap(ledger, f"{EVENT}-{leg}", last_pull - timedelta(minutes=5), "0.47", "0.49")
    ledger.record_feed_pulls({NFL: 1}, "v3", last_pull)
    _snap(ledger, f"{EVENT}-BUF", now - timedelta(minutes=1), "0.49", "0.52")  # 3c
    _snap(ledger, f"{EVENT}-KC", now - timedelta(minutes=1), "0.41", "0.43")  # 6c
    assert _trigger(tmp_path, monkeypatch, ledger, now) == NFL
    assert ledger.conn.execute("SELECT ticker,move FROM odds_triggers").fetchall() == [
        (f"{EVENT}-KC", "0.06")
    ]


def test_move_triggers_are_at_least_three_hours_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    now = datetime.now(UTC)
    last_pull = now - timedelta(hours=2)
    _snap(ledger, f"{EVENT}-KC", last_pull - timedelta(minutes=5), "0.47", "0.49")
    ledger.record_feed_pulls({NFL: 1}, "v3", last_pull)
    _snap(ledger, f"{EVENT}-KC", now - timedelta(minutes=1), "0.52", "0.55")
    ledger.record_odds_trigger("soccer_epl", "X", "0.5", now - timedelta(hours=2, minutes=59))
    ledger.finish_odds_trigger("soccer_epl", "pulled")
    assert _trigger(tmp_path, monkeypatch, ledger, now) == ""
    ledger.conn.execute(
        "UPDATE odds_triggers SET created_at=?", ((now - timedelta(hours=3)).isoformat(),)
    )
    ledger.conn.commit()
    assert _trigger(tmp_path, monkeypatch, ledger, now) == NFL


# --- 2.4 measurement pulls are paid from the plan's share --------------------------------


def test_measure_pulls_are_charged_to_the_plan_allowance(tmp_path: Path) -> None:
    from prediction_bot.config import Settings
    from prediction_bot.pull_plan import plan_allowance_left

    settings = Settings(_env_file=None, rules_version="v3")  # type: ignore[call-arg]
    ledger = _ledger(tmp_path)
    now = datetime(2026, 11, 20, 12, tzinfo=UTC)
    ledger.record_run(["x"], 0, 0, 0, 0, odds_used=200, odds_remaining=300)
    ledger.conn.execute(
        "UPDATE runs SET ran_at=?", (datetime(2026, 11, 1, tzinfo=UTC).isoformat(),)
    )
    ledger.conn.commit()
    before = plan_allowance_left(ledger, settings, now)
    assert before is not None
    ledger.record_measure_pull("basketball_nba", "KXNBAGAME-X", "T-90m", now, now)
    assert plan_allowance_left(ledger, settings, now) == pytest.approx(before - 1)


def test_measure_credit_is_refused_below_the_plan_floor(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    now = datetime(2026, 11, 20, 12, tzinfo=UTC)

    def pacer(left: int) -> OddsPacer:
        client = _Quota(left)
        client.budget = 0
        p = OddsPacer(client, 12, ledger=ledger)  # type: ignore[arg-type]
        p.move_reserve = 50
        return p

    # Floor = 25 hard reserve + 50 unused move reserve.
    assert not pacer(75).grant_measure_credit("basketball_nba", 1, now)
    assert pacer(76).grant_measure_credit("basketball_nba", 1, now)


# --- 2.5 one sportsbook run per nominal slot ---------------------------------------------


def test_sportsbook_slot_start() -> None:
    from prediction_bot.cli import sportsbook_slot_start

    def at(h: int, m: int, day: int = 6) -> datetime:
        return datetime(2026, 11, day, h, m, tzinfo=UTC)

    assert sportsbook_slot_start(at(20, 17)) == at(20, 17)
    assert sportsbook_slot_start(at(21, 36)) == at(20, 17)
    assert sportsbook_slot_start(at(22, 16)) == at(20, 17)
    assert sportsbook_slot_start(at(0, 10, 7)) == at(22, 17)


def test_a_late_cron_run_does_not_repeat_the_sportsbook_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import prediction_bot.cli as cli
    from prediction_bot.config import Settings

    path = tmp_path / "slots.sqlite"
    ledger = Ledger(path, Decimal("1000"))
    ledger.record_run(["sportsbook_v3"], 0, 0, 0, 0, at=datetime(2026, 11, 6, 20, 19, tzinfo=UTC))
    clock = {"now": datetime(2026, 11, 6, 21, 36, tzinfo=UTC)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return clock["now"]

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cli, "_settings", lambda: Settings(_env_file=None, paper_db_path=path))  # type: ignore[call-arg]

    def guard(slot: str = "sportsbook") -> str:
        cli.slot_guard(slot=slot, within_minutes=25, plan_due=False)
        return capsys.readouterr().out.strip()

    assert guard() == "skip"  # the 20:17 slot ran at 20:19; 21:36 is the same slot
    clock["now"] = datetime(2026, 11, 6, 22, 18, tzinfo=UTC)
    assert guard() == "run"  # the 22:17 slot has not run
    # A late run of the previous slot (20:10 for 18:17) does not cover the 20:17 slot.
    ledger.record_run(["sportsbook_v3"], 0, 0, 0, 0, at=datetime(2026, 11, 7, 20, 10, tzinfo=UTC))
    clock["now"] = datetime(2026, 11, 7, 20, 20, tzinfo=UTC)
    assert guard() == "run"
    # The scanner path keeps its window.
    ledger.record_run(["book_scanner"], 0, 0, 0, 0, at=datetime(2026, 11, 7, 20, 0, tzinfo=UTC))
    assert guard("scanner") == "skip"


# --- 2.6a only a really pulled row absorbs, and only targets it came after ---------------


def _plan_row(
    ledger: Ledger,
    event: str,
    target_at: datetime,
    *,
    status: str = "planned",
    credits: float = 0.0,
    pulled_at: datetime | None = None,
    note: str | None = None,
    priority: int = 2,
) -> int:
    cursor = ledger.conn.execute(
        "INSERT INTO pull_plan (event_ticker,feed,target,kickoff,target_at,window_start,"
        "window_end,priority,status,credits,planned_at,pulled_at,source,note) "
        "VALUES (?,'soccer_epl','K-90m',?,?,?,?,?,?,?,?,?,'kickoff',?)",
        (event, (target_at + timedelta(minutes=90)).isoformat(), target_at.isoformat(),
         target_at.isoformat(), (target_at + timedelta(minutes=60)).isoformat(), priority,
         status, credits, target_at.isoformat(),
         None if pulled_at is None else pulled_at.isoformat(), note),
    )  # fmt: skip
    ledger.conn.commit()
    return int(cursor.lastrowid or 0)


def _status_of(ledger: Ledger, row_id: int) -> tuple[str, Any]:
    row = ledger.conn.execute(
        "SELECT status,merged_into FROM pull_plan WHERE id=?", (row_id,)
    ).fetchone()
    return str(row[0]), row[1]


def test_a_pull_never_absorbs_a_target_due_after_it(tmp_path: Path) -> None:
    from prediction_bot.pull_plan import merge_near_targets

    ledger = _ledger(tmp_path)
    t = datetime(2026, 11, 7, 13, tzinfo=UTC)
    paid = _plan_row(ledger, "E1", t, status="pulled", credits=1.0, pulled_at=t)
    before = _plan_row(ledger, "E2", t - timedelta(minutes=15))
    after = _plan_row(ledger, "E3", t + timedelta(minutes=15))
    merge_near_targets(ledger, t + timedelta(minutes=1))
    assert ledger.conn.execute("SELECT status FROM pull_plan WHERE id=?", (paid,)).fetchone() == (
        "pulled",
    )
    assert _status_of(ledger, before) == ("pulled", paid)
    # Its window opens 15 minutes after the pull: it stays to be pulled on its own.
    assert _status_of(ledger, after) == ("planned", None)


def test_an_absorbed_row_never_absorbs_further_rows(tmp_path: Path) -> None:
    from prediction_bot.pull_plan import merge_near_targets

    ledger = _ledger(tmp_path)
    t = datetime(2026, 11, 7, 13, tzinfo=UTC)
    absorbed = _plan_row(
        ledger, "E1", t, status="pulled", credits=0.0, pulled_at=t + timedelta(minutes=30),
        note="merged:1",
    )  # fmt: skip
    chained = _plan_row(ledger, "E2", t + timedelta(minutes=15))
    rode = _plan_row(
        ledger, "E3", t + timedelta(hours=2), status="pulled", pulled_at=t + timedelta(hours=2),
        note="rode_along:rotation",
    )  # fmt: skip
    rider = _plan_row(ledger, "E4", t + timedelta(hours=2) - timedelta(minutes=10))
    merge_near_targets(ledger, t + timedelta(minutes=5))
    assert _status_of(ledger, chained) == ("planned", None)
    assert _status_of(ledger, rider) == ("pulled", rode)
    assert absorbed
