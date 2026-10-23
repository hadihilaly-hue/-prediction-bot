from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from prediction_bot.engine import Engine
from prediction_bot.models import QuoteSnapshot
from prediction_bot.odds import consensus
from prediction_bot.paper.ledger import Ledger
from tests.test_engine_v2 import FixedSignals, v2_signal
from tests.test_sportsbook_v2 import (
    FakeOdds,
    SportsbookV2Strategy,
    fixed_clock,
    make_market,
    odds_event,
)


def _append_book(event: dict[str, object], key: str, probabilities: dict[str, Decimal]) -> None:
    bookmakers = event["bookmakers"]
    assert isinstance(bookmakers, list)
    bookmaker = {
        "key": key,
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {"name": name, "price": str(Decimal(1) / probability)}
                    for name, probability in probabilities.items()
                ],
            }
        ],
    }
    existing = next((item for item in bookmakers if item.get("key") == key), None)
    if existing is None:
        bookmakers.append(bookmaker)
    else:
        existing.update(bookmaker)


@pytest.mark.parametrize(
    (
        "event_ticker",
        "home",
        "away",
        "home_code",
        "away_code",
        "home_title",
        "away_title",
        "commence",
        "now",
    ),
    [
        (
            "KXMLBGAME-26OCT041600SDMIL",
            "San Diego Padres",
            "Milwaukee Brewers",
            "SD",
            "MIL",
            "San Diego",
            "Milwaukee",
            "2026-10-04T20:00:00Z",
            "2026-10-04T22:21:00Z",
        ),
        (
            "KXNFLGAME-26OCT04KCLV",
            "Kansas City Chiefs",
            "Las Vegas Raiders",
            "KC",
            "LV",
            "Kansas City",
            "Las Vegas",
            "2026-10-04T20:25:00Z",
            "2026-10-04T22:21:00Z",
        ),
        (
            "KXNFLGAME-26OCT04DENSF",
            "Denver Broncos",
            "San Francisco 49ers",
            "DEN",
            "SF",
            "Denver",
            "San Francisco",
            "2026-10-04T20:25:00Z",
            "2026-10-04T22:21:00Z",
        ),
        (
            "KXNFLGAME-26OCT04MIAMIN",
            "Miami Dolphins",
            "Minnesota Vikings",
            "MIA",
            "MIN",
            "Miami",
            "Minnesota",
            "2026-10-04T20:05:00Z",
            "2026-10-04T22:21:00Z",
        ),
        (
            "KXMLBGAME-26OCT061800LADATL",
            "Los Angeles Dodgers",
            "Atlanta Braves",
            "LAD",
            "ATL",
            "Los Angeles",
            "Atlanta",
            "2026-10-06T22:00:00Z",
            "2026-10-06T23:05:00Z",
        ),
    ],
)
def test_kickoff_guard_regression_cases(
    settings,
    monkeypatch: pytest.MonkeyPatch,
    event_ticker: str,
    home: str,
    away: str,
    home_code: str,
    away_code: str,
    home_title: str,
    away_title: str,
    commence: str,
    now: str,
) -> None:  # type: ignore[no-untyped-def]
    if event_ticker.startswith("KXMLBGAME-"):
        ids = {
            "SD": "san-diego-padres",
            "MIL": "milwaukee-brewers",
            "LAD": "los-angeles-dodgers",
            "ATL": "atlanta-braves",
        }
        names = {
            "San Diego Padres": "san-diego-padres",
            "Milwaukee Brewers": "milwaukee-brewers",
            "Los Angeles Dodgers": "los-angeles-dodgers",
            "Atlanta Braves": "atlanta-braves",
        }
        monkeypatch.setattr(
            "prediction_bot.strategies.sportsbook_v2.team_for_code",
            lambda series, code: ids.get(code),
        )
        monkeypatch.setattr(
            "prediction_bot.strategies.sportsbook_v2.team_for_odds_name",
            lambda sport, name: names.get(name),
        )
        monkeypatch.setattr(
            "prediction_bot.strategies.sportsbook_v2.kalshi_name_ok",
            lambda series, team_id, title: True,
        )

    game = odds_event(
        event_ticker,
        home,
        away,
        commence,
        {home: Decimal("0.5"), away: Decimal("0.5")},
    )
    strategy = SportsbookV2Strategy(
        settings,
        FakeOdds([game]),
        clock=fixed_clock(now),
    )
    markets = [
        make_market(
            f"{event_ticker}-{home_code}",
            "0.49",
            "0.50",
            subtitle=home_title,
        ),
        make_market(
            f"{event_ticker}-{away_code}",
            "0.49",
            "0.50",
            subtitle=away_title,
        ),
    ]

    assert strategy.evaluate(markets) == []
    assert any(block.reason == "kickoff_guard" for block in strategy.seen_blocks())


