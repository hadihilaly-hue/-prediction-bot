from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from prediction_bot.config import Settings
from prediction_bot.models import Market
from prediction_bot.odds import OddsClient, consensus
from prediction_bot.paper.ledger import Ledger
from prediction_bot.strategies.sportsbook_v2 import (
    SportsbookV2Strategy,
    _parse_time,
    _ticker_date,
    _ticker_start_et,
)
from prediction_bot.venues.kalshi import parse_market
from tests.conftest import make_market as _make_market


def odds_event(
    event_id: str,
    home: str,
    away: str,
    commence: str,
    probabilities: dict[str, Decimal],
    *,
    updated: str | None = None,
) -> dict[str, object]:
    outcomes = [
        {"name": name, "price": str(Decimal(1) / probability)}
        for name, probability in probabilities.items()
    ]
    bookmakers: list[dict[str, object]] = [
        {
            "key": key,
            "markets": [{"key": "h2h", "outcomes": [dict(outcome) for outcome in outcomes]}],
        }
        for key in ("pinnacle", "circa", "betonlineag", "draftkings", "fanduel")
    ]
    if updated is not None:
        bookmakers[0]["last_update"] = updated
    return {
        "id": event_id,
        "home_team": home,
        "away_team": away,
        "commence_time": commence,
        "bookmakers": bookmakers,
    }


class FakeOdds:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events
        self.calls = 0
        self.bypass_calls = 0

    def h2h(self, sport, bookmakers=None, *, bypass_cache=False):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.bypass_calls += int(bypass_cache)
        return self.events


class FakeKalshi:
    def __init__(self, markets: dict[str, Market] | None = None) -> None:
        self.markets = markets or {}
        self.calls: list[str] = []

    def get_market(self, ticker: str) -> Market:
        self.calls.append(ticker)
        return self.markets[ticker]


class FakeFeeCache:
    def get(self, series: str) -> tuple[str, Decimal]:
        return "quadratic_with_maker_fees", Decimal(1)


_SportsbookV2Strategy = SportsbookV2Strategy


class SportsbookV2Strategy(_SportsbookV2Strategy):
    def __init__(self, settings, odds, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        if len(args) < 1:
            kwargs.setdefault("kalshi_client", FakeKalshi())
        if len(args) < 2:
            kwargs.setdefault("ledger", Ledger(":memory:", Decimal("1000")))
        super().__init__(settings, odds, *args, **kwargs)
        self.fee_cache = FakeFeeCache()


def make_market(*args, **kwargs):  # type: ignore[no-untyped-def]
    kwargs.setdefault("yes_bid_size", Decimal("100"))
    kwargs.setdefault("yes_ask_size", Decimal("100"))
    return _make_market(*args, **kwargs)


def fixed_clock(value: str):
    return lambda: datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_time_parsers_handle_utc_and_market_ticker_time() -> None:
    assert _parse_time("2026-10-25T20:25:00Z") == datetime(
        2026, 10, 25, 20, 25, tzinfo=timezone.utc
    )
    assert _parse_time("2026-10-25T20:25:00") == datetime(2026, 10, 25, 20, 25, tzinfo=timezone.utc)
    assert _parse_time("not-a-time") is None
    assert _ticker_date("KXMLBGAME-26OCT041600SDMIL") is not None
    assert _ticker_date("KXMLBGAME-invalid") is None
    encoded = _ticker_start_et("KXMLBGAME-26OCT041600SDMIL")
    assert encoded is not None
    assert encoded.astimezone(timezone.utc) == datetime(2026, 10, 4, 20, tzinfo=timezone.utc)
    assert _ticker_start_et("KXNFLGAME-26OCT04KCLV") is None


def test_settings_and_provider_parsers_preserve_v2_fields() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        shadow_strategies="sportsbook_arb, underdog_value",
    )
    assert settings.rules_version == "v1"
    assert settings.shadow_strategies == ["sportsbook_arb", "underdog_value"]
    assert settings.max_gross_edge == Decimal("0.07")
    assert settings.big_edge_whitelist == []
    assert settings.kickoff_buffer_minutes["americanfootball_nfl"] == 30
    assert settings.kickoff_buffer_default_minutes == 15
    assert settings.v2_position_bankroll_fraction == Decimal("0.02")
    assert settings.v2_event_bankroll_fraction == Decimal("0.02")
    assert settings.min_books == 2
    assert settings.max_book_spread == Decimal("0.03")
    assert settings.longshot_haircut == "slope"

    market = parse_market(
        {
            "ticker": "KXMLSGAME-26OCT25LAFCLAG-LAG",
            "event_ticker": "KXMLSGAME-26OCT25LAFCLAG",
            "yes_sub_title": "Los Angeles G",
            "last_price_dollars": "0.53",
        }
    )
    assert market.last_price == Decimal("0.53")
    assert market.event_ticker == "KXMLSGAME-26OCT25LAFCLAG"

    event = odds_event(
        "nfl",
        "Kansas City Chiefs",
        "Las Vegas Raiders",
        "2026-10-25T20:25:00Z",
        {"Kansas City Chiefs": Decimal("0.5"), "Las Vegas Raiders": Decimal("0.5")},
        updated="2026-10-23T06:55:00Z",
    )
    parsed = consensus(event, ["pinnacle"])
    assert parsed is not None
    assert parsed.book_updates == {"pinnacle": "2026-10-23T06:55:00Z"}


