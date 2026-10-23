from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from rich.console import Console

import prediction_bot.cli as cli
from prediction_bot.config import Settings
from prediction_bot.engine import CycleResult
from prediction_bot.feed_selection import (
    FeedStat,
    compute_feed_stats,
    compute_feed_stats_from_connection,
    eligible,
    feed_scores,
    learning_rule_demoted,
    liquidity_weights,
    rotation_scores,
    select_entry_feeds,
    select_rotation_feeds,
)
from prediction_bot.odds_planner import PlannedGame
from prediction_bot.paper.ledger import Ledger
from prediction_bot.strategies.sportsbook_v3 import SportsbookV3Strategy
from scripts.odds_budget import _feed_report_sections


def _snapshot(
    ledger: Ledger,
    feed: str,
    event_id: str,
    created_at: str,
    commence_time: str,
    team: str = "Home",
) -> None:
    ledger.conn.execute(
        "INSERT INTO odds_snapshots "
        "(created_at,feed,event_id,home_team,away_team,commence_time,team,prob,books_used) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            created_at,
            feed,
            event_id,
            "Home",
            "Away",
            commence_time,
            team,
            "0.5",
            2,
        ),
    )


def _market_snapshot(
    ledger: Ledger,
    event_ticker: str,
    ticker: str,
    fetched_at: datetime,
    bid: str,
    ask: str,
    ask_size: str,
) -> None:
    ledger.conn.execute(
        "INSERT INTO market_snapshots "
        "(fetched_at,ticker,event_ticker,series,yes_bid,yes_ask,yes_bid_size,yes_ask_size,"
        "last_price,open_interest,volume,expected_expiration) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fetched_at.isoformat(),
            ticker,
            event_ticker,
            ticker.split("-", 1)[0],
            bid,
            ask,
            "50",
            ask_size,
            "0.5",
            "0",
            "0",
            (fetched_at + timedelta(days=1)).isoformat(),
        ),
    )


def _stat(feed: str, p90: float, *, p50: float | None = None) -> FeedStat:
    return FeedStat(feed, p90 if p50 is None else p50, p90, 0, 0, False)


def test_compute_feed_stats_uses_seeds_for_provisional_feeds_and_logs_them(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "feed-stats.sqlite", Decimal("100"))

    stats = compute_feed_stats(ledger, ["soccer_epl", "other_feed"], now)
    assert stats["soccer_epl"].lead_p50_h == 168
    assert stats["soccer_epl"].lead_p90_h == 168
    assert stats["soccer_epl"].provisional
    assert stats["other_feed"].lead_p50_h == 48
    assert stats["other_feed"].provisional

    ledger.record_feed_stats(
        stats,
        {feed: eligible(stat, Settings(_env_file=None)) for feed, stat in stats.items()},
        now,
    )
    row = ledger.conn.execute(
        "SELECT lead_p50_h,lead_p90_h,provisional,eligible FROM feed_stats WHERE feed='soccer_epl'"
    ).fetchone()
    assert row == (168.0, 168.0, 1, 1)


def test_compute_feed_stats_uses_nearest_rank_and_distinct_event_observations(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "feed-percentiles.sqlite", Decimal("100"))
    for index, lead in enumerate((10, 20, 30, 40, 50)):
        created = now - timedelta(hours=1)
        created_text = (
            created.isoformat().replace("+00:00", "Z") if index == 0 else created.isoformat()
        )
        commence = (created + timedelta(hours=lead)).isoformat()
        event_id = f"event-{index}"
        _snapshot(ledger, "basketball_nba", event_id, created_text, commence, "Home")
        if index == 0:
            _snapshot(ledger, "basketball_nba", event_id, created_text, commence, "Away")
            _snapshot(ledger, "basketball_nba", event_id, created_text, commence, "Draw")
    _snapshot(
        ledger,
        "basketball_nba",
        "too-old",
        (now - timedelta(days=15)).isoformat(),
        (now - timedelta(days=15) + timedelta(hours=500)).isoformat(),
    )
    ledger.conn.commit()

    stats = compute_feed_stats(ledger, ["basketball_nba"], now)
    assert stats["basketball_nba"].lead_p50_h == 30
    assert stats["basketball_nba"].lead_p90_h == 50
    assert not stats["basketball_nba"].provisional


