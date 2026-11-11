"""The underdog lane: underdog_value picks as resting paper orders, with an instant-fill
comparison book built from the same picks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from prediction_bot.clv import record_clv, refresh_clv_statuses
from prediction_bot.config import Settings
from prediction_bot.dashboard import _measurement_data, render_dashboard
from prediction_bot.engine import Engine
from prediction_bot.models import MakerShadowUpdate, Market, OutcomeSide, Signal
from prediction_bot.paper.ledger import UNDERDOG_LANE_REASON, Ledger
from prediction_bot.strategies.sportsbook_arb import GamePick
from prediction_bot.strategies.underdog_value import UnderdogValueStrategy
from prediction_bot.underdog_lane import lane_halt_reason, lane_key, run_underdog_lane
from tests.conftest import make_market

EVENT_TICKER = "KXNFLGAME-26NOV22LVSF"
LV = f"{EVENT_TICKER}-LV"
SF = f"{EVENT_TICKER}-SF"
KEY = f"ud:{EVENT_TICKER}:las-vegas-raiders"


def odds_event(*, days: float = 10, lv: float = 4.0, sf: float = 1.30) -> dict[str, Any]:
    """Raiders at 49ers. At 4.0 / 1.30 the Raiders de-vig to about 24.5%."""
    outcomes = [
        {"name": "San Francisco 49ers", "price": sf},
        {"name": "Las Vegas Raiders", "price": lv},
    ]
    return {
        "id": "nfl-lvsf",
        "home_team": "San Francisco 49ers",
        "away_team": "Las Vegas Raiders",
        "commence_time": (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(),
        "bookmakers": [
            {"key": book, "markets": [{"key": "h2h", "outcomes": outcomes}]}
            for book in ("pinnacle", "draftkings")
        ],
    }


class FakeOdds:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events

    def h2h(self, sport: str, bookmakers: list[str] | None = None) -> list[dict[str, Any]]:
        return self.events if sport == "americanfootball_nfl" else []


class Candles:
    """A Kalshi client that only serves 1-minute candles."""

    def __init__(self, candles: list[dict[str, Any]] | None = None) -> None:
        self.candles = candles or []
        self.calls = 0

    def get_candlesticks(self, *_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        self.calls += 1
        return self.candles

    def create_order(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("the lane is paper only")


def lane_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        paper_db_path=tmp_path / "paper.sqlite",
        rules_version="v3",
        underdog_lane="paper",
        min_edge=Decimal("0.01"),
        odds_bookmakers=["pinnacle", "draftkings"],
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def wide_book() -> list[Market]:
    """Raiders 16c bid / 19c ask: room inside the spread, so the pick rests at 17c."""
    return [
        make_market(LV, "0.16", "0.19", subtitle="Las Vegas"),
        make_market(SF, "0.81", "0.84", subtitle="San Francisco"),
    ]


def tight_book() -> list[Market]:
    """Raiders 18c bid / 19c ask: no room inside the spread, so the pick is the ask."""
    return [
        make_market(LV, "0.18", "0.19", subtitle="Las Vegas"),
        make_market(SF, "0.81", "0.82", subtitle="San Francisco"),
    ]


def engine_for(
    settings: Settings,
    ledger: Ledger,
    events: list[dict[str, Any]],
    *,
    client: Candles | None = None,
    clock: datetime | None = None,
) -> Engine:
    return Engine(
        settings,
        client or Candles(),  # type: ignore[arg-type]
        [],
        ledger,
        shadow_strategies=[UnderdogValueStrategy(settings, odds_client=FakeOdds(events))],  # type: ignore[arg-type]
        clock=(lambda: clock) if clock is not None else None,
    )


def rows(ledger: Ledger, sql: str) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in ledger.conn.execute(sql).fetchall()]


# --- the strategy reports its fixture -------------------------------------------------


def test_strategy_reports_each_pick_with_its_game(tmp_path: Path) -> None:
    settings = lane_settings(tmp_path)
    strat = UnderdogValueStrategy(settings, odds_client=FakeOdds([odds_event()]))  # type: ignore[arg-type]
    signals = strat.evaluate(wide_book())
    picks = strat.seen_game_picks()
    assert [p.signal for p in picks] == signals and len(picks) == 1
    pick = picks[0]
    assert (pick.signal.limit_price, pick.signal.taker) == (Decimal("0.17"), False)
    assert (pick.event_ticker, pick.odds_event_id, pick.sport) == (
        EVENT_TICKER,
        "nfl-lvsf",
        "americanfootball_nfl",
    )
    assert pick.outcome == "Las Vegas Raiders" and pick.teams_exact
    assert pick.commence_time is not None and pick.commence_time.tzinfo is not None
    assert strat.seen_evaluated_events() == {EVENT_TICKER}
    assert lane_key(pick) == KEY


def test_a_leg_whose_code_names_the_other_team_fails_the_exact_check(tmp_path: Path) -> None:
    """The LAFC / LA Galaxy bug: the fuzzy name match is happy, the ticker code is the
    other team. The alias tables catch it and the lane refuses the pick."""
    settings = lane_settings(tmp_path)
    swapped = [
        make_market(SF, "0.16", "0.19", subtitle="Las Vegas"),
        make_market(LV, "0.81", "0.84", subtitle="San Francisco"),
    ]
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    result = engine_for(settings, ledger, [odds_event()]).run_cycle(swapped)
    assert result.lane is not None and result.lane.blocked == {"lane_mapping": 1}
    assert rows(ledger, "SELECT COUNT(*) FROM paper_orders") == [(0,)]
    assert rows(ledger, "SELECT reason,strategy,rules_version,detail FROM blocks") == [
        ("lane_mapping", "underdog_value", "v1", "teams")
    ]


# --- the lane book: honest fills -----------------------------------------------------


def test_a_pick_inside_the_spread_rests_and_moves_no_cash(tmp_path: Path) -> None:
    settings = lane_settings(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    result = engine_for(settings, ledger, [odds_event()]).run_cycle(wide_book())

    assert result.lane is not None and len(result.lane.orders_placed) == 1
    assert result.acted == [] and result.orders_placed == result.lane.orders_placed
    assert rows(
        ledger,
        "SELECT strategy,rules_version,ticker,side,position_key,event_ticker,price,size,"
        "maker_fee,status,tags FROM paper_orders",
    ) == [
        (
            "underdog_value",
            "v1",
            LV,
            "yes",
            KEY,
            EVENT_TICKER,
            "0.17",
            "100",
            "0.25",  # 0.0175 x 100 x 0.17 x 0.83, rounded up to the cent
            "open",
            "maker,underdog_lane",
        )
    ]
    assert rows(ledger, "SELECT COUNT(*) FROM fills") == [(0,)]
    assert ledger.cash == Decimal("1000")
    assert ledger.available_cash(include_v3=True) == Decimal("982.75")
    # v3's own caps do not see the lane's order.
    assert ledger.v2_total_open_cost(include_v3=True) == 0
    assert ledger.held_keys(EVENT_TICKER, include_v3=True) == set()
    # The pick is tracked for settlement and shown as a prediction.
    assert rows(ledger, "SELECT strategy,acted,tags FROM signals") == [
        ("underdog_value", 1, "maker,underdog_lane")
    ]


def test_the_order_fills_only_when_the_market_trades_through_it(tmp_path: Path) -> None:
    settings = lane_settings(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    events = [odds_event()]
    engine_for(settings, ledger, events).run_cycle(wide_book())
    created = datetime.fromisoformat(rows(ledger, "SELECT created_at FROM paper_orders")[0][0])

    def candle(minutes: int, low: str) -> dict[str, Any]:
        at = created + timedelta(minutes=minutes)
        return {"end_period_ts": int(at.timestamp()), "price": {"low_dollars": low}}

    # A trade AT our price proves nothing about our place in the queue: still resting.
    later = created + timedelta(minutes=30)
    touched = Candles([candle(5, "0.17")])
    engine_for(settings, ledger, events, client=touched, clock=later).run_cycle(wide_book())
    assert touched.calls == 1
    assert rows(ledger, "SELECT status FROM paper_orders") == [("open",)]
    assert rows(ledger, "SELECT COUNT(*) FROM fills") == [(0,)]

    # A trade below it does: the order fills at 17c with the maker fee.
    through = Candles([candle(5, "0.17"), candle(12, "0.16")])
    result = engine_for(settings, ledger, events, client=through, clock=later).run_cycle(
        wide_book()
    )
    assert result.paper_fills_resolved == 1
    assert rows(ledger, "SELECT status,fill_rule FROM paper_orders") == [
        ("filled", "trade_through")
    ]
    assert rows(
        ledger,
        "SELECT strategy,rules_version,ticker,side,price,count,fee,position_key,event_ticker,"
        "tags,commence_time IS NOT NULL FROM fills",
    ) == [
        (
            "underdog_value",
            "v1",
            LV,
            "yes",
            "0.17",
            "100",
            "0.25",
            KEY,
            EVENT_TICKER,
            "maker,underdog_lane",
            1,
        )
    ]
    assert ledger.cash == Decimal("982.75")
    # Same cycle, same pick: one side per game, so nothing new is placed.
    assert result.lane is not None and result.lane.blocked == {"lane_held": 1}
    assert rows(ledger, "SELECT COUNT(*) FROM paper_orders") == [(1,)]


def test_an_unfilled_order_expires_and_the_pick_can_rest_again(tmp_path: Path) -> None:
    settings = lane_settings(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    events = [odds_event()]
    engine_for(settings, ledger, events).run_cycle(wide_book())
    created = datetime.fromisoformat(rows(ledger, "SELECT created_at FROM paper_orders")[0][0])

    # While it rests, the same pick is not stacked on top of it.
    again = engine_for(settings, ledger, events, clock=created + timedelta(hours=1)).run_cycle(
        wide_book()
    )
    assert again.lane is not None and again.lane.blocked == {"lane_resting": 1}

    after_ttl = created + timedelta(minutes=settings.underdog_lane_ttl_minutes + 1)
    result = engine_for(settings, ledger, events, clock=after_ttl).run_cycle(wide_book())
    assert rows(ledger, "SELECT status FROM paper_orders ORDER BY id") == [
        ("expired",),
        ("open",),
    ]
    assert result.lane is not None and len(result.lane.orders_placed) == 1
    assert rows(ledger, "SELECT COUNT(*) FROM fills") == [(0,)] and ledger.cash == 1000
    # The instant-fill book keeps the game's first pick only.
    assert rows(
        ledger, f"SELECT COUNT(*) FROM shadow_decisions WHERE reason='{UNDERDOG_LANE_REASON}'"
    ) == [(1,)]


def test_a_pick_with_no_room_inside_the_spread_buys_at_the_ask(tmp_path: Path) -> None:
    settings = lane_settings(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    result = engine_for(settings, ledger, [odds_event()]).run_cycle(tight_book())

    assert result.lane is not None and len(result.lane.filled) == 1
    assert result.acted == result.lane.filled and result.orders_placed == []
    assert rows(ledger, "SELECT COUNT(*) FROM paper_orders") == [(0,)]
    # 50 contracts are on the ask; the taker fee is 0.07 x 50 x 0.19 x 0.81 -> 0.54.
    assert rows(ledger, "SELECT strategy,rules_version,price,count,fee,tags FROM fills") == [
        ("underdog_value", "v1", "0.19", "50", "0.54", "taker,underdog_lane")
    ]
    assert ledger.cash == Decimal("1000") - Decimal("9.50") - Decimal("0.54")


def test_a_resting_order_is_pulled_when_fresh_odds_no_longer_make_it_a_pick(
    tmp_path: Path,
) -> None:
    settings = lane_settings(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    engine_for(settings, ledger, [odds_event()]).run_cycle(wide_book())
    created = datetime.fromisoformat(rows(ledger, "SELECT created_at FROM paper_orders")[0][0])
    soon = created + timedelta(minutes=20)

    # No odds pull for the game this cycle: nothing is known, the order stays.
    quiet = engine_for(settings, ledger, [], clock=soon).run_cycle(wide_book())
    assert quiet.lane is not None and quiet.lane.cancelled == 0
    assert rows(ledger, "SELECT status FROM paper_orders") == [("open",)]

    # A fresh pull prices the Raiders at 17.5%: no edge at 17c any more.
    faded = [odds_event(lv=5.6, sf=1.20)]
    result = engine_for(settings, ledger, faded, clock=soon).run_cycle(wide_book())
    assert result.lane is not None and result.lane.cancelled == 1
    assert rows(ledger, "SELECT status,cancel_reason FROM paper_orders") == [
        ("cancelled", "edge_gone")
    ]
    assert ledger.available_cash(include_v3=True) == Decimal("1000")


def test_v3_fair_updates_never_cancel_a_lane_order(tmp_path: Path) -> None:
    """v3 cancels its own resting orders when its sharp fair drops. The lane's fair is the
    all-book consensus, so a v3 update for the same contract must leave it alone."""
    settings = lane_settings(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    engine_for(settings, ledger, [odds_event()]).run_cycle(wide_book())
    now = datetime.now(timezone.utc)
    update = MakerShadowUpdate(
        at=now,
        ticker=LV,
        side=OutcomeSide.yes,
        ask=Decimal("0.19"),
        last_price=None,
        fair=Decimal("0.10"),
    )
    assert ledger.update_paper_orders_for_fair([update], settings.v3_maker_cancel_move) == 0
    assert rows(ledger, "SELECT status FROM paper_orders") == [("open",)]


# --- guards ------------------------------------------------------------------------


def handmade_pick(
    *,
    hours: float | None = 240,
    fair: str = "0.24",
    price: str = "0.17",
    sport: str = "americanfootball_nfl",
    exact: bool = True,
) -> GamePick:
    now = datetime.now(timezone.utc)
    signal = Signal(
        strategy="underdog_value",
        ticker=LV,
        side=OutcomeSide.yes,
        fair_prob=Decimal(fair),
        limit_price=Decimal(price),
        edge=Decimal(fair) - Decimal(price) - Decimal("0.01"),
        size=Decimal(100),
        taker=False,
        created_at=now,
        fee_type="quadratic_with_maker_fees",
        fee_multiplier=Decimal(1),
    )
    return GamePick(
        signal=signal,
        event_ticker=EVENT_TICKER,
        odds_event_id="nfl-lvsf",
        sport=sport,
        outcome="Las Vegas Raiders",
        commence_time=None if hours is None else now + timedelta(hours=hours),
        teams_exact=exact,
    )


def lane_run(
    settings: Settings,
    ledger: Ledger,
    pick: GamePick,
    links: dict[str, str] | None = None,
) -> dict[str, int]:
    engine = Engine(settings, Candles(), [], ledger)  # type: ignore[arg-type]
    result = run_underdog_lane(
        settings,
        ledger,
        [pick],
        evaluated_events={pick.event_ticker},
        event_links=links or {},
        now=datetime.now(timezone.utc),
        fee_amount=engine._fee_amount,
    )
    assert not result.halted
    return result.blocked


def test_guards_stop_a_pick_before_any_order(tmp_path: Path) -> None:
    settings = lane_settings(tmp_path, v2_disabled_sports=["basketball_nba"])
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    cases = {
        "lane_league_disabled": (handmade_pick(sport="basketball_nba"), None),
        "lane_no_kickoff": (handmade_pick(hours=None), None),
        "lane_entry_window": (handmade_pick(hours=71), None),
        "lane_mapping": (handmade_pick(exact=False), None),
        "lane_big_edge": (handmade_pick(fair="0.28"), None),  # 11c above a 17c price
    }
    for reason, (pick, links) in cases.items():
        assert lane_run(settings, ledger, pick, links) == {reason: 1}, reason
    # v3 matched this Kalshi event to another sportsbook game, or this sportsbook game
    # to another Kalshi event: either way the two rule sets disagree about the fixture.
    assert lane_run(settings, ledger, handmade_pick(), {"other-game": EVENT_TICKER}) == {
        "lane_mapping": 1
    }
    assert lane_run(settings, ledger, handmade_pick(), {"nfl-lvsf": "KXNFLGAME-26NOV22XXYY"}) == {
        "lane_mapping": 1
    }
    assert rows(ledger, "SELECT COUNT(*) FROM paper_orders") == [(0,)]
    assert rows(ledger, "SELECT COUNT(*) FROM shadow_decisions") == [(0,)]
    # With v3 agreeing on the fixture, the same pick goes through.
    assert lane_run(settings, ledger, handmade_pick(), {"nfl-lvsf": EVENT_TICKER}) == {}
    assert rows(ledger, "SELECT status FROM paper_orders") == [("open",)]


def test_the_lane_cannot_tie_up_more_than_its_share_of_the_bankroll(tmp_path: Path) -> None:
    settings = lane_settings(tmp_path, underdog_lane_max_open_fraction=Decimal("0.03"))
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    # One order costs 17.25; the lane may hold 3% of 1000 = 30.
    assert lane_run(settings, ledger, handmade_pick()) == {}
    other = handmade_pick()
    other = GamePick(
        signal=Signal(
            **{
                **other.signal.__dict__,
                "ticker": "KXNFLGAME-26NOV22NYJKC-NYJ",
            }
        ),
        event_ticker="KXNFLGAME-26NOV22NYJKC",
        odds_event_id="nfl-nyjkc",
        sport=other.sport,
        outcome="New York Jets",
        commence_time=other.commence_time,
        teams_exact=True,
    )
    assert lane_run(settings, ledger, other) == {"lane_budget": 1}
    assert ledger.underdog_lane_open_cost() == Decimal("17.25")


def test_only_the_best_pick_of_a_game_is_entered(tmp_path: Path) -> None:
    settings = lane_settings(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    weak, strong = handmade_pick(fair="0.20"), handmade_pick(fair="0.24")
    engine = Engine(settings, Candles(), [], ledger)  # type: ignore[arg-type]
    result = run_underdog_lane(
        settings,
        ledger,
        [weak, strong],
        evaluated_events={EVENT_TICKER},
        event_links={},
        now=datetime.now(timezone.utc),
        fee_amount=engine._fee_amount,
    )
    assert len(result.orders_placed) == 1 and result.blocked == {}
    assert rows(ledger, "SELECT fair FROM paper_orders") == [("0.24",)]


# --- off switch and v1 mode ---------------------------------------------------------------


def test_with_the_lane_off_the_strategy_stays_a_shadow(tmp_path: Path) -> None:
    settings = lane_settings(tmp_path, underdog_lane="off")
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    result = engine_for(settings, ledger, [odds_event()]).run_cycle(wide_book())
    assert result.lane is None
    for table in ("paper_orders", "fills", "signals", "blocks"):
        assert rows(ledger, f"SELECT COUNT(*) FROM {table}") == [(0,)], table
    assert rows(ledger, "SELECT strategy,rules_version,reason,price FROM shadow_decisions") == [
        ("underdog_value", "v1", "", "0.17")
    ]


def test_v1_mode_still_books_its_own_fills_and_the_lane_stays_out(tmp_path: Path) -> None:
    """With underdog_value as a primary strategy (PBOT_RULES_VERSION=v1) the old engine
    path runs unchanged; the lane only ever acts on the strategy's shadow run."""
    settings = lane_settings(tmp_path, rules_version="v1")
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    strat = UnderdogValueStrategy(settings, odds_client=FakeOdds([odds_event()]))  # type: ignore[arg-type]
    result = Engine(settings, Candles(), [strat], ledger).run_cycle(wide_book())  # type: ignore[arg-type]
    assert result.lane is None and len(result.acted) == 1
    assert rows(ledger, "SELECT strategy,rules_version,price,tags FROM fills") == [
        ("underdog_value", None, "0.17", None)
    ]
    assert rows(ledger, "SELECT COUNT(*) FROM paper_orders") == [(0,)]


