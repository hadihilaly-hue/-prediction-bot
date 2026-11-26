"""The sharp lane: v3's sharp-backed sides (the practice book's feed) as real paper
entries in two bands, plus the underdog lane's shadow (no-cash) mode."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from prediction_bot.clv_report import clv_report
from prediction_bot.config import Settings
from prediction_bot.dashboard import _measurement_data, render_dashboard
from prediction_bot.engine import Engine
from prediction_bot.models import Market, OutcomeSide, SharpLaneCandidate
from prediction_bot.paper.ledger import (
    SHARP_LANE_REASON,
    UNDERDOG_LANE_NOCASH_REASON,
    Ledger,
)
from prediction_bot.sharp_lane import (
    band_of,
    gap_bucket,
    lane_halt_reason,
    maker_price,
    run_sharp_lane,
)
from prediction_bot.strategies.book_scanner import BookScannerStrategy
from prediction_bot.strategies.sportsbook_v2 import SportsbookV2Strategy
from tests.test_sportsbook_v2 import v3_markets
from tests.test_underdog_lane import (
    LV,
    Candles,
    engine_for,
    lane_settings,
    odds_event,
    tight_book,
    wide_book,
)
from tests.test_v5_entry_rules import _strategy

NOW = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
EVENT = "KXNFLGAME-26DEC01AAABBB"
AAA, BBB = f"{EVENT}-AAA", f"{EVENT}-BBB"
MAKER_FEES = ("quadratic_with_maker_fees", Decimal(1))


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        paper_db_path=tmp_path / "paper.sqlite", rules_version="v3", sharp_lane="paper"
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def candidate(
    *,
    ticker: str = AAA,
    side: OutcomeSide = OutcomeSide.yes,
    fair: str,
    bid: str,
    ask: str,
    event: str = EVENT,
    gates: tuple[str, ...] = ("blocked_net_edge",),
    sport: str = "americanfootball_nfl",
    measure_only: bool = False,
    days: float = 5,
    at: datetime = NOW,
) -> SharpLaneCandidate:
    return SharpLaneCandidate(
        created_at=at,
        ticker=ticker,
        side=side,
        event_ticker=event,
        position_key=f"{event}:{ticker.rsplit('-', 1)[1].lower()}",
        sport=sport,
        fair=Decimal(fair),
        mid=(Decimal(bid) + Decimal(ask)) / 2,
        bid=Decimal(bid),
        ask=Decimal(ask),
        commence_time=NOW + timedelta(days=days),
        fee_type=MAKER_FEES[0],
        fee_multiplier=MAKER_FEES[1],
        gates=gates,
        near_miss_reason="net_edge",
        measure_only=measure_only,
    )


def run(
    settings: Settings,
    ledger: Ledger,
    candidates: list[SharpLaneCandidate],
    *,
    priced: set[str] | None = None,
    now: datetime = NOW,
):  # type: ignore[no-untyped-def]
    return run_sharp_lane(
        settings,
        ledger,
        candidates,
        priced_events={c.event_ticker for c in candidates} if priced is None else priced,
        now=now,
    )


def rows(ledger: Ledger, sql: str) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in ledger.conn.execute(sql).fetchall()]


# --- candidates: exactly the practice feed's sides -----------------------------------


def test_candidates_are_exactly_the_sides_the_practice_feed_considers(settings) -> None:  # type: ignore[no-untyped-def]
    settings.rules_version = "v3"
    settings.v3_devig_method = "power"
    strategy = _strategy(settings, miami=Decimal("0.562"))
    seen: dict[str, Any] = {}
    feed = strategy._feed_maker_shadows

    def spy(markets, candidates, entry_window_events, now):  # type: ignore[no-untyped-def]
        seen["candidates"] = candidates
        return feed(markets, candidates, entry_window_events, now)

    strategy._feed_maker_shadows = spy  # type: ignore[method-assign]
    strategy.evaluate(v3_markets())

    sides = [
        sig
        for sig, reason in strategy.seen_near_miss_decisions()
        if reason in SportsbookV2Strategy.MAKER_FEED_GATES
    ] + [sig for signals in seen["candidates"].values() for sig in signals]
    expected = {
        (sig.ticker, sig.side)
        for sig in sides
        if sig.mid is not None and sig.fair_prob > sig.mid and sig.commence_time is not None
    }
    lane = strategy.seen_sharp_lane_candidates()
    assert lane and {(c.ticker, c.side) for c in lane} == expected
    assert all(c.fair > c.mid for c in lane)
    assert strategy.seen_sharp_lane_events() == {"KXNFLGAME-26OCT25MIAMIN"}
    # Every practice shadow is one of the lane's candidates, with the same gate tags.
    by_side = {(c.ticker, c.side): c for c in lane}
    shadows = strategy.seen_maker_shadows()
    assert shadows
    for shadow in shadows:
        assert by_side[(shadow.ticker, shadow.side)].gates == shadow.tags


def test_the_practice_book_is_unchanged_by_the_lane(settings) -> None:  # type: ignore[no-untyped-def]
    settings.rules_version = "v3"
    settings.v3_devig_method = "power"
    strategy = _strategy(settings, miami=Decimal("0.562"))
    strategy.evaluate(v3_markets())
    [shadow] = [s for s in strategy.seen_maker_shadows() if s.ticker.endswith("-MIA")]
    assert shadow.price == Decimal("0.55") and shadow.maker_price_rule == "bid_plus_1"
    assert shadow.tags == ("blocked_net_edge",) and shadow.size == Decimal(1)


# --- pricing helpers -----------------------------------------------------------------


def test_bands_gap_buckets_and_the_practice_price_rule() -> None:
    assert band_of(Decimal("0.10")) == band_of(Decimal("0.25")) == "longshot"
    assert band_of(Decimal("0.26")) == band_of(Decimal("0.90")) == "main"
    assert band_of(Decimal("0.09")) is None and band_of(Decimal("0.91")) is None
    assert maker_price(Decimal("0.48"), Decimal("0.52")) == (Decimal("0.49"), "bid_plus_1")
    assert maker_price(Decimal("0.49"), Decimal("0.50")) == (Decimal("0.49"), "at_bid")
    assert maker_price(Decimal("0.50"), Decimal("0.50")) is None
    assert gap_bucket(Decimal("0.515"), Decimal("0.50")) == "1-2c"
    assert gap_bucket(Decimal("0.525"), Decimal("0.50")) == "2-3c"
    assert gap_bucket(Decimal("0.54"), Decimal("0.50")) == "3c+"
    assert gap_bucket(Decimal("0.505"), Decimal("0.50")) == "0-1c"


# --- the entry rule ------------------------------------------------------------------


def test_enough_edge_at_the_ask_buys_now_in_the_main_band(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    # Taker fee at 52c is 0.07 x 0.52 x 0.48 = 1.75c: 56 - 52 - 1.75 = 2.25c >= 1c.
    result = run(settings, ledger, [candidate(fair="0.56", bid="0.48", ask="0.52")])
    assert len(result.filled) == 1 and result.orders_placed == []
    [(strategy, version, price, count, fee, key, tags)] = rows(
        ledger, "SELECT strategy,rules_version,price,count,fee,position_key,tags FROM fills"
    )
    assert (strategy, version, price, count, fee) == (
        "sharp_lane",
        "sharp_lane",
        "0.52",
        "19",
        "0.34",
    )
    assert key == f"sl:{EVENT}:aaa"
    assert set(tags.split(",")) == {
        "sharp_lane",
        "taker",
        "band_main",
        "rule_ask",
        "gap_3c+",
        "blocked_net_edge",
    }
    assert ledger.cash == Decimal("1000") - Decimal("0.52") * 19 - Decimal("0.34")
    # v3's caps and held checks never see the lane.
    assert ledger.held_keys(EVENT, include_v3=True) == set()
    assert ledger.v2_total_open_cost(include_v3=True) == 0


def test_a_longshot_with_edge_at_the_ask_is_tagged_longshot(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    run(settings, ledger, [candidate(fair="0.25", bid="0.15", ask="0.18", gates=("taker_pass",))])
    [(price, count, tags)] = rows(ledger, "SELECT price,count,tags FROM fills")
    assert (price, count) == ("0.18", "55")  # floor(10 / 0.18)
    assert {"band_longshot", "taker", "taker_pass"} <= set(tags.split(","))


def test_short_of_the_take_edge_it_rests_at_bid_plus_one(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    # At the ask: 53 - 52 - 1.75 < 1c. Resting at 49c: 53 - 49 - 0.44 = 3.56c.
    result = run(settings, ledger, [candidate(fair="0.53", bid="0.48", ask="0.52")])
    assert len(result.orders_placed) == 1 and result.filled == []
    [(strategy, version, price, size, status, expires, tags)] = rows(
        ledger,
        "SELECT strategy,rules_version,price,size,status,expires_at,tags FROM paper_orders",
    )
    assert (strategy, version, price, size, status) == (
        "sharp_lane",
        "sharp_lane",
        "0.49",
        "20",
        "open",
    )
    assert datetime.fromisoformat(expires) == NOW + timedelta(minutes=480)
    assert {"maker", "band_main", "rule_bid_plus_1", "gap_3c+"} <= set(tags.split(","))
    assert rows(ledger, "SELECT COUNT(*) FROM fills") == [(0,)]
    assert ledger.cash == 1000 and ledger.available_cash(include_v3=True) < 1000
    # v3's own open cost leaves the lane out.
    assert ledger.v2_total_open_cost(include_v3=True) == 0


def test_a_one_cent_spread_rests_at_the_bid(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    run(settings, ledger, [candidate(fair="0.52", bid="0.49", ask="0.50")])
    assert rows(ledger, "SELECT price,tags LIKE '%rule_at_bid%' FROM paper_orders") == [("0.49", 1)]


def test_the_ttl_never_runs_past_the_kickoff_buffer(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    run(settings, ledger, [candidate(fair="0.53", bid="0.48", ask="0.52", days=0.1)])
    [(expires,)] = rows(ledger, "SELECT expires_at FROM paper_orders")
    buffer = settings.kickoff_buffer_minutes.get(
        "americanfootball_nfl", settings.kickoff_buffer_default_minutes
    )
    assert datetime.fromisoformat(expires) == NOW + timedelta(days=0.1) - timedelta(minutes=buffer)


def test_edge_band_and_eligibility_blocks(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, sharp_lane_bands=["main"])
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    result = run(
        settings,
        ledger,
        [
            # 50.2 - 49 - 0.44 < 1c at the resting price, worse at the ask.
            candidate(fair="0.502", bid="0.48", ask="0.52"),
            # A longshot while the longshot band is switched off.
            candidate(fair="0.30", bid="0.15", ask="0.18", event="KXNFLGAME-26DEC02CCCDDD"),
            # 95c: outside both bands.
            candidate(fair="0.99", bid="0.94", ask="0.96", event="KXNFLGAME-26DEC03EEEFFF"),
            candidate(fair="0.60", bid="0.48", ask="0.52", event="KXNHLGAME-X", measure_only=True),
            candidate(
                fair="0.60",
                bid="0.48",
                ask="0.52",
                event="KXNBAGAME-Y",
                sport="basketball_nba",
            ),
        ],
    )
    assert result.blocked == {
        "sharp_lane_edge": 1,
        "sharp_lane_band": 2,
        "sharp_lane_measure_only": 1,
        "sharp_lane_league_disabled": 1,
    }
    assert rows(ledger, "SELECT COUNT(*) FROM fills") == [(0,)]
    assert rows(ledger, "SELECT COUNT(*) FROM paper_orders") == [(0,)]
    details = dict(rows(ledger, "SELECT reason,detail FROM blocks WHERE reason='sharp_lane_edge'"))
    assert details["sharp_lane_edge"] == "band=main maker rule=bid_plus_1 gates=blocked_net_edge"
    assert {r[0] for r in rows(ledger, "SELECT DISTINCT strategy FROM blocks")} == {"sharp_lane"}


# --- one per game ----------------------------------------------------------------------


def test_one_entry_per_game_the_highest_edge(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    result = run(
        settings,
        ledger,
        [
            candidate(fair="0.53", bid="0.48", ask="0.52"),  # rests: ~3.6c
            candidate(ticker=BBB, fair="0.30", bid="0.15", ask="0.18"),  # ask: ~10.97c
        ],
    )
    assert len(result.filled) == 1 and result.orders_placed == []
    assert result.blocked == {"sharp_lane_game": 1}
    assert rows(ledger, "SELECT ticker FROM fills") == [(BBB,)]
    # No re-entry after a fill, on either side of the game.
    again = run(settings, ledger, [candidate(fair="0.60", bid="0.48", ask="0.52")])
    assert again.blocked == {"sharp_lane_held": 1}


def test_one_resting_order_per_game(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    run(settings, ledger, [candidate(fair="0.53", bid="0.48", ask="0.52")])
    again = run(settings, ledger, [candidate(fair="0.53", bid="0.48", ask="0.52")])
    assert again.blocked == {"sharp_lane_resting": 1}
    assert rows(ledger, "SELECT COUNT(*) FROM paper_orders") == [(1,)]


def test_a_game_v3_holds_is_skipped(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    ledger.record_fill(
        "sportsbook_v3",
        BBB,
        OutcomeSide.yes,
        Decimal("0.40"),
        Decimal(5),
        Decimal("0.10"),
        Decimal("0.45"),
        rules_version="v3",
        position_key=f"{EVENT}:bbb",
        event_ticker=EVENT,
    )
    result = run(settings, ledger, [candidate(fair="0.56", bid="0.48", ask="0.52")])
    assert result.blocked == {"sharp_lane_v3_held": 1}
    assert rows(ledger, "SELECT COUNT(*) FROM fills WHERE strategy='sharp_lane'") == [(0,)]


# --- stake and limits ------------------------------------------------------------------


def test_budget_and_cash_blocks(tmp_path: Path) -> None:
    tight = settings_for(tmp_path, sharp_lane_max_open_fraction=Decimal("0.005"))
    ledger = Ledger(tight.paper_db_path, Decimal("1000"))
    assert run(tight, ledger, [candidate(fair="0.56", bid="0.48", ask="0.52")]).blocked == {
        "sharp_lane_budget": 1
    }
    poor = Ledger(tmp_path / "poor.sqlite", Decimal("5"))
    big = settings_for(tmp_path, sharp_lane_max_open_fraction=Decimal("10"))
    assert run(big, poor, [candidate(fair="0.56", bid="0.48", ask="0.52")]).blocked == {
        "sharp_lane_cash": 1
    }
    assert poor.cash == 5


def test_stake_is_flat_and_capped_by_max_position_contracts(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, max_position_contracts=Decimal("7"))
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    run(settings, ledger, [candidate(fair="0.56", bid="0.48", ask="0.52")])
    assert rows(ledger, "SELECT count FROM fills") == [("7",)]


def test_the_lanes_own_daily_stop_and_the_global_stop_ignores_it(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, sharp_lane_max_daily_loss=Decimal("5"))
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    run(settings, ledger, [candidate(fair="0.56", bid="0.48", ask="0.52")])
    ledger.settle(AAA, OutcomeSide.no, at=NOW)  # lose 19 x 52c + fee
    assert lane_halt_reason(settings, ledger, NOW) is not None
    day = NOW.replace(hour=0, tzinfo=None)
    assert ledger.worst_drawdown_since(day, lane="exclude") == 0
    assert ledger.worst_drawdown_since(day, lane="sharp") < -5
    other = "KXNFLGAME-26DEC09GGGHHH"
    result = run(
        settings,
        ledger,
        [candidate(ticker=f"{other}-GGG", event=other, fair="0.56", bid="0.48", ask="0.52")],
    )
    assert result.halted is not None and result.blocked == {"sharp_lane_daily_stop": 1}
    assert rows(ledger, "SELECT COUNT(*) FROM fills") == [(1,)]


# --- edge gone and fills ---------------------------------------------------------------


def test_a_resting_order_is_pulled_when_the_game_is_priced_again_without_edge(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    run(settings, ledger, [candidate(fair="0.53", bid="0.48", ask="0.52")])
    later = NOW + timedelta(minutes=30)
    # The game is not priced this cycle: the order keeps resting.
    quiet = run(settings, ledger, [], priced=set(), now=later)
    assert quiet.cancelled == 0
    # Priced again, still worth 1c+ at 49c: kept.
    kept = run(
        settings, ledger, [candidate(fair="0.51", bid="0.48", ask="0.52", at=later)], now=later
    )
    assert kept.cancelled == 0 and kept.blocked == {"sharp_lane_resting": 1}
    # Priced again, under 1c at 49c (and gone from the feed): pulled.
    gone = run(settings, ledger, [], priced={EVENT}, now=later)
    assert gone.cancelled == 1
    assert rows(ledger, "SELECT status,cancel_reason FROM paper_orders") == [
        ("cancelled", "edge_gone")
    ]


def test_a_resting_order_fills_on_a_trade_through_and_moves_cash(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    run(settings, ledger, [candidate(fair="0.53", bid="0.48", ask="0.52")])
    [(order_id, fee)] = rows(ledger, "SELECT id,maker_fee FROM paper_orders")
    candles = Candles(
        [
            {
                "end_period_ts": int((NOW + timedelta(minutes=5)).timestamp()),
                "price": {"low_dollars": "0.48"},
            }
        ]
    )
    engine = Engine(settings, candles, [], ledger, clock=lambda: NOW + timedelta(minutes=30))  # type: ignore[arg-type]
    assert engine._resolve_paper_orders(NOW + timedelta(minutes=30)) == 1
    [(strategy, price, count, tags)] = rows(ledger, "SELECT strategy,price,count,tags FROM fills")
    assert (strategy, price, count) == ("sharp_lane", "0.49", "20")
    assert {"maker", "sharp_lane", "band_main"} <= set(tags.split(","))
    assert ledger.cash == Decimal("1000") - Decimal("0.49") * 20 - Decimal(fee)
    assert order_id and ledger.sharp_lane_event_state(EVENT) == "held"


def test_the_instant_fill_twin_logs_the_first_pick_of_each_game_without_cash(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    run(settings, ledger, [candidate(fair="0.53", bid="0.48", ask="0.52")])
    cash = ledger.cash
    ledger.cancel_paper_order(1, NOW, "edge_gone")
    run(settings, ledger, [candidate(fair="0.53", bid="0.48", ask="0.52")])
    twins = rows(
        ledger,
        "SELECT strategy,rules_version,price,size,would_trade FROM shadow_decisions "
        f"WHERE reason='{SHARP_LANE_REASON}'",
    )
    assert twins == [("sharp_lane", "sharp_lane", "0.49", "20", 1)]
    assert ledger.cash == cash


# --- the switch ------------------------------------------------------------------------


class _Feed:
    """A stand-in strategy that hands the engine sharp-lane candidates."""

    name = "sportsbook_v3"
    rules_version = "v3"

    def __init__(self, candidates: list[SharpLaneCandidate]) -> None:
        self.candidates = candidates

    def evaluate(self, markets: list[Market]) -> list[Any]:
        return []

    def seen_sharp_lane_candidates(self) -> list[SharpLaneCandidate]:
        return self.candidates

    def seen_sharp_lane_events(self) -> set[str]:
        return {c.event_ticker for c in self.candidates}


def test_the_engine_runs_the_lane_in_paper_and_never_in_live_or_off(tmp_path: Path) -> None:
    feed = _Feed([candidate(fair="0.56", bid="0.48", ask="0.52")])
    for mode, lane, ran in (
        ("paper", "paper", True),
        ("paper", "off", False),
        ("live", "paper", False),
    ):
        settings = settings_for(tmp_path / f"{mode}-{lane}", mode=mode, sharp_lane=lane)
        settings.paper_db_path.parent.mkdir(parents=True, exist_ok=True)
        ledger = Ledger(settings.paper_db_path, Decimal("1000"))
        engine = Engine(settings, Candles(), [feed], ledger, clock=lambda: NOW)  # type: ignore[arg-type,list-item]
        result = engine.run_cycle([])
        assert (result.sharp_lane is not None) is ran
        assert rows(ledger, "SELECT COUNT(*) FROM fills") == [(1 if ran else 0,)]


def test_a_book_scanner_run_skips_the_lane(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    run(settings, ledger, [candidate(fair="0.53", bid="0.48", ask="0.52")])  # one resting order
    scanner = BookScannerStrategy(settings)
    engine = Engine(settings, Candles(), [scanner], ledger, clock=lambda: NOW)  # type: ignore[arg-type]
    result = engine.run_cycle([])
    assert result.sharp_lane is None
    assert rows(ledger, "SELECT status FROM paper_orders") == [("open",)]


def test_settings_defaults() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.sharp_lane == "off" and s.sharp_lane_bands == ["longshot", "main"]
    assert (s.sharp_lane_take_edge, s.sharp_lane_min_edge) == (Decimal("0.01"), Decimal("0.01"))
    assert (s.sharp_lane_ttl_minutes, s.sharp_lane_order_notional) == (480, Decimal("10"))
    assert s.sharp_lane_max_open_fraction == Decimal("0.30")
    assert s.sharp_lane_max_daily_loss == Decimal("50")
    assert Settings(_env_file=None, sharp_lane_bands="main").sharp_lane_bands == ["main"]  # type: ignore[call-arg]


# --- the underdog lane in shadow mode -------------------------------------------------


def test_underdog_shadow_mode_rests_and_fills_without_touching_cash(tmp_path: Path) -> None:
    settings = lane_settings(tmp_path, underdog_lane="shadow")
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    events = [odds_event()]
    result = engine_for(settings, ledger, events).run_cycle(wide_book())
    assert result.lane is not None and len(result.lane.orders_placed) == 1
    assert result.orders_placed == [] and result.acted == []
    [(tags,)] = rows(ledger, "SELECT tags FROM paper_orders")
    assert tags == "maker,no_cash,underdog_lane"
    assert ledger.available_cash(include_v3=True) == 1000
    assert ledger.underdog_lane_open_cost() == 0
    created = datetime.fromisoformat(rows(ledger, "SELECT created_at FROM paper_orders")[0][0])
    through = Candles(
        [
            {
                "end_period_ts": int((created + timedelta(minutes=12)).timestamp()),
                "price": {"low_dollars": "0.16"},
            }
        ]
    )
    later = created + timedelta(minutes=30)
    again = engine_for(settings, ledger, events, client=through, clock=later).run_cycle(wide_book())
    assert rows(ledger, "SELECT status FROM paper_orders") == [("filled",)]
    assert rows(ledger, "SELECT COUNT(*) FROM fills") == [(0,)]
    assert ledger.positions() == [] and ledger.cash == 1000
    assert rows(
        ledger,
        "SELECT strategy,ticker,price,size,would_trade,tags FROM shadow_decisions "
        f"WHERE reason='{UNDERDOG_LANE_NOCASH_REASON}'",
    ) == [("underdog_value", LV, "0.17", "100", 1, "maker,no_cash,underdog_lane")]
    # One side per game still holds after a no-cash fill.
    assert again.lane is not None and again.lane.blocked == {"lane_held": 1}


def test_underdog_shadow_mode_books_ask_picks_without_cash(tmp_path: Path) -> None:
    settings = lane_settings(tmp_path, underdog_lane="shadow", underdog_lane_max_open_fraction=0)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    result = engine_for(settings, ledger, [odds_event()]).run_cycle(tight_book())
    # No budget in shadow mode, so a zero budget blocks nothing.
    assert result.lane is not None and len(result.lane.filled) == 1 and result.lane.blocked == {}
    assert rows(ledger, "SELECT COUNT(*) FROM fills") == [(0,)] and ledger.cash == 1000
    assert rows(
        ledger,
        f"SELECT price,tags FROM shadow_decisions WHERE reason='{UNDERDOG_LANE_NOCASH_REASON}'",
    ) == [("0.19", "no_cash,taker,underdog_lane")]
    # The instant-fill book keeps recording.
    assert rows(ledger, "SELECT COUNT(*) FROM shadow_decisions WHERE reason='underdog_lane'") == [
        (1,)
    ]


def test_underdog_fills_from_before_the_switch_stay_and_settle(tmp_path: Path) -> None:
    paper = lane_settings(tmp_path)
    ledger = Ledger(paper.paper_db_path, Decimal("1000"))
    engine_for(paper, ledger, [odds_event()]).run_cycle(tight_book())
    assert rows(ledger, "SELECT COUNT(*) FROM fills") == [(1,)]
    shadow = lane_settings(tmp_path, underdog_lane="shadow")
    engine_for(shadow, ledger, [odds_event()]).run_cycle(tight_book())
    assert rows(ledger, "SELECT COUNT(*) FROM fills") == [(1,)]
    pnl = ledger.settle(LV, OutcomeSide.yes, at=NOW)
    assert pnl > 0


# --- evidence ------------------------------------------------------------------------


def _scored(ledger: Ledger, table: str, clv: str = "1.5") -> None:
    ledger.conn.execute(f"UPDATE {table} SET clv_kalshi=?, clv_status='ok'", (clv,))
    ledger.conn.commit()


def test_new_and_old_cohorts_accumulate_across_the_switch(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    run(settings, ledger, [candidate(fair="0.56", bid="0.48", ask="0.52")])  # at the ask
    other = "KXNFLGAME-26DEC09GGGHHH"
    run(
        settings,
        ledger,
        [candidate(ticker=f"{other}-GGG", event=other, fair="0.53", bid="0.48", ask="0.52")],
    )
    ledger.fill_paper_order(1, NOW + timedelta(minutes=5))
    # An underdog lane fill from before the switch and a no-cash one after it.
    paper = lane_settings(tmp_path / "ud")
    (tmp_path / "ud").mkdir()
    engine_for(paper, ledger, [odds_event()]).run_cycle(tight_book())
    ledger.conn.execute(
        "INSERT INTO shadow_decisions (created_at,rules_version,window,strategy,ticker,side,"
        "position_key,would_trade,reason,fair,price,size,net_edge,tags) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            NOW.isoformat(),
            "v1",
            "72h+",
            "underdog_value",
            "KXNFLGAME-26DEC10IIIJJJ-III",
            "yes",
            "ud:KXNFLGAME-26DEC10IIIJJJ:iii",
            1,
            UNDERDOG_LANE_NOCASH_REASON,
            "0.25",
            "0.19",
            "50",
            "0.05",
            "no_cash,taker,underdog_lane",
        ),
    )
    _scored(ledger, "fills")
    _scored(ledger, "shadow_decisions")
    cohorts = clv_report(ledger.conn)["cohorts"]
    assert cohorts["sharp_lane"]["rows"] == 1 and cohorts["sharp_lane"]["fee_style"] == "maker"
    assert cohorts["sharp_lane_ask"]["rows"] == 1
    assert cohorts["sharp_lane_instant"]["rows"] == 2
    assert cohorts["underdog_lane_ask"]["rows"] == 2  # the paper fill and the no-cash one
    metrics = cohorts["sharp_lane_instant"]["metrics"]["clv_kalshi"]
    assert set(metrics["by_band"]) == {"main"}
    assert set(metrics["by_price_rule"]) == {"ask", "bid_plus_1"}
    assert set(metrics["by_sharp_gap"]) == {"3c+"}
    assert set(metrics["by_window"]) and set(metrics["by_sport"]) == {"KXNFLGAME"}
    # The instant twin nets each row of its own fee: maker ~0.44c at 49c, taker ~1.75c at
    # 52c.
    by_rule = metrics["by_price_rule"]
    assert by_rule["ask"]["fee"] > by_rule["bid_plus_1"]["fee"]
    assert metrics["all"]["verdict"] == "open"  # fewer than 30 games


def test_dashboard_shows_the_sharp_lane_and_relabels_the_underdog_shadow(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path, underdog_lane="shadow")
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    run(settings, ledger, [candidate(fair="0.56", bid="0.48", ask="0.52")])
    run(
        settings,
        ledger,
        [
            candidate(
                ticker="KXNFLGAME-26DEC09GGGHHH-GGG",
                event="KXNFLGAME-26DEC09GGGHHH",
                fair="0.53",
                bid="0.48",
                ask="0.52",
            )
        ],
    )
    data = _measurement_data(ledger, settings)
    sharp = data["quality"]["sharp_lane"]
    assert sharp["mode"] == "paper" and sharp["placed"] == 1
    assert sharp["orders"] == {"open": 1} and sharp["fill_rate"] is None
    assert sharp["bands"]["main"]["bets"] == 1 and sharp["bands"]["main"]["open"] == 1
    assert sharp["bands"]["longshot"]["bets"] == 0
    assert sharp["instant"]["bets"] == 2 and sharp["bought_at_ask"] == 1
    assert data["quality"]["underdog_lane"]["mode"] == "shadow"
    page = render_dashboard(ledger, settings)
    assert "Sharp lane (sharp-backed bets at every price)" in page
    assert "comparison only, no cash" in page
    assert json.dumps("sharp_lane_v3_held")[1:-1] in page
