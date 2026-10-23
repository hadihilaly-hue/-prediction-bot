from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from prediction_bot.clv import (
    _quote_at,
    backfill_clv_commence_times,
    clv_status,
    game_day_target,
    record_clv,
    refresh_clv_statuses,
)
from prediction_bot.config import Settings
from prediction_bot.maker_scoring import score_maker_shadows
from prediction_bot.models import MakerShadowCandidate, OutcomeSide, Signal
from prediction_bot.paper.ledger import Ledger

FIXTURES = Path(__file__).parent / "fixtures"


def test_clv_status_rules_and_buffer_boundary() -> None:
    assert clv_status("2026-10-04T22:21Z", "2026-10-04T20:00Z", 15, True) == "in_game"
    assert clv_status("2026-10-04T18:00Z", None, 15, True) == "no_commence"
    assert clv_status("2026-10-01T20:00:00+00:00", "2026-10-04T20:00:00Z", 15, True) == "ok"
    assert clv_status("2026-10-01T20:00Z", "2026-10-04T20:00Z", 15, False) == "pending"
    assert clv_status("2026-10-04T19:45Z", "2026-10-04T20:00Z", 15, True) == "in_game"
    assert clv_status("2026-10-04T19:44:59Z", "2026-10-04T20:00Z", 15, True) == "ok"


def test_commence_backfill_prefers_kalshi_and_never_overwrites_existing(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "commence-backfill.sqlite", Decimal("1000"))
    event_ticker = "KXMLBGAME-26OCT041600SDMIL"
    missing_mlb_ticker = "KXMLBGAME-26OCT051600CHCSTL-CHC"
    missing_nfl_event = "KXNFLGAME-26OCT20UNKNOWN"
    existing_ticker = "KXNFLGAME-26OCT21EXIST-HOME"
    for ticker, event in (
        (f"{event_ticker}-MIL", event_ticker),
        (missing_mlb_ticker, missing_mlb_ticker.rsplit("-", 1)[0]),
        (f"{missing_nfl_event}-HOME", missing_nfl_event),
    ):
        ledger.record_fill(
            "sportsbook_v1",
            ticker,
            OutcomeSide.yes,
            Decimal("0.5"),
            Decimal(1),
            Decimal(0),
            Decimal("0.6"),
            rules_version="v1",
            event_ticker=event,
        )
    existing_commence = datetime(2026, 10, 21, 20, tzinfo=timezone.utc)
    ledger.record_fill(
        "sportsbook_v1",
        existing_ticker,
        OutcomeSide.yes,
        Decimal("0.5"),
        Decimal(1),
        Decimal(0),
        Decimal("0.6"),
        rules_version="v1",
        commence_time=existing_commence,
    )

    class Events:
        calls: list[tuple[str, str]] = []

        def get_events(self, *, series_ticker, status, with_nested_markets):  # type: ignore[no-untyped-def]
            self.calls.append((series_ticker, status))
            if status == "settled" and series_ticker == "KXMLBGAME":
                return [
                    {
                        "event_ticker": event_ticker,
                        "start_time": "2026-10-04T20:30:00Z",
                    }
                ]
            if status == "settled" and series_ticker == "KXNFLGAME":
                return [
                    {
                        "event_ticker": "KXNFLGAME-26OCT20FOUND",
                        "start_time": "2026-10-20T20:00:00Z",
                    }
                ]
            return []

    client = Events()
    changed = backfill_clv_commence_times(
        ledger,
        client,  # type: ignore[arg-type]
        Settings(_env_file=None),
        now=datetime(2026, 10, 22, tzinfo=timezone.utc),
    )
    rows = {
        ticker: (commence, backfill, source, status)
        for ticker, commence, backfill, source, status in ledger.conn.execute(
            "SELECT ticker,commence_time,commence_time_backfill,commence_source,clv_status "
            "FROM fills"
        )
    }

    assert changed == 2
    assert rows[f"{event_ticker}-MIL"][2] == "kalshi_event"
    assert rows[f"{event_ticker}-MIL"][1] == "2026-10-04T20:30:00+00:00"
    assert rows[missing_mlb_ticker][2] == "mlb_ticker"
    assert rows[missing_mlb_ticker][3] == "in_game"
    assert rows[f"{missing_nfl_event}-HOME"][1:] == (None, None, "no_commence")
    assert rows[existing_ticker][0] == existing_commence.isoformat()
    assert rows[existing_ticker][1] is None
    assert rows[existing_ticker][2] is None
    assert client.calls[0] == ("KXMLBGAME", "settled")
    assert ("KXMLBGAME", "settled") in client.calls