# --- the lane's own daily stop -----------------------------------------------------------------


def lane_fill(ledger: Ledger, ticker: str, *, price: str = "0.20", count: int = 100) -> None:
    ledger.record_fill(
        "underdog_value",
        ticker,
        OutcomeSide.yes,
        Decimal(price),
        Decimal(count),
        Decimal("0.28"),
        Decimal("0.24"),
        rules_version="v1",
        position_key=f"ud:{ticker.rsplit('-', 1)[0]}:x",
        event_ticker=ticker.rsplit("-", 1)[0],
        tags=("maker", "underdog_lane"),
    )


def test_a_bad_underdog_day_pauses_the_lane_and_nothing_else(tmp_path: Path) -> None:
    settings = lane_settings(tmp_path, max_daily_loss=Decimal("50"))
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    now = datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    for code in ("AAA", "BBB", "CCC"):
        ticker = f"KXNFLGAME-26NOV01{code}ZZZ-{code}"
        lane_fill(ledger, ticker)
        ledger.settle(ticker, OutcomeSide.no)  # three 20c underdogs lose: -60.84

    assert ledger.worst_drawdown_since(day) == Decimal("-60.84")
    assert ledger.worst_drawdown_since(day, lane="only") == Decimal("-60.84")
    assert ledger.worst_drawdown_since(day, lane="exclude") == 0

    engine = engine_for(settings, ledger, [odds_event()])
    assert engine.halt_reason() is None  # the rest of the bot keeps trading
    assert lane_halt_reason(settings, ledger, now) is not None
    result = engine.run_cycle(wide_book())
    assert result.halted is None
    assert result.lane is not None and result.lane.halted and not result.lane.orders_placed
    assert rows(ledger, "SELECT COUNT(*) FROM paper_orders") == [(0,)]

    # And the other way round: a v3 loss trips the global stop, which halts everything.
    v3 = "KXNFLGAME-26NOV01DDDZZZ-DDD"
    ledger.record_fill(
        "sportsbook_v3",
        v3,
        OutcomeSide.yes,
        Decimal("0.60"),
        Decimal(100),
        Decimal("1.68"),
        Decimal("0.65"),
        rules_version="v3",
    )
    ledger.settle(v3, OutcomeSide.no)
    assert ledger.worst_drawdown_since(day, lane="exclude") == Decimal("-61.68")
    halted = engine_for(settings, ledger, [odds_event()]).run_cycle(wide_book())
    assert halted.halted is not None and halted.lane is None


