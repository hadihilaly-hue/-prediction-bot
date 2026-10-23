from __future__ import annotations

import json
from decimal import Decimal

import pytest

from prediction_bot.teams import _TEAMS, team_for_code
from tests.test_sportsbook_v2 import (
    FakeOdds,
    SportsbookV2Strategy,
    fixed_clock,
    make_market,
    odds_event,
)

EVENT = "KXNFLGAME-26OCT25KCLV"
NOW = "2026-10-23T00:00:00Z"
COMMENCE = "2026-10-25T20:25:00Z"
HOME = "Kansas City Chiefs"
AWAY = "Las Vegas Raiders"


def evaluate_pair(
    settings,
    *,
    home_fair: str = "0.58",
    home_ask: str = "0.50",
    home_bid: str | None = None,
    home_depth: str = "100",
    books: list[str] | None = None,
    max_gross_edge: str = "0.2",
):  # type: ignore[no-untyped-def]
    settings.min_lead_hours = 0
    settings.max_gross_edge = Decimal(max_gross_edge)
    settings.longshot_haircut = "none"
    if books is not None:
        settings.odds_bookmakers = books
    home_probability = Decimal(home_fair)
    game = odds_event(
        "phase2-kc-lv",
        HOME,
        AWAY,
        COMMENCE,
        {HOME: home_probability, AWAY: Decimal(1) - home_probability},
    )
    strategy = SportsbookV2Strategy(
        settings,
        FakeOdds([game]),
        clock=fixed_clock(NOW),
    )
    bid = home_bid or str(Decimal(home_ask) - Decimal("0.01"))
    markets = [
        make_market(
            f"{EVENT}-KC",
            bid,
            home_ask,
            subtitle="Kansas City",
            yes_ask_size=Decimal(home_depth),
        ),
        make_market(f"{EVENT}-LV", "0.39", "0.40", subtitle="Las Vegas"),
    ]
    return strategy, strategy.evaluate(markets)


def test_v2_fee_aware_reference_signal_records_devig_and_haircut(settings) -> None:  # type: ignore[no-untyped-def]
    strategy, signals = evaluate_pair(settings)

    assert len(signals) == 1
    signal = signals[0]
    assert signal.ticker == f"{EVENT}-KC"
    assert signal.reference_source == "pinnacle"
    assert signal.book_count == 5
    assert signal.fee_type == "quadratic_with_maker_fees"
    assert signal.fee_multiplier == Decimal(1)
    assert signal.fair_pre_haircut is not None
    assert abs(signal.fair_pre_haircut - Decimal("0.58")) < Decimal("1e-25")
    assert signal.fair_prob == signal.fair_pre_haircut
    assert abs(signal.edge - (signal.fair_prob - signal.limit_price - Decimal("0.0175"))) < Decimal(
        "1e-25"
    )
    assert {"multiplicative", "additive", "power", "shin"} <= set(
        json.loads(signal.devig_json or "{}")
    )
    assert not any(block.reason == "fee_unknown" for block in strategy.seen_blocks())


def test_reference_falls_back_to_book_median_and_requires_minimum_books(settings) -> None:  # type: ignore[no-untyped-def]
    strategy, signals = evaluate_pair(settings, books=["circa", "draftkings"])
    assert signals
    assert signals[0].reference_source == "median"
    assert signals[0].book_count == 2

    strategy, signals = evaluate_pair(settings, books=["pinnacle"])
    assert signals == []
    assert any(block.reason == "few_books" for block in strategy.seen_blocks())


def test_reference_dispersion_blocks_inconsistent_books(settings) -> None:  # type: ignore[no-untyped-def]
    settings.min_lead_hours = 0
    settings.max_gross_edge = Decimal("0.2")
    game = odds_event(
        "phase2-kc-lv",
        HOME,
        AWAY,
        COMMENCE,
        {HOME: Decimal("0.58"), AWAY: Decimal("0.42")},
    )
    books = game["bookmakers"]
    assert isinstance(books, list)
    circa = next(book for book in books if book["key"] == "circa")
    markets = circa["markets"]
    assert isinstance(markets, list)
    outcomes = markets[0]["outcomes"]
    assert isinstance(outcomes, list)
    for outcome in outcomes:
        outcome["price"] = str(
            Decimal(1) / (Decimal("0.68") if outcome["name"] == HOME else Decimal("0.32"))
        )
    strategy = SportsbookV2Strategy(settings, FakeOdds([game]), clock=fixed_clock(NOW))

    strategy.evaluate(
        [
            make_market(f"{EVENT}-KC", "0.49", "0.50", subtitle="Kansas City"),
            make_market(f"{EVENT}-LV", "0.39", "0.40", subtitle="Las Vegas"),
        ]
    )
    assert any(block.reason == "dispersion" for block in strategy.seen_blocks())


@pytest.mark.parametrize(
    ("fair", "ask", "reason"),
    [
        ("0.70", "0.09", "price_floor"),
        ("0.98", "0.91", "price_ceiling"),
        ("0.58", "0.57", "net_edge"),
        ("0.245", "0.21", "longshot_edge"),
    ],
)
def test_v2_thresholds_block_in_order(settings, fair: str, ask: str, reason: str) -> None:  # type: ignore[no-untyped-def]
    strategy, signals = evaluate_pair(settings, home_fair=fair, home_ask=ask)
    assert all(signal.ticker != f"{EVENT}-KC" for signal in signals)
    assert any(
        block.reason == reason and block.ticker == f"{EVENT}-KC" for block in strategy.seen_blocks()
    )