@pytest.mark.parametrize(
    (
        "sport",
        "event_ticker",
        "event_id",
        "home",
        "away",
        "home_code",
        "away_code",
        "home_title",
        "away_title",
        "commence",
        "now",
        "current_home_prob",
        "current_away_prob",
        "home_bid",
        "home_ask",
    ),
    [
        (
            "icehockey_nhl",
            "KXNHLGAME-26OCT10EDMSJ",
            "edm-sj",
            "Edmonton Oilers",
            "San Jose Sharks",
            "EDM",
            "SJ",
            "Edmonton",
            "San Jose",
            "2026-10-10T23:00:00Z",
            "2026-10-09T18:00:00Z",
            Decimal("0.044"),
            Decimal("0.956"),
            "0.99",
            "0.99",
        ),
        (
            "americanfootball_ncaaf",
            "KXNCAAFGAME-26OCT03AUBTENN",
            "auburn-tennessee",
            "Auburn Tigers",
            "Tennessee Volunteers",
            "AUB",
            "TENN",
            "Auburn",
            "Tennessee",
            "2026-10-03T21:00:00Z",
            "2026-10-03T15:00:00Z",
            Decimal("0.094"),
            Decimal("0.906"),
            "0.06",
            "0.07",
        ),
    ],
)
def test_future_commence_in_play_detector_uses_snapshot_history(
    settings,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    sport: str,
    event_ticker: str,
    event_id: str,
    home: str,
    away: str,
    home_code: str,
    away_code: str,
    home_title: str,
    away_title: str,
    commence: str,
    now: str,
    current_home_prob: Decimal,
    current_away_prob: Decimal,
    home_bid: str,
    home_ask: str,
) -> None:  # type: ignore[no-untyped-def]
    if event_id == "edm-sj":
        ids = {"EDM": "edmonton-oilers", "SJ": "san-jose-sharks"}
        names = {
            "Edmonton Oilers": "edmonton-oilers",
            "San Jose Sharks": "san-jose-sharks",
        }
        monkeypatch.setattr(
            "prediction_bot.strategies.sportsbook_v2.team_for_code",
            lambda series, code: ids.get(code),
        )
        monkeypatch.setattr(
            "prediction_bot.strategies.sportsbook_v2.team_for_odds_name",
            lambda sport, name: names.get(name),
        )
        monkeypatch.setattr(
            "prediction_bot.strategies.sportsbook_v2.kalshi_name_ok",
            lambda series, team_id, title: True,
        )
    ledger = Ledger(tmp_path / f"{event_id}.sqlite", Decimal("100"))
    before = datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(hours=1)
    prior = odds_event(
        event_id,
        home,
        away,
        commence,
        {home: Decimal("0.5"), away: Decimal("0.5")},
    )
    prior_consensus = consensus(prior, settings.odds_bookmakers)
    assert prior_consensus is not None
    ledger.record_consensus({sport: {event_id: prior_consensus}}, at=before)
    home_ticker = f"{event_ticker}-{home_code}"
    market = make_market(home_ticker, home_bid, home_ask, subtitle=home_title)
    other_market = make_market(
        f"{event_ticker}-{away_code}",
        "0.49",
        "0.50",
        subtitle=away_title,
    )
    ledger.record_quote_snapshots(
        [
            QuoteSnapshot(
                ticker=home_ticker,
                event_ticker=event_ticker,
                yes_bid=Decimal("0.49"),
                yes_ask=Decimal("0.50"),
                last_price=Decimal("0.50"),
                volume=Decimal(1),
                open_interest=Decimal(1),
                created_at=before,
            )
        ]
    )
    current = odds_event(
        event_id,
        home,
        away,
        commence,
        {home: current_home_prob, away: current_away_prob},
    )
    current_consensus = consensus(current, settings.odds_bookmakers)
    assert current_consensus is not None
    strategy = SportsbookV2Strategy(
        settings,
        FakeOdds([current]),
        ledger=ledger,
        clock=fixed_clock(now),
    )

    assert strategy.evaluate([market, other_market]) == []
    reasons = {block.reason for block in strategy.seen_blocks()}
    assert "in_play:consensus_jump" in reasons
    if event_id == "edm-sj":
        assert "in_play:kalshi_extreme" in reasons


