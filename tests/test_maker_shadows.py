from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from prediction_bot.engine import Engine
from prediction_bot.models import (
    MakerShadowCandidate,
    MakerShadowUpdate,
    OutcomeSide,
    QuoteSnapshot,
)
from prediction_bot.paper.ledger import Ledger
from tests.conftest import make_market


def test_maker_shadow_lifecycle_keeps_hypothetical_fills_out_of_ledger(tmp_path) -> None:
    ledger = Ledger(tmp_path / "maker-shadow.sqlite", Decimal("100"))
    created = datetime(2026, 10, 23, tzinfo=timezone.utc)
    expires = created + timedelta(days=2)
    candidate = MakerShadowCandidate(
        created_at=created,
        ticker="KXNFLGAME-26OCT25KCLV-KC",
        side=OutcomeSide.yes,
        event_ticker="KXNFLGAME-26OCT25KCLV",
        position_key="KXNFLGAME-26OCT25KCLV:kansas-city-chiefs",
        price=Decimal("0.46"),
        size=Decimal(10),
        fair=Decimal("0.60"),
        maker_fee=Decimal("0.05"),
        expires_at=expires,
    )
    assert ledger.record_maker_shadows([candidate]) == 1
    assert ledger.record_maker_shadows([candidate]) == 0
    ledger.record_quote_snapshots(
        [
            QuoteSnapshot(
                ticker=candidate.ticker,
                event_ticker=candidate.event_ticker,
                yes_bid=Decimal("0.47"),
                yes_ask=Decimal("0.49"),
                last_price=Decimal("0.48"),
                volume=Decimal(1),
                open_interest=Decimal(1),
                created_at=created + timedelta(hours=1),
            )
        ]
    )

    through_time = created + timedelta(hours=2)
    ledger.update_maker_shadows(
        [
            MakerShadowUpdate(
                at=through_time,
                ticker=candidate.ticker,
                side=OutcomeSide.yes,
                ask=Decimal("0.55"),
                last_price=Decimal("0.45"),
                fair=Decimal("0.60"),
            )
        ]
    )
    status, trade_through_at = ledger.conn.execute(
        "SELECT status, trade_through_at FROM maker_shadow"
    ).fetchone()
    assert status == "open"
    assert trade_through_at == through_time.isoformat()

    ledger.update_maker_shadows(
        [
            MakerShadowUpdate(
                at=created + timedelta(hours=3),
                ticker=candidate.ticker,
                side=OutcomeSide.yes,
                ask=Decimal("0.45"),
                last_price=Decimal("0.44"),
                fair=Decimal("0.60"),
            )
        ]
    )
    assert ledger.conn.execute("SELECT status FROM maker_shadow").fetchone()[0] == "filled"
    assert ledger.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    assert ledger.positions() == []
    metrics = ledger.maker_shadow_metrics()
    assert metrics["filled"] == 1
    assert metrics["clv_count"] == 1
    assert metrics["mean_clv"] == Decimal("0.02")


def test_maker_shadow_cancels_on_negative_edge_and_expires(tmp_path) -> None:
    ledger = Ledger(tmp_path / "maker-shadow-state.sqlite", Decimal("100"))
    created = datetime(2026, 10, 23, tzinfo=timezone.utc)
    rows = [
        MakerShadowCandidate(
            created_at=created,
            ticker=f"KXNFLGAME-26OCT25KCLV-{code}",
            side=OutcomeSide.yes,
            event_ticker="KXNFLGAME-26OCT25KCLV",
            position_key=f"KXNFLGAME-26OCT25KCLV:{code}",
            price=Decimal("0.46"),
            size=Decimal(10),
            fair=Decimal("0.60"),
            maker_fee=Decimal("0.05"),
            expires_at=created + timedelta(days=1),
        )
        for code in ("LV", "MIA")
    ]
    ledger.record_maker_shadows(rows)
    ledger.update_maker_shadows(
        [
            MakerShadowUpdate(
                at=created + timedelta(hours=1),
                ticker=rows[0].ticker,
                side=OutcomeSide.yes,
                ask=Decimal("0.55"),
                last_price=None,
                fair=Decimal("0.45"),
            ),
            MakerShadowUpdate(
                at=rows[1].expires_at,
                ticker=rows[1].ticker,
                side=OutcomeSide.yes,
                ask=Decimal("0.55"),
                last_price=None,
                fair=Decimal("0.60"),
            ),
        ]
    )

    statuses = dict(ledger.conn.execute("SELECT ticker, status FROM maker_shadow"))
    assert statuses[rows[0].ticker] == "cancelled"
    assert statuses[rows[1].ticker] == "expired"