def test_repeat_pulls_do_not_drag_feed_percentiles_below_distinct_event_leads(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "repeat-feed-pulls.sqlite", Decimal("100"))
    commence = now + timedelta(hours=24)
    for index in range(10):
        created = now - timedelta(hours=index)
        _snapshot(
            ledger,
            "americanfootball_nfl",
            "repeated-event",
            created.isoformat(),
            commence.isoformat(),
        )
        _snapshot(
            ledger,
            "americanfootball_nfl",
            "repeated-event",
            created.isoformat(),
            commence.isoformat(),
            team="Away",
        )
    for index in range(5):
        created = now - timedelta(hours=1)
        _snapshot(
            ledger,
            "americanfootball_nfl",
            f"early-event-{index}",
            created.isoformat(),
            (created + timedelta(hours=200)).isoformat(),
        )
    ledger.conn.commit()

    stat = compute_feed_stats(ledger, ["americanfootball_nfl"], now)["americanfootball_nfl"]
    settings = Settings(_env_file=None, min_lead_hours=72, rules_version="v3")

    assert stat.lead_p90_h >= 200
    assert not stat.provisional
    assert eligible(stat, settings)


def test_feed_stats_count_recent_and_useful_pulls_from_feed_pulls(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "feed-pulls-stats.sqlite", Decimal("100"))
    ledger.record_feed_pulls({"soccer_epl": 0}, "v3", now - timedelta(days=1))
    ledger.record_feed_pulls({"soccer_epl": 2}, "v3", now - timedelta(hours=1))
    ledger.record_feed_pulls({"soccer_epl": 0}, "v3", now - timedelta(days=15))

    stat = compute_feed_stats(ledger, ["soccer_epl"], now)["soccer_epl"]
    assert stat.pulls_14d == 2
    assert stat.useful_pulls_14d == 1


def test_compute_feed_stats_from_connection_handles_legacy_outright_feed_names(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "outright-stats.sqlite", Decimal("100"))
    created = now - timedelta(hours=1)
    for index in range(5):
        _snapshot(
            ledger,
            "outrights:golf_masters_tournament_winner",
            f"golfer-{index}",
            created.isoformat(),
            (created + timedelta(hours=400 + index)).isoformat(),
        )
    ledger.conn.commit()

    stats = compute_feed_stats_from_connection(ledger.conn, ["golf_masters_tournament_winner"], now)
    assert stats["golf_masters_tournament_winner"].lead_p50_h == 402
    assert not stats["golf_masters_tournament_winner"].provisional


