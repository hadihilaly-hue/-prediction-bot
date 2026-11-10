"""Scoreboard (PR 1): favoured side, game-clustered verdicts net of fees, Polymarket
game-winner matching and line_move_follower recorded once at the ask."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from prediction_bot.clv_report import (
    MIN_GAMES,
    clustered_mean,
    clv_report,
    format_report,
    summarize_rows,
    verdict,
)
from prediction_bot.engine import Engine
from prediction_bot.models import Market, OutcomeSide, Quote, Signal
from prediction_bot.paper.ledger import Ledger
from prediction_bot.reference_quotes import TrackedEvent, match_tokens
from prediction_bot.strategies.base import Strategy
from prediction_bot.venues.polymarket import PolyEvent, PolyMarket

KICK = datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc)


def _shadow(
    ledger: Ledger,
    *,
    ticker: str,
    side: str = "yes",
    price: str,
    mid: str | None,
    fair: str,
    clv: str,
    model_side: int | None = None,
    reason: str = "net_edge",
    position_key: str | None = None,
    created_at: str = "2026-11-01T00:00:00+00:00",
) -> None:
    ledger.conn.execute(
        "INSERT INTO shadow_decisions (created_at,rules_version,window,strategy,ticker,side,"
        "position_key,would_trade,reason,fair,price,mid,model_side,clv_kalshi,clv_status,"
        "commence_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            created_at,
            "v3",
            "72h+",
            "sportsbook_v3",
            ticker,
            side,
            position_key or ticker,
            0,
            reason,
            fair,
            price,
            mid,
            model_side,
            clv,
            "ok",
            KICK.isoformat(),
        ),
    )


# --- 1.4 favoured side vs both sides -----------------------------------------------


def test_two_sided_cohort_reads_minus_half_spread_on_all_rows_and_drift_on_favoured(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    """Both teams of each game logged: their CLVs sum to minus the spread, so the
    all-rows mean is -spread/2 whatever the model; the favoured side shows the drift."""
    ledger = Ledger(tmp_path / "p.sqlite", Decimal("100"))
    spread, drift = Decimal("0.02"), Decimal("0.015")  # 2c spread; favoured mid moves +1.5c
    for game in range(40):
        event = f"KXNFLGAME-26NOV{game:02d}AAABBB"
        mid_a = Decimal("0.55")
        # Side A: favoured (fair above mid), mid moves +drift; side B mirrors it.
        for team, mid, fair, move in (
            ("AAA", mid_a, mid_a + Decimal("0.015"), drift),
            ("BBB", 1 - mid_a, 1 - mid_a - Decimal("0.015"), -drift),
        ):
            price = mid + spread / 2
            _shadow(
                ledger,
                ticker=f"{event}-{team}",
                price=str(price),
                mid=str(mid),
                fair=str(fair),
                clv=str((mid + move - price) * 100),
                model_side=1 if fair > mid else 0,
            )
    ledger.conn.commit()
    cohort = clv_report(ledger.conn)["cohorts"]["v3_net_edge"]
    assert cohort["all_rows"]["mean"] == pytest.approx(-1.0)  # -spread/2, in cents
    assert cohort["all_rows"]["n"] == 80 and cohort["all_rows"]["games"] == 40
    headline = cohort["metrics"]["clv_kalshi"]["all"]
    assert headline["n"] == 40 and headline["games"] == 40
    assert headline["mean_move"] == pytest.approx(1.5)  # the true mid-to-mid drift
    assert headline["mean"] == pytest.approx(0.5)  # drift minus half the spread
    buckets = {b["bucket"]: b for b in cohort["dose_response"]}
    assert buckets["1..2"]["n"] == 40 and buckets["1..2"]["mean_move"] == pytest.approx(1.5)
    assert buckets["-2..-1"]["n"] == 40 and buckets["-2..-1"]["mean_move"] == pytest.approx(-1.5)
    text = format_report(clv_report(ledger.conn))
    assert "both sides: equals minus half the spread by construction" in text
    assert "dose-response" in text and "Verdict rule:" in text


def test_favoured_side_derived_from_fair_and_mid_when_column_is_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "p.sqlite", Decimal("100"))
    _shadow(ledger, ticker="KXNFLGAME-X-A", price="0.51", mid="0.50", fair="0.52", clv="1")
    _shadow(ledger, ticker="KXNFLGAME-X-B", price="0.51", mid="0.50", fair="0.49", clv="-1")
    _shadow(ledger, ticker="KXNFLGAME-X-C", price="0.51", mid=None, fair="0.49", clv="-1")
    ledger.conn.commit()
    cohort = clv_report(ledger.conn)["cohorts"]["v3_net_edge"]
    assert cohort["favoured_rows"] == 1 and cohort["unknown_side_rows"] == 1


# --- 1.2 / 1.3 clustering, fees and the verdict rule --------------------------------


def test_clustered_se_matches_a_hand_computed_case() -> None:
    values = [1.0, 3.0, 2.0, 6.0, 4.0]
    clusters = ["g1", "g1", "g2", "g2", "g3"]
    mean = 16.0 / 5  # 3.2
    sums = {"g1": (1 - mean) + (3 - mean), "g2": (2 - mean) + (6 - mean), "g3": 4 - mean}
    expected_se = math.sqrt(3 / 2 * sum(v**2 for v in sums.values()) / 25)
    out = clustered_mean(values, clusters)
    assert out["mean"] == pytest.approx(mean)
    assert out["se"] == pytest.approx(expected_se)
    assert out["ci95"] == pytest.approx([mean - 1.96 * expected_se, mean + 1.96 * expected_se])
    # Every row its own cluster: CR1 reduces to sd/sqrt(n) up to the n/(n-1) factor.
    iid = clustered_mean(values, [str(i) for i in range(5)])
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / 4)
    assert iid["se"] == pytest.approx(sd / math.sqrt(5))
    assert clustered_mean([1.0, 2.0], ["g", "g"])["se"] is None


def _rows(values: list[float], fee: float, games: int) -> list[dict[str, object]]:
    return [
        {"clv_kalshi": v, "fee": fee, "event": f"g{i % games}", "position_key": f"p{i}"}
        for i, v in enumerate(values)
    ]


def test_positive_gross_cohort_that_loses_after_the_taker_fee_is_not_supported() -> None:
    # +0.65c gross, tight enough to be "supported" on gross, minus a 1.42c taker fee.
    values = [0.65 + (0.1 if i % 2 else -0.1) for i in range(120)]
    summary = summarize_rows(_rows(values, 1.42, 60))
    assert summary["mean"] == pytest.approx(0.65)
    assert summary["mean_net"] == pytest.approx(-0.77)
    assert summary["ci95_gross"][0] > 0
    assert summary["verdict"] == "rejected"
    assert summary["verdict"] != "supported"


def test_verdict_needs_thirty_games_whatever_the_row_count() -> None:
    values = [2.0 + (0.1 if i % 2 else -0.1) for i in range(400)]
    few = summarize_rows(_rows(values, 0.0, MIN_GAMES - 1))
    assert few["n"] == 400 and few["games"] == MIN_GAMES - 1 and few["verdict"] == "open"
    enough = summarize_rows(_rows(values, 0.0, MIN_GAMES))
    assert enough["verdict"] == "supported"
    assert verdict(MIN_GAMES, [-1.0, -0.1]) == "rejected"
    assert verdict(MIN_GAMES, [-1.0, 0.1]) == "open"


def test_report_nets_the_series_fee_by_cohort_style(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "p.sqlite", Decimal("100"))
    ledger.save_series_fee("KXNFLGAME", "quadratic_with_maker_fees", Decimal(1), KICK)
    _shadow(
        ledger, ticker="KXNFLGAME-X-A", price="0.50", mid="0.49", fair="0.52", clv="1", model_side=1
    )
    ledger.conn.execute(
        "INSERT INTO maker_shadow (created_at,ticker,side,position_key,price,size,fair,maker_fee,"
        "status,status_at,event_ticker,expires_at,commence_time,rules_version,fill_queue_at,"
        "clv_kalshi,clv_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "2026-11-01T00:00:00+00:00",
            "KXNFLGAME-X-A",
            "yes",
            "KXNFLGAME-X:a",
            "0.50",
            "1",
            "0.52",
            "0.0044",
            "filled",
            "2026-11-01T00:00:00+00:00",
            "KXNFLGAME-X",
            KICK.isoformat(),
            KICK.isoformat(),
            "v3",
            "2026-11-01T01:00:00+00:00",
            "1",
            "ok",
        ),
    )
    ledger.conn.commit()
    report = clv_report(ledger.conn)
    taker = report["cohorts"]["v3_net_edge"]["metrics"]["clv_kalshi"]["all"]
    maker = report["cohorts"]["maker_queue"]["metrics"]["clv_kalshi"]["all"]
    assert taker["fee"] == pytest.approx(1.75)  # 0.07 * 0.5 * 0.5, in cents
    assert taker["mean_net"] == pytest.approx(1 - 1.75)
    assert maker["fee"] == pytest.approx(0.4375)  # 0.0175 * 0.5 * 0.5
    assert maker["mean_net"] == pytest.approx(1 - 0.4375)


# --- 1.1 model_side on new rows and the backfill ---------------------------------------


def test_near_miss_rows_store_the_mid_and_model_side(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "p.sqlite", Decimal("100"))
    base = {
        "strategy": "sportsbook_v3",
        "side": OutcomeSide.yes,
        "limit_price": Decimal("0.51"),
        "edge": Decimal("-0.01"),
        "size": Decimal(0),
        "rules_version": "v3",
        "created_at": KICK - timedelta(days=4),
        "commence_time": KICK,
        "mid": Decimal("0.50"),
    }
    favoured = Signal(ticker="KXNFLGAME-X-A", fair_prob=Decimal("0.505"), position_key="a", **base)
    other = Signal(ticker="KXNFLGAME-X-B", fair_prob=Decimal("0.49"), position_key="b", **base)
    assert ledger.record_near_miss_decisions([(favoured, "net_edge"), (other, "net_edge")]) == 2
    rows = ledger.conn.execute(
        "SELECT ticker,mid,model_side FROM shadow_decisions ORDER BY ticker"
    ).fetchall()
    assert rows == [("KXNFLGAME-X-A", "0.50", 1), ("KXNFLGAME-X-B", "0.50", 0)]


def test_quote_mid_for_side() -> None:
    quote = Quote("T", Decimal("0.40"), Decimal("0.44"))
    assert quote.mid_for(OutcomeSide.yes) == Decimal("0.42")
    assert quote.mid_for(OutcomeSide.no) == Decimal("0.58")
    assert Quote("T", None, Decimal("0.44")).mid_for(OutcomeSide.yes) is None


def _snapshot(ledger: Ledger, ticker: str, at: datetime, bid: str, ask: str) -> None:
    ledger.conn.execute(
        "INSERT INTO market_snapshots (fetched_at,ticker,event_ticker,series,yes_bid,yes_ask,"
        "yes_bid_size,yes_ask_size,open_interest,volume,expected_expiration) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            at.isoformat(),
            ticker,
            ticker.rsplit("-", 1)[0],
            "KXNHLGAME",
            bid,
            ask,
            "1",
            "1",
            "1",
            "1",
            (KICK + timedelta(hours=3)).isoformat(),
        ),
    )


def test_backfill_uses_the_nearest_snapshot_within_five_minutes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "p.sqlite", Decimal("100"))
    at = KICK - timedelta(days=3)
    for ticker, side, fair in (
        ("KXNHLGAME-X-NYR", "yes", "0.52"),
        ("KXNHLGAME-X-BOS", "no", "0.52"),
        ("KXNHLGAME-Y-NYR", "yes", "0.52"),
    ):
        _shadow(
            ledger,
            ticker=ticker,
            side=side,
            price="0.52",
            mid=None,
            fair=fair,
            clv="0",
            created_at=at.isoformat(),
        )
    _snapshot(ledger, "KXNHLGAME-X-NYR", at - timedelta(minutes=4), "0.48", "0.50")  # mid .49
    _snapshot(ledger, "KXNHLGAME-X-NYR", at + timedelta(minutes=1), "0.50", "0.52")  # nearer
    _snapshot(ledger, "KXNHLGAME-X-BOS", at + timedelta(minutes=2), "0.50", "0.52")  # NO .49
    _snapshot(ledger, "KXNHLGAME-Y-NYR", at - timedelta(minutes=6), "0.40", "0.42")  # too old
    ledger.conn.commit()
    assert ledger.backfill_model_side() == 2
    rows = dict(
        (r[0], (r[1], r[2]))
        for r in ledger.conn.execute("SELECT ticker,mid,model_side FROM shadow_decisions")
    )
    assert rows["KXNHLGAME-X-NYR"] == ("0.51", 1)
    assert rows["KXNHLGAME-X-BOS"] == ("0.49", 1)
    assert rows["KXNHLGAME-Y-NYR"] == (None, None)


def test_scoreboard_migrations_run_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "p.sqlite"
    ledger = Ledger(path, Decimal("100"))
    ledger.conn.execute("DELETE FROM ledger_migrations")
    for ticker in ("KXMLSGAME-26NOV01VANSEA-SEA", "KXNHLGAME-26NOV01NYRBOS-NYR"):
        ledger.conn.execute(
            "INSERT INTO shadow_decisions (created_at,window,strategy,ticker,side,would_trade,"
            "reason,clv_polymarket) VALUES (?,?,?,?,?,?,?,?)",
            ("2026-11-01T00:00:00+00:00", "72h+", "s", ticker, "yes", 0, "net_edge", "1.0"),
        )
        ledger.record_reference_quote(
            ticker.rsplit("-", 1)[0], "x", "polymarket", Decimal("0.5"), None, None, "t", KICK
        )
    ledger.conn.execute(
        "INSERT INTO shadow_decisions (created_at,window,strategy,ticker,side,would_trade,"
        "reason,tags) VALUES (?,?,?,?,?,?,?,?)",
        (
            "2026-11-01T00:00:00+00:00",
            "72h+",
            "line_move_follower",
            "KXNHLGAME-Z-A",
            "yes",
            1,
            "line_move_follower",
            "line_move_follower",
        ),
    )
    ledger.conn.commit()
    ledger.close()
    reopened = Ledger(path, Decimal("100"))
    poly = dict(
        reopened.conn.execute(
            "SELECT ticker,clv_polymarket FROM shadow_decisions WHERE clv_polymarket IS NOT NULL"
        ).fetchall()
    )
    assert poly == {"KXMLSGAME-26NOV01VANSEA-SEA": "1.0"}
    assert [r[0] for r in reopened.conn.execute("SELECT event_ticker FROM reference_quotes")] == [
        "KXMLSGAME-26NOV01VANSEA"
    ]
    assert reopened.conn.execute(
        "SELECT tags FROM shadow_decisions WHERE reason='line_move_follower'"
    ).fetchone() == ("line_move_follower,legacy_double_mid",)
    # A second open changes nothing (new non-MLS quotes written later survive).
    reopened.record_reference_quote(
        "KXNHLGAME-26NOV02NYRBOS", "x", "polymarket", Decimal("0.5"), None, None, "t", KICK
    )
    reopened.close()
    again = Ledger(path, Decimal("100"))
    assert again.conn.execute("SELECT COUNT(*) FROM reference_quotes").fetchone() == (2,)
    assert again.conn.execute("SELECT COUNT(*) FROM ledger_migrations").fetchone() == (3,)


# --- 1.5 Polymarket game-winner market -------------------------------------------------

NHL_EVENT = TrackedEvent(
    "KXNHLGAME-26NOV08NYRBOS",
    "icehockey_nhl",
    KICK,
    frozenset({"new-york-rangers", "boston-bruins"}),
)


def _pm(question: str, tokens: tuple[str, str], market_type: str = "") -> PolyMarket:
    return PolyMarket(question, ("Rangers", "Bruins"), tokens, "", KICK, market_type)


@pytest.mark.parametrize("typed", [True, False])
def test_polymarket_picks_the_moneyline_over_a_spread_market(typed: bool) -> None:
    title = "Rangers vs. Bruins"
    spread = _pm("Spread: Rangers (-1.5)", ("s1", "s2"), "spreads" if typed else "")
    moneyline = _pm(title, ("m1", "m2"), "moneyline" if typed else "")
    # The spread market comes last as well as first: neither order may let it win.
    for markets in ((spread, moneyline), (moneyline, spread)):
        tokens, _ = match_tokens(NHL_EVENT, [PolyEvent(title, KICK, markets)])
        assert tokens == {"new-york-rangers": "m1", "boston-bruins": "m2"}


def test_polymarket_event_with_only_a_spread_market_is_unmatched() -> None:
    title = "Rangers vs. Bruins"
    event = PolyEvent(title, KICK, (_pm("Spread: Rangers (-1.5)", ("s1", "s2"), "spreads"),))
    assert match_tokens(NHL_EVENT, [event])[0] == {}


class _Poly:
    requests = 0

    def __init__(self, books: dict[str, tuple[Decimal, Decimal]], event: PolyEvent) -> None:
        self.books, self.event = books, event

    def events(self, tag_slug: str) -> list[PolyEvent]:
        self.requests += 1
        return [self.event]

    def book(self, token_id: str) -> tuple[Decimal, Decimal]:
        self.requests += 1
        return self.books[token_id]


def test_poll_rejects_wide_books_and_quotes_far_from_kalshi(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from prediction_bot.config import Settings
    from prediction_bot.reference_quotes import poll_reference_quotes

    now = KICK - timedelta(minutes=13)
    ledger = Ledger(tmp_path / "p.sqlite", Decimal("100"))
    event = "KXNHLGAME-26NOV08NYRBOS"
    _snapshot(ledger, f"{event}-NYR", now - timedelta(minutes=5), "0.54", "0.56")
    _snapshot(ledger, f"{event}-BOS", now - timedelta(minutes=5), "0.44", "0.46")
    ledger.conn.execute(
        "INSERT INTO shadow_decisions (created_at,window,strategy,ticker,side,would_trade,reason,"
        "commence_time) VALUES (?,?,?,?,?,?,?,?)",
        (
            (now - timedelta(hours=1)).isoformat(),
            "<2h",
            "s",
            f"{event}-NYR",
            "yes",
            0,
            "net_edge",
            KICK.isoformat(),
        ),
    )
    ledger.conn.commit()
    poly_event = PolyEvent(
        "Rangers vs. Bruins", KICK, (_pm("Rangers vs. Bruins", ("m1", "m2"), "moneyline"),)
    )
    books = {
        "m1": (Decimal("0.30"), Decimal("0.32")),  # 24c off Kalshi's 0.55: a mismatch
        "m2": (Decimal("0.40"), Decimal("0.50")),  # 10c wide
    }
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert poll_reference_quotes(ledger, _Poly(books, poly_event), settings, now=now) == 0  # type: ignore[arg-type]
    reasons = {
        r[0] for r in ledger.conn.execute("SELECT reason FROM reference_unmatched").fetchall()
    }
    # One reference_unmatched row per event and day; the first rejection is logged.
    assert reasons == {"wide_book"}
    assert ledger.conn.execute("SELECT COUNT(*) FROM reference_quotes").fetchone() == (0,)
    good = {"m1": (Decimal("0.54"), Decimal("0.56")), "m2": (Decimal("0.44"), Decimal("0.46"))}
    assert poll_reference_quotes(ledger, _Poly(good, poly_event), settings, now=now) == 2  # type: ignore[arg-type]


# --- 1.6 line_move_follower once, at the ask -------------------------------------------


class _LineMover(Strategy):
    def __init__(self, settings, name: str, signal: Signal) -> None:  # type: ignore[no-untyped-def]
        super().__init__(settings)
        self.name = name
        self.signal = signal

    def evaluate(self, markets):  # type: ignore[no-untyped-def]
        return []

    def seen_line_move_decisions(self) -> list[Signal]:
        return [self.signal]


def test_line_move_follower_writes_one_row_when_both_loops_see_it(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "p.sqlite", Decimal("1000"))
    sig = Signal(
        strategy="line_move_follower",
        ticker="KXNFLGAME-26NOV08ARIDAL-ARI",
        side=OutcomeSide.yes,
        fair_prob=Decimal("0.55"),
        limit_price=Decimal("0.52"),
        edge=Decimal("0.03"),
        size=Decimal(1),
        rules_version="v2",
        position_key="KXNFLGAME-26NOV08ARIDAL:arizona-cardinals",
        commence_time=KICK,
        mid=Decimal("0.51"),
    )
    engine = Engine(
        settings,
        object(),  # type: ignore[arg-type]
        [_LineMover(settings, "sportsbook_v3", sig)],
        ledger,
        shadow_strategies=[_LineMover(settings, "sportsbook_v2", sig)],
    )
    engine.run_cycle([])
    rows = ledger.conn.execute(
        "SELECT price,mid FROM shadow_decisions WHERE reason='line_move_follower'"
    ).fetchall()
    assert rows == [("0.52", "0.51")]


def test_line_move_detection_prices_at_the_ask(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from prediction_bot.odds import ConsensusOdds
    from prediction_bot.strategies.sportsbook_v2 import SportsbookV2Strategy

    ledger = Ledger(tmp_path / "p.sqlite", Decimal("1000"))
    now = KICK - timedelta(days=4)
    ticker = "KXNFLGAME-26NOV08ARIDAL-ARI"
    strat = SportsbookV2Strategy.__new__(SportsbookV2Strategy)
    strat.ledger = ledger  # type: ignore[assignment]
    strat.settings = settings.model_copy(update={"exp_line_move_points": Decimal("0.02")})
    strat._line_move_decisions = []
    ledger.previous_consensus = lambda event_id, at: {"Arizona Cardinals": Decimal("0.50")}  # type: ignore[method-assign]
    ledger.previous_quote_snapshot = lambda t, at: (Decimal("0.49"), Decimal("0.51"))  # type: ignore[method-assign]
    game = ConsensusOdds(
        "e1",
        "Dallas Cowboys",
        "Arizona Cardinals",
        KICK.isoformat(),
        {"Arizona Cardinals": Decimal("0.55")},
        3,
    )
    market = Market(
        ticker=ticker,
        event_ticker="KXNFLGAME-26NOV08ARIDAL",
        title="",
        subtitle="Arizona",
        status="active",
        close_time=None,
        quote=Quote(ticker, Decimal("0.49"), Decimal("0.52")),
    )
    strat._record_line_move(
        "KXNFLGAME-26NOV08ARIDAL",
        game,
        [market],
        {ticker: "arizona-cardinals"},
        {"arizona-cardinals": "Arizona Cardinals"},
        KICK,
        now,
    )
    [sig] = strat._line_move_decisions
    assert sig.limit_price == Decimal("0.52") and sig.mid == Decimal("0.505")