def _response(name: str) -> list[dict[str, object]]:
    data = json.loads((FIXTURES / f"clv_{name}.json").read_text())
    return data["response"]["candlesticks"]


def test_appendix_d_game_day_quotes_match_recorded_kalshi_fixtures() -> None:
    cases = [
        (
            "texokla",
            "KXNCAAFGAME-26OCT10TEXOKLA",
            datetime(2026, 10, 10, 14, tzinfo=timezone.utc),
            "0.255",
        ),
        (
            "sfsea",
            "KXNFLGAME-26OCT11SFSEA",
            datetime(2026, 10, 11, 14, tzinfo=timezone.utc),
            "0.385",
        ),
        (
            "scarfla_game_date",
            "KXNCAAFGAME-26OCT10SCARFLA",
            datetime(2026, 10, 10, 14, tzinfo=timezone.utc),
            "0.195",
        ),
        (
            "scarfla_oct5",
            "KXNCAAFGAME-26OCT10SCARFLA",
            datetime(2026, 10, 5, 14, tzinfo=timezone.utc),
            "0.165",
        ),
    ]
    for name, event, target, expected in cases:
        quote = _quote_at(_response(name), target)  # type: ignore[arg-type]
        assert quote is not None
        assert quote[3] == Decimal(expected)
        assert game_day_target(event) is not None
        assert (FIXTURES / f"clv_{name}.json").stat().st_size < 30_000


def test_record_clv_scores_near_miss_like_fill_from_texokla_fixture(tmp_path: Path) -> None:
    created = datetime(2026, 10, 7, 14, 5, tzinfo=timezone.utc)
    commence = datetime(2026, 10, 10, 14, 5, tzinfo=timezone.utc)
    ticker = "KXNCAAFGAME-26OCT10TEXOKLA"
    ledger = Ledger(tmp_path / "texokla-near-miss.sqlite", Decimal("1000"))
    ledger.record_fill(
        "sportsbook_v2",
        ticker,
        OutcomeSide.yes,
        Decimal("0.20"),
        Decimal(1),
        Decimal(0),
        Decimal("0.70"),
        created,
        rules_version="v2",
        position_key=f"{ticker}:fill",
        event_ticker=ticker,
        commence_time=commence,
    )
    signal = Signal(
        strategy="sportsbook_v2",
        ticker=ticker,
        side=OutcomeSide.yes,
        fair_prob=Decimal("0.70"),
        limit_price=Decimal("0.20"),
        edge=Decimal("-0.01"),
        size=Decimal(0),
        created_at=created,
        rules_version="v2",
        position_key=f"{ticker}:near-miss",
        event_ticker=ticker,
        commence_time=commence,
    )
    assert ledger.record_near_miss_decisions([(signal, "net_edge")]) == 1

    class FixtureCandles:
        def get_candlesticks(self, series, market, start, end, period_interval=1):  # type: ignore[no-untyped-def]
            assert series == "KXNCAAFGAME"
            assert market == ticker
            assert period_interval == 1
            return _response("texokla")

    assert (
        record_clv(
            ledger,
            FixtureCandles(),  # type: ignore[arg-type]
            now=commence + timedelta(hours=1),
        )
        == 1
    )
    fill_clv, fill_bid = ledger.conn.execute(
        "SELECT clv_kalshi,clv_kalshi_bid FROM fills WHERE ticker=?", (ticker,)
    ).fetchone()
    shadow_clv, shadow_bid = ledger.conn.execute(
        "SELECT clv_kalshi,clv_kalshi_bid FROM shadow_decisions WHERE ticker=?", (ticker,)
    ).fetchone()
    assert Decimal(fill_clv) == Decimal(shadow_clv)
    assert Decimal(fill_bid) == Decimal(shadow_bid)
    assert Decimal(fill_clv) == Decimal("5.5")
    assert Decimal(fill_bid) == Decimal("5.0")