def test_rotation_value_requires_eligible_games_and_uses_staleness(tmp_path) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "rotation-score.sqlite", Decimal("100"))
    games = {
        "americanfootball_nfl": [PlannedGame(now + timedelta(hours=100), event_ticker="NFL-EVENT")],
        "icehockey_nhl": [PlannedGame(now + timedelta(hours=100), event_ticker="NHL-EVENT")],
        "soccer_epl": [PlannedGame(now + timedelta(hours=100), event_ticker="EPL-EVENT")],
        "baseball_mlb": [PlannedGame(now + timedelta(hours=100), event_ticker="MLB-EVENT")],
        "basketball_nba": [PlannedGame(now + timedelta(hours=100), event_ticker="NBA-EVENT")],
    }
    games["americanfootball_nfl"].append(
        PlannedGame(now + timedelta(hours=120), event_ticker="NFL-EVENT-2")
    )
    _market_snapshot(ledger, "NFL-EVENT", "NFL-EVENT-H", now, "0.45", "0.49", "10")
    _market_snapshot(ledger, "NFL-EVENT-2", "NFL-EVENT-2-H", now, "0.45", "0.49", "10")
    _market_snapshot(ledger, "NHL-EVENT", "NHL-EVENT-H", now, "0.45", "0.49", "10")
    _market_snapshot(ledger, "EPL-EVENT", "EPL-EVENT-H", now, "0.45", "0.49", "10")
    _market_snapshot(ledger, "MLB-EVENT", "MLB-EVENT-H", now, "0.45", "0.49", "10")
    _market_snapshot(ledger, "NBA-EVENT", "NBA-EVENT-H", now, "0.45", "0.49", "10")
    ledger.record_feed_pulls({"americanfootball_nfl": 0}, "v3", now - timedelta(hours=2))
    stats = {
        "americanfootball_nfl": _stat("americanfootball_nfl", 100),
        "icehockey_nhl": _stat("icehockey_nhl", 30),
        "soccer_epl": _stat("soccer_epl", 168),
        "baseball_mlb": _stat("baseball_mlb", 24),
        "basketball_nba": _stat("basketball_nba", 100),
    }
    scores = rotation_scores(
        ledger,
        games,
        stats,
        Settings(_env_file=None, rules_version="v3", min_lead_hours=72),
        now,
    )
    assert scores["americanfootball_nfl"].staleness_hours == 2
    assert scores["icehockey_nhl"].staleness_hours == 24
    assert scores["icehockey_nhl"].value == 0
    assert scores["baseball_mlb"].value == 0
    assert scores["basketball_nba"].value == 0
    assert scores["americanfootball_nfl"].value == 2
    assert scores["soccer_epl"].score > scores["americanfootball_nfl"].score

    class Events:
        events_free = True

        def events(self, *_args, **_kwargs):
            return [{"commence_time": (now + timedelta(hours=100)).isoformat()}]

    selected, _skips, _scores = select_rotation_feeds(
        ledger,
        games,
        stats,
        Settings(_env_file=None, rules_version="v3", min_lead_hours=72),
        Events(),
        now,
        1,
    )
    assert selected == ["soccer_epl"]


def test_rotation_never_selects_feed_without_eligible_window_games(tmp_path) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "rotation-empty-window.sqlite", Decimal("100"))
    games = {
        "americanfootball_ncaaf": [
            PlannedGame(now + timedelta(hours=24), event_ticker="NCAAF-EVENT")
        ]
    }
    _market_snapshot(ledger, "NCAAF-EVENT", "NCAAF-EVENT-H", now, "0.45", "0.49", "10")

    class Events:
        events_free = True

        def events(self, *_args, **_kwargs):
            raise AssertionError("zero-value feed must not be checked")

    selected, skips, scores = select_rotation_feeds(
        ledger,
        games,
        {"americanfootball_ncaaf": _stat("americanfootball_ncaaf", 200)},
        Settings(_env_file=None, rules_version="v3", min_lead_hours=72),
        Events(),
        now,
        1,
    )
    assert scores["americanfootball_ncaaf"].value == 0
    assert selected == []
    assert skips == {}


def test_rotation_skips_no_move_at_under_six_hours_and_allows_exact_boundary(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "rotation-skip.sqlite", Decimal("100"))
    games = {
        "americanfootball_nfl": [PlannedGame(now + timedelta(hours=100), event_ticker="NFL-EVENT")]
    }
    _market_snapshot(
        ledger, "NFL-EVENT", "NFL-EVENT-H", now - timedelta(hours=6), "0.45", "0.49", "10"
    )
    _market_snapshot(ledger, "NFL-EVENT", "NFL-EVENT-H", now, "0.45", "0.49", "10")
    ledger.record_feed_pulls({"americanfootball_nfl": 1}, "v3", now - timedelta(hours=6))
    stats = {"americanfootball_nfl": _stat("americanfootball_nfl", 100)}

    class Events:
        events_free = True

        def events(self, *_args, **_kwargs):
            return [{"commence_time": (now + timedelta(hours=100)).isoformat()}]

    settings = Settings(_env_file=None, rules_version="v3", min_lead_hours=72)
    selected, skips, _scores = select_rotation_feeds(
        ledger, games, stats, settings, Events(), now, 1
    )
    assert selected == ["americanfootball_nfl"]
    assert skips == {}

    ledger.record_feed_pulls({"americanfootball_nfl": 1}, "v3", now - timedelta(hours=1))
    selected, skips, _scores = select_rotation_feeds(
        ledger, games, stats, settings, Events(), now, 1
    )
    assert selected == []
    assert skips == {"americanfootball_nfl": "no_move"}
    _market_snapshot(ledger, "NFL-EVENT", "NFL-EVENT-H", now, "0.455", "0.49", "10")
    selected, skips, _scores = select_rotation_feeds(
        ledger, games, stats, settings, Events(), now, 1
    )
    assert selected == []
    assert skips == {"americanfootball_nfl": "no_move"}
    _market_snapshot(ledger, "NFL-EVENT", "NFL-EVENT-H", now, "0.46", "0.49", "10")
    selected, skips, _scores = select_rotation_feeds(
        ledger, games, stats, settings, Events(), now, 1
    )
    assert selected == ["americanfootball_nfl"]
    assert skips == {}