def test_v3_maker_shadow_cancellation_reasons_are_explicit(tmp_path) -> None:
    ledger = Ledger(tmp_path / "maker-shadow-v3-cancel.sqlite", Decimal("100"))
    created = datetime(2026, 10, 23, tzinfo=timezone.utc)
    adverse, edge_gone = [
        MakerShadowCandidate(
            created_at=created,
            ticker=f"KXNFLGAME-26OCT25KCLV-{code}",
            side=OutcomeSide.yes,
            event_ticker="KXNFLGAME-26OCT25KCLV",
            position_key=f"KXNFLGAME-26OCT25KCLV:{code}",
            price=price,
            size=Decimal(10),
            fair=fair,
            maker_fee=Decimal("0.10"),
            expires_at=created + timedelta(hours=2),
            rules_version="v3",
        )
        for code, price, fair in (
            ("KC", Decimal("0.50"), Decimal("0.70")),
            ("LV", Decimal("0.59"), Decimal("0.60")),
        )
    ]
    ledger.record_maker_shadows([adverse, edge_gone])
    ledger.update_maker_shadows(
        [
            MakerShadowUpdate(
                at=created + timedelta(minutes=5),
                ticker=adverse.ticker,
                side=adverse.side,
                ask=Decimal("0.55"),
                last_price=None,
                fair=Decimal("0.68"),
            ),
            MakerShadowUpdate(
                at=created + timedelta(minutes=5),
                ticker=edge_gone.ticker,
                side=edge_gone.side,
                ask=Decimal("0.60"),
                last_price=None,
                fair=Decimal("0.595"),
            ),
        ],
        v3_cancel_move=Decimal("0.01"),
    )

    reasons = dict(ledger.conn.execute("SELECT ticker,cancel_reason FROM maker_shadow"))
    assert reasons == {adverse.ticker: "adverse_move", edge_gone.ticker: "edge_gone"}


def test_cancelled_maker_shadow_does_not_block_a_new_row_after_migration(tmp_path) -> None:
    path = tmp_path / "maker-shadow-index.sqlite"
    ledger = Ledger(path, Decimal("100"))
    created = datetime(2026, 10, 23, tzinfo=timezone.utc)
    first = MakerShadowCandidate(
        created_at=created,
        ticker="KXNFLGAME-26OCT25KCLV-LV",
        side=OutcomeSide.yes,
        event_ticker="KXNFLGAME-26OCT25KCLV",
        position_key="KXNFLGAME-26OCT25KCLV:las-vegas-raiders",
        price=Decimal("0.46"),
        size=Decimal(10),
        fair=Decimal("0.60"),
        maker_fee=Decimal("0.05"),
        expires_at=created + timedelta(days=2),
    )
    assert ledger.record_maker_shadows([first]) == 1
    ledger.update_maker_shadows(
        [
            MakerShadowUpdate(
                at=created + timedelta(hours=1),
                ticker=first.ticker,
                side=first.side,
                ask=Decimal("0.55"),
                last_price=None,
                fair=Decimal("0.40"),
            )
        ]
    )
    assert ledger.conn.execute("SELECT status FROM maker_shadow").fetchone()[0] == "cancelled"

    ledger.conn.execute("DROP INDEX maker_shadow_position_key")
    ledger.conn.execute(
        "CREATE UNIQUE INDEX maker_shadow_position_key ON maker_shadow(position_key)"
    )
    ledger.conn.commit()
    ledger.close()

    migrated = Ledger(path, Decimal("100"))
    second = MakerShadowCandidate(
        **{
            **first.__dict__,
            "ticker": "KXNFLGAME-26OCT25KCLV-LV-ALT",
            "created_at": created + timedelta(hours=2),
        }
    )
    assert migrated.record_maker_shadows([second]) == 1
    assert migrated.conn.execute("SELECT COUNT(*) FROM maker_shadow").fetchone()[0] == 2
    index_sql = migrated.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='maker_shadow_position_key'"
    ).fetchone()[0]
    assert "WHERE status='open'" in index_sql


def test_filled_maker_shadow_ticker_is_settled_without_cash_or_fill_changes(tmp_path) -> None:
    ledger = Ledger(tmp_path / "maker-shadow-settlement.sqlite", Decimal("100"))
    created = datetime(2026, 10, 23, tzinfo=timezone.utc)
    ticker = "KXNFLGAME-26OCT25KCLV-LV"
    candidate = MakerShadowCandidate(
        created_at=created,
        ticker=ticker,
        side=OutcomeSide.yes,
        event_ticker="KXNFLGAME-26OCT25KCLV",
        position_key="KXNFLGAME-26OCT25KCLV:las-vegas-raiders",
        price=Decimal("0.40"),
        size=Decimal(10),
        fair=Decimal("0.60"),
        maker_fee=Decimal("0.10"),
        expires_at=created + timedelta(days=1),
    )
    ledger.record_maker_shadows([candidate])
    ledger.update_maker_shadows(
        [
            MakerShadowUpdate(
                at=created + timedelta(hours=1),
                ticker=ticker,
                side=OutcomeSide.yes,
                ask=Decimal("0.40"),
                last_price=None,
                fair=Decimal("0.60"),
            )
        ]
    )
    assert ledger.unsettled_tickers() == [ticker]

    class Venue:
        def get_markets(self, status, tickers):  # type: ignore[no-untyped-def]
            return [make_market(ticker, "0.50", "0.51", result="yes")]

    cash_before = ledger.cash
    result = Engine(object(), Venue(), [], ledger).settle_open_positions()  # type: ignore[arg-type]

    assert result == {ticker: Decimal(0)}
    assert ledger.cash == cash_before
    assert ledger.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    assert (
        ledger.conn.execute("SELECT pnl FROM settlements WHERE ticker=?", (ticker,)).fetchone()[0]
        == "0"
    )
    metrics = ledger.maker_shadow_metrics()
    assert metrics["settled"] == 1
    assert metrics["pnl"] == Decimal("5.90")