def test_mls_galaxy_fair_and_mapping_failures(settings) -> None:  # type: ignore[no-untyped-def]
    settings.min_lead_hours = 0
    settings.longshot_haircut = "none"
    event_ticker = "KXMLSGAME-26OCT25LAFCLAG"
    game = odds_event(
        "lafc-lag",
        "Los Angeles FC",
        "LA Galaxy",
        "2026-10-25T20:00:00Z",
        {
            "Los Angeles FC": Decimal("0.614"),
            "LA Galaxy": Decimal("0.186"),
            "Draw": Decimal("0.2"),
        },
    )
    strategy = SportsbookV2Strategy(
        settings,
        FakeOdds([game]),
        clock=fixed_clock("2026-10-23T00:00:00Z"),
    )
    lafc = make_market(f"{event_ticker}-LAFC", "0.59", "0.60", subtitle="Los Angeles F")
    galaxy = make_market(f"{event_ticker}-LAG", "0.13", "0.14", subtitle="Los Angeles G")
    tie = make_market(f"{event_ticker}-TIE", "0.19", "0.20", subtitle="Tie")

    signals = strategy.evaluate([lafc, galaxy, tie])
    galaxy_signal = next(signal for signal in signals if signal.ticker == galaxy.ticker)
    assert galaxy_signal.fair_prob.quantize(Decimal("0.001")) == Decimal("0.186")

    unmapped = make_market(f"{event_ticker}-UNKNOWN", "0.49", "0.50", subtitle="Unknown")
    strategy.evaluate([lafc, galaxy, tie, unmapped])
    assert any(block.reason == "mapping_failed" for block in strategy.seen_blocks())


def test_mls_swapped_fairs_fail_mapping_sum(settings) -> None:  # type: ignore[no-untyped-def]
    settings.min_lead_hours = 0
    event_ticker = "KXMLSGAME-26OCT25LAFCLAG"
    game = odds_event(
        "lafc-lag-swapped",
        "Los Angeles FC",
        "LA Galaxy",
        "2026-10-25T20:00:00Z",
        {
            "Los Angeles FC": Decimal("0.186"),
            "LA Galaxy": Decimal("0.614"),
            "Draw": Decimal("0.2"),
        },
    )
    strategy = SportsbookV2Strategy(
        settings,
        FakeOdds([game]),
        clock=fixed_clock("2026-10-23T00:00:00Z"),
    )

    strategy.evaluate(
        [
            make_market(
                f"{event_ticker}-LAFC",
                "0.59",
                "0.60",
                subtitle="Los Angeles F",
            ),
            make_market(
                f"{event_ticker}-LAG",
                "0.13",
                "0.14",
                subtitle="Los Angeles G",
            ),
        ]
    )

    assert any(block.reason == "mapping_sum" for block in strategy.seen_blocks())