def test_rotation_no_events_is_recorded_and_events_cost_disables_blocking(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "rotation-events.sqlite", Decimal("100"))
    games = {
        "americanfootball_nfl": [PlannedGame(now + timedelta(hours=100), event_ticker="NFL-EVENT")]
    }
    _market_snapshot(ledger, "NFL-EVENT", "NFL-EVENT-H", now, "0.45", "0.49", "10")
    stats = {"americanfootball_nfl": _stat("americanfootball_nfl", 100)}

    class Events:
        events_free = True

        def events(self, *_args, **_kwargs):
            return []

    selected, skips, _scores = select_rotation_feeds(
        ledger,
        games,
        stats,
        Settings(_env_file=None, rules_version="v3", min_lead_hours=72),
        Events(),
        now,
        1,
    )
    assert selected == []
    assert skips == {"americanfootball_nfl": "no_events"}

    class ChargedEvents:
        events_free = True

        def events(self, *_args, **_kwargs):
            self.events_free = False
            return []

    selected, skips, _scores = select_rotation_feeds(
        ledger,
        games,
        stats,
        Settings(_env_file=None, rules_version="v3", min_lead_hours=72),
        ChargedEvents(),
        now,
        1,
    )
    assert selected == ["americanfootball_nfl"]
    assert skips == {}


def test_rotation_evaluates_top_feed_on_each_two_hour_slot_in_no_move_day(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    start = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "rotation-day.sqlite", Decimal("100"))
    games = {
        "americanfootball_nfl": [
            PlannedGame(start + timedelta(hours=100), event_ticker="NFL-EVENT")
        ]
    }
    _market_snapshot(
        ledger, "NFL-EVENT", "NFL-EVENT-H", start - timedelta(minutes=1), "0.45", "0.49", "10"
    )
    stats = {"americanfootball_nfl": _stat("americanfootball_nfl", 100)}

    class Events:
        events_free = True

        def events(self, *_args, **_kwargs):
            return [{"commence_time": (start + timedelta(hours=100)).isoformat()}]

    checked: list[datetime] = []
    for slot in range(12):
        now = start + timedelta(hours=slot * 2)
        selected, skips, scores = select_rotation_feeds(
            ledger,
            games,
            stats,
            Settings(_env_file=None, rules_version="v3", min_lead_hours=72),
            Events(),
            now,
            1,
        )
        assert scores["americanfootball_nfl"].score > 0
        assert set(selected).isdisjoint(skips)
        checked.append(now)
        if selected:
            ledger.record_feed_pulls({selected[0]: 1}, "v3", now)
    assert len(checked) == 12
    assert all(
        (right - left) == timedelta(hours=2)
        for left, right in zip(checked, checked[1:], strict=False)
    )


def test_workflow_uses_twelve_two_hour_sportsbook_slots() -> None:
    workflow = (Path(__file__).parents[1] / ".github/workflows/paper.yml").read_text()
    assert '- cron: "17 */2 * * *"' in workflow
    assert '- cron: "17 1-23/2 * * *"' in workflow
    assert '- cron: "47 * * * *"' in workflow
    assert 'PBOT_ODDS_RUNS_PER_DAY: "12"' in workflow
    assert "github.event.schedule == '17 */2 * * *'" in workflow


def test_eligible_uses_lead_buffer_and_v2_disabled_sports() -> None:
    settings = Settings(_env_file=None, rules_version="v3", min_lead_hours=72)
    assert eligible(_stat("americanfootball_nfl", 78), settings)
    assert not eligible(_stat("icehockey_nhl", 77.9), settings)
    assert not eligible(_stat("basketball_nba", 100), settings)
    assert eligible(
        _stat("basketball_nba", 100),
        settings.model_copy(update={"rules_version": "v1"}),
    )


