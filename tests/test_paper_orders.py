from datetime import datetime, timedelta, timezone
from decimal import Decimal

from prediction_bot.models import MakerShadowUpdate, OutcomeSide, Signal
from prediction_bot.paper.ledger import Ledger


def signal() -> Signal:
    created = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    return Signal(
        strategy="sportsbook_v3",
        ticker="KXNFLGAME-26OCT20KCLV-KC",
        side=OutcomeSide.yes,
        fair_prob=Decimal("0.70"),
        limit_price=Decimal("0.60"),
        edge=Decimal("0.05"),
        size=Decimal(10),
        rules_version="v3",
        position_key="KXNFLGAME-26OCT20KCLV:kansas-city-chiefs",
        event_ticker="KXNFLGAME-26OCT20KCLV",
        commence_time=created + timedelta(days=5),
        fee_type="quadratic_with_maker_fees",
        fee_multiplier=Decimal(1),
    )


def test_open_paper_orders_reserve_v3_caps_and_cash(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "paper-orders-caps.sqlite", Decimal("100"))
    candidate = signal()
    ledger.record_paper_order(
        candidate,
        Decimal("0.48"),
        Decimal("0.02"),
        candidate.commence_time - timedelta(minutes=30),  # type: ignore[operator]
    )

    reserved = Decimal("4.82")
    assert ledger.has_open_paper_order(candidate.position_key or "")
    assert ledger.paper_order_reserved_cost() == reserved
    assert ledger.available_cash(include_v3=True) == Decimal("95.18")
    assert ledger.position_cost(candidate.position_key or "", include_v3=True) == reserved
    assert ledger.event_cost(candidate.event_ticker or "", include_v3=True) == reserved
    assert ledger.v2_new_risk_since(candidate.created_at, include_v3=True) == reserved
    assert ledger.v2_total_open_cost(include_v3=True) == reserved
    assert ledger.v2_total_open_cost() == 0


def test_filling_paper_order_creates_a_normal_v3_maker_fill(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "paper-orders-fill.sqlite", Decimal("100"))
    candidate = signal()
    order_id = ledger.record_paper_order(
        candidate,
        Decimal("0.48"),
        Decimal("0.02"),
        candidate.commence_time - timedelta(minutes=30),  # type: ignore[operator]
    )
    filled_at = candidate.created_at + timedelta(minutes=3)

    assert ledger.fill_paper_order(order_id, filled_at)
    row = ledger.conn.execute(
        "SELECT created_at,price,count,fee,rules_version,tags FROM fills"
    ).fetchone()
    assert row == (filled_at.isoformat(), "0.48", "10", "0.02", "v3", "maker")
    order = ledger.latest_paper_order(candidate.position_key or "")
    assert order is not None
    assert (order.status, order.filled_at, order.fill_rule) == (
        "filled",
        filled_at,
        "trade_through",
    )
    assert ledger.cash == Decimal("95.18")
    assert not ledger.has_open_paper_order(candidate.position_key or "")


def test_cancelled_order_releases_all_reservations(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "paper-orders-cancel.sqlite", Decimal("100"))
    candidate = signal()
    order_id = ledger.record_paper_order(
        candidate,
        Decimal("0.48"),
        Decimal("0.02"),
        candidate.commence_time - timedelta(minutes=30),  # type: ignore[operator]
    )

    assert ledger.cancel_paper_order(order_id, candidate.created_at, "edge_gone")
    assert ledger.available_cash(include_v3=True) == Decimal("100")
    assert ledger.v2_total_open_cost(include_v3=True) == 0
    order = ledger.latest_paper_order(candidate.position_key or "")
    assert order is not None and order.cancel_reason == "edge_gone"


def test_open_v3_order_cancels_on_adverse_fair_move(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "paper-orders-adverse.sqlite", Decimal("100"))
    candidate = signal()
    order_id = ledger.record_paper_order(
        candidate,
        Decimal("0.48"),
        Decimal("0.02"),
        candidate.commence_time - timedelta(minutes=30),  # type: ignore[operator]
    )

    assert (
        ledger.update_paper_orders_for_fair(
            [
                MakerShadowUpdate(
                    at=candidate.created_at + timedelta(minutes=1),
                    ticker=candidate.ticker,
                    side=candidate.side,
                    ask=Decimal("0.60"),
                    last_price=None,
                    fair=Decimal("0.68"),
                )
            ],
            Decimal("0.01"),
        )
        == 1
    )
    order = ledger.latest_paper_order(candidate.position_key or "")
    assert order is not None
    assert (order.id, order.status, order.cancel_reason) == (
        order_id,
        "cancelled",
        "adverse_move",
    )