@pytest.mark.parametrize("case", ["miami", "ucla", "sfsea"])
def test_big_edge_cases_persist_review_and_block(settings, tmp_path, case: str) -> None:  # type: ignore[no-untyped-def]
    settings.min_lead_hours = 0
    if case == "miami":
        event_ticker = "KXNFLGAME-26OCT04MIAMIN"
        home, away = "Miami Dolphins", "Minnesota Vikings"
        commence, now = "2026-10-04T20:05:00Z", "2026-10-04T12:00:00Z"
        probs = {home: Decimal("0.78"), away: Decimal("0.22")}
        second_probs = {home: Decimal("0.79"), away: Decimal("0.21")}
        targets = [
            ("MIA", "Miami", "0.87", "0.88"),
            ("MIN", "Minnesota", "0.21", "0.22"),
        ]
        edge_ticker = f"{event_ticker}-MIA"
    elif case == "ucla":
        event_ticker = "KXNCAAFGAME-26OCT10UCLAORE"
        home, away = "UCLA Bruins", "Oregon Ducks"
        commence, now = "2026-10-10T20:00:00Z", "2026-10-04T00:00:00Z"
        probs = {home: Decimal("0.11"), away: Decimal("0.89")}
        second_probs = None
        targets = [
            ("UCLA", "UCLA", "0.182", "0.192"),
            ("ORE", "Oregon", "0.88", "0.89"),
        ]
        edge_ticker = f"{event_ticker}-UCLA"
    else:
        event_ticker = "KXNFLGAME-26OCT11SFSEA"
        home, away = "San Francisco 49ers", "Seattle Seahawks"
        commence, now = "2026-10-11T20:25:00Z", "2026-10-04T00:00:00Z"
        probs = {home: Decimal("0.53"), away: Decimal("0.47")}
        second_probs = {home: Decimal("0.54"), away: Decimal("0.46")}
        targets = [
            ("SF", "San Francisco", "0.605", "0.615"),
            ("SEA", "Seattle", "0.455", "0.465"),
        ]
        edge_ticker = f"{event_ticker}-SF"

    game = odds_event(f"{case}-event", home, away, commence, probs)
    if second_probs is not None:
        _append_book(game, "circa", second_probs)
    markets = [
        make_market(
            f"{event_ticker}-{code}",
            bid,
            ask,
            subtitle=title,
        )
        for code, title, bid, ask in targets
    ]
    ledger = Ledger(tmp_path / f"{case}.sqlite", Decimal("100"))
    strategy = SportsbookV2Strategy(
        settings,
        FakeOdds([game]),
        ledger=ledger,
        clock=fixed_clock(now),
    )
    result = Engine(settings, object(), [strategy], ledger).run_cycle(markets)  # type: ignore[arg-type]

    assert result.signals == []
    assert (
        ledger.conn.execute("SELECT COUNT(*) FROM blocks WHERE reason='big_edge'").fetchone()[0]
        >= 1
    )
    assert (
        ledger.conn.execute(
            "SELECT COUNT(*) FROM edge_reviews WHERE ticker=?", (edge_ticker,)
        ).fetchone()[0]
        >= 1
    )


def test_entry_window_uses_matched_commence_time_and_zero_disables(settings) -> None:  # type: ignore[no-untyped-def]
    event_ticker = "KXNFLGAME-26OCT25KCLV"
    game = odds_event(
        "kc-lv-entry-window",
        "Kansas City Chiefs",
        "Las Vegas Raiders",
        "2026-10-25T20:25:00Z",
        {
            "Kansas City Chiefs": Decimal("0.55"),
            "Las Vegas Raiders": Decimal("0.45"),
        },
    )
    markets = [
        make_market(f"{event_ticker}-KC", "0.49", "0.50", subtitle="Kansas City"),
        make_market(f"{event_ticker}-LV", "0.49", "0.50", subtitle="Las Vegas"),
    ]
    now = "2026-10-23T00:00:00Z"
    strategy = SportsbookV2Strategy(
        settings,
        FakeOdds([game]),
        clock=fixed_clock(now),
    )

    assert strategy.evaluate(markets) == []
    assert any(block.reason == "entry_window" for block in strategy.seen_blocks())

    settings.min_lead_hours = 0
    disabled = SportsbookV2Strategy(
        settings,
        FakeOdds([game]),
        clock=fixed_clock(now),
    )
    assert disabled.evaluate(markets)
    assert not any(block.reason == "entry_window" for block in disabled.seen_blocks())


def test_hand_verified_lac_alias_matches_kalshi_subtitle(settings) -> None:  # type: ignore[no-untyped-def]
    settings.min_lead_hours = 0
    event_ticker = "KXNFLGAME-26OCT18LACKC"
    game = odds_event(
        "lac-kc",
        "Los Angeles Chargers",
        "Kansas City Chiefs",
        "2026-10-18T20:25:00Z",
        {
            "Los Angeles Chargers": Decimal("0.5"),
            "Kansas City Chiefs": Decimal("0.5"),
        },
    )
    strategy = SportsbookV2Strategy(
        settings,
        FakeOdds([game]),
        clock=fixed_clock("2026-10-18T10:00:00Z"),
    )

    strategy.evaluate(
        [
            make_market(
                f"{event_ticker}-LAC",
                "0.49",
                "0.50",
                subtitle="Los Angeles C",
            ),
            make_market(
                f"{event_ticker}-KC",
                "0.49",
                "0.50",
                subtitle="Kansas City",
            ),
        ]
    )

    assert not any(
        block.reason in {"mapping_failed", "title_mismatch"} for block in strategy.seen_blocks()
    )


