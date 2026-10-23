from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from prediction_bot.config import Settings
from prediction_bot.engine import Engine
from prediction_bot.models import OutcomeSide, QuoteSnapshot, Signal
from prediction_bot.odds import consensus
from prediction_bot.paper.ledger import Ledger
from prediction_bot.teams import team_for_odds_name
from tests.test_sportsbook_v2 import (
    FakeOdds,
    SportsbookV2Strategy,
    fixed_clock,
    make_market,
    odds_event,
)


def test_line_move_follower_is_shadow_only_and_uses_previous_snapshots(tmp_path) -> None:
    commence = "2026-10-25T20:25:00Z"
    previous_game = odds_event(
        "kc-lv",
        "Kansas City Chiefs",
        "Las Vegas Raiders",
        commence,
        {
            "Kansas City Chiefs": Decimal("0.50"),
            "Las Vegas Raiders": Decimal("0.50"),
        },
    )
    current_game = odds_event(
        "kc-lv",
        "Kansas City Chiefs",
        "Las Vegas Raiders",
        commence,
        {
            "Kansas City Chiefs": Decimal("0.40"),
            "Las Vegas Raiders": Decimal("0.60"),
        },
    )
    ledger = Ledger(tmp_path / "line-move.sqlite", Decimal("1000"))
    ledger.record_consensus(
        {"odds": {"kc-lv": consensus(previous_game)}},
        at=datetime(2026, 10, 22, tzinfo=timezone.utc),
    )
    markets = [
        make_market(
            "KXNFLGAME-26OCT25KCLV-KC",
            "0.49",
            "0.51",
            subtitle="Kansas City",
        ),
        make_market(
            "KXNFLGAME-26OCT25KCLV-LV",
            "0.49",
            "0.51",
            subtitle="Las Vegas",
        ),
    ]
    ledger.record_quote_snapshots(
        [
            QuoteSnapshot(
                ticker=market.ticker,
                event_ticker=market.event_ticker,
                yes_bid=Decimal("0.49"),
                yes_ask=Decimal("0.51"),
                last_price=None,
                volume=Decimal(0),
                open_interest=Decimal(0),
                created_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
            )
            for market in markets
        ]
    )
    strategy = SportsbookV2Strategy(
        Settings(_env_file=None, min_lead_hours=0),
        FakeOdds([current_game]),
        ledger=ledger,
        clock=fixed_clock("2026-10-23T00:00:00Z"),
    )

    result = Engine(strategy.settings, object(), [strategy], ledger).run_cycle(markets)  # type: ignore[arg-type]

    assert all(signal.strategy != "line_move_follower" for signal in result.signals)
    row = ledger.conn.execute("SELECT strategy,reason,would_trade FROM shadow_decisions").fetchone()
    assert tuple(row) == ("line_move_follower", "line_move_follower", 1)


def test_line_move_is_not_recorded_after_commence(tmp_path) -> None:
    commence = "2026-10-22T20:25:00Z"
    previous_game = odds_event(
        "kc-lv",
        "Kansas City Chiefs",
        "Las Vegas Raiders",
        commence,
        {"Kansas City Chiefs": Decimal("0.50"), "Las Vegas Raiders": Decimal("0.50")},
    )
    current_game = odds_event(
        "kc-lv",
        "Kansas City Chiefs",
        "Las Vegas Raiders",
        commence,
        {"Kansas City Chiefs": Decimal("0.40"), "Las Vegas Raiders": Decimal("0.60")},
    )
    ledger = Ledger(tmp_path / "line-move-past.sqlite", Decimal("1000"))
    ledger.record_consensus(
        {"odds": {"kc-lv": consensus(previous_game)}},
        at=datetime(2026, 10, 22, 18, tzinfo=timezone.utc),
    )
    markets = [
        make_market("KXNFLGAME-26OCT22KCLV-KC", "0.49", "0.51", subtitle="Kansas City"),
        make_market("KXNFLGAME-26OCT22KCLV-LV", "0.49", "0.51", subtitle="Las Vegas"),
    ]
    ledger.record_quote_snapshots(
        [
            QuoteSnapshot(
                ticker=market.ticker,
                event_ticker=market.event_ticker,
                yes_bid=Decimal("0.49"),
                yes_ask=Decimal("0.51"),
                last_price=None,
                volume=Decimal(0),
                open_interest=Decimal(0),
                created_at=datetime(2026, 10, 22, 18, tzinfo=timezone.utc),
            )
            for market in markets
        ]
    )
    strategy = SportsbookV2Strategy(
        Settings(_env_file=None, min_lead_hours=0),
        FakeOdds([current_game]),
        ledger=ledger,
        clock=fixed_clock("2026-10-23T00:00:00Z"),
    )

    strategy.evaluate(markets)

    assert not strategy.seen_line_move_decisions()


