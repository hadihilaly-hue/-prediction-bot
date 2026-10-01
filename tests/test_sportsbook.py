from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from prediction_bot.models import OutcomeSide
from prediction_bot.odds import best_team, consensus, devig
from prediction_bot.strategies.sportsbook_arb import SportsbookArbStrategy
from tests.conftest import make_market

EVENT: dict[str, Any] = {
    "id": "e1",
    "home_team": "Troy Trojans",
    "away_team": "Southern Miss Golden Eagles",
    "commence_time": "2026-10-06T23:00:00Z",
    "bookmakers": [
        {
            "key": "pinnacle",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Troy Trojans", "price": 1.50},
                        {"name": "Southern Miss Golden Eagles", "price": 2.80},
                    ],
                }
            ],
        },
        {
            "key": "draftkings",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Troy Trojans", "price": 1.45},
                        {"name": "Southern Miss Golden Eagles", "price": 2.90},
                    ],
                }
            ],
        },
        {
            "key": "shadybook",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Troy Trojans", "price": 9.0},
                        {"name": "Southern Miss Golden Eagles", "price": 1.01},
                    ],
                }
            ],
        },
    ],
}


def test_devig_normalises() -> None:
    p = devig({"A": Decimal("1.5"), "B": Decimal("2.8")})
    assert abs(sum(p.values()) - 1) < Decimal("1e-12")
    assert p["A"] > Decimal("0.65")


def test_consensus_filters_books() -> None:
    c = consensus(EVENT, ["pinnacle", "draftkings"])
    assert c is not None and c.books_used == 2
    assert Decimal("0.64") < c.probs["Troy Trojans"] < Decimal("0.67")


def test_team_matching() -> None:
    cands = ["Troy Trojans", "Southern Miss Golden Eagles"]
    assert best_team("Troy", cands) == "Troy Trojans"
    assert best_team("Southern Miss", cands) == "Southern Miss Golden Eagles"
    assert best_team("Kennesaw St.", cands) is None


class FakeOdds:
    def h2h(self, sport: str, bookmakers: list[str] | None = None) -> list[dict[str, Any]]:
        assert sport == "americanfootball_ncaaf"
        return [EVENT]


def test_sportsbook_arb_signals(settings) -> None:  # type: ignore[no-untyped-def]
    settings.odds_bookmakers = ["pinnacle", "draftkings"]
    strat = SportsbookArbStrategy(settings, odds_client=FakeOdds())  # type: ignore[arg-type]
    # Kalshi prices Troy at 0.55 ask while books say ~0.655 -> buy YES Troy.
    troy = make_market("KXNCAAFGAME-26OCT06USMTROY-TROY", "0.50", "0.55", subtitle="Troy")
    usm = make_market("KXNCAAFGAME-26OCT06USMTROY-USM", "0.45", "0.50", subtitle="Southern Miss")
    sigs = strat.evaluate([troy, usm])
    assert [(s.ticker.rsplit("-", 1)[1], s.side) for s in sigs] == [
        ("TROY", OutcomeSide.yes),
        ("USM", OutcomeSide.no),
    ]
    assert all(s.edge >= settings.min_edge for s in sigs)


def test_sportsbook_odds_refetched_each_cycle(settings) -> None:  # type: ignore[no-untyped-def]
    class Counting(FakeOdds):
        calls = 0

        def h2h(self, sport: str, bookmakers: list[str] | None = None) -> list[dict[str, Any]]:
            self.calls += 1
            return super().h2h(sport, bookmakers)

    odds = Counting()
    strat = SportsbookArbStrategy(settings, odds_client=odds)  # type: ignore[arg-type]
    troy = make_market("KXNCAAFGAME-26OCT06USMTROY-TROY", "0.50", "0.55", subtitle="Troy")
    strat.evaluate([troy])
    strat.evaluate([troy])
    assert odds.calls == 2


def test_repeated_matchup_picks_game_nearest_expiration(settings) -> None:  # type: ignore[no-untyped-def]
    game1 = dict(EVENT, id="g1", commence_time="2026-10-06T23:00:00Z")
    game2 = dict(EVENT, id="g2", commence_time="2026-10-08T23:00:00Z")

    class TwoGames:
        def h2h(self, sport: str, bookmakers: list[str] | None = None) -> list[dict[str, Any]]:
            return [game1, game2]

    strat = SportsbookArbStrategy(settings, odds_client=TwoGames())  # type: ignore[arg-type]
    legs = [
        make_market(
            "KXNCAAFGAME-26OCT08USMTROY-TROY",
            "0.50",
            "0.55",
            subtitle="Troy",
            expected_expiration=datetime(2026, 10, 9, 3, tzinfo=timezone.utc),
        ),
        make_market("KXNCAAFGAME-26OCT08USMTROY-USM", "0.45", "0.50", subtitle="Southern Miss"),
    ]
    game = strat._match_game(legs, strat.games_for("americanfootball_ncaaf"))
    assert game is not None and game.event_id == "g2"

    # a leg with only close_time (2 days after kickoff) must not widen the window once
    # another leg supplies expected_expiration
    mixed = [
        make_market(
            "KXNCAAFGAME-26OCT08USMTROY-USM",
            "0.45",
            "0.50",
            subtitle="Southern Miss",
            close_time=datetime(2026, 10, 11, 3, tzinfo=timezone.utc),
        ),
        legs[0],
    ]
    game = strat._match_game(mixed, strat.games_for("americanfootball_ncaaf"))
    assert game is not None and game.event_id == "g2"
    close_only = [mixed[0]]
    game = strat._match_game(close_only, strat.games_for("americanfootball_ncaaf"))
    assert game is not None and game.event_id == "g2"

    # without any date on the market, a duplicated matchup is ambiguous -> no match
    undated = [make_market("KXNCAAFGAME-26OCT08USMTROY-TROY", "0.50", "0.55", subtitle="Troy")]
    assert strat._match_game(undated, strat.games_for("americanfootball_ncaaf")) is None


