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
