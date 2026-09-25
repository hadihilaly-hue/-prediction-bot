from __future__ import annotations

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


def test_sportsbook_arb_without_key_is_noop(settings) -> None:  # type: ignore[no-untyped-def]
    settings.odds_api_key = None
    assert SportsbookArbStrategy(settings).evaluate([]) == []
