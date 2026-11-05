from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from prediction_bot.config import Settings
from prediction_bot.feed_selection import entry_lookahead, select_measure_pull
from prediction_bot.models import OutcomeSide, Signal
from prediction_bot.odds import OddsPacer
from prediction_bot.odds_planner import PlannedGame
from prediction_bot.paper.ledger import Ledger
from tests.test_sportsbook_v2 import (
    FakeOdds,
    FakeV3Kalshi,
    SportsbookV3Strategy,
    fixed_clock,
    make_market,
    v3_markets,
    v3_odds_event,
)

EVENT = "KXNFLGAME-26OCT25MIAMIN"
MIA, MIN = f"{EVENT}-MIA", f"{EVENT}-MIN"
INSIDE_WINDOW = "2026-10-22T10:00:00Z"  # 82h before the 2026-10-25T20:25Z kickoff


def _strategy(settings, miami=Decimal("0.60"), clock=INSIDE_WINDOW, **event_kw):  # type: ignore[no-untyped-def]
    settings.max_gross_edge = Decimal("0.20")
    return SportsbookV3Strategy(
        settings,
        FakeOdds([v3_odds_event(miami_probability=miami, **event_kw)]),
        kalshi_client=FakeV3Kalshi(),
        ledger=Ledger(":memory:", Decimal("1000")),
        clock=fixed_clock(clock),
    )


def _near_misses(strategy) -> dict[tuple[str, str], tuple[Signal, str]]:  # type: ignore[no-untyped-def]
    return {
        (sig.ticker, sig.side.value): (sig, reason)
        for sig, reason in strategy.seen_near_miss_decisions()
    }


def test_settings_defaults_and_entry_window_sync() -> None:
    s = Settings(_env_file=None)
    assert s.v2_price_floor == Decimal("0.25")
    assert s.v3_low_price_threshold_multiplier == Decimal("2.0")
    assert (s.entry_window_min_hours, s.entry_window_max_hours, s.min_lead_hours) == (72, 168, 72)
    assert s.measure_feeds == ["icehockey_nhl", "basketball_nba", "basketball_ncaab"]
    assert Settings(_env_file=None, entry_window_min_hours=24).min_lead_hours == 24
    assert Settings(_env_file=None, min_lead_hours=48).entry_window_min_hours == 48
    assert entry_lookahead(s) == timedelta(hours=168)
    assert entry_lookahead(Settings(_env_file=None, entry_window_max_hours=0)) == timedelta(days=7)


def test_low_price_threshold_doubles_and_is_logged(settings) -> None:  # type: ignore[no-untyped-def]
    # MIN at a 36c ask: net edge ~3.2c clears 2c but not the 4c low-price bar.
    strategy = _strategy(settings, miami=Decimal("0.585"))
    assert strategy.evaluate(v3_markets()) == []
    block = next(b for b in strategy.seen_blocks() if b.reason == "net_edge" and b.ticker == MIN)
    assert "threshold=0.040" in block.detail and block.detail.startswith("net_edge=")
    sig, reason = _near_misses(strategy)[(MIN, "yes")]
    assert reason == "net_edge"
    assert sig.tags == ("low_price_threshold",)
    assert (sig.available_size, sig.open_interest) == (Decimal(50), Decimal(200))

    settings.v3_low_price_threshold_multiplier = Decimal("1")
    plain = _strategy(settings, miami=Decimal("0.585"))
    assert (MIN, OutcomeSide.yes) in {(s.ticker, s.side) for s in plain.evaluate(v3_markets())}


def test_price_floor_rows_are_kept_as_near_misses(settings) -> None:  # type: ignore[no-untyped-def]
    markets = v3_markets()
    markets[1] = make_market(
        MIN, "0.13", "0.15", subtitle="Minnesota", open_interest=Decimal(200),
        yes_ask_size=Decimal(50),
        raw={"rules_primary": "If Minnesota wins the game, then the market resolves to Yes."},
    )  # fmt: skip
    strategy = _strategy(settings, miami=Decimal("0.80"))
    signals = strategy.evaluate(markets)
    assert all(s.ticker != MIN or s.side is OutcomeSide.no for s in signals)
    assert any(b.reason == "price_floor" and b.ticker == MIN for b in strategy.seen_blocks())
    sig, reason = _near_misses(strategy)[(MIN, "yes")]
    assert reason == "price_floor"
    assert sig.limit_price == Decimal("0.15") and sig.edge > 0  # the edge is still recorded