def test_clv_bid_migration_backfills_empty_values_without_rewriting(tmp_path: Path) -> None:
    path = tmp_path / "clv-bid-migration.sqlite"
    ledger = Ledger(path, Decimal("1000"))
    created = datetime(2026, 10, 1, tzinfo=timezone.utc)
    commence = created + timedelta(days=4)
    ticker = "KXNFLGAME-26OCT05KCLV-LV"
    event = "KXNFLGAME-26OCT05KCLV"
    ledger.record_fill(
        "sportsbook_v2",
        ticker,
        OutcomeSide.yes,
        Decimal("0.50"),
        Decimal(1),
        Decimal(0),
        Decimal("0.70"),
        created,
        rules_version="v2",
        position_key=f"{event}:fill",
        event_ticker=event,
        commence_time=commence,
    )
    ledger.record_shadow_decision(
        Signal(
            strategy="sportsbook_v2",
            ticker=ticker,
            side=OutcomeSide.yes,
            fair_prob=Decimal("0.70"),
            limit_price=Decimal("0.50"),
            edge=Decimal("0.20"),
            size=Decimal(0),
            created_at=created,
            rules_version="v2",
            position_key=f"{event}:near-miss",
            event_ticker=event,
            commence_time=commence,
        ),
        "v2",
        reason="net_edge",
    )
    ledger.record_maker_shadows(
        [
            MakerShadowCandidate(
                created_at=created,
                ticker=ticker,
                side=OutcomeSide.yes,
                event_ticker=event,
                position_key=f"{event}:maker",
                price=Decimal("0.50"),
                size=Decimal(1),
                fair=Decimal("0.70"),
                maker_fee=Decimal("0.01"),
                expires_at=commence - timedelta(minutes=10),
                commence_time=commence,
            )
        ]
    )
    ledger.conn.execute("UPDATE fills SET clv_exit='4',clv_kalshi_bid='99'")
    ledger.conn.execute("UPDATE maker_shadow SET clv_exit='5',clv_kalshi_bid=NULL")
    ledger.conn.execute("UPDATE shadow_decisions SET clv_exit='6',clv_kalshi_bid=NULL")
    ledger.conn.commit()
    ledger.conn.close()

    migrated = Ledger(path, Decimal("1000"))

    assert migrated.conn.execute("SELECT clv_kalshi_bid FROM fills").fetchone()[0] == "99"
    assert migrated.conn.execute("SELECT clv_kalshi_bid FROM maker_shadow").fetchone()[0] == "5"
    assert migrated.conn.execute("SELECT clv_kalshi_bid FROM shadow_decisions").fetchone()[0] == "6"


def test_game_day_target_uses_known_commence_et_date() -> None:
    commence = datetime(2026, 10, 5, 4, 5, tzinfo=timezone.utc)
    target = game_day_target("KXMLBGAME-26OCT042300NYKBOS", commence)
    assert target == datetime(2026, 10, 5, 14, tzinfo=timezone.utc)


def test_quote_at_rejects_missing_crossed_and_impossible_quotes() -> None:
    target = datetime(2026, 10, 10, 14, tzinfo=timezone.utc)
    cases = [
        {"end_period_ts": int(target.timestamp()), "yes_bid": None, "yes_ask": None},
        {
            "end_period_ts": int(target.timestamp()),
            "yes_bid": {"close_dollars": "0.40"},
            "yes_ask": {"close_dollars": "0.30"},
        },
        {
            "end_period_ts": int(target.timestamp()),
            "yes_bid": {"close_dollars": "0.00"},
            "yes_ask": {"close_dollars": "0.10"},
        },
    ]
    assert all(_quote_at([candle], target) is None for candle in cases)


