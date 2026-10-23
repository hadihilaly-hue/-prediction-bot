from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast

from prediction_bot.cli import _snapshot_markets
from prediction_bot.clv import record_clv
from prediction_bot.config import Settings
from prediction_bot.dashboard import _mean_ci95, dashboard_data
from prediction_bot.engine import Engine
from prediction_bot.models import Market, OutcomeSide, Quote, Signal
from prediction_bot.odds_planner import PlannedGame, plan_odds_pulls
from prediction_bot.paper.ledger import Ledger, decision_window
from prediction_bot.strategies.base import Strategy
from scripts.odds_budget import _cumulative_deltas


def _market(
    ticker: str,
    expires: datetime,
    *,
    yes_bid: Decimal = Decimal("0.40"),
    yes_ask: Decimal = Decimal("0.42"),
    bid_size: Decimal = Decimal("20"),
    ask_size: Decimal = Decimal("25"),
    open_interest: Decimal = Decimal("0"),
    volume: Decimal = Decimal("100"),
    last_price: Decimal = Decimal("0.41"),
    event_ticker: str = "KXNFLGAME-26OCT20KCLV",
) -> Market:
    return Market(
        ticker=ticker,
        event_ticker=event_ticker,
        title="Home",
        subtitle="Home",
        status="open",
        close_time=None,
        quote=Quote(ticker, yes_bid, yes_ask, bid_size, ask_size),
        expected_expiration=expires,
        volume=volume,
        open_interest=open_interest,
        last_price=last_price,
    )


def test_snapshot_markets_only_fetches_series_missing_from_scanner_data() -> None:
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    nfl = _market(
        "KXNFLGAME-26OCT04KCLV-KC",
        now + timedelta(days=3),
        event_ticker="KXNFLGAME-26OCT04KCLV",
    )
    nba = _market(
        "KXNBAGAME-26OCT04KCLV-LAL",
        now + timedelta(days=3),
        event_ticker="KXNBAGAME-26OCT04KCLV",
    )
    calls: list[list[str]] = []

    def fetch_missing(series: list[str]) -> list[Market]:
        calls.append(series)
        return [nba]

    assert _snapshot_markets([nfl], fetch_missing, now, ("KXNFLGAME",)) == [nfl]
    assert calls == []
    markets = _snapshot_markets(
        [nfl],
        fetch_missing,
        now,
        ("KXNFLGAME", "KXNBAGAME"),
    )
    assert calls == [["KXNBAGAME"]]
    assert {market.ticker for market in markets} == {nfl.ticker, nba.ticker}


def test_odds_budget_turns_cumulative_usage_into_non_reset_deltas() -> None:
    runs = [
        (382, datetime(2026, 10, 1, 8, tzinfo=timezone.utc), 100),
        (383, datetime(2026, 10, 2, 8, tzinfo=timezone.utc), 105),
        (384, datetime(2026, 10, 3, 8, tzinfo=timezone.utc), 4),
        (385, datetime(2026, 10, 4, 8, tzinfo=timezone.utc), 11),
        (386, datetime(2026, 11, 1, 8, tzinfo=timezone.utc), 20),
        (387, datetime(2026, 11, 2, 8, tzinfo=timezone.utc), 25),
    ]

    assert _cumulative_deltas(runs) == [
        (383, 382, runs[1][1], 5),
        (385, 384, runs[3][1], 7),
        (387, 386, runs[5][1], 5),
    ]


def test_market_snapshots_write_changes_and_six_hour_heartbeat(tmp_path: Path) -> None:
    at = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    ticker = "KXNFLGAME-26OCT20KCLV-HOME"
    ledger = Ledger(tmp_path / "snapshots.sqlite", Decimal("1000"))
    market = _market(ticker, at + timedelta(days=7))

    assert ledger.record_market_snapshots([market], at=at) == 1
    unchanged_non_trigger_fields = _market(
        ticker,
        at + timedelta(days=7),
        bid_size=Decimal("21"),
        ask_size=Decimal("26"),
        volume=Decimal("200"),
        last_price=Decimal("0.39"),
    )
    assert (
        ledger.record_market_snapshots(
            [unchanged_non_trigger_fields], at=at + timedelta(hours=5, minutes=59)
        )
        == 0
    )
    changed_oi = _market(
        ticker,
        at + timedelta(days=7),
        open_interest=Decimal("1"),
        volume=Decimal("200"),
        last_price=Decimal("0.39"),
    )
    assert ledger.record_market_snapshots([changed_oi], at=at + timedelta(hours=2)) == 1
    assert ledger.record_market_snapshots([changed_oi], at=at + timedelta(hours=5)) == 0
    assert ledger.record_market_snapshots([changed_oi], at=at + timedelta(hours=8)) == 1
    assert (
        ledger.conn.execute(
            "SELECT COUNT(*) FROM market_snapshots WHERE ticker=?", (ticker,)
        ).fetchone()[0]
        == 3
    )


