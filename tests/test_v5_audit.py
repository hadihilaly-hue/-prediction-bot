from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from prediction_bot.config import Settings
from prediction_bot.models import OutcomeSide
from prediction_bot.odds import consensus
from prediction_bot.paper.ledger import Ledger
from prediction_bot.venues.kalshi import _ts, parse_orderbook, parse_orderbook_levels
from tests.test_sportsbook_v2 import (
    FakeOdds,
    FakeV3Kalshi,
    SportsbookV3Strategy,
    fixed_clock,
    v3_markets,
    v3_odds_event,
)

NOW = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)


# --- 7.1a depth fields and order book keys ---------------------------------------------


def test_orderbook_parsers_accept_dollar_keys_and_legacy_cents() -> None:
    dollars = {
        "orderbook_fp": {"yes_dollars": [["0.4500", "12.50"]], "no_dollars": [["0.5300", "8"]]}
    }
    quote = parse_orderbook("T", dollars)
    assert (quote.yes_bid, quote.yes_bid_size, quote.yes_ask) == (
        Decimal("0.45"),
        Decimal("12.50"),
        Decimal("0.47"),
    )
    legacy = {"orderbook": {"yes": [[45, 100], [44, 20]], "no": [[53, 7]]}}
    levels = parse_orderbook_levels(legacy)
    assert levels["yes"][0] == (Decimal("0.45"), Decimal(100))
    assert parse_orderbook("T", legacy).yes_ask == Decimal("0.47")


# --- 7.1d stale books fail closed ------------------------------------------------------


def _event_with_updates(updates: dict[str, str | None]) -> dict[str, object]:
    event = v3_odds_event()
    for book in event["bookmakers"]:  # type: ignore[union-attr]
        key = book["key"]  # type: ignore[index]
        if key in updates:
            if updates[key] is None:
                book.pop("last_update", None)  # type: ignore[union-attr]
            else:
                book["last_update"] = updates[key]  # type: ignore[index]
    return event


def test_fresh_books_drops_books_with_missing_or_unreadable_update_times(settings) -> None:  # type: ignore[no-untyped-def]
    strategy = SportsbookV3Strategy(
        settings,
        FakeOdds([]),
        kalshi_client=FakeV3Kalshi(),
        ledger=Ledger(":memory:", Decimal("1000")),
        clock=fixed_clock(NOW.isoformat()),
    )
    event = _event_with_updates(
        {"pinnacle": None, "circa": "not-a-time", "betonlineag": "2026-10-22T09:30:00Z"}
    )
    game = consensus(event)
    assert game is not None
    books = {book: {} for book in ("pinnacle", "circa", "betonlineag")}
    fresh, stale = strategy._fresh_books(game, books, NOW)  # type: ignore[arg-type]
    assert set(fresh) == {"betonlineag"} and stale


# --- 7.1e aware timestamps ---------------------------------------------------------------


def test_kalshi_timestamps_are_aware_utc() -> None:
    assert _ts("2026-10-25T20:25:00") == datetime(2026, 10, 25, 20, 25, tzinfo=timezone.utc)
    assert _ts("2026-10-25T20:25:00Z") == datetime(2026, 10, 25, 20, 25, tzinfo=timezone.utc)
    assert _ts("") is None


# --- 7.1c one economic outcome per key -------------------------------------------------


def test_position_keys_merge_two_way_legs_but_not_three_way_legs(settings) -> None:  # type: ignore[no-untyped-def]
    settings.max_gross_edge = Decimal("0.20")
    strategy = SportsbookV3Strategy(
        settings,
        FakeOdds([v3_odds_event(miami_probability=Decimal("0.585"))]),
        kalshi_client=FakeV3Kalshi(),
        ledger=Ledger(":memory:", Decimal("1000")),
        clock=fixed_clock(NOW.isoformat()),
    )
    strategy.evaluate(v3_markets())
    keys = {
        (sig.ticker.rsplit("-", 1)[1], sig.side): sig.position_key
        for sig, _ in strategy.seen_near_miss_decisions()
    }
    # Two-way: NO on Miami is the same economic outcome as YES on Minnesota.
    assert keys[("MIA", OutcomeSide.no)] == "KXNFLGAME-26OCT25MIAMIN:minnesota-vikings"


