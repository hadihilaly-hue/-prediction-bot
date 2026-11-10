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