def test_liquidity_weights_choose_tightest_leg_and_default_to_quarter(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "liquidity.sqlite", Decimal("100"))
    games = {
        "americanfootball_nfl": [
            PlannedGame(now + timedelta(hours=100), event_ticker="NFL-1"),
            PlannedGame(now + timedelta(hours=110), event_ticker="NFL-2"),
            PlannedGame(now + timedelta(hours=120), event_ticker="NFL-3"),
        ]
    }
    _market_snapshot(ledger, "NFL-1", "LEG-1A", now, "0.40", "0.44", "50")
    _market_snapshot(ledger, "NFL-1", "LEG-1B", now, "0.40", "0.45", "500")
    _market_snapshot(ledger, "NFL-2", "LEG-2", now, "0.40", "0.42", "49")
    _market_snapshot(ledger, "NFL-2", "LEG-2", now + timedelta(hours=1), "0.40", "0.45", "500")
    ledger.conn.commit()

    weights = liquidity_weights(ledger, games, Settings(_env_file=None))
    assert weights == {
        "NFL-1": Decimal("1"),
        "NFL-2": Decimal("0.25"),
        "NFL-3": Decimal("0.25"),
    }
    scores = feed_scores(games, weights, Settings(_env_file=None), now)
    assert scores["americanfootball_nfl"].weighted_game_count == Decimal("1.5")
    ranking_games = {
        "americanfootball_nfl": [games["americanfootball_nfl"][0]],
        "soccer_epl": [games["americanfootball_nfl"][1]],
        "americanfootball_ncaaf": [games["americanfootball_nfl"][2]],
    }
    ranking = select_entry_feeds(
        ranking_games,
        {feed: _stat(feed, 200) for feed in ranking_games},
        weights,
        Settings(_env_file=None),
        ledger,
        now,
    )
    assert ranking[0] == "americanfootball_nfl"


def test_feed_scores_only_counts_games_inside_inclusive_entry_window() -> None:
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    assert PlannedGame(now).event_ticker == ""
    settings = Settings(_env_file=None, min_lead_hours=72)
    games = {
        "americanfootball_nfl": [
            PlannedGame(now + timedelta(hours=71, minutes=59), event_ticker="too-early"),
            PlannedGame(now + timedelta(hours=72), event_ticker="start"),
            PlannedGame(now + timedelta(days=7), event_ticker="end"),
            PlannedGame(now + timedelta(days=7, seconds=1), event_ticker="too-late"),
        ]
    }
    scores = feed_scores(
        games,
        {"start": Decimal("1"), "end": Decimal("0.25")},
        settings,
        now,
    )
    assert scores["americanfootball_nfl"].weighted_game_count == Decimal("1.25")
    assert scores["americanfootball_nfl"].soonest_kickoff == now + timedelta(hours=72)


def test_zero_score_feed_is_not_selected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "zero-score.sqlite", Decimal("100"))
    games = {"americanfootball_nfl": [PlannedGame(now + timedelta(hours=100), event_ticker="NFL")]}
    assert (
        select_entry_feeds(
            games,
            {"americanfootball_nfl": _stat("americanfootball_nfl", 200)},
            {"NFL": Decimal(0)},
            Settings(_env_file=None),
            ledger,
            now,
        )
        == []
    )