def test_line_move_follower_accounts_for_no_side_movement(tmp_path) -> None:
    commence = "2026-10-25T20:25:00Z"
    previous_game = odds_event(
        "kc-lv",
        "Kansas City Chiefs",
        "Las Vegas Raiders",
        commence,
        {
            "Kansas City Chiefs": Decimal("0.50"),
            "Las Vegas Raiders": Decimal("0.50"),
        },
    )
    current_game = odds_event(
        "kc-lv",
        "Kansas City Chiefs",
        "Las Vegas Raiders",
        commence,
        {
            "Kansas City Chiefs": Decimal("0.40"),
            "Las Vegas Raiders": Decimal("0.60"),
        },
    )
    ledger = Ledger(tmp_path / "line-move-no-side.sqlite", Decimal("1000"))
    ledger.record_consensus(
        {"odds": {"kc-lv": consensus(previous_game)}},
        at=datetime(2026, 10, 22, tzinfo=timezone.utc),
    )
    old_quotes = [
        make_market(
            "KXNFLGAME-26OCT25KCLV-KC",
            "0.49",
            "0.51",
            subtitle="Kansas City",
        ),
        make_market(
            "KXNFLGAME-26OCT25KCLV-LV",
            "0.49",
            "0.51",
            subtitle="Las Vegas",
        ),
    ]
    ledger.record_quote_snapshots(
        [
            QuoteSnapshot(
                ticker=market.ticker,
                event_ticker=market.event_ticker,
                yes_bid=Decimal("0.49"),
                yes_ask=Decimal("0.51"),
                last_price=None,
                volume=Decimal(0),
                open_interest=Decimal(0),
                created_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
            )
            for market in old_quotes
        ]
    )
    current_quotes = [
        make_market(
            "KXNFLGAME-26OCT25KCLV-KC",
            "0.44",
            "0.46",
            subtitle="Kansas City",
        ),
        make_market(
            "KXNFLGAME-26OCT25KCLV-LV",
            "0.54",
            "0.56",
            subtitle="Las Vegas",
        ),
    ]
    strategy = SportsbookV2Strategy(
        Settings(_env_file=None, min_lead_hours=0),
        FakeOdds([current_game]),
        ledger=ledger,
        clock=fixed_clock("2026-10-23T00:00:00Z"),
    )

    strategy.evaluate(current_quotes)

    assert not strategy.seen_line_move_decisions()