def test_thin_market_rows_are_kept_as_near_misses_with_depth(settings) -> None:  # type: ignore[no-untyped-def]
    strategy = _strategy(settings)
    strategy.evaluate(v3_markets(yes_ask_size="10"))
    assert any(b.reason == "thin_market" and b.ticker == MIA for b in strategy.seen_blocks())
    sig, reason = _near_misses(strategy)[(MIA, "yes")]
    assert reason == "thin_market"
    assert (sig.available_size, sig.open_interest) == (Decimal(10), Decimal(200))
    assert sig.edge > Decimal("0.02")  # would have traded on edge alone


def test_entry_window_max_blocks_games_too_far_out(settings) -> None:  # type: ignore[no-untyped-def]
    strategy = _strategy(settings, clock="2026-10-17T10:00:00Z")  # 202h out
    assert strategy.evaluate(v3_markets()) == []
    detail = next(b.detail for b in strategy.seen_blocks() if b.reason == "entry_window")
    assert detail.endswith("beyond 168h")
    assert any(sig.ticker == MIA for sig in strategy.seen_shadow_decisions())

    settings.entry_window_max_hours = 0
    assert _strategy(settings, clock="2026-10-17T10:00:00Z").evaluate(v3_markets())


def test_measure_only_feed_never_trades_even_when_disabled(settings) -> None:  # type: ignore[no-untyped-def]
    settings.v2_disabled_sports = ["americanfootball_nfl"]
    strategy = _strategy(settings)
    strategy.measure_only_feeds = {"americanfootball_nfl"}
    assert strategy.evaluate(v3_markets()) == []
    reasons = {b.reason for b in strategy.seen_blocks()}
    assert "league_disabled" not in reasons
    assert any(
        b.reason == "entry_window" and "measure_only" in b.detail for b in strategy.seen_blocks()
    )
    sig, reason = _near_misses(strategy)[(MIN, "yes")]
    assert reason == "entry_window" and "measure_only" in (sig.tags or ())

    strategy.measure_only_feeds = set()
    strategy.evaluate(v3_markets())
    assert any(b.reason == "league_disabled" for b in strategy.seen_blocks())


def test_soft_reference_off_gives_no_fair(settings) -> None:  # type: ignore[no-untyped-def]
    settings.v3_allow_soft_reference = False
    strategy = _strategy(settings, soft_only=True)
    assert strategy.evaluate(v3_markets()) == []
    assert any(b.reason == "few_books" for b in strategy.seen_blocks())


def test_ledger_stores_near_miss_tags_depth_and_open_interest(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "p.sqlite", Decimal("100"))
    sig = Signal(
        strategy="sportsbook_v3", ticker=MIN, side=OutcomeSide.yes, fair_prob=Decimal("0.4"),
        limit_price=Decimal("0.36"), edge=Decimal("0.024"), size=Decimal(0),
        created_at=datetime(2026, 10, 22, 10, tzinfo=timezone.utc), rules_version="v3",
        position_key=f"{EVENT}:minnesota-vikings", event_ticker=EVENT,
        commence_time=datetime(2026, 10, 25, 20, 25, tzinfo=timezone.utc),
        tags=("low_price_threshold",), available_size=Decimal(50), open_interest=Decimal(200),
    )  # fmt: skip
    assert ledger.record_near_miss_decisions([(sig, "net_edge")]) == 1
    row = ledger.conn.execute(
        "SELECT reason,tags,ask_depth,open_interest,window FROM shadow_decisions"
    ).fetchone()
    assert row == ("net_edge", "low_price_threshold", "50", "200", "72h+")


# --- Phase 2.5 measurement pulls --------------------------------------------------

KICK = datetime(2026, 11, 5, 0, 0, tzinfo=timezone.utc)


