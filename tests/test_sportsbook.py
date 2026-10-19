from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from prediction_bot.models import OutcomeSide
from prediction_bot.odds import (
    OddsBudgetExhausted,
    OddsClient,
    OddsPacer,
    best_team,
    consensus,
    devig,
    per_run_budget,
)
from prediction_bot.strategies.sportsbook_arb import SportsbookArbStrategy
from tests.conftest import make_market


def _iso(**kw: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(**kw)).isoformat()


EVENT: dict[str, Any] = {
    "id": "e1",
    "home_team": "Troy Trojans",
    "away_team": "Southern Miss Golden Eagles",
    # 10 days out: comfortably past the min_lead_hours entry cutoff
    "commence_time": _iso(days=10),
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
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = events if events is not None else [EVENT]

    def h2h(self, sport: str, bookmakers: list[str] | None = None) -> list[dict[str, Any]]:
        return self.events


def test_sportsbook_arb_signals(settings) -> None:  # type: ignore[no-untyped-def]
    settings.odds_bookmakers = ["pinnacle", "draftkings"]
    strat = SportsbookArbStrategy(settings, odds_client=FakeOdds())  # type: ignore[arg-type]
    # Kalshi prices Troy at 0.55 ask while books say ~0.655 -> back Troy. "Troy YES" and
    # "Southern Miss NO" are the same bet; only the cheaper leg (NO at 0.54) is signalled.
    troy = make_market("KXNCAAFGAME-26OCT06USMTROY-TROY", "0.50", "0.55", subtitle="Troy")
    usm = make_market("KXNCAAFGAME-26OCT06USMTROY-USM", "0.46", "0.50", subtitle="Southern Miss")
    sigs = strat.evaluate([troy, usm])
    assert [(s.ticker.rsplit("-", 1)[1], s.side) for s in sigs] == [("USM", OutcomeSide.no)]
    assert all(s.edge >= settings.min_edge for s in sigs)
    assert sigs[0].equivalents == (("KXNCAAFGAME-26OCT06USMTROY-TROY", OutcomeSide.yes),)
    # with equal prices exactly one leg is kept
    tie = make_market("KXNCAAFGAME-26OCT06USMTROY-USM", "0.45", "0.50", subtitle="Southern Miss")
    assert len(strat.evaluate([troy, tie])) == 1


def test_soccer_draw_keeps_both_legs(settings) -> None:  # type: ignore[no-untyped-def]
    import copy

    ev = copy.deepcopy(EVENT)
    ev["home_team"], ev["away_team"] = "Arsenal", "Chelsea"
    for b in ev["bookmakers"]:
        b["markets"][0]["outcomes"] = [
            {"name": "Arsenal", "price": 2.0},
            {"name": "Chelsea", "price": 4.0},
            {"name": "Draw", "price": 4.0},
        ]
    settings.odds_bookmakers = ["pinnacle", "draftkings"]
    strat = SportsbookArbStrategy(settings, odds_client=FakeOdds([ev]))  # type: ignore[arg-type]
    # Arsenal ~0.50, Chelsea ~0.25. "Arsenal YES" (0.40) and "Chelsea NO" (0.60, fair 0.75)
    # are different bets because Chelsea NO also wins on a draw: both are kept.
    ars = make_market("KXEPLGAME-26OCT06CHEARS-ARS", "0.35", "0.40", subtitle="Arsenal")
    che = make_market("KXEPLGAME-26OCT06CHEARS-CHE", "0.40", "0.45", subtitle="Chelsea")
    sigs = strat.evaluate([ars, che])
    assert {(s.ticker.rsplit("-", 1)[1], s.side) for s in sigs} == {
        ("ARS", OutcomeSide.yes),
        ("CHE", OutcomeSide.no),
    }
    assert all(s.equivalents == () for s in sigs)


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


def test_outright_ignores_incomplete_fields() -> None:
    def book(key: str, outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        return {"key": key, "markets": [{"key": "outrights", "outcomes": outcomes}]}

    # two golfers summing to <1: normalising would make them ~0.58/0.42 and the raw
    # quotes still carry margin, so an incomplete book contributes nothing
    thin = dict(OUTRIGHT)
    thin["bookmakers"] = [
        book("thin", [{"name": "Scottie Scheffler", "price": 5.0}, {"name": "Rory", "price": 7.0}])
    ]
    assert consensus(thin, market="outrights") is None
    # a book quoting a subset of the field is dropped even when its margin pushes its
    # implied sum above 1 (normalising would hand it the missing golfers' mass)
    full_outcomes = OUTRIGHT["bookmakers"][0]["markets"][0]["outcomes"]
    subset = dict(OUTRIGHT)
    subset["bookmakers"] = OUTRIGHT["bookmakers"] + [
        book("subset", [dict(o, price=1.5) for o in full_outcomes[:20]])
    ]
    c = consensus(subset, market="outrights")
    assert c is not None and c.books_used == 1
    assert abs(sum(c.probs.values()) - 1) < Decimal("1e-12")


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
    # a zero ask never reaches the payout division, even with no price floor
    settings.underdog_min_price = Decimal(0)
    zero = make_market("KXNCAAFGAME-26OCT06USMTROY-USM", "0", "0", subtitle="Southern Miss")
    assert strat.evaluate([troy, zero]) == []


def test_min_lead_blocks_games_too_soon(settings) -> None:  # type: ignore[no-untyped-def]
    # same edge as test_sportsbook_arb_signals, but kickoff is inside the lead window
    soon = dict(EVENT, id="soon", commence_time=_iso(hours=24))
    settings.odds_bookmakers = ["pinnacle", "draftkings"]
    strat = SportsbookArbStrategy(settings, odds_client=FakeOdds([soon]))  # type: ignore[arg-type]
    troy = make_market("KXNCAAFGAME-26OCT06USMTROY-TROY", "0.50", "0.55", subtitle="Troy")
    usm = make_market("KXNCAAFGAME-26OCT06USMTROY-USM", "0.46", "0.50", subtitle="Southern Miss")
    assert strat.evaluate([troy, usm]) == []

    # underdog_value inherits the same cutoff through evaluate()
    from prediction_bot.strategies.underdog_value import UnderdogValueStrategy

    dogs = UnderdogValueStrategy(settings, odds_client=FakeOdds([soon]))  # type: ignore[arg-type]
    assert dogs.evaluate([troy, usm]) == []


def test_min_lead_disabled(settings) -> None:  # type: ignore[no-untyped-def]
    settings.min_lead_hours = 0
    soon = dict(EVENT, id="soon", commence_time=_iso(hours=24))
    settings.odds_bookmakers = ["pinnacle", "draftkings"]
    strat = SportsbookArbStrategy(settings, odds_client=FakeOdds([soon]))  # type: ignore[arg-type]
    troy = make_market("KXNCAAFGAME-26OCT06USMTROY-TROY", "0.50", "0.55", subtitle="Troy")
    usm = make_market("KXNCAAFGAME-26OCT06USMTROY-USM", "0.46", "0.50", subtitle="Southern Miss")
    assert len(strat.evaluate([troy, usm])) == 1


def test_min_lead_uses_market_expiry(settings) -> None:  # type: ignore[no-untyped-def]
    # the market's own expiry beats the odds feed's commence_time when both exist:
    # this game starts in 10 days but the market resolves tomorrow
    settings.odds_bookmakers = ["pinnacle", "draftkings"]
    strat = SportsbookArbStrategy(settings, odds_client=FakeOdds())  # type: ignore[arg-type]
    troy = make_market(
        "KXNCAAFGAME-26OCT06USMTROY-TROY",
        "0.50",
        "0.55",
        subtitle="Troy",
        expected_expiration=datetime.now(timezone.utc) + timedelta(hours=20),
    )
    usm = make_market("KXNCAAFGAME-26OCT06USMTROY-USM", "0.46", "0.50", subtitle="Southern Miss")
    assert strat.evaluate([troy, usm]) == []


def test_min_lead_close_time_is_not_a_game_anchor(settings) -> None:  # type: ignore[no-untyped-def]
    # close_time trails kickoff by days: a game starting in 24h must not slip through
    # just because its market keeps trading for four more days
    soon = dict(EVENT, id="soon", commence_time=_iso(hours=24))
    settings.odds_bookmakers = ["pinnacle", "draftkings"]
    strat = SportsbookArbStrategy(settings, odds_client=FakeOdds([soon]))  # type: ignore[arg-type]
    troy = make_market(
        "KXNCAAFGAME-26OCT06USMTROY-TROY",
        "0.50",
        "0.55",
        subtitle="Troy",
        close_time=datetime.now(timezone.utc) + timedelta(days=4),
    )
    usm = make_market(
        "KXNCAAFGAME-26OCT06USMTROY-USM",
        "0.46",
        "0.50",
        subtitle="Southern Miss",
        close_time=datetime.now(timezone.utc) + timedelta(days=4),
    )
    assert strat.evaluate([troy, usm]) == []


def test_outright_stops_entering_near_expiry(settings) -> None:  # type: ignore[no-untyped-def]
    class SoonOutrights(FakeOutrights):
        # feed's next tournament starts tomorrow, so the market match succeeds and
        # only the lead-time cutoff can reject it
        def outrights(
            self, sport: str, bookmakers: list[str] | None = None
        ) -> list[dict[str, Any]]:
            return [dict(OUTRIGHT, commence_time=_iso(hours=24))]

    strat = SportsbookArbStrategy(settings, odds_client=SoonOutrights())  # type: ignore[arg-type]
    sch = make_market(
        "KXMASTERS-27-SS",
        "0.13",
        "0.15",
        subtitle="Scottie Scheffler",
        expected_expiration=datetime.now(timezone.utc) + timedelta(hours=48),
    )
    assert strat.evaluate([sch]) == []


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


def test_team_matching_spelling_variants() -> None:
    assert best_team("Atletico", ["VfB Stuttgart", "Atlético Madrid"]) == "Atlético Madrid"
    assert best_team("Bodoe/Glimt", ["Napoli", "Bodø/Glimt"]) == "Bodø/Glimt"
    assert best_team("PSG", ["Paris Saint Germain", "Barcelona"]) == "Paris Saint Germain"
    assert best_team("Brighton", ["Liverpool", "Brighton and Hove Albion"]) == (
        "Brighton and Hove Albion"
    )
    assert best_team("Milan", ["Inter Milan", "AC Milan"]) == "AC Milan"
    assert best_team("Inter", ["Inter Milan", "AC Milan"]) == "Inter Milan"
    # UFC: transliteration slips, apostrophes and hyphenated names
    assert best_team("Jasmin Lucindo", ["Iasmin Lucindo", "Nicolle Caliari"]) == "Iasmin Lucindo"
    assert best_team("Loneer Kavanagh", ["Lone'er Kavanagh", "Ramazonbek Temirov"]) == (
        "Lone'er Kavanagh"
    )
    assert best_team("Abdul-Rakhman Yakhyaev", ["Nikita Krylov", "Abdulrakhman Yakhyaev"]) == (
        "Abdulrakhman Yakhyaev"
    )
    # looser matching must not blur genuinely different teams
    assert best_team("Arkansas", ["Kansas Jayhawks", "Arkansas Razorbacks"]) == (
        "Arkansas Razorbacks"
    )
    assert best_team("Texas", ["Texas Tech Red Raiders", "Texas Longhorns"]) == "Texas Longhorns"
    assert best_team("Southern Miss", ["Ole Miss Rebels", "Mississippi State"]) is None


def _soccer_event() -> dict[str, Any]:
    import copy

    ev = copy.deepcopy(EVENT)
    ev["home_team"], ev["away_team"] = "Arsenal", "Chelsea"
    for b in ev["bookmakers"]:
        b["markets"][0]["outcomes"] = [
            {"name": "Arsenal", "price": 2.0},
            {"name": "Chelsea", "price": 4.0},
            {"name": "Draw", "price": 4.0},
        ]
    return ev


def test_soccer_tie_leg_priced_from_draw_odds(settings) -> None:  # type: ignore[no-untyped-def]
    settings.odds_bookmakers = ["pinnacle", "draftkings"]
    strat = SportsbookArbStrategy(settings, odds_client=FakeOdds([_soccer_event()]))  # type: ignore[arg-type]
    ars = make_market("KXEPLGAME-26OCT06CHEARS-ARS", "0.48", "0.52", subtitle="Arsenal")
    che = make_market("KXEPLGAME-26OCT06CHEARS-CHE", "0.23", "0.27", subtitle="Chelsea")
    tie = make_market("KXEPLGAME-26OCT06CHEARS-TIE", "0.15", "0.18", subtitle="Tie")
    sigs = strat.evaluate([ars, che, tie])
    assert [(s.ticker.rsplit("-", 1)[1], s.side) for s in sigs] == [("TIE", OutcomeSide.yes)]
    assert abs(sigs[0].fair_prob - Decimal("0.25")) < Decimal("0.001")
    assert sigs[0].equivalents == ()
    assert "Draw consensus" in sigs[0].rationale


def test_per_run_budget_spreads_quota_to_month_end() -> None:
    oct19 = datetime(2026, 10, 19, tzinfo=timezone.utc)
    assert per_run_budget(216, 3, oct19) == 216 // 39  # 13 days x 3 runs left
    assert per_run_budget(10, 3, datetime(2026, 10, 31, 23, tzinfo=timezone.utc)) == 10
    assert per_run_budget(20, 3, datetime(2026, 12, 2, tzinfo=timezone.utc)) == 0  # 90 runs
    assert per_run_budget(-5, 3, oct19) == 0


def test_odds_client_stops_at_budget() -> None:
    import httpx
    import pytest

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], headers={"x-requests-remaining": "41"})

    oc = OddsClient("k", client=httpx.Client(transport=httpx.MockTransport(handler)), budget=1)
    assert oc.quota_remaining() == 41 and oc.requests_made == 0  # /sports is free
    oc.h2h("soccer_epl")
    oc.h2h("soccer_epl")  # memoised: no new request
    with pytest.raises(OddsBudgetExhausted):
        oc.h2h("icehockey_nhl")
    assert oc.requests_made == 1


