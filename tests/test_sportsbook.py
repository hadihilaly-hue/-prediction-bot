from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

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
from prediction_bot.paper.ledger import Ledger
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


def test_consensus_weights_sharp_books() -> None:
    # pinnacle (weight 3) says Troy ~0.65, draftkings (1) ~0.67, shadybook (1) ~0.10.
    # Equal weighting would land ~0.47, pulled a third of the way to the bad book;
    # weighted the consensus stays within ~0.11 of pinnacle at ~0.54.
    c = consensus(EVENT)
    assert c is not None and c.books_used == 3
    assert Decimal("0.52") < c.probs["Troy Trojans"] < Decimal("0.56")


def test_engine_snapshots_consensus_once_per_event(settings) -> None:  # type: ignore[no-untyped-def]
    from prediction_bot.engine import Engine
    from prediction_bot.paper.ledger import Ledger
    from prediction_bot.strategies.underdog_value import UnderdogValueStrategy

    ledger = Ledger(settings.paper_db_path, settings.paper_starting_cash)
    odds = FakeOdds()
    # two strategies sharing one odds client see the same consensus; the snapshot must
    # record it once per event, not once per strategy
    eng = Engine(
        settings,
        object(),  # type: ignore[arg-type]
        [
            SportsbookArbStrategy(settings, odds_client=odds),  # type: ignore[arg-type]
            UnderdogValueStrategy(settings, odds_client=odds),  # type: ignore[arg-type]
        ],
        ledger,
    )
    troy = make_market("KXNCAAFGAME-26OCT06USMTROY-TROY", "0.50", "0.55", subtitle="Troy")
    eng.run_cycle([troy])
    rows = ledger.conn.execute(
        "SELECT feed, event_id, team, books_used FROM odds_snapshots"
    ).fetchall()
    assert len(rows) == 2
    assert {r[0] for r in rows} == {"americanfootball_ncaaf"}
    assert {r[2] for r in rows} == {"Troy Trojans", "Southern Miss Golden Eagles"}
    assert all(r[3] == 2 for r in rows)  # pinnacle + draftkings in EVENT


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
    # Kalshi prices Troy at 0.55 ask (0.50 bid) while books say ~0.66 -> back Troy.
    # "Troy YES" and "Southern Miss NO" are the same bet; both maker-enter at bid+1c
    # (0.51) and the higher-edge leg wins the de-dup.
    troy = make_market("KXNCAAFGAME-26OCT06USMTROY-TROY", "0.50", "0.55", subtitle="Troy")
    usm = make_market("KXNCAAFGAME-26OCT06USMTROY-USM", "0.46", "0.50", subtitle="Southern Miss")
    sigs = strat.evaluate([troy, usm])
    assert [(s.ticker.rsplit("-", 1)[1], s.side) for s in sigs] == [("USM", OutcomeSide.no)]
    assert all(s.edge >= settings.min_edge for s in sigs)
    assert sigs[0].equivalents == (("KXNCAAFGAME-26OCT06USMTROY-TROY", OutcomeSide.yes),)
    assert sigs[0].limit_price == Decimal("0.51") and not sigs[0].taker
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


def test_maker_entry_posts_inside_spread(settings) -> None:  # type: ignore[no-untyped-def]
    settings.odds_bookmakers = ["pinnacle", "draftkings"]
    strat = SportsbookArbStrategy(settings, odds_client=FakeOdds())  # type: ignore[arg-type]
    # 0.50/0.55 spread -> maker at 0.51 with the maker fee; a 1c spread can't improve
    # so the same signal falls back to a taker fill at the ask.
    wide = make_market("KXNCAAFGAME-26OCT06USMTROY-TROY", "0.50", "0.55", subtitle="Troy")
    tight = make_market("KXNCAAFGAME-26OCT06USMTROY-TROY", "0.54", "0.55", subtitle="Troy")
    (sig,) = strat.evaluate([wide])
    assert sig.limit_price == Decimal("0.51") and not sig.taker
    assert sig.edge == sig.fair_prob - Decimal("0.51") - Decimal("0.01")
    (sig,) = strat.evaluate([tight])
    assert sig.limit_price == Decimal("0.55") and sig.taker