def test_lead_aware_selection_excludes_short_lead_and_disabled_nba(tmp_path) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "selection.sqlite", Decimal("100"))
    leads = {
        "icehockey_nhl": 30,
        "baseball_mlb": 24,
        "basketball_nba": 40,
        "soccer_epl": 170,
        "americanfootball_nfl": 220,
        "americanfootball_ncaaf": 160,
    }
    stats = {feed: _stat(feed, lead) for feed, lead in leads.items()}
    games = {feed: [PlannedGame(now + timedelta(hours=100), event_ticker=feed)] for feed in leads}
    settings = Settings(
        _env_file=None,
        rules_version="v3",
        min_lead_hours=72,
        v2_disabled_sports=["basketball_nba"],
    )
    selected = select_entry_feeds(
        games,
        stats,
        {feed: 1 for feed in leads},
        settings,
        ledger,
        now,
    )
    assert set(selected) == {
        "americanfootball_nfl",
        "americanfootball_ncaaf",
        "soccer_epl",
    }
    assert "icehockey_nhl" not in selected
    assert "baseball_mlb" not in selected
    assert "basketball_nba" not in selected

    zero_lead = settings.model_copy(update={"min_lead_hours": 0})
    selected_zero = select_entry_feeds(
        games, stats, {feed: 1 for feed in leads}, zero_lead, ledger, now
    )
    assert "icehockey_nhl" in selected_zero
    assert "baseball_mlb" in selected_zero
    assert "basketball_nba" not in selected_zero


def test_learning_rule_demotes_only_three_recent_zero_candidate_pulls(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "learning.sqlite", Decimal("100"))
    ledger.record_feed_pulls({"americanfootball_nfl": 0}, "v3", now - timedelta(hours=50))
    ledger.record_feed_pulls({"americanfootball_nfl": 0}, "v3", now - timedelta(hours=49))
    ledger.record_feed_pulls({"americanfootball_nfl": 0}, "v3", now - timedelta(hours=48))
    assert learning_rule_demoted(ledger, "americanfootball_nfl", now)
    ledger.record_feed_pulls({"americanfootball_nfl": 1}, "v3", now - timedelta(minutes=1))
    assert not learning_rule_demoted(ledger, "americanfootball_nfl", now)

    ledger.record_feed_pulls({"icehockey_nhl": 0}, "v3", now - timedelta(hours=50))
    ledger.record_feed_pulls({"icehockey_nhl": 0}, "v3", now - timedelta(hours=49))
    ledger.record_feed_pulls({"icehockey_nhl": 0}, "v3", now - timedelta(hours=48, seconds=1))
    assert not learning_rule_demoted(ledger, "icehockey_nhl", now)


def test_learning_rule_keeps_demoted_feeds_included_at_the_bottom(tmp_path) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "demotion-order.sqlite", Decimal("100"))
    for age in (3, 2, 1):
        ledger.record_feed_pulls({"americanfootball_nfl": 0}, "v3", now - timedelta(hours=age))
    feeds = ("americanfootball_nfl", "soccer_epl")
    games = {feed: [PlannedGame(now + timedelta(hours=100), event_ticker=feed)] for feed in feeds}
    stats = {feed: _stat(feed, 200) for feed in feeds}

    selected = select_entry_feeds(
        games,
        stats,
        {feed: Decimal("1") for feed in feeds},
        Settings(_env_file=None),
        ledger,
        now,
    )
    assert selected == ["soccer_epl", "americanfootball_nfl"]


def test_learning_rule_without_three_pulls_does_not_demote(tmp_path) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "few-pulls.sqlite", Decimal("100"))
    ledger.record_feed_pulls({"americanfootball_nfl": 0}, "v3", now - timedelta(hours=1))
    ledger.record_feed_pulls({"americanfootball_nfl": 0}, "v3", now - timedelta(minutes=1))
    assert not learning_rule_demoted(ledger, "americanfootball_nfl", now)


def test_compute_feed_stats_from_connection_accepts_empty_feed_list() -> None:
    conn = sqlite3.connect(":memory:")
    assert compute_feed_stats_from_connection(conn, [], datetime.now(timezone.utc)) == {}


def test_empty_odds_bookmakers_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least one bookmaker"):
        Settings(_env_file=None, odds_bookmakers=[])
    assert Settings(_env_file=None).odds_feed_selection == "rotation"


class _OddsStub:
    allowed_sports: set[str] | None = None
    budget: int | None = None
    requests_used: str | None = None
    requests_remaining: str | None = None


class _EngineStub:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def settle_open_positions(self) -> dict[str, str]:
        return {}

    def run_cycle(self, markets: list[object]) -> CycleResult:
        return CycleResult()