def test_road_dog_requires_away_team_attention_and_uses_one_percent_cap(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        min_lead_hours=0,
        exp_road_dog_rivalries=["Kansas City Chiefs vs Las Vegas Raiders"],
    )
    game_event = odds_event(
        "kc-lv",
        "Kansas City Chiefs",
        "Las Vegas Raiders",
        "2026-10-25T20:25:00Z",
        {
            "Kansas City Chiefs": Decimal("0.60"),
            "Las Vegas Raiders": Decimal("0.40"),
        },
    )
    ledger = Ledger(tmp_path / "road-dog.sqlite", Decimal("1000"))
    strategy = SportsbookV2Strategy(
        settings,
        FakeOdds([game_event]),
        ledger=ledger,
        clock=fixed_clock("2026-10-20T20:25:00Z"),
    )
    game = consensus(game_event)
    assert game is not None
    away = team_for_odds_name("americanfootball_nfl", "Las Vegas Raiders")
    assert away is not None
    assert (
        strategy._road_dog_candidate(
            "americanfootball_nfl",
            "KXNFLGAME-26OCT25KCLV",
            game,
            datetime(2026, 10, 25, 20, 25, tzinfo=timezone.utc),
            away,
            OutcomeSide.yes,
            Decimal("0.30"),
            Decimal("0.01"),
            datetime(2026, 10, 20, 20, 25, tzinfo=timezone.utc),
        )
        is True
    )
    assert not strategy._road_dog_candidate(
        "americanfootball_nfl",
        "KXNFLGAME-26OCT25KCLV",
        game,
        datetime(2026, 10, 25, 20, 25, tzinfo=timezone.utc),
        away,
        OutcomeSide.no,
        Decimal("0.30"),
        Decimal("0.01"),
        datetime(2026, 10, 20, 20, 25, tzinfo=timezone.utc),
    )
    candidate = Signal(
        strategy="sportsbook_v2",
        ticker="KXNFLGAME-26OCT25KCLV-LV",
        side=OutcomeSide.yes,
        fair_prob=Decimal("0.90"),
        limit_price=Decimal("0.30"),
        edge=Decimal("0.50"),
        size=Decimal(1),
        created_at=datetime(2026, 10, 20, 20, 25, tzinfo=timezone.utc),
        rules_version="v2",
        position_key="KXNFLGAME-26OCT25KCLV:LV",
        event_ticker="KXNFLGAME-26OCT25KCLV",
        tags=("exp_road_dog",),
        fee_type="quadratic_with_maker_fees",
        fee_multiplier=Decimal(1),
        available_size=Decimal(100),
    )
    reserves = {
        "reserved_position": {},
        "reserved_event": {},
        "reserved_day": Decimal(0),
        "reserved_total": Decimal(0),
        "reserved_cash": Decimal(0),
    }
    road_dog_size = strategy._size_v2_signal(candidate, **reserves)
    main_size = strategy._size_v2_signal(
        replace(candidate, tags=("exp_road_dog", "value")),
        **reserves,
    )
    assert road_dog_size is not None
    assert main_size is not None
    assert road_dog_size.size < main_size.size
    signals = strategy.evaluate(
        [
            make_market(
                "KXNFLGAME-26OCT25KCLV-KC",
                "0.59",
                "0.61",
                subtitle="Kansas City",
            ),
            make_market(
                "KXNFLGAME-26OCT25KCLV-LV",
                "0.32",
                "0.34",
                subtitle="Las Vegas",
            ),
        ]
    )
    assert len(signals) == 1
    assert set(signals[0].tags or ()) >= {"value", "exp_road_dog"}


def test_road_dog_tag_and_cap_follow_the_selected_leg(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        min_lead_hours=0,
        longshot_haircut="none",
        max_gross_edge=Decimal("0.40"),
        v2_min_net_edge=Decimal(0),
        exp_road_dog_rivalries=["Kansas City Chiefs vs Las Vegas Raiders"],
    )
    game = odds_event(
        "kc-lv",
        "Kansas City Chiefs",
        "Las Vegas Raiders",
        "2026-10-25T20:25:00Z",
        {"Kansas City Chiefs": Decimal("0.60"), "Las Vegas Raiders": Decimal("0.40")},
    )
    strategy = SportsbookV2Strategy(
        settings,
        FakeOdds([game]),
        ledger=Ledger(tmp_path / "road-dog-selected-leg.sqlite", Decimal("1000")),
        clock=fixed_clock("2026-10-20T20:25:00Z"),
    )

    signals = strategy.evaluate(
        [
            make_market(
                "KXNFLGAME-26OCT25KCLV-KC",
                "0.86",
                "0.88",
                subtitle="Kansas City",
            ),
            make_market(
                "KXNFLGAME-26OCT25KCLV-LV",
                "0.19",
                "0.20",
                subtitle="Las Vegas",
            ),
        ]
    )

    assert len(signals) == 1
    assert signals[0].side is OutcomeSide.no
    assert "exp_road_dog" not in (signals[0].tags or ())


def test_nba_is_disabled_by_default_in_v2(tmp_path) -> None:
    game_event = odds_event(
        "bos-nyk",
        "Boston Celtics",
        "New York Knicks",
        "2026-10-25T20:00:00Z",
        {
            "Boston Celtics": Decimal("0.55"),
            "New York Knicks": Decimal("0.45"),
        },
    )
    ledger = Ledger(tmp_path / "nba-disabled.sqlite", Decimal("1000"))
    strategy = SportsbookV2Strategy(
        Settings(_env_file=None, min_lead_hours=0),
        FakeOdds([game_event]),
        ledger=ledger,
        clock=fixed_clock("2026-10-23T00:00:00Z"),
    )
    markets = [
        make_market(
            "KXNBAGAME-26OCT25BOSNYK-BOS",
            "0.53",
            "0.55",
            subtitle="Boston",
        ),
        make_market(
            "KXNBAGAME-26OCT25BOSNYK-NYK",
            "0.43",
            "0.45",
            subtitle="New York",
        ),
    ]

    assert not strategy.evaluate(markets)
    assert any(block.reason == "league_disabled" for block in strategy.seen_blocks())