def test_taker_entry_style_pays_the_ask(settings) -> None:  # type: ignore[no-untyped-def]
    settings.entry_style = "taker"
    settings.odds_bookmakers = ["pinnacle", "draftkings"]
    strat = SportsbookArbStrategy(settings, odds_client=FakeOdds())  # type: ignore[arg-type]
    wide = make_market("KXNCAAFGAME-26OCT06USMTROY-TROY", "0.50", "0.55", subtitle="Troy")
    (sig,) = strat.evaluate([wide])
    assert sig.limit_price == Decimal("0.55") and sig.taker


def test_min_book_depth_rejects_dead_quotes(settings) -> None:  # type: ignore[no-untyped-def]
    settings.odds_bookmakers = ["pinnacle", "draftkings"]
    strat = SportsbookArbStrategy(settings, odds_client=FakeOdds())  # type: ignore[arg-type]
    # maker joins the bid side: a 3-contract bid fails the 10-contract depth gate
    thin = make_market(
        "KXNCAAFGAME-26OCT06USMTROY-TROY", "0.50", "0.55", subtitle="Troy", yes_bid_size="3"
    )
    assert strat.evaluate([thin]) == []
    # a taker entry checks the ask side instead
    settings.entry_style = "taker"
    thin_ask = make_market(
        "KXNCAAFGAME-26OCT06USMTROY-TROY", "0.54", "0.55", subtitle="Troy", yes_ask_size="3"
    )
    assert strat.evaluate([thin_ask]) == []


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
    # Books: Troy ~0.66 / Southern Miss ~0.34. Troy YES at 0.55 is a favourite -> skipped;
    # Southern Miss YES maker-enters at 0.20 (bid 0.19 + 1c), an underdog in-band -> bought.
    troy = make_market("KXNCAAFGAME-26OCT06USMTROY-TROY", "0.50", "0.55", subtitle="Troy")
    usm = make_market("KXNCAAFGAME-26OCT06USMTROY-USM", "0.19", "0.22", subtitle="Southern Miss")
    sigs = strat.evaluate([troy, usm])
    assert [(s.ticker.rsplit("-", 1)[1], s.side) for s in sigs] == [("USM", OutcomeSide.yes)]
    assert sigs[0].strategy == "underdog_value"
    assert "5.0x payout" in sigs[0].rationale
    # an entry price above the 10-25c band (0.28 bid -> maker at 0.29) is skipped,
    # whatever the consensus says
    pricey = make_market("KXNCAAFGAME-26OCT06USMTROY-USM", "0.28", "0.30", subtitle="Southern Miss")
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