def test_sportsbook_arb_without_key_is_noop(settings) -> None:  # type: ignore[no-untyped-def]
    settings.odds_api_key = None
    assert SportsbookArbStrategy(settings).evaluate([]) == []


OUTRIGHT: dict[str, Any] = {
    "id": "masters",
    "sport_key": "golf_masters_tournament_winner",
    "home_team": None,
    "away_team": None,
    "commence_time": "2027-04-08T11:00:00Z",
    "bookmakers": [
        {
            "key": "draftkings",
            "markets": [
                {
                    "key": "outrights",
                    "outcomes": [
                        {"name": "Scottie Scheffler", "price": 5.0},
                        {"name": "Rory McIlroy", "price": 7.0},
                        {"name": "Zach Johnson", "price": 100.0},
                        *({"name": f"Golfer {i}", "price": 60.0} for i in range(40)),
                    ],
                }
            ],
        }
    ],
}


class FakeOutrights:
    calls: list[str] = []

    def h2h(self, sport: str, bookmakers: list[str] | None = None) -> list[dict[str, Any]]:
        raise AssertionError("h2h must not be used for tournament winners")

    def outrights(self, sport: str, bookmakers: list[str] | None = None) -> list[dict[str, Any]]:
        self.calls.append(sport)
        return [OUTRIGHT]


def test_outright_consensus_handles_null_teams() -> None:
    c = consensus(OUTRIGHT, market="outrights")
    assert c is not None and c.home_team == "" and c.books_used == 1
    assert c.probs["Scottie Scheffler"] > c.probs["Rory McIlroy"] > c.probs["Zach Johnson"]
    assert consensus(OUTRIGHT) is None  # no h2h market on an outright event


def test_golf_major_uses_outrights(settings) -> None:  # type: ignore[no-untyped-def]
    odds = FakeOutrights()
    strat = SportsbookArbStrategy(settings, odds_client=odds)  # type: ignore[arg-type]
    exp = datetime(2027, 4, 12, tzinfo=timezone.utc)
    # books put Scheffler ~0.20 (de-vigged over the field); Kalshi asks 0.15 -> buy YES.
    sch = make_market(
        "KXMASTERS-27-SS", "0.13", "0.15", subtitle="Scottie Scheffler", expected_expiration=exp
    )
    # Kalshi asks 0.20 for Johnson vs ~0.03 fair -> buy NO.
    zj = make_market(
        "KXMASTERS-27-ZJ", "0.18", "0.20", subtitle="Zach J Johnson", expected_expiration=exp
    )
    unknown = make_market(
        "KXMASTERS-27-XX", "0.10", "0.12", subtitle="Nobody Here", expected_expiration=exp
    )
    sigs = strat.evaluate([sch, zj, unknown])
    assert odds.calls == ["golf_masters_tournament_winner"]
    assert [(s.ticker.rsplit("-", 1)[1], s.side) for s in sigs] == [
        ("SS", OutcomeSide.yes),
        ("ZJ", OutcomeSide.no),
    ]
    assert "outright consensus" in sigs[0].rationale


def test_golf_major_skips_wrong_edition(settings) -> None:  # type: ignore[no-untyped-def]
    strat = SportsbookArbStrategy(settings, odds_client=FakeOutrights())  # type: ignore[arg-type]
    # market for a past edition: the feed's next tournament starts after it expires
    exp = datetime(2026, 4, 12, tzinfo=timezone.utc)
    sch = make_market(
        "KXMASTERS-26-SS", "0.13", "0.15", subtitle="Scottie Scheffler", expected_expiration=exp
    )
    assert strat.evaluate([sch]) == []


def test_underdog_value_only_buys_price_band(settings) -> None:  # type: ignore[no-untyped-def]
    from prediction_bot.strategies.underdog_value import UnderdogValueStrategy

    settings.odds_bookmakers = ["pinnacle", "draftkings"]
    strat = UnderdogValueStrategy(settings, odds_client=FakeOdds())  # type: ignore[arg-type]
    # Books: Troy ~0.655 / Southern Miss ~0.345. Troy YES at 0.55 is a favourite -> skipped;
    # Southern Miss YES at 0.30 is an underdog priced below consensus -> bought.
    troy = make_market("KXNCAAFGAME-26OCT06USMTROY-TROY", "0.50", "0.55", subtitle="Troy")
    usm = make_market("KXNCAAFGAME-26OCT06USMTROY-USM", "0.25", "0.30", subtitle="Southern Miss")
    sigs = strat.evaluate([troy, usm])
    assert [(s.ticker.rsplit("-", 1)[1], s.side) for s in sigs] == [("USM", OutcomeSide.yes)]
    assert sigs[0].strategy == "underdog_value"
    assert "3.3x payout" in sigs[0].rationale
    # an underdog priced *above* consensus (0.40 ask vs 0.345) is not a value bet
    pricey = make_market("KXNCAAFGAME-26OCT06USMTROY-USM", "0.38", "0.40", subtitle="Southern Miss")
    assert strat.evaluate([troy, pricey]) == []


def test_odds_client_shares_fetch_within_ttl() -> None:
    import httpx

    from prediction_bot.odds import OddsClient

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[EVENT], headers={"x-requests-remaining": "9"})

    oc = OddsClient("k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert oc.h2h("americanfootball_ncaaf") == oc.h2h("americanfootball_ncaaf")
    assert calls == 1
    oc.outrights("golf_masters_tournament_winner")
    assert calls == 2