def test_odds_client_bypass_cache_for_approved_refetch() -> None:
    import httpx

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[])

    client = OddsClient("test-key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.h2h("americanfootball_nfl", ["pinnacle"])
    client.h2h("americanfootball_nfl", ["pinnacle"])
    assert calls == 1
    client.h2h("americanfootball_nfl", ["pinnacle"], bypass_cache=True)
    assert calls == 2


def test_v2_deduplicates_two_way_no_and_yes_to_canonical_position(settings) -> None:  # type: ignore[no-untyped-def]
    commence = "2026-10-25T20:25:00Z"
    settings.min_lead_hours = 0
    game = odds_event(
        "kc-lv",
        "Kansas City Chiefs",
        "Las Vegas Raiders",
        commence,
        {
            "Kansas City Chiefs": Decimal("0.2"),
            "Las Vegas Raiders": Decimal("0.8"),
        },
    )
    strategy = SportsbookV2Strategy(
        settings, FakeOdds([game]), clock=fixed_clock("2026-10-23T00:00:00Z")
    )
    kc = make_market(
        "KXNFLGAME-26OCT25KCLV-KC",
        "0.18",
        "0.21",
        subtitle="Kansas City",
    )
    lv = make_market(
        "KXNFLGAME-26OCT25KCLV-LV",
        "0.74",
        "0.76",
        subtitle="Las Vegas",
    )
    signals = strategy.evaluate([kc, lv])
    assert len(signals) == 1
    assert signals[0].ticker == lv.ticker
    assert signals[0].position_key == "KXNFLGAME-26OCT25KCLV:las-vegas-raiders"
    assert signals[0].rules_version == "v2"
    assert signals[0].event_ticker == "KXNFLGAME-26OCT25KCLV"
    assert signals[0].tags == ("value",)
    assert signals[0].commence_time == datetime(2026, 10, 25, 20, 25, tzinfo=timezone.utc)


def test_entry_window_candidate_is_shadowed_but_not_traded(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from prediction_bot.engine import Engine

    settings.min_lead_hours = 72
    commence = "2026-10-25T20:25:00Z"
    game = odds_event(
        "kc-lv",
        "Kansas City Chiefs",
        "Las Vegas Raiders",
        commence,
        {
            "Kansas City Chiefs": Decimal("0.2"),
            "Las Vegas Raiders": Decimal("0.8"),
        },
    )
    ledger = Ledger(tmp_path / "entry-window.sqlite", Decimal("1000"))
    strategy = SportsbookV2Strategy(
        settings,
        FakeOdds([game]),
        ledger=ledger,
        clock=fixed_clock("2026-10-23T00:00:00Z"),
    )
    markets = [
        make_market(
            "KXNFLGAME-26OCT25KCLV-KC",
            "0.20",
            "0.22",
            subtitle="Kansas City",
        ),
        make_market(
            "KXNFLGAME-26OCT25KCLV-LV",
            "0.74",
            "0.76",
            subtitle="Las Vegas",
        ),
    ]
    engine = Engine(settings, object(), [strategy], ledger)  # type: ignore[arg-type]
    assert engine.run_cycle(markets).acted == []
    assert ledger.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    decision = ledger.conn.execute(
        "SELECT rules_version,window,would_trade,reason FROM shadow_decisions"
    ).fetchone()
    assert decision == ("v2", "24-72h", 1, "entry_window")


def test_kickoff_guard_uses_sport_buffer(settings) -> None:  # type: ignore[no-untyped-def]
    cases = [
        (
            "KXNFLGAME-26OCT04KCLV",
            "Kansas City Chiefs",
            "Las Vegas Raiders",
            "KC",
            "LV",
            "Kansas City",
            "Las Vegas",
            "2026-10-04T20:25:00Z",
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
        ),
    ]
    for event_ticker, home, away, home_code, away_code, home_title, away_title, commence in cases:
        game = odds_event(
            event_ticker,
            home,
            away,
            commence,
            {home: Decimal("0.5"), away: Decimal("0.5")},
        )
        strategy = SportsbookV2Strategy(
            settings, FakeOdds([game]), clock=fixed_clock("2026-10-04T22:21:00Z")
        )
        markets = [
            make_market(
                f"{event_ticker}-{home_code}",
                "0.49",
                "0.5",
                subtitle=home_title,
            ),
            make_market(
                f"{event_ticker}-{away_code}",
                "0.49",
                "0.5",
                subtitle=away_title,
            ),
        ]
        assert strategy.evaluate(markets) == []
        assert any(block.reason == "kickoff_guard" for block in strategy.seen_blocks())


def test_big_edge_requires_whitelist_and_both_fresh_quotes(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    event_ticker = "KXNFLGAME-26OCT25MIAMIN"
    settings.min_lead_hours = 0
    mia = make_market(f"{event_ticker}-MIA", "0.77", "0.78", subtitle="Miami")
    minnesota = make_market(f"{event_ticker}-MIN", "0.21", "0.22", subtitle="Minnesota")
    game = odds_event(
        "mia-min",
        "Miami Dolphins",
        "Minnesota Vikings",
        "2026-10-25T20:05:00Z",
        {
            "Miami Dolphins": Decimal("0.88"),
            "Minnesota Vikings": Decimal("0.12"),
        },
    )
    settings.min_edge = Decimal("0.02")
    odds = FakeOdds([game])
    kalshi = FakeKalshi({mia.ticker: mia, minnesota.ticker: minnesota})
    ledger = Ledger(tmp_path / "big-edge.sqlite", Decimal("100"))
    strategy = SportsbookV2Strategy(
        settings,
        odds,
        kalshi,
        ledger,
        clock=fixed_clock("2026-10-23T00:00:00Z"),
    )
    from prediction_bot.engine import Engine

    result = Engine(settings, object(), [strategy], ledger).run_cycle([mia, minnesota])  # type: ignore[arg-type]
    assert result.signals == []
    assert len(kalshi.calls) >= 1
    assert odds.bypass_calls == 0
    assert ledger.conn.execute("SELECT COUNT(*) FROM edge_reviews").fetchone()[0] >= 1
    assert (
        ledger.conn.execute("SELECT COUNT(*) FROM blocks WHERE reason='big_edge'").fetchone()[0]
        >= 1
    )
    assert ledger.conn.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0] == 2

    settings.big_edge_whitelist = [mia.ticker]
    whitelisted_odds = FakeOdds([game])
    fresh_kalshi = FakeKalshi({mia.ticker: mia, minnesota.ticker: minnesota})
    whitelisted_ledger = Ledger(tmp_path / "whitelist.sqlite", Decimal("100"))
    whitelisted = SportsbookV2Strategy(
        settings,
        whitelisted_odds,
        fresh_kalshi,
        whitelisted_ledger,
        clock=fixed_clock("2026-10-23T00:00:00Z"),
    )
    result = Engine(settings, object(), [whitelisted], whitelisted_ledger).run_cycle(
        [mia, minnesota]
    )  # type: ignore[arg-type]
    assert result.acted
    assert whitelisted_odds.bypass_calls == 1
    reviews = whitelisted_ledger.conn.execute(
        "SELECT ticker, confirmed, whitelisted FROM edge_reviews"
    ).fetchall()
    assert any(row == (mia.ticker, 1, 1) for row in reviews)


def test_mls_mapping_uses_galaxy_fair_not_lafc_fair(settings) -> None:  # type: ignore[no-untyped-def]
    event_ticker = "KXMLSGAME-26OCT25LAFCLAG"
    settings.min_lead_hours = 0
    settings.longshot_haircut = "none"
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
        settings, FakeOdds([game]), clock=fixed_clock("2026-10-23T00:00:00Z")
    )
    lafc = make_market(f"{event_ticker}-LAFC", "0.59", "0.60", subtitle="Los Angeles F")
    lag = make_market(f"{event_ticker}-LAG", "0.13", "0.14", subtitle="Los Angeles G")
    tie = make_market(f"{event_ticker}-TIE", "0.19", "0.20", subtitle="Tie")
    signals = strategy.evaluate([lafc, lag, tie])
    galaxy = next(signal for signal in signals if signal.ticker == lag.ticker)
    assert galaxy.fair_prob < Decimal("0.2")
    assert galaxy.fair_pre_haircut is not None
    assert abs(galaxy.fair_pre_haircut - Decimal("0.186")) < Decimal("0.001")
    assert galaxy.position_key == f"{event_ticker}:la-galaxy"
    assert not any(
        block.reason in {"mapping_failed", "mapping_sum"} for block in strategy.seen_blocks()
    )


def test_missing_alias_fails_closed_and_ticker_start_mismatch(settings) -> None:  # type: ignore[no-untyped-def]
    game = odds_event(
        "sd-mil",
        "San Diego Padres",
        "Milwaukee Brewers",
        "2026-10-04T20:00:00Z",
        {
            "San Diego Padres": Decimal("0.5"),
            "Milwaukee Brewers": Decimal("0.5"),
        },
    )
    strategy = SportsbookV2Strategy(
        settings, FakeOdds([game]), clock=fixed_clock("2026-10-04T22:21:00Z")
    )
    sd = make_market(
        "KXMLBGAME-26OCT041600SDMIL-UNMAPPED",
        "0.49",
        "0.5",
        subtitle="San Diego",
    )
    mil = make_market(
        "KXMLBGAME-26OCT041600SDMIL-MIL",
        "0.49",
        "0.5",
        subtitle="Milwaukee",
    )
    assert strategy.evaluate([sd, mil]) == []
    assert any(block.reason == "mapping_failed" for block in strategy.seen_blocks())
    assert (
        strategy._start_mismatch(
            sd.event_ticker,
            [sd, mil],
            datetime(2026, 10, 4, 22, tzinfo=timezone.utc),
        )
        == "MLB ticker start differs from commence by more than 60 minutes"
    )
    assert (
        strategy._start_mismatch(
            "KXNFLGAME-26OCT25KCLV",
            [
                make_market(
                    "KXNFLGAME-26OCT25KCLV-KC",
                    "0.49",
                    "0.5",
                    subtitle="Kansas City",
                    expected_expiration=datetime(2026, 10, 26, 2, tzinfo=timezone.utc),
                )
            ],
            datetime(2026, 10, 25, 20, tzinfo=timezone.utc),
        )
        is None
    )
    bad_expiry = make_market(
        "KXNFLGAME-26OCT25KCLV-KC",
        "0.49",
        "0.5",
        subtitle="Kansas City",
        expected_expiration=datetime(2026, 10, 26, 3, tzinfo=timezone.utc),
    )
    assert (
        strategy._start_mismatch(
            "KXNFLGAME-26OCT25KCLV",
            [bad_expiry],
            datetime(2026, 10, 25, 20, tzinfo=timezone.utc),
        )
        == "Kalshi expected expiration is outside the allowed game-end window"
    )
    lad_atl = odds_event(
        "lad-atl",
        "Los Angeles Dodgers",
        "Atlanta Braves",
        "2026-10-06T19:00:00Z",
        {
            "Los Angeles Dodgers": Decimal("0.5"),
            "Atlanta Braves": Decimal("0.5"),
        },
    )
    baseball = SportsbookV2Strategy(
        settings,
        FakeOdds([lad_atl]),
        clock=fixed_clock("2026-10-06T10:00:00Z"),
    )
    assert (
        baseball.evaluate(
            [
                make_market(
                    "KXMLBGAME-26OCT061800LADATL-UNMAPPED",
                    "0.49",
                    "0.5",
                    subtitle="Los Angeles",
                ),
                make_market(
                    "KXMLBGAME-26OCT061800LADATL-ATL",
                    "0.49",
                    "0.5",
                    subtitle="Atlanta",
                ),
            ]
        )
        == []
    )
    assert any(block.reason == "mapping_failed" for block in baseball.seen_blocks())


def test_in_play_rules_use_first_quote_and_consensus_snapshots(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from prediction_bot.models import QuoteSnapshot

    ledger = Ledger(tmp_path / "in-play.sqlite", Decimal("100"))
    before = datetime(2026, 10, 1, tzinfo=timezone.utc)
    prior = odds_event(
        "edm-sj",
        "Edmonton Oilers",
        "San Jose Sharks",
        "2026-10-30T03:00:00Z",
        {
            "Edmonton Oilers": Decimal("0.5"),
            "San Jose Sharks": Decimal("0.5"),
        },
    )
    from prediction_bot.odds import consensus

    prior_consensus = consensus(prior, ["pinnacle"])
    assert prior_consensus is not None
    ledger.record_consensus({"icehockey_nhl": {"edm-sj": prior_consensus}}, at=before)
    ticker = "KXNHLGAME-26OCT10EDMSJ-EDM"
    ledger.record_quote_snapshots(
        [
            QuoteSnapshot(
                ticker=ticker,
                event_ticker=ticker.rsplit("-", 1)[0],
                yes_bid=Decimal("0.49"),
                yes_ask=Decimal("0.5"),
                last_price=Decimal("0.5"),
                volume=Decimal(1),
                open_interest=Decimal(1),
                created_at=before,
            )
        ]
    )
    current = odds_event(
        "edm-sj",
        "Edmonton Oilers",
        "San Jose Sharks",
        "2026-10-30T03:00:00Z",
        {
            "Edmonton Oilers": Decimal("0.956"),
            "San Jose Sharks": Decimal("0.044"),
        },
    )
    current_consensus = consensus(current, ["pinnacle"])
    assert current_consensus is not None
    market = make_market(ticker, "0.01", "0.02", subtitle="Edmonton")
    strategy = SportsbookV2Strategy(
        settings,
        FakeOdds([current]),
        ledger=ledger,
        clock=fixed_clock("2026-10-24T00:00:00Z"),
    )
    reasons = {
        reason
        for reason, _ in strategy._in_play_reasons(
            ticker.rsplit("-", 1)[0],
            current_consensus,
            datetime(2026, 10, 30, 3, tzinfo=timezone.utc),
            [market],
            datetime(2026, 10, 24, tzinfo=timezone.utc),
        )
    }
    assert "in_play:consensus_extreme" in reasons
    assert "in_play:consensus_jump" in reasons
    assert "in_play:kalshi_extreme" in reasons