def test_min_lead_skips_feed_without_spending_odds_budget(settings) -> None:  # type: ignore[no-untyped-def]
    # an event whose market timing is already inside the lead window can't produce a
    # signal -- it must be skipped before the feed is even fetched, or the per-run
    # odds budget goes to leagues that are never bettable
    calls: list[str] = []

    class CountingOdds(FakeOdds):
        def h2h(self, sport: str, bookmakers: list[str] | None = None) -> list[dict[str, Any]]:
            calls.append(sport)
            return []

    strat = SportsbookArbStrategy(settings, odds_client=CountingOdds())  # type: ignore[arg-type]
    troy = make_market(
        "KXNCAAFGAME-26OCT06USMTROY-TROY",
        "0.50",
        "0.55",
        subtitle="Troy",
        expected_expiration=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    assert strat.evaluate([troy]) == []
    assert calls == []


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
        return httpx.Response(
            200,
            json=[EVENT],
            headers={
                "x-requests-used": "341",
                "x-requests-remaining": "9",
                "x-requests-last": "1",
            },
        )

    oc = OddsClient("k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert oc.h2h("americanfootball_ncaaf") == oc.h2h("americanfootball_ncaaf")
    assert calls == 1
    assert oc.requests_used == "341"
    assert oc.requests_remaining == "9"
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
        return httpx.Response(
            200,
            json=[],
            headers={"x-requests-remaining": "41", "x-requests-last": "1"},
        )

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

    # anchors must sit beyond min_lead_hours or the events are ineligible and no
    # longer compete for budget; lookahead is widened so "soon" still discriminates
    settings.odds_lookahead_hours = 200
    now = datetime.now(timezone.utc)
    soon, later = now + timedelta(hours=80), now + timedelta(hours=150)
    mk = [
        make_market("KXNHLGAME-X1-A", "0.4", "0.5", subtitle="A", expected_expiration=soon),
        make_market("KXEPLGAME-Y1-A", "0.4", "0.5", subtitle="A", expected_expiration=soon),
        make_market("KXEPLGAME-Y2-A", "0.4", "0.5", subtitle="A", expected_expiration=soon),
        make_market("KXEPLGAME-Y3-A", "0.4", "0.5", subtitle="A", expected_expiration=soon),
        make_market("KXMLBGAME-Z1-A", "0.4", "0.5", subtitle="A", expected_expiration=later),
        make_market("KXMLBGAME-Z2-A", "0.4", "0.5", subtitle="A", expected_expiration=later),
    ]
    odds = OneCall()
    strat = SportsbookArbStrategy(settings, odds_client=odds)  # type: ignore[arg-type]
    assert strat.evaluate(mk) == []
    assert odds.sports == ["soccer_epl"]


def test_close_only_event_does_not_outrank_timed_feeds(settings) -> None:  # type: ignore[no-untyped-def]
    # close_time trails kickoff by days, so it can't prove a game is bettable: a
    # close-only event sorts behind feeds with real expiry timing instead of
    # spending odds budget to discover its kickoff is too soon
    calls: list[str] = []

    class OneCall(FakeOdds):
        def h2h(self, sport: str, bookmakers: list[str] | None = None) -> list[dict[str, Any]]:
            if calls:
                raise OddsBudgetExhausted("spent")
            calls.append(sport)
            return []

    now = datetime.now(timezone.utc)
    nhl = make_market(
        "KXNHLGAME-X1-A",
        "0.4",
        "0.5",
        subtitle="A",
        close_time=now + timedelta(hours=79),  # earlier than the EPL expiry below
    )
    epl = make_market(
        "KXEPLGAME-Y1-A",
        "0.4",
        "0.5",
        subtitle="A",
        expected_expiration=now + timedelta(hours=80),
    )
    strat = SportsbookArbStrategy(settings, odds_client=OneCall())  # type: ignore[arg-type]
    strat.evaluate([nhl, epl])
    assert calls == ["soccer_epl"]


def test_close_only_outright_still_competes_for_budget(settings) -> None:  # type: ignore[no-untyped-def]
    # outrights have no kickoff separate from their market close -- the close IS the
    # resolution clock, so a close-only tournament still counts for priority instead
    # of sorting last like a close-only game
    calls: list[str] = []

    class OneCall(FakeOdds):
        def h2h(self, sport: str, bookmakers: list[str] | None = None) -> list[dict[str, Any]]:
            if calls:
                raise OddsBudgetExhausted("spent")
            calls.append(sport)
            return []

        def outrights(
            self, sport: str, bookmakers: list[str] | None = None
        ) -> list[dict[str, Any]]:
            if calls:
                raise OddsBudgetExhausted("spent")
            calls.append(sport)
            return []

    now = datetime.now(timezone.utc)
    outright = make_market(
        "KXMASTERS-27-SS",
        "0.13",
        "0.15",
        subtitle="Scottie Scheffler",
        close_time=now + timedelta(hours=79),  # earlier than the EPL expiry below
    )
    epl = make_market(
        "KXEPLGAME-Y1-A",
        "0.4",
        "0.5",
        subtitle="A",
        expected_expiration=now + timedelta(hours=80),
    )
    strat = SportsbookArbStrategy(settings, odds_client=OneCall())  # type: ignore[arg-type]
    strat.evaluate([outright, epl])
    assert calls == ["golf_masters_tournament_winner"]


def test_draw_leg_alone_matches_no_fixture(settings) -> None:  # type: ignore[no-untyped-def]
    settings.odds_bookmakers = ["pinnacle", "draftkings"]
    strat = SportsbookArbStrategy(settings, odds_client=FakeOdds([_soccer_event()]))  # type: ignore[arg-type]
    tie = make_market("KXEPLGAME-26OCT06CHEARS-TIE", "0.15", "0.18", subtitle="Tie")
    assert strat.evaluate([tie]) == []


def test_odds_pacer_resets_each_run_and_fails_closed() -> None:
    import httpx

    status = {"code": 200}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status["code"],
            json=[],
            headers={"x-requests-remaining": "10", "x-requests-last": "1"},
        )

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


def test_events_uses_free_window_without_spending_budget() -> None:
    import httpx

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/sports"):
            return httpx.Response(
                200,
                json=[],
                headers={"x-requests-used": "10", "x-requests-remaining": "100"},
            )
        return httpx.Response(
            200,
            json=[{"commence_time": "2026-10-05T12:00:00Z"}],
            headers={"x-requests-used": "10", "x-requests-remaining": "100"},
        )

    client = OddsClient("key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.quota_remaining() == 100
    before = client.requests_made
    events = client.events(
        "americanfootball_nfl",
        datetime(2026, 10, 1, tzinfo=timezone.utc),
        datetime(2026, 10, 8, tzinfo=timezone.utc),
    )
    assert events
    assert client.requests_made == before
    assert requests[-1].url.path.endswith("/sports/americanfootball_nfl/events")
    assert requests[-1].url.params["commenceTimeFrom"] == "2026-10-01T00:00:00Z"
    assert requests[-1].url.params["commenceTimeTo"] == "2026-10-08T00:00:00Z"


def test_events_cost_detection_disables_remaining_event_checks(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import httpx

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        used = "10" if request.url.path.endswith("/sports") else "12"
        return httpx.Response(
            200,
            json=[],
            headers={"x-requests-used": used, "x-requests-remaining": "100"},
        )

    monkeypatch.setattr("prediction_bot.odds.run_share", lambda *_args, **_kwargs: 2)
    ledger = Ledger(tmp_path / "events-cost-pacer.sqlite", Decimal("100"))
    client = OddsClient("key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    pacer = OddsPacer(client, runs_per_day=12, ledger=ledger)
    assert pacer.start_run(datetime(2026, 10, 1, tzinfo=timezone.utc)) == 2
    assert (
        client.events(
            "americanfootball_nfl",
            datetime(2026, 10, 1, tzinfo=timezone.utc),
            datetime(2026, 10, 8, tzinfo=timezone.utc),
        )
        == []
    )
    assert not client.events_free
    assert client.requests_made == 2
    assert client._events_requests_used_baseline == 12
    with pytest.raises(OddsBudgetExhausted):
        client.h2h("americanfootball_nfl")
    request_count = len(requests)
    assert (
        client.events(
            "americanfootball_ncaaf",
            datetime(2026, 10, 1, tzinfo=timezone.utc),
            datetime(2026, 10, 8, tzinfo=timezone.utc),
        )
        == []
    )
    assert len(requests) == request_count
    assert client.requests_made == 2
    pacer.finish_run()
    assert ledger.odds_pacer_carry() == 0


def test_ledger_pacer_carry_persists_and_caps(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[],
            headers={"x-requests-used": "10", "x-requests-remaining": "100"},
        )

    monkeypatch.setattr("prediction_bot.odds.run_share", lambda *_args, **_kwargs: 0.75)
    ledger = Ledger(tmp_path / "pacer.sqlite", Decimal("100"))
    first = OddsPacer(
        OddsClient("key", client=httpx.Client(transport=httpx.MockTransport(handler))),
        runs_per_day=12,
        ledger=ledger,
    )
    assert first.start_run(datetime(2026, 10, 1, tzinfo=timezone.utc)) == 0
    first.finish_run()
    assert ledger.odds_pacer_carry() == 0.75

    second = OddsPacer(
        OddsClient("key", client=httpx.Client(transport=httpx.MockTransport(handler))),
        runs_per_day=12,
        ledger=ledger,
    )
    second.client.requests_last_by_sport["americanfootball_nfl"] = 1
    assert second.start_run(datetime(2026, 10, 1, tzinfo=timezone.utc)) == 1
    assert second.client.requests_last_by_sport == {}
    second.client.requests_made = 1
    second.finish_run()
    assert ledger.odds_pacer_carry() == 0.5

    ledger.record_odds_pacer_carry(0.9)
    monkeypatch.setattr("prediction_bot.odds.run_share", lambda *_args, **_kwargs: 1.5)
    capped = OddsPacer(
        OddsClient("key", client=httpx.Client(transport=httpx.MockTransport(handler))),
        runs_per_day=12,
        ledger=ledger,
    )
    assert capped.start_run(datetime(2026, 10, 1, tzinfo=timezone.utc)) == 2
    capped.finish_run()
    assert ledger.odds_pacer_carry() == 1.0


def test_trigger_pacer_spends_carry_and_scheduled_run_recovers_debt(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import httpx

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        headers = {"x-requests-used": "10", "x-requests-remaining": "100"}
        if request.url.path.endswith("/odds"):
            headers["x-requests-last"] = "1"
        return httpx.Response(200, json=[], headers=headers)

    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "trigger-pacer.sqlite", Decimal("100"))
    ledger.record_odds_pacer_carry(0.5, now)
    trigger = OddsPacer(
        OddsClient("key", client=httpx.Client(transport=httpx.MockTransport(handler))),
        runs_per_day=12,
        ledger=ledger,
        trigger_mode=True,
    )
    assert trigger.start_run(now) == 1
    trigger.client.h2h("americanfootball_nfl")
    trigger.finish_run(now)
    assert ledger.odds_pacer_carry() == pytest.approx(-0.5)

    monkeypatch.setattr("prediction_bot.odds.run_share", lambda *_args, **_kwargs: 1.33)
    scheduled = OddsPacer(
        OddsClient("key", client=httpx.Client(transport=httpx.MockTransport(handler))),
        runs_per_day=12,
        ledger=ledger,
    )
    assert scheduled.start_run(now + timedelta(hours=2)) == 0
    scheduled.finish_run(now + timedelta(hours=2))
    assert ledger.odds_pacer_carry() == pytest.approx(0.83)

    ledger.record_odds_pacer_carry(-0.2, now)
    refused = OddsPacer(
        OddsClient("key", client=httpx.Client(transport=httpx.MockTransport(handler))),
        runs_per_day=12,
        ledger=ledger,
        trigger_mode=True,
    )
    assert refused.start_run(now) == 0
    assert sum(request.url.path.endswith("/odds") for request in requests) == 1
    ledger.record_odds_pacer_carry(2, now)
    assert ledger.odds_pacer_carry() == 1
    ledger.record_odds_pacer_carry(-2, now)
    assert ledger.odds_pacer_carry() == -1


def test_requests_last_other_than_one_halts_additional_odds_pulls() -> None:
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[],
            headers={
                "x-requests-used": "10",
                "x-requests-remaining": "100",
                "x-requests-last": "2",
            },
        )

    client = OddsClient("key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.budget = 3
    client.h2h("americanfootball_nfl")
    assert client.requests_last_by_sport["americanfootball_nfl"] == 2
    assert client.budget == 1
    with pytest.raises(OddsBudgetExhausted):
        client.h2h("americanfootball_ncaaf")


def test_requests_last_sums_multiple_responses_for_a_sport() -> None:
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[],
            headers={
                "x-requests-used": "10",
                "x-requests-remaining": "100",
                "x-requests-last": "1",
            },
        )

    client = OddsClient("key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.budget = 3
    client.h2h("americanfootball_nfl", bypass_cache=True)
    client.h2h("americanfootball_nfl", bypass_cache=True)

    assert client.requests_last_by_sport == {"americanfootball_nfl": 2}


def test_fractional_pacer_spends_at_most_sixteen_credits_over_twelve_runs(
    monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    import httpx

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[],
            headers={"x-requests-used": "0", "x-requests-remaining": "16"},
        )

    monkeypatch.setattr("prediction_bot.odds.run_share", lambda *_args, **_kwargs: 4 / 3)
    ledger = Ledger(tmp_path / "twelve-runs.sqlite", Decimal("100"))
    budgets: list[int] = []
    for slot in range(12):
        pacer = OddsPacer(
            OddsClient("key", client=httpx.Client(transport=httpx.MockTransport(handler))),
            runs_per_day=12,
            ledger=ledger,
        )
        budget = pacer.start_run(
            datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(hours=2 * slot)
        )
        budgets.append(budget)
        pacer.client.requests_made = budget
        pacer.finish_run()
    assert budgets[:3] == [1, 1, 2]
    assert sum(budgets) <= 16
