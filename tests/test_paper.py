from __future__ import annotations

from decimal import Decimal

import pytest

from prediction_bot.engine import Engine
from prediction_bot.models import OutcomeSide, fee_for
from prediction_bot.paper.ledger import Ledger
from prediction_bot.strategies.book_scanner import BookScannerStrategy
from tests.conftest import make_market


def test_fee_rounds_up_to_cent() -> None:
    # 0.07 * 10 * 0.5 * 0.5 = 0.175 -> 0.18
    assert fee_for(Decimal("0.5"), Decimal(10), Decimal("0.07")) == Decimal("0.18")
    assert fee_for(Decimal("0.99"), Decimal(1), Decimal("0.07")) == Decimal("0.01")


def test_ledger_fill_settle_and_performance(tmp_path) -> None:  # type: ignore[no-untyped-def]
    led = Ledger(tmp_path / "l.sqlite", Decimal("100"))
    led.record_fill(
        "s", "T1", OutcomeSide.yes, Decimal("0.40"), Decimal(10), Decimal("0.17"), Decimal("0.6")
    )
    led.record_fill(
        "s", "T2", OutcomeSide.no, Decimal("0.30"), Decimal(5), Decimal("0.08"), Decimal("0.5")
    )
    assert led.cash == Decimal("100") - Decimal("4.17") - Decimal("1.58")
    assert led.position_count("T1", OutcomeSide.yes) == 10
    assert led.open_tickers() == ["T1", "T2"]

    assert led.settle("T1", OutcomeSide.yes) == Decimal("10") - Decimal("4.17")
    assert led.settle("T1", OutcomeSide.yes) == 0  # idempotent
    assert led.settle("T2", OutcomeSide.yes) == -Decimal("1.58")  # we held NO, lost

    p = led.performance()
    assert p.cash == Decimal("100") + Decimal("5.83") - Decimal("1.58")
    assert p.open_positions == 0
    assert p.realized_pnl == Decimal("5.83") - Decimal("1.58")
    assert p.fees_paid == Decimal("0.25")
    assert p.wins == 1
    # brier weighted by contracts: T1 (0.6-1)^2*10 + T2 (0.5-0)^2*5 over 15
    assert p.brier_score == (Decimal("0.16") * 10 + Decimal("0.25") * 5) / 15
    assert p.market_brier_score == (Decimal("0.36") * 10 + Decimal("0.09") * 5) / 15


def test_ledger_rejects_overspend(tmp_path) -> None:  # type: ignore[no-untyped-def]
    led = Ledger(tmp_path / "l.sqlite", Decimal("1"))
    with pytest.raises(ValueError):
        led.record_fill(
            "s", "T", OutcomeSide.yes, Decimal("0.5"), Decimal(10), Decimal(0), Decimal("0.5")
        )


def test_book_scanner_finds_locked_arb(settings) -> None:  # type: ignore[no-untyped-def]
    strat = BookScannerStrategy(settings)
    # yes ask 0.40, no ask (1 - yes_bid) = 0.50 -> total 0.90 < 1
    arb = make_market("E-A", yes_bid="0.50", yes_ask="0.40")
    fair = make_market("E-B", yes_bid="0.48", yes_ask="0.52")
    sigs = strat.evaluate([arb, fair])
    assert {s.ticker for s in sigs} == {"E-A"}
    assert {s.side for s in sigs} == {OutcomeSide.yes, OutcomeSide.no}
    yes = next(s for s in sigs if s.side is OutcomeSide.yes)
    assert yes.limit_price == Decimal("0.40")
    assert yes.size == 25  # max_order_notional 10 / 0.40


def test_engine_paper_cycle_never_calls_venue(settings) -> None:  # type: ignore[no-untyped-def]
    class NoVenue:
        def create_order(self, *a: object, **k: object) -> None:
            raise AssertionError("live order in paper mode")

    ledger = Ledger(settings.paper_db_path, settings.paper_starting_cash)
    eng = Engine(settings, NoVenue(), [BookScannerStrategy(settings)], ledger)  # type: ignore[arg-type]
    res = eng.run_cycle([make_market("E-A", yes_bid="0.50", yes_ask="0.40")])
    assert len(res.acted) == 2
    assert ledger.cash < settings.paper_starting_cash
    # position cap: a second identical cycle must not exceed max_position_contracts
    settings.max_position_contracts = Decimal(30)
    res2 = eng.run_cycle([make_market("E-A", yes_bid="0.50", yes_ask="0.40")])
    assert res2.acted == []