def test_record_clv_persists_tminus_and_side_adjusted_values(tmp_path: Path) -> None:
    commence = datetime(2026, 10, 20, 20, tzinfo=timezone.utc)
    ticker = "KXNFLGAME-26OCT20KCLV-LV"
    event = "KXNFLGAME-26OCT20KCLV"
    ledger = Ledger(tmp_path / "clv.sqlite", Decimal("1000"))
    ledger.record_fill(
        "sportsbook_v2",
        ticker,
        OutcomeSide.no,
        Decimal("0.20"),
        Decimal(1),
        Decimal(0),
        Decimal("0.65"),
        rules_version="v2",
        event_ticker=event,
        commence_time=commence,
    )
    ledger.conn.execute(
        "INSERT INTO odds_snapshots "
        "(created_at,feed,event_id,home_team,away_team,commence_time,team,prob,books_used) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            (commence - timedelta(hours=1)).isoformat(),
            "oddsapi",
            "event-id",
            "Kansas City Chiefs",
            "Las Vegas Raiders",
            commence.isoformat(),
            "Las Vegas Raiders",
            "0.30",
            1,
        ),
    )
    ledger.conn.commit()

    class Candles:
        calls = 0

        def get_candlesticks(self, series, market, start, end, period_interval=1):  # type: ignore[no-untyped-def]
            self.calls += 1
            assert series == "KXNFLGAME"
            assert market == ticker
            assert period_interval == 1
            assert start == int((commence - timedelta(hours=6, minutes=2)).timestamp())
            assert end == int(commence.timestamp())

            def candle(at: datetime, bid: str, ask: str) -> dict[str, object]:
                return {
                    "end_period_ts": int(at.timestamp()),
                    "yes_bid": {"close_dollars": bid},
                    "yes_ask": {"close_dollars": ask},
                }

            return [
                candle(datetime(2026, 10, 20, 14, tzinfo=timezone.utc), "0.29", "0.31"),
                candle(commence - timedelta(minutes=5), "0.74", "0.76"),
            ]

    client = Candles()
    assert record_clv(ledger, client, now=commence + timedelta(hours=1)) == 1  # type: ignore[arg-type]
    clv, game_day, exit_clv, clv_bid = ledger.conn.execute(
        "SELECT clv_kalshi,clv_gameday,clv_exit,clv_kalshi_bid FROM fills"
    ).fetchone()
    assert Decimal(clv) == Decimal("5.00")
    assert Decimal(game_day) == Decimal("50.0")
    assert Decimal(exit_clv) == Decimal("4.00")
    assert Decimal(clv_bid) == Decimal("4.00")
    assert Decimal(
        ledger.conn.execute("SELECT line_move_since_entry FROM fills").fetchone()[0]
    ) == Decimal("0.05")
    assert client.calls == 1
    assert record_clv(ledger, client, now=commence + timedelta(hours=1)) == 0  # type: ignore[arg-type]
    assert client.calls == 1

    ledger.record_maker_shadows(
        [
            MakerShadowCandidate(
                created_at=commence - timedelta(hours=1),
                ticker=ticker,
                side=OutcomeSide.yes,
                event_ticker=event,
                position_key=f"{event}:las-vegas-raiders:shadow",
                price=Decimal("0.50"),
                size=Decimal(1),
                fair=Decimal("0.70"),
                maker_fee=Decimal("0.01"),
                expires_at=commence - timedelta(minutes=10),
                commence_time=commence,
            )
        ]
    )
    ledger.conn.execute("UPDATE maker_shadow SET status='filled'")
    ledger.conn.commit()
    assert record_clv(ledger, client, now=commence + timedelta(hours=1)) == 0  # type: ignore[arg-type]
    shadow_clv = ledger.conn.execute(
        "SELECT clv_kalshi,clv_gameday,clv_kalshi_bid FROM maker_shadow"
    ).fetchone()
    assert Decimal(shadow_clv[0]) == Decimal("25.0")
    assert Decimal(shadow_clv[1]) == Decimal("-20.0")
    assert Decimal(shadow_clv[2]) == Decimal("24.0")
    assert client.calls == 1