def test_market_snapshots_keep_only_expirations_within_eight_days(tmp_path: Path) -> None:
    at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "snapshot-window.sqlite", Decimal("1000"))
    markets = [
        _market("KXNFLGAME-26OCT01-A", at + timedelta(days=7)),
        _market("KXNFLGAME-26OCT01-B", at + timedelta(days=8)),
        _market("KXNFLGAME-26OCT01-C", at + timedelta(days=8, seconds=1)),
        _market("KXNFLGAME-26SEP30-D", at - timedelta(seconds=1)),
        _market(
            "KXWNBAGAME-26OCT01-E",
            at + timedelta(days=3),
            event_ticker="KXWNBAGAME-26OCT01",
        ),
    ]
    assert ledger.record_market_snapshots(markets, at=at) == 2


def test_entry_window_bucket_boundaries() -> None:
    created = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert decision_window(created, created + timedelta(hours=72)) == "72h+"
    assert decision_window(created, created + timedelta(hours=72, minutes=-1)) == "24-72h"
    assert decision_window(created, created + timedelta(hours=24)) == "24-72h"
    assert decision_window(created, created + timedelta(hours=24, minutes=-1)) == "2-24h"
    assert decision_window(created, created + timedelta(hours=2)) == "2-24h"
    assert decision_window(created, created + timedelta(hours=2, minutes=-1)) == "<2h"


def test_normal_approximation_ci_is_null_below_five_samples() -> None:
    assert _mean_ci95([1.0, 2.0, 3.0, 4.0]) is None
    interval = _mean_ci95([1.0, 2.0, 3.0, 4.0, 5.0])
    assert interval is not None
    assert interval[0] < 3.0 < interval[1]


def test_planner_prioritizes_event_windows_and_obeys_budget() -> None:
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    games = {
        "soccer_usa_mls": [PlannedGame(now + timedelta(hours=24))],
        "baseball_mlb": [PlannedGame(now + timedelta(hours=6))],
        "basketball_nba": [PlannedGame(now + timedelta(minutes=90))],
        "americanfootball_nfl": [PlannedGame(now + timedelta(hours=96))],
    }
    assert plan_odds_pulls(games, now, remaining_credits=60, days_left_in_month=10) == [
        "basketball_nba",
        "baseball_mlb",
    ]


def test_planner_falls_back_to_entry_window_and_pending_reverify() -> None:
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    games = {
        "americanfootball_nfl": [PlannedGame(now + timedelta(hours=96))],
        "basketball_nba": [PlannedGame(now + timedelta(hours=48))],
    }
    assert plan_odds_pulls(games, now, 1000, 10) == ["americanfootball_nfl"]
    games["icehockey_nhl"] = [PlannedGame(now + timedelta(hours=48), large_gap_pending=True)]
    assert plan_odds_pulls(games, now, 1000, 10)[0] == "icehockey_nhl"
    assert plan_odds_pulls(games, now, 6, 10) == []


def test_fixed_planner_remains_the_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("PBOT_ODDS_PLANNER", raising=False)
    assert Settings().odds_planner == "fixed"


