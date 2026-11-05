from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from prediction_bot.clv import sharp_fairs
from prediction_bot.engine import Engine
from prediction_bot.paper.ledger import Ledger
from tests.test_sportsbook_v2 import (
    FakeOdds,
    FakeV3Kalshi,
    SportsbookV3Strategy,
    fixed_clock,
    v3_markets,
    v3_odds_event,
)

EVENT = "KXNFLGAME-26OCT25MIAMIN"
MIA = f"{EVENT}-MIA"
NOW = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)
EARLIER = NOW - timedelta(hours=6)


def _seed(ledger: Ledger, *, miami_before: str, ask_before: str, books=("pinnacle",)) -> None:  # type: ignore[no-untyped-def]
    # Same vig as the current fixture, so only the probability moves.
    event = v3_odds_event(miami_probability=Decimal(miami_before))
    odds = {
        str(o["name"]): str(o["price"])
        for o in event["bookmakers"][0]["markets"][0]["outcomes"]  # type: ignore[index]
    }
    ledger.conn.execute(
        "INSERT INTO odds_snapshots (created_at,feed,event_id,home_team,away_team,commence_time,"
        "team,prob,books_used,book_odds_json,event_ticker) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            EARLIER.isoformat(), "americanfootball_nfl", "event-v3", "Miami Dolphins",
            "Minnesota Vikings", "2026-10-25T20:25:00Z", "Miami Dolphins", miami_before, 1,
            json.dumps({b: {"odds": odds, "last_update": EARLIER.isoformat()} for b in books}),
            EVENT,
        ),
    )  # fmt: skip
    ledger.conn.execute(
        "INSERT INTO market_snapshots (fetched_at,ticker,event_ticker,series,yes_bid,yes_ask,"
        "yes_bid_size,yes_ask_size,open_interest,volume,expected_expiration) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            (EARLIER - timedelta(minutes=5)).isoformat(), MIA, EVENT, "KXNFLGAME",
            str(Decimal(ask_before) - Decimal("0.02")), ask_before, "100", "100", "200", "0",
            "2026-10-25T23:25:00+00:00",
        ),
    )  # fmt: skip
    ledger.conn.commit()


def _evaluate(settings, ledger: Ledger, ask_now: str):  # type: ignore[no-untyped-def]
    settings.max_gross_edge = Decimal("0.20")
    strategy = SportsbookV3Strategy(
        settings,
        FakeOdds([v3_odds_event(miami_probability=Decimal("0.65"))]),
        kalshi_client=FakeV3Kalshi(),
        ledger=ledger,
        clock=fixed_clock(NOW.isoformat()),
    )
    strategy.evaluate(v3_markets(yes_price=ask_now))
    moves = [
        (sig, reason)
        for sig, reason in strategy.seen_near_miss_decisions()
        if reason == "sharp_move"
    ]
    return strategy, moves


def _fair(miami: str) -> Decimal:
    from prediction_bot.odds import consensus

    game = consensus(v3_odds_event(miami_probability=Decimal(miami)))
    assert game is not None
    result = sharp_fairs("americanfootball_nfl", game.book_odds)
    assert result is not None
    return result[0]["miami-dolphins"]


def test_sharp_fairs_prefers_pinnacle_then_blends_circa_and_betonline() -> None:
    names = {"Miami Dolphins": "2.0", "Minnesota Vikings": "2.0"}
    fair, source = sharp_fairs("americanfootball_nfl", {"pinnacle": names, "draftkings": names})  # type: ignore[misc]
    assert source == "pinnacle" and fair["miami-dolphins"] == Decimal("0.5")
    _fair, source = sharp_fairs(  # type: ignore[misc]
        "americanfootball_nfl", {"circa": names, "betonlineag": names}
    )
    assert source == "consensus"
    assert sharp_fairs("americanfootball_nfl", {"fanduel": names}) is None


def test_lagging_kalshi_ask_is_logged_and_followed_in_shadow(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "p.sqlite", Decimal("1000"))
    _seed(ledger, miami_before="0.60", ask_before="0.56")
    strategy, moves = _evaluate(settings, ledger, ask_now="0.57")  # Kalshi +1c vs sharp +5c
    sig, _ = next((s, r) for s, r in moves if s.ticker == MIA)
    assert sig.side.value == "yes"
    expected = _fair("0.65") - _fair("0.60")
    assert set(sig.tags or ()) >= {
        f"move_{int((expected * 100).to_integral_value())}",
        "kalshi_lagged",
    }
    assert sig.limit_price == Decimal("0.57") and sig.fair_prob > Decimal("0.64")
    follows = strategy.seen_sharp_follow_decisions()
    assert [(f.ticker, f.side.value) for f in follows] == [(MIA, "yes")]
    assert "sharp_move_follow" in (follows[0].tags or ())

    # The engine stores the follow cohort as would-trade shadows, never as fills.
    Engine(settings, object(), [strategy], ledger)  # type: ignore[arg-type]
    ledger.record_shadow_decision(follows[0], "v3", reason="sharp_move_follow")
    row = ledger.conn.execute(
        "SELECT would_trade,net_edge FROM shadow_decisions WHERE reason='sharp_move_follow'"
    ).fetchone()
    assert row[0] == 1 and Decimal(row[1]) > 0
    assert ledger.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0


def test_kalshi_that_already_followed_is_not_a_follow_candidate(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "p.sqlite", Decimal("1000"))
    _seed(ledger, miami_before="0.60", ask_before="0.56")
    strategy, moves = _evaluate(settings, ledger, ask_now="0.60")  # +4c >= half of 5c
    sig = next(s for s, _ in moves if s.ticker == MIA)
    assert "kalshi_followed" in (sig.tags or ())
    assert strategy.seen_sharp_follow_decisions() == []


def test_small_moves_old_snapshots_and_book_switches_are_ignored(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    small = Ledger(tmp_path / "small.sqlite", Decimal("1000"))
    _seed(small, miami_before="0.645", ask_before="0.56")
    assert _fair("0.65") - _fair("0.645") < Decimal("0.02")
    assert _evaluate(settings, small, ask_now="0.57")[1] == []

    switched = Ledger(tmp_path / "switch.sqlite", Decimal("1000"))
    _seed(switched, miami_before="0.60", ask_before="0.56", books=("circa",))
    # Before: Circa only; now Pinnacle quotes too, so the reference changed.
    assert _evaluate(settings, switched, ask_now="0.57")[1] == []
