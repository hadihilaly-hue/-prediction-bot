"""Regression tests for the 2026-11-25 code review fixes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from prediction_bot.clv import backfill_clv_commence_times
from prediction_bot.config import Settings
from prediction_bot.models import OutcomeSide, Signal
from prediction_bot.paper.ledger import Ledger
from prediction_bot.venues.kalshi import parse_orderbook

NOW = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
TICKER = "KXNBAGAME-26NOV25AB-A"


def _order(ledger: Ledger) -> tuple[Signal, int]:
    sig = Signal(
        "sportsbook_v3",
        TICKER,
        OutcomeSide.yes,
        Decimal("0.6"),
        Decimal("0.5"),
        Decimal("0.05"),
        Decimal(10),
        rules_version="v3",
        position_key="pk",
        event_ticker="KXNBAGAME-26NOV25AB",
        created_at=NOW,
    )
    oid = ledger.record_paper_order(sig, Decimal("0.5"), Decimal("0.05"), NOW + timedelta(hours=8))
    ledger.record_signal(sig, acted=True)
    return sig, oid


def test_settle_cancels_open_paper_orders_and_refuses_later_fill() -> None:
    ledger = Ledger(":memory:", Decimal(100))
    sig, oid = _order(ledger)
    ledger.settle(sig.ticker, OutcomeSide.yes, at=NOW + timedelta(hours=3))
    assert ledger.open_paper_orders() == []
    assert not ledger.fill_paper_order(oid, NOW + timedelta(hours=1))
    assert ledger.cash == Decimal(100)
    assert ledger.performance().realized_pnl == 0


def test_fill_after_settlement_is_refused_even_if_order_still_open() -> None:
    ledger = Ledger(":memory:", Decimal(100))
    sig, oid = _order(ledger)
    ledger.settle(sig.ticker, OutcomeSide.yes, at=NOW + timedelta(hours=3))
    ledger.conn.execute("UPDATE paper_orders SET status='open' WHERE id=?", (oid,))
    assert not ledger.fill_paper_order(oid, NOW + timedelta(hours=1))
    assert ledger.cash == Decimal(100)


def test_legacy_cents_orderbook_reads_one_cent_level_as_cents() -> None:
    quote = parse_orderbook("T", {"orderbook": {"yes": [[1, 500], [40, 10]], "no": [[1, 300]]}})
    assert quote.yes_bid == Decimal("0.40")
    assert quote.yes_ask == Decimal("0.99")


def test_commence_backfill_retries_after_transient_kalshi_failure() -> None:
    ledger = Ledger(":memory:", Decimal(100))
    notnull = [r[1] for r in ledger.conn.execute("PRAGMA table_info(fills)") if r[3]]
    vals = {
        "created_at": "2026-11-20T00:00:00+00:00",
        "strategy": "x",
        "ticker": "KXNBAGAME-26NOV20LALBOS-LAL",
        "side": "yes",
        "price": "0.5",
        "count": "1",
        "fee": "0",
        "fair_prob": "0.5",
    }
    for col in notnull:
        vals.setdefault(col, "0")
    ledger.conn.execute(
        f"INSERT INTO fills ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})",
        list(vals.values()),
    )

    class Down:
        def get_events(self, **_: object) -> list[dict[str, object]]:
            raise RuntimeError("HTTP 503")

    backfill_clv_commence_times(ledger, Down(), Settings())  # type: ignore[arg-type]
    assert ledger.conn.execute("SELECT COUNT(*) FROM clv_commence_attempts").fetchone()[0] == 0


def _maker_price(bid: str, ask: str, fair: str) -> Decimal | None:
    from types import SimpleNamespace

    from prediction_bot.strategies.sportsbook_v2 import SportsbookV2Strategy as V2

    class Quote:
        def bid_for(self, _side: OutcomeSide) -> Decimal:
            return Decimal(bid)

        def ask_for(self, _side: OutcomeSide) -> Decimal:
            return Decimal(ask)

    strat = SimpleNamespace(settings=Settings(), rules_version="v3")
    strat._net_edge_threshold = lambda p: V2._net_edge_threshold(strat, p)  # type: ignore[arg-type]
    market = SimpleNamespace(ticker="KXNBAGAME-X-A", quote=Quote())
    return V2._v3_maker_price(
        strat,  # type: ignore[arg-type]
        market,  # type: ignore[arg-type]
        OutcomeSide.yes,
        Decimal(fair),
        "quadratic_with_maker_fees",
        Decimal(1),
    )


def test_v3_maker_price_is_on_a_whole_cent_tick() -> None:
    assert _maker_price("0.40", "0.45", "0.4312345") == Decimal("0.40")


def test_v3_maker_price_applies_low_price_threshold() -> None:
    # Below 40c the v3 threshold doubles to 0.04: with fair 0.34 the order rests at 0.29
    # (0.34 - 0.29 - 0.01 fee), not at the flat-2c price 0.31.
    assert _maker_price("0.30", "0.35", "0.34") == Decimal("0.29")
    assert _maker_price("0.30", "0.35", "0.37") == Decimal("0.31")


def test_finalized_market_without_yes_no_result_is_reported_not_settled(caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    from prediction_bot.engine import Engine
    from tests.conftest import make_market

    ledger = Ledger(":memory:", Decimal(100))
    sig, _oid = _order(ledger)

    class Venue:
        def get_markets(self, status, tickers):  # type: ignore[no-untyped-def]
            return [make_market(sig.ticker, None, None, status="finalized", result="void")]

    with caplog.at_level(logging.WARNING):
        out = Engine(object(), Venue(), [], ledger).settle_open_positions()  # type: ignore[arg-type]
    assert out == {}
    assert ledger.unsettled_tickers() == [sig.ticker]
    assert "UNSETTLEABLE" in caplog.text