def test_engine_attributes_entry_window_shadow_to_v3(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    created = datetime(2026, 10, 1, tzinfo=timezone.utc)
    signal = Signal(
        strategy="sportsbook_v3",
        ticker="KXNFLGAME-26OCT04KCLV-KC",
        side=OutcomeSide.yes,
        fair_prob=Decimal("0.70"),
        limit_price=Decimal("0.50"),
        edge=Decimal("0.20"),
        size=Decimal(1),
        created_at=created,
        event_ticker="KXNFLGAME-26OCT04KCLV",
        commence_time=created + timedelta(hours=80),
    )

    class V3WithShadowDecision(Strategy):
        name = "sportsbook_v3"

        def evaluate(self, markets):  # type: ignore[no-untyped-def]
            return []

        def seen_shadow_decisions(self) -> list[Signal]:
            return [signal]

    ledger = Ledger(tmp_path / "v3-window.sqlite", Decimal("1000"))
    Engine(
        settings,
        object(),  # type: ignore[arg-type]
        [V3WithShadowDecision(settings)],
        ledger,
    ).run_cycle([])
    version, window = ledger.conn.execute(
        "SELECT rules_version,window FROM shadow_decisions"
    ).fetchone()
    assert version == "v3"
    assert window == "72h+"


def test_dashboard_exposes_versioned_window_clv_and_null_small_sample_ci(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "window-clv.sqlite", Decimal("1000"))
    created = datetime(2026, 10, 1, tzinfo=timezone.utc)
    commence = created + timedelta(hours=80)
    for version, count in (("v3", 5), ("v2", 4)):
        for index in range(count):
            ticker = f"KXNFLGAME-26OCT04-{version}-{index}"
            signal = Signal(
                strategy=f"sportsbook_{version}",
                ticker=ticker,
                side=OutcomeSide.yes,
                fair_prob=Decimal("0.70"),
                limit_price=Decimal("0.50"),
                edge=Decimal("0.20"),
                size=Decimal(1),
                created_at=created,
                rules_version=version,
                position_key=f"{ticker}:home",
                event_ticker="KXNFLGAME-26OCT04",
                commence_time=commence,
            )
            ledger.record_shadow_decision(signal, version, reason="entry_window")
            ledger.conn.execute(
                "UPDATE shadow_decisions SET clv_kalshi=?,clv_exit=?,clv_status='ok' "
                "WHERE ticker=?",
                (str(index + 1), str(index + 2), ticker),
            )
    near_miss = Signal(
        strategy="sportsbook_v3",
        ticker="KXNFLGAME-26OCT04-v3-near-miss",
        side=OutcomeSide.yes,
        fair_prob=Decimal("0.70"),
        limit_price=Decimal("0.50"),
        edge=Decimal("-0.01"),
        size=Decimal(0),
        created_at=created,
        rules_version="v3",
        position_key="KXNFLGAME-26OCT04:v3-near-miss",
        event_ticker="KXNFLGAME-26OCT04",
        commence_time=commence,
    )
    assert ledger.record_near_miss_decisions([(near_miss, "entry_window")]) == 1
    ledger.conn.execute(
        "UPDATE shadow_decisions SET clv_kalshi='99',clv_exit='99',clv_status='ok' WHERE ticker=?",
        (near_miss.ticker,),
    )
    ledger.conn.commit()

    measurement = cast(dict[str, object], dashboard_data(ledger)["measurement"])
    quality = cast(dict[str, object], measurement["quality"])
    summary = cast(dict[str, object], quality["entry_window_clv"])
    v3 = cast(dict[str, object], summary["v3"])
    v2 = cast(dict[str, object], summary["v2"])
    v3_window = cast(dict[str, object], v3["72h+"])
    v2_window = cast(dict[str, object], v2["72h+"])
    assert v3_window["n"] == 5
    assert v3_window["ci95"] is not None
    assert v3_window["mean_clv_exit"] == 4
    assert v2_window["n"] == 4
    assert v2_window["ci95"] is None
    assert (
        next(
            row
            for row in cast(list[dict[str, object]], quality["shadow_decisions"])
            if row["version"] == "v3" and row["window"] == "72h+"
        )["n"]
        == 5
    )


def test_record_clv_scores_v3_entry_window_shadow_and_exit_bid(tmp_path: Path) -> None:
    created = datetime(2026, 10, 17, 20, tzinfo=timezone.utc)
    commence = datetime(2026, 10, 20, 20, tzinfo=timezone.utc)
    ticker = "KXNFLGAME-26OCT20KCLV-KC"
    signal = Signal(
        strategy="sportsbook_v3",
        ticker=ticker,
        side=OutcomeSide.yes,
        fair_prob=Decimal("0.70"),
        limit_price=Decimal("0.50"),
        edge=Decimal("0.20"),
        size=Decimal(1),
        created_at=created,
        rules_version="v3",
        position_key="KXNFLGAME-26OCT20KCLV:kc",
        event_ticker="KXNFLGAME-26OCT20KCLV",
        commence_time=commence,
    )
    ledger = Ledger(tmp_path / "entry-clv.sqlite", Decimal("1000"))
    ledger.record_shadow_decision(signal, "v3", reason="entry_window")

    class Candles:
        def get_candlesticks(
            self,
            series_ticker: str,
            market_ticker: str,
            start_ts: int,
            end_ts: int,
            period_interval: int = 1,
        ) -> list[dict[str, object]]:
            assert series_ticker == "KXNFLGAME"
            assert market_ticker == ticker
            at = commence - timedelta(minutes=5)
            return [
                {
                    "end_period_ts": int(at.timestamp()),
                    "yes_bid": {"close_dollars": "0.60"},
                    "yes_ask": {"close_dollars": "0.62"},
                }
            ]

    assert record_clv(ledger, Candles(), now=commence + timedelta(hours=1)) == 1  # type: ignore[arg-type]
    clv, clv_exit = ledger.conn.execute(
        "SELECT clv_kalshi,clv_exit FROM shadow_decisions WHERE ticker=?", (ticker,)
    ).fetchone()
    assert Decimal(clv) == Decimal("11.00")
    assert Decimal(clv_exit) == Decimal("10.0")