@pytest.mark.parametrize(
    ("selection", "expected_allowed", "expected_stats"),
    [
        ("lead_aware", {"americanfootball_nfl"}, 1),
        ("legacy", None, 0),
    ],
)
def test_paper_applies_feed_selection_only_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
    expected_allowed: set[str] | None,
    expected_stats: int,
) -> None:
    settings = Settings(
        _env_file=None,
        rules_version="v1",
        odds_planner="fixed",
        odds_feed_selection=selection,
        paper_db_path=tmp_path / f"{selection}.sqlite",
    )
    odds = _OddsStub()
    now = datetime.now(timezone.utc)
    games = {
        "americanfootball_nfl": [PlannedGame(now + timedelta(hours=100), event_ticker="NFL-EVENT")]
    }
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_client", lambda _settings: object())
    monkeypatch.setattr(cli, "_odds_client", lambda _settings: odds)
    monkeypatch.setattr(cli, "_strategies", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, "_fetch", lambda _client, _series: [])
    monkeypatch.setattr(cli, "_planner_games", lambda *args, **kwargs: games)
    monkeypatch.setattr(cli, "Engine", _EngineStub)
    monkeypatch.setattr(cli, "score_maker_shadows", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "record_clv", lambda *args, **kwargs: 0)

    cli.paper(strategy=["sportsbook_arb"], interval=0, cycles=1)

    assert odds.allowed_sports == expected_allowed
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    assert ledger.conn.execute("SELECT COUNT(*) FROM feed_stats").fetchone()[0] == expected_stats


def test_feed_plan_does_not_request_odds_or_write_feed_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(_env_file=None, paper_db_path=tmp_path / "feed-plan.sqlite")
    now = datetime.now(timezone.utc)
    games = {
        "americanfootball_nfl": [PlannedGame(now + timedelta(hours=100), event_ticker="NFL-EVENT")]
    }
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_client", lambda _settings: object())
    monkeypatch.setattr(cli, "_fetch", lambda _client, _series: [])
    monkeypatch.setattr(cli, "_planner_games", lambda *args, **kwargs: games)

    class FreeEventsOnly:
        events_free = True

        def events(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(cli, "_odds_client", lambda *_args, **_kwargs: FreeEventsOnly())
    monkeypatch.setattr(cli, "console", Console(record=True))

    cli.feed_plan(budget=1)

    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    assert ledger.conn.execute("SELECT COUNT(*) FROM feed_stats").fetchone()[0] == 0


def test_odds_budget_reports_run_feeds_credits_and_reconstructed_useful_pulls(
    tmp_path: Path,
) -> None:
    path = tmp_path / "budget.sqlite"
    ledger = Ledger(path, Decimal("100"))
    run_times = [datetime(2026, 10, 1, hour, tzinfo=timezone.utc) for hour in (8, 10, 12, 14)]
    usage = (100, 105, 3, 8)
    for index, (ran_at, cumulative) in enumerate(zip(run_times, usage, strict=True), 1):
        ledger.conn.execute(
            "INSERT INTO runs (id,ran_at,strategies,scanned,signals,filled,settled,cash,"
            "open_cost,odds_used) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (index, ran_at.isoformat(), "sportsbook_v3", 0, 0, 0, 0, "100", "0", cumulative),
        )

    def add_odds_snapshot(at: datetime, feed: str, event_id: str) -> None:
        _snapshot(
            ledger,
            feed,
            event_id,
            at.isoformat(),
            (at + timedelta(hours=200)).isoformat(),
        )

    add_odds_snapshot(run_times[1] - timedelta(minutes=1), "americanfootball_nfl", "nfl-1")
    add_odds_snapshot(run_times[1], "soccer_uefa_europa_league", "uel-1")
    add_odds_snapshot(run_times[2], "baseball_mlb", "mlb-2")
    add_odds_snapshot(run_times[3], "soccer_uefa_europa_league", "uel-3")
    ledger.record_feed_skips({"soccer_uefa_europa_league": "no_move"}, run_times[3])
    ledger.conn.execute(
        "INSERT INTO blocks (created_at,event_ticker,reason,rules_version) VALUES (?,?,?,?)",
        (run_times[1].isoformat(), "KXNFLGAME-26OCT01", "net_edge", "v3"),
    )
    ledger.conn.execute(
        "INSERT INTO blocks (created_at,event_ticker,reason,rules_version) VALUES (?,?,?,?)",
        (run_times[1].isoformat(), "KXNFLGAME-26OCT01", "entry_window", "v3"),
    )
    ledger.conn.execute(
        "INSERT INTO signals (created_at,strategy,ticker,side,fair_prob,limit_price,edge,size,"
        "rationale,event_ticker) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            run_times[3].isoformat(),
            "sportsbook_v3",
            "KXUELGAME-26OCT01-HOME",
            "yes",
            "0.6",
            "0.4",
            "0.1",
            "1",
            "test",
            "KXUELGAME-26OCT01",
        ),
    )
    ledger.conn.commit()

    report = _feed_report_sections(
        path,
        [
            (index, ran_at, cumulative)
            for index, (ran_at, cumulative) in enumerate(zip(run_times, usage, strict=True), 1)
        ],
        run_times[0],
        run_times[-1] + timedelta(minutes=1),
    )
    assert "2,1,5,americanfootball_nfl|soccer_uefa_europa_league" in report
    assert "4,3,5,soccer_uefa_europa_league" in report
    assert (
        "useful-pull ratio per feed (source: feed_pulls per run window; blocks+signals "
        "reconstructed when absent" in report
    )
    assert "americanfootball_nfl,1,0,0.000" in report
    assert "soccer_uefa_europa_league,2,1,0.500" in report
    assert "feed stats and eligibility (nearest-rank percentiles; min_lead_hours=72)" in report
    ledger.record_feed_pulls({"baseball_mlb": 1}, "v3", run_times[2], {"baseball_mlb": 2})
    report_with_feed_pulls = _feed_report_sections(
        path,
        [
            (index, ran_at, cumulative)
            for index, (ran_at, cumulative) in enumerate(zip(run_times, usage, strict=True), 1)
        ],
        run_times[0],
        run_times[-1] + timedelta(minutes=1),
    )
    assert "useful-pull ratio per feed (source: feed_pulls per run window" in report_with_feed_pulls
    assert "americanfootball_nfl,1,0,0.000" in report_with_feed_pulls
    assert "baseball_mlb,1,1,1.000" in report_with_feed_pulls
    assert "soccer_uefa_europa_league,2,1,0.500" in report_with_feed_pulls
    assert (
        "credits by feed (SUM(requests_last), fallback to feed attribution)"
        in report_with_feed_pulls
    )
    assert "baseball_mlb,2" in report_with_feed_pulls
    assert "feed skips by reason:" in report_with_feed_pulls
    assert "soccer_uefa_europa_league,no_move,1" in report_with_feed_pulls