def test_score_maker_shadows_persists_both_fill_rules(tmp_path: Path) -> None:
    created = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    expires = created + timedelta(hours=2)
    ticker = "KXNFLGAME-26OCT20KCLV-KC"
    ledger = Ledger(tmp_path / "maker-score.sqlite", Decimal("100"))
    ledger.record_maker_shadows(
        [
            MakerShadowCandidate(
                created_at=created,
                ticker=ticker,
                side=OutcomeSide.yes,
                event_ticker="KXNFLGAME-26OCT20KCLV",
                position_key="KXNFLGAME-26OCT20KCLV:kansas-city-chiefs",
                price=Decimal("0.50"),
                size=Decimal(10),
                fair=Decimal("0.70"),
                maker_fee=Decimal("0.03"),
                expires_at=expires,
                commence_time=expires + timedelta(hours=1),
                rules_version="v3",
            )
        ]
    )
    ledger.conn.execute(
        "UPDATE maker_shadow SET status='expired',status_at=?",
        (expires.isoformat(),),
    )
    ledger.conn.commit()

    class Candles:
        def get_candlesticks(self, series, market, start, end, period_interval=1):  # type: ignore[no-untyped-def]
            assert (series, market, start, end, period_interval) == (
                "KXNFLGAME",
                ticker,
                int(created.timestamp()),
                int(expires.timestamp()),
                1,
            )
            return [
                {
                    "end_period_ts": int((created + timedelta(minutes=1)).timestamp()),
                    "price": {"low_dollars": "0.49", "high_dollars": "0.55"},
                    "yes_ask": {"low_dollars": "0.50"},
                    "yes_bid": {"high_dollars": "0.49"},
                }
            ]

    assert (
        score_maker_shadows(
            ledger,
            Candles(),
            now=expires + timedelta(minutes=1),  # type: ignore[arg-type]
        )
        == 1
    )
    row = ledger.conn.execute(
        "SELECT fill_trade_through_at,fill_optimistic_at,score_status,score_attempts "
        "FROM maker_shadow"
    ).fetchone()
    expected_fill = (created + timedelta(minutes=1)).isoformat()
    assert row == (expected_fill, expected_fill, "scored", 1)
    assert ledger.maker_shadow_metrics()["trade_through_filled"] == 1
    assert ledger.maker_shadow_metrics()["optimistic_filled"] == 1


