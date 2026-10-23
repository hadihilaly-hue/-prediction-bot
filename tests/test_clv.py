from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from prediction_bot.clv import _quote_at, game_day_target, record_clv
from prediction_bot.models import MakerShadowCandidate, OutcomeSide
from prediction_bot.paper.ledger import Ledger

FIXTURES = Path(__file__).parent / "fixtures"


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
    clv, game_day = ledger.conn.execute("SELECT clv_kalshi,clv_gameday FROM fills").fetchone()
    assert Decimal(clv) == Decimal("5.00")
    assert Decimal(game_day) == Decimal("50.0")
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
    shadow_clv = ledger.conn.execute("SELECT clv_kalshi,clv_gameday FROM maker_shadow").fetchone()
    assert Decimal(shadow_clv[0]) == Decimal("25.0")
    assert Decimal(shadow_clv[1]) == Decimal("-20.0")
    assert client.calls == 1


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