def test_v3_evaluate_counts_net_edge_blocks_and_signals_as_feed_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    class OddsStub:
        allowed_sports: set[str] | None = None

        def h2h(self, *_args: object, **_kwargs: object) -> list[object]:
            return []

    ledger = Ledger(tmp_path / "candidate-count.sqlite", Decimal("100"))
    strategy = SportsbookV3Strategy(
        Settings(_env_file=None, rules_version="v3"),
        OddsStub(),  # type: ignore[arg-type]
        ledger=ledger,
    )
    original_finish = strategy._finish_feed_pulls
    event_ticker = "KXNFLGAME-26OCT25HOMEAWAY"

    def inject_candidates(_signals: object) -> None:
        strategy._fetched_feeds.add("americanfootball_nfl")
        strategy._blocks.extend(
            [
                SimpleNamespace(reason="net_edge", event_ticker=event_ticker),
                SimpleNamespace(reason="net_edge", event_ticker=event_ticker),
            ]
        )
        original_finish([SimpleNamespace(event_ticker=event_ticker)])  # type: ignore[list-item]

    monkeypatch.setattr(strategy, "_finish_feed_pulls", inject_candidates)
    strategy.evaluate([])

    assert strategy.seen_feed_pulls() == {"americanfootball_nfl": 3}
    ledger.record_feed_pulls(strategy.seen_feed_pulls(), "v3")
    row = ledger.conn.execute(
        "SELECT candidates FROM feed_pulls WHERE feed='americanfootball_nfl'"
    ).fetchone()
    assert row == (3,)