def _games() -> dict[str, list[PlannedGame]]:
    return {
        "icehockey_nhl": [
            PlannedGame(KICK, event_ticker="KXNHLGAME-26NOV04BOSTOR"),
            PlannedGame(KICK + timedelta(hours=1), event_ticker="KXNHLGAME-26NOV04NYRNJ"),
        ],
        "basketball_nba": [PlannedGame(KICK, event_ticker="KXNBAGAME-26NOV04LALBOS")],
        "americanfootball_nfl": [PlannedGame(KICK, event_ticker="KXNFLGAME-26NOV04X")],
    }


def test_select_measure_pull_targets_t6h_and_t90m_of_the_earliest_game(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "m.sqlite", Decimal("100"))
    s = Settings(_env_file=None)
    assert select_measure_pull(ledger, _games(), s, KICK - timedelta(hours=9)) is None
    first = select_measure_pull(ledger, _games(), s, KICK - timedelta(hours=6, minutes=30))
    assert first is not None
    assert (first.feed, first.event_ticker, first.target) == (
        "icehockey_nhl", "KXNHLGAME-26NOV04BOSTOR", "T-6h",
    )  # fmt: skip
    ledger.record_measure_pull(first.feed, first.event_ticker, first.target, KICK, KICK)
    # Same window again: NHL's T-6h is done, so the next feed (NBA) is offered.
    again = select_measure_pull(ledger, _games(), s, KICK - timedelta(hours=6))
    assert again is not None and again.feed == "basketball_nba"
    # At T-90m the NHL game already measured is finished first.
    late = select_measure_pull(ledger, _games(), s, KICK - timedelta(minutes=80))
    assert late is not None and (late.feed, late.target) == ("icehockey_nhl", "T-90m")
    assert (
        select_measure_pull(
            ledger, _games(), s, KICK - timedelta(minutes=80), exclude=["icehockey_nhl"]
        ).feed  # type: ignore[union-attr]
        == "basketball_nba"
    )
    # Once the first game starts, the 01:00 game is the earliest upcoming one.
    nxt = select_measure_pull(ledger, _games(), s, KICK + timedelta(minutes=5))
    assert nxt is not None and nxt.event_ticker == "KXNHLGAME-26NOV04NYRNJ"
    assert select_measure_pull(ledger, _games(), s, KICK + timedelta(hours=2)) is None


def _pacer(tmp_path, budget=1, remaining="300", trigger=False):  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "pacer.sqlite", Decimal("100"))
    client = SimpleNamespace(
        budget=budget, requests_remaining=remaining, requests_last_by_sport={},
        requests_made=0, odds_requests_made=0,
    )  # fmt: skip
    return OddsPacer(client, 12, ledger=ledger, trigger_mode=trigger), client, ledger  # type: ignore[arg-type]


def test_measure_credit_is_capped_reserved_and_kept_out_of_the_carry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pacer, client, ledger = _pacer(tmp_path)
    assert pacer.grant_measure_credit("icehockey_nhl", 2, KICK)
    assert client.budget == 2
    assert not pacer.grant_measure_credit("basketball_nba", 2, KICK)  # one per run
    pacer.credit = 1.0
    client.requests_made = 2
    client.requests_last_by_sport = {"americanfootball_nfl": 1, "icehockey_nhl": 1}
    assert pacer.measure_pulled()
    pacer.finish_run(KICK)
    assert ledger.odds_pacer_carry() == 0.0  # only the rotation pull counts

    for _ in range(2):
        ledger.record_measure_pull("icehockey_nhl", "E", "T-6h", KICK, KICK)
    capped, client2, _ = _pacer(tmp_path)
    capped.ledger = ledger
    assert not capped.grant_measure_credit("icehockey_nhl", 2, KICK)
    assert client2.budget == 1

    low, _, _ = _pacer(tmp_path, remaining="26")
    assert not low.grant_measure_credit("icehockey_nhl", 2, KICK)  # keeps the reserve
    trig, _, _ = _pacer(tmp_path, trigger=True)
    assert not trig.grant_measure_credit("icehockey_nhl", 2, KICK)
    zero, _, _ = _pacer(tmp_path)
    assert not zero.grant_measure_credit("icehockey_nhl", 0, KICK)