def test_budget_goes_to_league_with_most_games_soon(settings) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    class OneCall(FakeOdds):
        def __init__(self) -> None:
            super().__init__([])
            self.sports: list[str] = []

        def h2h(self, sport: str, bookmakers: list[str] | None = None) -> list[dict[str, Any]]:
            if self.sports:
                raise OddsBudgetExhausted("spent")
            self.sports.append(sport)
            return []

    now = datetime.now(timezone.utc)
    soon, later = now + timedelta(hours=5), now + timedelta(hours=100)
    mk = [
        make_market("KXNHLGAME-X1-A", "0.4", "0.5", subtitle="A", expected_expiration=soon),
        make_market("KXEPLGAME-Y1-A", "0.4", "0.5", subtitle="A", expected_expiration=soon),
        make_market("KXEPLGAME-Y2-A", "0.4", "0.5", subtitle="A", expected_expiration=soon),
        make_market("KXMLBGAME-Z1-A", "0.4", "0.5", subtitle="A", expected_expiration=later),
        make_market("KXMLBGAME-Z2-A", "0.4", "0.5", subtitle="A", expected_expiration=later),
        make_market("KXMLBGAME-Z3-A", "0.4", "0.5", subtitle="A", expected_expiration=later),
    ]
    odds = OneCall()
    strat = SportsbookArbStrategy(settings, odds_client=odds)  # type: ignore[arg-type]
    assert strat.evaluate(mk) == []
    assert odds.sports == ["soccer_epl"]