def test_v2_sizing_respects_bankroll_caps_and_displayed_depth(settings) -> None:  # type: ignore[no-untyped-def]
    strategy, signals = evaluate_pair(
        settings,
        home_fair="0.80",
        home_ask="0.50",
        home_depth="2",
        max_gross_edge="0.4",
    )

    assert len(signals) == 1
    signal = signals[0]
    assert signal.size == Decimal(2)
    assert any(block.reason == "depth_limited" for block in strategy.seen_blocks())
    fee = Decimal("0.07") * signal.size * signal.limit_price * (1 - signal.limit_price)
    assert signal.limit_price * signal.size + fee <= Decimal("20")


@pytest.mark.parametrize(
    ("ticker", "series", "target_code", "other_code", "ask"),
    [
        ("KXNCAAFGAME-26OCT03VANUGA", "KXNCAAFGAME", "VAN", "UGA", "0.05"),
        ("KXNCAAFGAME-26OCT03NDUNC", "KXNCAAFGAME", "ND", "UNC", "0.07"),
        ("KXNCAAFGAME-26OCT03AUBTENN", "KXNCAAFGAME", "AUB", "TENN", "0.07"),
        ("KXNCAAFGAME-26OCT03USUBSU", "KXNCAAFGAME", "USU", "BSU", "0.08"),
        ("KXNCAAFGAME-26OCT03WYONDSU", "KXNCAAFGAME", "WYO", "NDSU", "0.09"),
        ("KXNCAAFGAME-26OCT03TOLBALL", "KXNCAAFGAME", "TOL", "BALL", "0.09"),
    ],
)
def test_appendix_price_floor_cases(
    settings,
    ticker: str,
    series: str,
    target_code: str,
    other_code: str,
    ask: str,
) -> None:  # type: ignore[no-untyped-def]
    settings.min_lead_hours = 0
    settings.max_gross_edge = Decimal("0.2")
    settings.longshot_haircut = "none"
    target_id = team_for_code(series, target_code)
    other_id = team_for_code(series, other_code)
    assert target_id is not None and other_id is not None
    sport = "americanfootball_ncaaf"
    target_name = _TEAMS[sport][target_id]["odds_names"][0]
    other_name = _TEAMS[sport][other_id]["odds_names"][0]
    commence = "2026-10-03T20:00:00Z"
    game = odds_event(
        "appendix-floor",
        target_name,
        other_name,
        commence,
        {target_name: Decimal("0.5"), other_name: Decimal("0.5")},
    )
    strategy = SportsbookV2Strategy(
        settings, FakeOdds([game]), clock=fixed_clock("2026-10-01T00:00:00Z")
    )
    strategy.evaluate(
        [
            make_market(
                f"{ticker}-{target_code}",
                str(Decimal(ask) - Decimal("0.01")),
                ask,
                subtitle=_TEAMS[sport][target_id]["kalshi_names"][0],
            ),
            make_market(
                f"{ticker}-{other_code}",
                "0.49",
                "0.50",
                subtitle=_TEAMS[sport][other_id]["kalshi_names"][0],
            ),
        ]
    )
    assert any(
        block.reason == "price_floor" and block.ticker == f"{ticker}-{target_code}"
        for block in strategy.seen_blocks()
    )


@pytest.mark.parametrize(
    ("ticker", "target_code", "other_code", "fair", "ask", "now"),
    [
        (
            "KXNFLGAME-26OCT18CARPHI",
            "CAR",
            "PHI",
            Decimal("0.540"),
            "0.51",
            "2026-10-15T00:00:00Z",
        ),
        (
            "KXNFLGAME-26OCT25CINBAL",
            "CIN",
            "BAL",
            Decimal("0.620"),
            "0.59",
            "2026-10-22T00:00:00Z",
        ),
    ],
)
def test_appendix_net_edge_cases(
    settings,
    ticker: str,
    target_code: str,
    other_code: str,
    fair: Decimal,
    ask: str,
    now: str,
) -> None:  # type: ignore[no-untyped-def]
    settings.min_lead_hours = 0
    settings.max_gross_edge = Decimal("0.2")
    settings.longshot_haircut = "none"
    target_id = team_for_code("KXNFLGAME", target_code)
    other_id = team_for_code("KXNFLGAME", other_code)
    assert target_id is not None and other_id is not None
    sport = "americanfootball_nfl"
    target_name = _TEAMS[sport][target_id]["odds_names"][0]
    other_name = _TEAMS[sport][other_id]["odds_names"][0]
    game = odds_event(
        "appendix-net-edge",
        target_name,
        other_name,
        "2026-10-18T20:00:00Z" if "26OCT18" in ticker else "2026-10-25T20:00:00Z",
        {target_name: fair, other_name: Decimal(1) - fair},
    )
    strategy = SportsbookV2Strategy(settings, FakeOdds([game]), clock=fixed_clock(now))
    strategy.evaluate(
        [
            make_market(
                f"{ticker}-{target_code}",
                str(Decimal(ask) - Decimal("0.01")),
                ask,
                subtitle=_TEAMS[sport][target_id]["kalshi_names"][0],
            ),
            make_market(
                f"{ticker}-{other_code}",
                "0.39",
                "0.40",
                subtitle=_TEAMS[sport][other_id]["kalshi_names"][0],
            ),
        ]
    )
    assert any(
        block.reason == "net_edge" and block.ticker == f"{ticker}-{target_code}"
        for block in strategy.seen_blocks()
    )