def test_three_way_no_leg_is_its_own_outcome(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from prediction_bot.analysis import Fill, outcome_key

    def fill(ticker: str, side: str) -> Fill:
        return Fill(
            1,
            "2026-10-01",
            "s",
            ticker,
            side,
            Decimal("0.5"),
            Decimal(1),
            Decimal(0),
            Decimal("0.5"),
            None,
        )

    no_chi = outcome_key(fill("KXMLSGAME-26OCT28CHIORL-CHI", "no"))
    yes_orl = outcome_key(fill("KXMLSGAME-26OCT28CHIORL-ORL", "yes"))
    assert no_chi != yes_orl  # NO Chicago also wins on a draw


# --- 7.2 sizing --------------------------------------------------------------------------


def _sizer(tmp_path, mode: str, clvs: list[str]):  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "s.sqlite", Decimal("1000"))
    for index, clv in enumerate(clvs):
        ledger.conn.execute(
            "INSERT INTO fills (created_at,strategy,ticker,side,price,count,fee,fair_prob,"
            "rules_version,clv_kalshi,clv_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "2026-10-01",
                "sportsbook_v3",
                f"KXNFLGAME-X{index}-A",
                "yes",
                "0.5",
                "1",
                "0",
                "0.6",
                "v3",
                clv,
                "ok",
            ),
        )
    ledger.conn.commit()
    settings = Settings(
        _env_file=None,
        rules_version="v3",
        sizing_mode=mode,  # type: ignore[call-arg]
        max_order_notional=Decimal("25"),
    )
    return SportsbookV3Strategy(settings, FakeOdds([]), kalshi_client=FakeV3Kalshi(), ledger=ledger)


def test_flat_sizing_stakes_one_unit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    strategy = _sizer(tmp_path, "flat", [])
    assert strategy._stake(Decimal("0.05"), Decimal("0.50"), Decimal("1000")) == Decimal("25")


def test_kelly_is_flat_until_the_cohort_is_supported(tmp_path) -> None:  # type: ignore[no-untyped-def]
    unsupported = _sizer(tmp_path, "kelly", ["1", "-1"] * 30)  # CI straddles 0
    assert unsupported._stake(Decimal("0.05"), Decimal("0.50"), Decimal("1000")) == Decimal("25")


def test_kelly_caps_the_edge_at_half_the_supported_cohort_clv(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Gross 3.75c minus the 1.75c taker fee at 0.50 = 2c net, 50 games: supported.
    strategy = _sizer(tmp_path, "kelly", ["3.25", "4.25"] * 25)
    # Edge used = min(5c model, 2c net / 2 = 1c); quarter-Kelly at 0.50: 0.01 / 0.5 x 0.25 x 1000.
    assert strategy._stake(Decimal("0.05"), Decimal("0.50"), Decimal("1000")) == Decimal("5.00")
    assert strategy._stake(Decimal("0.004"), Decimal("0.50"), Decimal("1000")) == Decimal("2.000")


def test_kelly_stakes_flat_when_the_cohort_loses_after_fees(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # +1.0c gross, tight and over 60 games, but the taker fee is 1.5c (multiplier 6/7 at 0.50).
    strategy = _sizer(tmp_path, "kelly", ["0.9", "1.1"] * 30)
    assert strategy.ledger is not None
    strategy.ledger.save_series_fee(
        "KXNFLGAME",
        "quadratic",
        Decimal(6) / Decimal(7),
        datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    assert strategy._stake(Decimal("0.05"), Decimal("0.50"), Decimal("1000")) == Decimal("25")
    assert strategy._cohort_clv_cents() is None


def test_kelly_ignores_a_supported_row_count_on_too_few_games(tmp_path) -> None:  # type: ignore[no-untyped-def]
    strategy = _sizer(tmp_path, "kelly", ["3.25", "4.25"] * 25)
    assert strategy.ledger is not None
    strategy.ledger.conn.execute("UPDATE fills SET ticker='KXNFLGAME-ONE-A' WHERE id % 2 = 0")
    strategy.ledger.conn.execute("UPDATE fills SET ticker='KXNFLGAME-TWO-A' WHERE id % 2 = 1")
    strategy.ledger.conn.commit()
    assert strategy._stake(Decimal("0.05"), Decimal("0.50"), Decimal("1000")) == Decimal("25")