def test_a_ticker_shared_with_v3_splits_its_settlement_between_the_two_stops(
    tmp_path: Path,
) -> None:
    settings = lane_settings(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    ticker = "KXNFLGAME-26NOV01AAAZZZ-AAA"
    lane_fill(ledger, ticker)  # costs 20.28
    ledger.record_fill(
        "sportsbook_v3",
        ticker,
        OutcomeSide.no,
        Decimal("0.80"),
        Decimal(10),
        Decimal("0.12"),
        Decimal("0.83"),
        rules_version="v3",
    )  # costs 8.12, wins 10
    ledger.settle(ticker, OutcomeSide.no)
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, tzinfo=None)
    assert ledger.worst_drawdown_since(day, lane="only") == Decimal("-20.28")
    assert ledger.worst_drawdown_since(day, lane="exclude") == 0  # +1.88, never below zero
    assert ledger.worst_drawdown_since(day) == Decimal("-18.40")


# --- the instant-fill book ---------------------------------------------------------------


def test_the_instant_book_logs_the_pick_as_filled_at_once_and_is_scored(tmp_path: Path) -> None:
    settings = lane_settings(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    engine_for(settings, ledger, [odds_event()]).run_cycle(wide_book())
    row = rows(
        ledger,
        "SELECT strategy,rules_version,window,ticker,side,position_key,would_trade,reason,"
        "price,size,net_edge,tags,commence_time FROM shadow_decisions WHERE reason='underdog_lane'",
    )
    assert len(row) == 1
    assert row[0][:10] == (
        "underdog_value",
        "v1",
        "72h+",
        LV,
        "yes",
        KEY,
        1,
        "underdog_lane",
        "0.17",
        "100",
    )
    fair = Decimal(
        rows(ledger, "SELECT fair FROM shadow_decisions WHERE reason='underdog_lane'")[0][0]
    )
    # fair - price - fee per contract, with the whole order's fee (0.25 / 100).
    assert Decimal(row[0][10]) == fair - Decimal("0.17") - Decimal("0.0025")
    assert row[0][11] == "maker,underdog_lane"

    # After kickoff the closing-price scorer treats it like any other scored row.
    commence = datetime.fromisoformat(row[0][12])

    class Close:
        def get_candlesticks(self, *_args: object, **_kwargs: object) -> list[dict[str, Any]]:
            return [
                {
                    "end_period_ts": int((commence - timedelta(minutes=5)).timestamp()),
                    "yes_bid": {"close_dollars": "0.20"},
                    "yes_ask": {"close_dollars": "0.21"},
                }
            ]

    after = commence + timedelta(hours=1)
    assert record_clv(ledger, Close(), now=after) == 1  # type: ignore[arg-type]
    refresh_clv_statuses(ledger, settings)
    scored = rows(
        ledger,
        "SELECT clv_kalshi,clv_kalshi_bid,clv_status FROM shadow_decisions "
        "WHERE reason='underdog_lane'",
    )
    assert (Decimal(scored[0][0]), Decimal(scored[0][1]), scored[0][2]) == (
        Decimal("3.5"),  # closing mid 20.5c against a 17c entry
        Decimal("3"),
        "ok",
    )


def test_dashboard_shows_the_two_books_side_by_side(tmp_path: Path) -> None:
    """Two games. In one the resting order filled and the underdog won; in the other the
    order never filled and the underdog lost. The instant book takes the loss as well."""
    settings = lane_settings(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    now = datetime.now(timezone.utc)
    engine = Engine(settings, Candles(), [], ledger)  # type: ignore[arg-type]

    def enter(event: str, code: str, team: str) -> None:
        base = handmade_pick()
        pick = GamePick(
            signal=Signal(**{**base.signal.__dict__, "ticker": f"{event}-{code}"}),
            event_ticker=event,
            odds_event_id=event,
            sport=base.sport,
            outcome=team,
            commence_time=base.commence_time,
            teams_exact=True,
        )
        run_underdog_lane(
            settings,
            ledger,
            [pick],
            evaluated_events={event},
            event_links={},
            now=now,
            fee_amount=engine._fee_amount,
        )

    enter("KXNFLGAME-26NOV22LVSF", "LV", "Las Vegas Raiders")
    enter("KXNFLGAME-26NOV22NYJKC", "NYJ", "New York Jets")
    first, second = ledger.open_underdog_lane_orders()
    assert ledger.fill_paper_order(first.id, now + timedelta(minutes=5))
    assert ledger.expire_paper_order(second.id, now + timedelta(hours=8))
    ledger.settle("KXNFLGAME-26NOV22LVSF-LV", OutcomeSide.yes)
    ledger.settle("KXNFLGAME-26NOV22NYJKC-NYJ", OutcomeSide.no)

    data = _measurement_data(ledger, settings)["quality"]["underdog_lane"]  # type: ignore[index]
    assert data["mode"] == "paper" and data["halted"] is None
    assert data["orders"] == {"expired": 1, "filled": 1} and data["bought_at_ask"] == 0
    lane, instant = data["lane"], data["instant"]
    # Lane book: one fill, won 100 for a cost of 17.25.
    assert (lane["bets"], lane["settled"], lane["wins"], lane["open"]) == (1, 1, 1, 0)
    assert lane["pnl"] == 82.75 and lane["staked"] == 17.25
    # Instant book: both picks, one win and one loss at the same price and fee.
    assert (instant["bets"], instant["settled"], instant["wins"]) == (2, 2, 1)
    assert instant["pnl"] == 82.75 - 17.25 and instant["staked"] == 34.5

    page = render_dashboard(ledger, settings)
    assert 'id="underdog"' in page and "Same picks if every order had filled at once" in page


# --- beside v3 in one cycle --------------------------------------------------------------


def test_v3_and_the_lane_share_a_cycle_without_touching_each_other(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The scheduled run: v3 trades, underdog_value is one of its shadows. Minnesota is
    13c bid / 15c ask, under v3's 25c floor, so v3 logs a near miss and buys nothing; the
    lane rests a bid at 14c. v3 matched the same sportsbook game, so the fixtures agree."""
    from tests.test_sportsbook_v2 import (
        FakeOdds as V3Odds,
    )
    from tests.test_sportsbook_v2 import (
        FakeV3Kalshi,
        SportsbookV3Strategy,
        fixed_clock,
        v3_markets,
        v3_odds_event,
    )

    clock = fixed_clock("2026-10-22T10:00:00Z")  # 82h before the 2026-10-25T20:25Z kickoff

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return clock() if tz is not None else clock().replace(tzinfo=None)

    # v1 reads the wall clock for its lead-time gate and for signal timestamps.
    monkeypatch.setattr("prediction_bot.strategies.sportsbook_arb.datetime", Frozen)
    monkeypatch.setattr("prediction_bot.models.datetime", Frozen)

    event = "KXNFLGAME-26OCT25MIAMIN"
    settings = lane_settings(
        tmp_path,
        min_edge=Decimal("0.02"),
        max_gross_edge=Decimal("0.20"),
        odds_bookmakers=Settings(_env_file=None).odds_bookmakers,  # type: ignore[call-arg]
    )
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    odds = V3Odds([v3_odds_event(miami_probability=Decimal("0.80"))])
    markets = v3_markets()
    markets[1] = make_market(
        f"{event}-MIN",
        "0.13",
        "0.15",
        subtitle="Minnesota",
        open_interest=Decimal(200),
        yes_ask_size=Decimal(50),
        raw={"rules_primary": "If Minnesota wins the game, then the market resolves to Yes."},
    )
    v3 = SportsbookV3Strategy(
        settings, odds, kalshi_client=FakeV3Kalshi(), ledger=ledger, clock=clock
    )
    engine = Engine(
        settings,
        FakeV3Kalshi(),  # type: ignore[arg-type]
        [v3],
        ledger,
        shadow_strategies=[UnderdogValueStrategy(settings, odds_client=odds)],  # type: ignore[arg-type]
        clock=clock,
    )
    result = engine.run_cycle(markets)

    assert v3.seen_event_links() == {"event-v3": event}
    assert result.lane is not None and result.lane.blocked == {}
    assert rows(
        ledger, "SELECT strategy,rules_version,ticker,side,price,status,tags FROM paper_orders"
    ) == [("underdog_value", "v1", f"{event}-MIN", "yes", "0.14", "open", "maker,underdog_lane")]
    # v3 took no position and still logged the side it refused.
    assert rows(ledger, "SELECT COUNT(*) FROM fills") == [(0,)]
    assert (
        "sportsbook_v3",
        "v3",
        "price_floor",
    ) in rows(
        ledger,
        f"SELECT strategy,rules_version,reason FROM shadow_decisions WHERE ticker='{event}-MIN'",
    )
    # The lane's row sits beside it, with a window measured from the same clock.
    assert rows(
        ledger,
        "SELECT rules_version,window,price FROM shadow_decisions WHERE reason='underdog_lane'",
    ) == [("v1", "72h+", "0.14")]
    assert ledger.v2_total_open_cost(include_v3=True) == 0
