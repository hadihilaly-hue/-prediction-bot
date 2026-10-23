from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from prediction_bot.models import (
    MakerShadowCandidate,
    MakerShadowUpdate,
    OutcomeSide,
    QuoteSnapshot,
)
from prediction_bot.paper.ledger import Ledger


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