def test_maker_shadow_score_marks_unavailable_after_three_fetch_failures(tmp_path: Path) -> None:
    created = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    expires = created + timedelta(hours=2)
    ledger = Ledger(tmp_path / "maker-score-retry.sqlite", Decimal("100"))
    ledger.record_maker_shadows(
        [
            MakerShadowCandidate(
                created_at=created,
                ticker="KXNFLGAME-26OCT20KCLV-KC",
                side=OutcomeSide.yes,
                event_ticker="KXNFLGAME-26OCT20KCLV",
                position_key="KXNFLGAME-26OCT20KCLV:kansas-city-chiefs",
                price=Decimal("0.50"),
                size=Decimal(1),
                fair=Decimal("0.70"),
                maker_fee=Decimal("0.01"),
                expires_at=expires,
                rules_version="v3",
            )
        ]
    )
    ledger.conn.execute(
        "UPDATE maker_shadow SET status='expired',status_at=?",
        (expires.isoformat(),),
    )
    ledger.conn.commit()

    class FailedCandles:
        def get_candlesticks(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("market data unavailable")

    for attempt in range(3):
        assert (
            score_maker_shadows(
                ledger,
                FailedCandles(),  # type: ignore[arg-type]
                now=expires + timedelta(minutes=attempt + 1),
            )
            == 1
        )
    assert (
        score_maker_shadows(
            ledger,
            FailedCandles(),
            now=expires + timedelta(minutes=10),  # type: ignore[arg-type]
        )
        == 0
    )
    assert ledger.conn.execute(
        "SELECT score_status,score_attempts FROM maker_shadow"
    ).fetchone() == ("unavailable", 3)


def test_record_clv_matches_z_snapshot_to_utc_fill_commence(tmp_path: Path) -> None:
    commence = datetime(2026, 10, 20, 20, tzinfo=timezone.utc)
    ticker = "KXNFLGAME-26OCT20KCLV-LV"
    ledger = Ledger(tmp_path / "clv-z-snapshot.sqlite", Decimal("100"))
    ledger.record_fill(
        "sportsbook_v2",
        ticker,
        OutcomeSide.yes,
        Decimal("0.50"),
        Decimal(1),
        Decimal(0),
        Decimal("0.65"),
        rules_version="v2",
        event_ticker="KXNFLGAME-26OCT20KCLV",
        commence_time=commence,
    )
    ledger.conn.execute(
        "INSERT INTO odds_snapshots "
        "(created_at,feed,event_id,home_team,away_team,commence_time,team,prob,books_used) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            (commence - timedelta(hours=1)).isoformat(),
            "oddsapi",
            "event-id",
            "Kansas City Chiefs",
            "Las Vegas Raiders",
            commence.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Las Vegas Raiders",
            "0.70",
            1,
        ),
    )
    ledger.conn.commit()

    class EmptyCandles:
        def get_candlesticks(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return []

    record_clv(ledger, EmptyCandles(), now=commence + timedelta(hours=1))  # type: ignore[arg-type]
    line_move = ledger.conn.execute("SELECT line_move_since_entry FROM fills").fetchone()[0]
    assert Decimal(line_move) == Decimal("0.05")


def test_record_clv_retries_failed_tickers_and_marks_unavailable_after_three(
    tmp_path: Path,
) -> None:
    commence = datetime(2026, 10, 20, 20, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "clv-attempts.sqlite", Decimal("100"))
    for index in range(51):
        day = 22 if index < 50 else 23
        start = datetime(2026, 10, day, 20, tzinfo=timezone.utc)
        event = f"KXNFLGAME-26OCT{day:02d}EV{index:02d}"
        ledger.record_fill(
            "sportsbook_v2",
            f"{event}-LV",
            OutcomeSide.yes,
            Decimal("0.50"),
            Decimal(1),
            Decimal(0),
            Decimal("0.65"),
            rules_version="v2",
            event_ticker=event,
            commence_time=start,
        )

    class EmptyCandles:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_candlesticks(self, series, ticker, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(ticker)
            return []

    client = EmptyCandles()
    now = datetime(2026, 10, 24, tzinfo=timezone.utc)
    record_clv(ledger, client, now=now, max_calls=50)  # type: ignore[arg-type]
    assert "KXNFLGAME-26OCT23EV50-LV" in client.calls

    single = Ledger(tmp_path / "clv-unavailable.sqlite", Decimal("100"))
    single.record_fill(
        "sportsbook_v2",
        "KXNFLGAME-26OCT20KCLV-LV",
        OutcomeSide.yes,
        Decimal("0.50"),
        Decimal(1),
        Decimal(0),
        Decimal("0.65"),
        rules_version="v2",
        event_ticker="KXNFLGAME-26OCT20KCLV",
        commence_time=commence,
    )
    for _ in range(3):
        record_clv(single, client, now=now, max_calls=1)  # type: ignore[arg-type]
    status = single.conn.execute(
        "SELECT status FROM clv_fetch_attempts WHERE ticker=?",
        ("KXNFLGAME-26OCT20KCLV-LV",),
    ).fetchone()[0]
    calls_before = len(client.calls)
    record_clv(single, client, now=now, max_calls=1)  # type: ignore[arg-type]
    assert status == "unavailable"
    assert len(client.calls) == calls_before


def test_line_move_shadow_clv_does_not_require_settlement(tmp_path: Path) -> None:
    commence = datetime(2026, 10, 20, 20, tzinfo=timezone.utc)
    ticker = "KXNFLGAME-26OCT20KCLV-LV"
    event = "KXNFLGAME-26OCT20KCLV"
    ledger = Ledger(tmp_path / "line-move-clv.sqlite", Decimal("100"))
    ledger.record_shadow_decision(
        Signal(
            strategy="line_move_follower",
            ticker=ticker,
            side=OutcomeSide.yes,
            fair_prob=Decimal("0.60"),
            limit_price=Decimal("0.50"),
            edge=Decimal("0.10"),
            size=Decimal(1),
            created_at=commence - timedelta(hours=1),
            commence_time=commence,
            event_ticker=event,
        ),
        "v2",
        reason="line_move_follower",
    )

    class Candles:
        def get_candlesticks(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return [
                {
                    "end_period_ts": int((commence - timedelta(minutes=5)).timestamp()),
                    "yes_bid": {"close_dollars": "0.59"},
                    "yes_ask": {"close_dollars": "0.61"},
                }
            ]

    record_clv(ledger, Candles(), now=commence + timedelta(hours=1))  # type: ignore[arg-type]
    value = ledger.conn.execute("SELECT clv_kalshi FROM shadow_decisions").fetchone()[0]
    assert Decimal(value) == Decimal("10.0")


def test_record_clv_scores_single_sharp_shadow_decision(tmp_path: Path) -> None:
    commence = datetime(2026, 10, 27, 20, tzinfo=timezone.utc)
    created = commence - timedelta(hours=100)
    event = "KXNCAAFGAME-26OCT27SINGLE"
    ticker = f"{event}-HOME"
    ledger = Ledger(tmp_path / "single-sharp-clv.sqlite", Decimal("100"))
    ledger.record_shadow_decision(
        Signal(
            strategy="sportsbook_v3",
            ticker=ticker,
            side=OutcomeSide.yes,
            fair_prob=Decimal("0.60"),
            limit_price=Decimal("0.50"),
            edge=Decimal("0.04"),
            size=Decimal(1),
            created_at=created,
            rules_version="v3",
            position_key=f"{event}:home",
            event_ticker=event,
            tags=("single_sharp",),
            commence_time=commence,
        ),
        "v3",
        reason="single_sharp",
    )

    class Candles:
        def get_candlesticks(self, series, market, start, end, period_interval=1):  # type: ignore[no-untyped-def]
            assert series == "KXNCAAFGAME"
            assert market == ticker
            return [
                {
                    "end_period_ts": int((commence - timedelta(minutes=5)).timestamp()),
                    "yes_bid": {"close_dollars": "0.59"},
                    "yes_ask": {"close_dollars": "0.61"},
                }
            ]

    settings = Settings()
    assert (
        record_clv(
            ledger,
            Candles(),  # type: ignore[arg-type]
            now=commence + timedelta(hours=1),
        )
        == 1
    )
    refresh_clv_statuses(ledger, settings)
    clv, bid_clv, status = ledger.conn.execute(
        "SELECT clv_kalshi,clv_kalshi_bid,clv_status "
        "FROM shadow_decisions WHERE reason='single_sharp'"
    ).fetchone()
    assert clv is not None
    assert bid_clv is not None
    assert status == "ok"


def test_record_clv_caps_candlestick_calls_per_run(tmp_path: Path) -> None:
    commence = datetime(2026, 10, 20, 20, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "call-cap.sqlite", Decimal("1000"))
    for ticker, event in (
        ("KXNFLGAME-26OCT20KCLV-LV", "KXNFLGAME-26OCT20KCLV"),
        ("KXNFLGAME-26OCT20DENSF-SF", "KXNFLGAME-26OCT20DENSF"),
    ):
        ledger.record_fill(
            "sportsbook_arb",
            ticker,
            OutcomeSide.yes,
            Decimal("0.40"),
            Decimal(1),
            Decimal(0),
            Decimal("0.50"),
            rules_version="v2",
            event_ticker=event,
            commence_time=commence,
        )

    class EmptyCandles:
        calls = 0

        def get_candlesticks(self, series, ticker, start, end, period_interval=1):  # type: ignore[no-untyped-def]
            self.calls += 1
            return []

    client = EmptyCandles()
    assert record_clv(ledger, client, now=commence + timedelta(hours=1), max_calls=1) == 1  # type: ignore[arg-type]
    assert client.calls == 1
    assert (
        ledger.conn.execute("SELECT COUNT(*) FROM fills WHERE clv_kalshi IS NULL").fetchone()[0]
        == 2
    )


def test_record_clv_skips_failed_ticker_and_retries_it(tmp_path: Path) -> None:
    commence = datetime(2026, 10, 20, 20, tzinfo=timezone.utc)
    tickers = (
        ("KXNFLGAME-26OCT20KCLV-LV", "KXNFLGAME-26OCT20KCLV"),
        ("KXNFLGAME-26OCT20DENSF-SF", "KXNFLGAME-26OCT20DENSF"),
        ("KXNFLGAME-26OCT20MIANIN-MIA", "KXNFLGAME-26OCT20MIANIN"),
    )
    ledger = Ledger(tmp_path / "failed-ticker.sqlite", Decimal("1000"))
    for ticker, event in tickers:
        ledger.record_fill(
            "sportsbook_v2",
            ticker,
            OutcomeSide.yes,
            Decimal("0.40"),
            Decimal(1),
            Decimal(0),
            Decimal("0.60"),
            rules_version="v2",
            event_ticker=event,
            commence_time=commence,
        )

    class SelectiveCandles:
        calls: list[str] = []
        failed_once = False

        def get_candlesticks(self, series, ticker, start, end, period_interval=1):  # type: ignore[no-untyped-def]
            self.calls.append(ticker)
            if ticker == tickers[0][0] and not self.failed_once:
                self.failed_once = True
                raise RuntimeError("temporary candlestick failure")
            return [
                {
                    "end_period_ts": int((commence - timedelta(minutes=5)).timestamp()),
                    "yes_bid": {"close_dollars": "0.74"},
                    "yes_ask": {"close_dollars": "0.76"},
                }
            ]

    client = SelectiveCandles()
    now = commence + timedelta(hours=1)
    assert record_clv(ledger, client, now=now, max_calls=2) == 2  # type: ignore[arg-type]
    first_run = dict(ledger.conn.execute("SELECT ticker,clv_kalshi FROM fills").fetchall())
    assert first_run[tickers[0][0]] is None
    assert Decimal(first_run[tickers[1][0]]) == Decimal("35.00")
    assert first_run[tickers[2][0]] is None
    assert client.calls == [tickers[0][0], tickers[1][0]]

    assert record_clv(ledger, client, now=now, max_calls=2) == 2  # type: ignore[arg-type]
    second_run = dict(ledger.conn.execute("SELECT ticker,clv_kalshi FROM fills").fetchall())
    assert all(Decimal(second_run[ticker]) == Decimal("35.00") for ticker, _ in tickers)
    assert client.calls == [
        tickers[0][0],
        tickers[1][0],
        tickers[2][0],
        tickers[0][0],
    ]


def test_record_clv_persists_settled_line_move_shadow_clv(tmp_path: Path) -> None:
    commence = datetime(2026, 10, 20, 20, tzinfo=timezone.utc)
    ticker = "KXNFLGAME-26OCT20KCLV-LV"
    event = "KXNFLGAME-26OCT20KCLV"
    ledger = Ledger(tmp_path / "line-move-clv.sqlite", Decimal("1000"))
    ledger.record_shadow_decision(
        Signal(
            strategy="line_move_follower",
            ticker=ticker,
            side=OutcomeSide.yes,
            fair_prob=Decimal("0.60"),
            limit_price=Decimal("0.50"),
            edge=Decimal("0.10"),
            size=Decimal(1),
            created_at=commence - timedelta(hours=3),
            rules_version="v2",
            position_key=f"{event}:LV",
            event_ticker=event,
            commence_time=commence,
        ),
        "v2",
        reason="line_move_follower",
    )
    ledger.conn.execute(
        "INSERT INTO settlements (ticker,settled_at,result,pnl) VALUES (?,?,?,?)",
        (ticker, commence.isoformat(), "yes", "0.50"),
    )
    ledger.conn.commit()

    class Candles:
        calls = 0

        def get_candlesticks(self, series, market, start, end, period_interval=1):  # type: ignore[no-untyped-def]
            self.calls += 1
            assert series == "KXNFLGAME"
            assert market == ticker
            assert period_interval == 1

            def candle(at: datetime, bid: str, ask: str) -> dict[str, object]:
                return {
                    "end_period_ts": int(at.timestamp()),
                    "yes_bid": {"close_dollars": bid},
                    "yes_ask": {"close_dollars": ask},
                }

            return [
                candle(datetime(2026, 10, 20, 14, tzinfo=timezone.utc), "0.51", "0.53"),
                candle(commence - timedelta(minutes=5), "0.61", "0.63"),
            ]

    client = Candles()
    assert record_clv(ledger, client, now=commence + timedelta(hours=1)) == 1  # type: ignore[arg-type]
    clv, game_day = ledger.conn.execute(
        "SELECT clv_kalshi,clv_gameday FROM shadow_decisions WHERE strategy='line_move_follower'"
    ).fetchone()
    assert Decimal(clv) == Decimal("12.00")
    assert Decimal(game_day) == Decimal("2.00")
    assert client.calls == 1