def test_draw_leg_alone_matches_no_fixture(settings) -> None:  # type: ignore[no-untyped-def]
    settings.odds_bookmakers = ["pinnacle", "draftkings"]
    strat = SportsbookArbStrategy(settings, odds_client=FakeOdds([_soccer_event()]))  # type: ignore[arg-type]
    tie = make_market("KXEPLGAME-26OCT06CHEARS-TIE", "0.15", "0.18", subtitle="Tie")
    assert strat.evaluate([tie]) == []


def test_odds_pacer_resets_each_run_and_fails_closed() -> None:
    import httpx

    status = {"code": 200}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status["code"], json=[], headers={"x-requests-remaining": "10"})

    oc = OddsClient("k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    oct31 = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)  # 12h left
    pacer = OddsPacer(oc, runs_per_day=48)  # 24 runs left -> 10/24 request per run
    assert pacer.start_run(oct31) == 0
    assert pacer.start_run(oct31) == 0
    assert pacer.start_run(oct31) == 1  # carried-over fractions add up
    oc.h2h("soccer_epl")
    assert pacer.start_run(oct31) == 0 and oc.requests_made == 0  # spent credit is deducted
    many = OddsPacer(oc, runs_per_day=1)
    assert many.start_run(oct31) == 10
    oc.h2h("icehockey_nhl")
    assert many.start_run(oct31) == 10  # a later run gets a fresh budget, not a used-up one
    status["code"] = 500
    assert many.start_run(oct31) == 0 and oc.budget == 0  # quota unreadable: no odds, no crash