def test_quote_snapshots_exclude_unsupported_series(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "supported-snapshots.sqlite", Decimal("100"))
    strategy = SportsbookV2Strategy(
        settings,
        FakeOdds([]),
        ledger=ledger,
        clock=fixed_clock("2026-10-23T00:00:00Z"),
    )
    supported = make_market(
        "KXNFLGAME-26OCT25KCLV-LV",
        "0.49",
        "0.50",
        subtitle="Las Vegas",
    )
    ufc = make_market(
        "KXUFCFIGHT-26OCT25FIGHT-A",
        "0.49",
        "0.50",
        subtitle="Fighter A",
    )
    golf = make_market(
        "KXMASTERS-26OCT25GOLF-A",
        "0.49",
        "0.50",
        subtitle="Golfer A",
    )

    strategy.evaluate([supported, ufc, golf])
    ledger.record_quote_snapshots(strategy.seen_quotes())

    rows = ledger.conn.execute("SELECT ticker FROM quote_snapshots").fetchall()
    assert rows == [(supported.ticker,)]


def test_kclv_opposite_legs_collapse_and_engine_caps_across_cycles(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings.min_lead_hours = 0
    event_ticker = "KXNFLGAME-26OCT25KCLV"
    game = odds_event(
        "kc-lv",
        "Kansas City Chiefs",
        "Las Vegas Raiders",
        "2026-10-25T20:25:00Z",
        {
            "Kansas City Chiefs": Decimal("0.2"),
            "Las Vegas Raiders": Decimal("0.8"),
        },
    )
    kc = make_market(f"{event_ticker}-KC", "0.18", "0.21", subtitle="Kansas City")
    lv = make_market(f"{event_ticker}-LV", "0.74", "0.76", subtitle="Las Vegas")
    strategy = SportsbookV2Strategy(
        settings,
        FakeOdds([game]),
        clock=fixed_clock("2026-10-23T00:00:00Z"),
    )

    signals = strategy.evaluate([kc, lv])

    assert len(signals) == 1
    assert signals[0].ticker == lv.ticker
    assert signals[0].position_key == f"{event_ticker}:las-vegas-raiders"

    ledger = Ledger(tmp_path / "kclv-engine.sqlite", Decimal("1000"))
    market = make_market(lv.ticker, "0.49", "0.50", subtitle="Las Vegas")
    first = v2_signal(size="10")
    engine = Engine(
        settings,
        object(),
        [FixedSignals(settings, [first])],
        ledger,
    )  # type: ignore[arg-type]
    assert engine.run_cycle([market]).acted == [first]

    too_large = v2_signal(size="50")
    engine = Engine(
        settings,
        object(),
        [FixedSignals(settings, [too_large])],
        ledger,
    )  # type: ignore[arg-type]
    assert engine.run_cycle([market]).acted == []
    assert ledger.conn.execute("SELECT reason FROM blocks").fetchone()[0] == "cap_position"


def test_utep_price_floor_candidate_fails_closed_on_unmapped_team(settings) -> None:  # type: ignore[no-untyped-def]
    event_ticker = "KXNCAAFGAME-26OCT03UTEPUNM"
    settings.min_lead_hours = 0
    game = odds_event(
        "utep-unm",
        "UTEP Miners",
        "New Mexico Lobos",
        "2026-10-03T20:00:00Z",
        {"UTEP Miners": Decimal("0.5"), "New Mexico Lobos": Decimal("0.5")},
    )
    strategy = SportsbookV2Strategy(
        settings,
        FakeOdds([game]),
        clock=fixed_clock("2026-10-03T16:00:00Z"),
    )

    # UTEP is unmapped, so this 6¢ market is blocked at mapping_failed, not price_floor.
    strategy.evaluate(
        [
            make_market(f"{event_ticker}-UTEP", "0.05", "0.06", subtitle="UTEP"),
            make_market(f"{event_ticker}-UNM", "0.49", "0.50", subtitle="New Mexico"),
        ]
    )

    assert any(
        block.reason == "mapping_failed" and block.ticker.startswith(event_ticker)
        for block in strategy.seen_blocks()
    )
