from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from prediction_bot.cli import predictions_markdown
from prediction_bot.config import Mode
from prediction_bot.engine import Engine
from prediction_bot.models import OutcomeSide, Signal, fee_for
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


def test_ledger_predictions_join_settlement(tmp_path) -> None:  # type: ignore[no-untyped-def]
    led = Ledger(tmp_path / "l.sqlite", Decimal("100"))
    sig = Signal(
        "s",
        "T1",
        OutcomeSide.yes,
        Decimal("0.6"),
        Decimal("0.4"),
        Decimal("0.18"),
        Decimal(5),
        rationale="[x](http://evil) | `y`",
    )
    led.record_signal(sig, acted=True)
    led.record_signal(replace(sig, ticker="T2"), acted=False)
    assert led.unsettled_tickers() == ["T1", "T2"]
    led.record_fill(
        "s", "T1", OutcomeSide.yes, Decimal("0.4"), Decimal(5), Decimal("0.1"), Decimal("0.6")
    )
    led.settle("T1", OutcomeSide.no)

    preds = led.predictions()
    assert [p.ticker for p in preds] == ["T2", "T1"]  # newest first
    assert preds[0].result is None and preds[0].won is None and not preds[0].acted
    assert preds[1].result is OutcomeSide.no and preds[1].won is False and preds[1].acted
    assert led.predictions(limit=1)[0].ticker == "T2"
    assert led.unsettled_tickers() == ["T2"]  # unfilled signals are tracked to resolution
    led.settle("T2", OutcomeSide.yes)
    assert led.unsettled_tickers() == []
    assert led.performance().settled_count == 1  # signal-only ticker adds no fill

    md = predictions_markdown(led, preds)
    assert "| T1 | yes | 0.4000 | 0.6000 | +0.1800 | 5 | yes | lost (no) |" in md
    assert "| T2 | yes |" in md and "| no | open |" in md
    assert "\\[x\\]\\(http://evil\\) \\| \\`y\\`" in md and "[x](" not in md


def test_ledger_trades_runs_and_dashboard(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from prediction_bot.dashboard import dashboard_data, render_dashboard

    led = Ledger(tmp_path / "l.sqlite", Decimal("100"))
    led.record_fill(
        "s", "T1", OutcomeSide.yes, Decimal("0.4"), Decimal(5), Decimal("0.1"), Decimal("0.6")
    )
    led.record_run(["s"], scanned=10, signals=1, filled=1, settled=0)
    led.settle("T1", OutcomeSide.yes)
    led.record_run(["s", "x"], scanned=12, signals=0, filled=0, settled=1)

    (t,) = led.trades()
    assert t.cost == Decimal("2.1") and t.pnl == Decimal("2.9") and t.result is OutcomeSide.yes
    r1, r2 = led.runs()
    assert (r1.cash, r1.open_cost) == (Decimal("97.9"), Decimal("2.1"))
    assert (r2.cash, r2.open_cost, r2.strategies) == (Decimal("102.9"), Decimal(0), "s,x")

    data = dashboard_data(led)
    assert data["runs"][1]["equity"] == 102.9  # type: ignore[index]
    assert data["trades"][0]["pnl"] == 2.9  # type: ignore[index]

    led.record_signal(
        Signal(
            "s",
            "T2",
            OutcomeSide.no,
            Decimal("0.5"),
            Decimal("0.4"),
            Decimal("0.05"),
            Decimal(1),
            rationale="</script><img src=x onerror=alert(1)>",
        ),
        acted=False,
    )
    page = render_dashboard(led)
    assert page.count("<script") == 2 and "</script><img" not in page
    assert "<\\/script><img" in page  # data stays inside the JSON block, escaped by the JS


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
    no = next(s for s in sigs if s.side is OutcomeSide.no)
    # legs are equal-sized: min(10/0.40=25, 10/0.50=20) so neither side is uncovered
    assert yes.size == no.size == 20
    assert yes.group == no.group == "E-A"


def test_book_scanner_pairs_legs_by_thinner_side(settings) -> None:  # type: ignore[no-untyped-def]
    strat = BookScannerStrategy(settings)
    m = make_market("E-A", yes_bid="0.50", yes_ask="0.40", yes_ask_size="3")  # 3 YES, 50 NO
    sigs = strat.evaluate([m])
    assert sorted(s.size for s in sigs) == [3, 3]


def test_book_scanner_skips_empty_levels(settings) -> None:  # type: ignore[no-untyped-def]
    strat = BookScannerStrategy(settings)
    m = make_market("E-A", yes_bid="0.50", yes_ask="0.40", yes_bid_size="0")
    assert strat.evaluate([m]) == []


def test_engine_grouped_legs_are_all_or_nothing(settings) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(settings.paper_db_path, settings.paper_starting_cash)
    eng = Engine(settings, object(), [BookScannerStrategy(settings)], ledger)  # type: ignore[arg-type]
    # hold 15 YES already; cap 30 means the 20-lot YES leg would breach, so NO must not fill
    ledger.record_fill(
        "seed", "E-A", OutcomeSide.yes, Decimal("0.40"), Decimal(15), Decimal(0), Decimal("0.5")
    )
    settings.max_position_contracts = Decimal(30)
    res = eng.run_cycle([make_market("E-A", yes_bid="0.50", yes_ask="0.40")])
    assert res.acted == []
    assert ledger.position_count("E-A", OutcomeSide.no) == 0


def test_engine_live_cap_counts_venue_positions_and_resting_orders(settings) -> None:  # type: ignore[no-untyped-def]
    class Venue:
        orders: list[tuple[object, ...]] = []

        def live_exposure(self, ticker: str, side: OutcomeSide) -> Decimal:
            return Decimal(95)

        def create_order(self, *a: object) -> dict[str, object]:
            self.orders.append(a)
            return {"order": {}}

    ledger = Ledger(settings.paper_db_path, settings.paper_starting_cash)
    venue = Venue()
    settings.mode = Mode.live
    eng = Engine(settings, venue, [BookScannerStrategy(settings)], ledger, allow_live=True)  # type: ignore[arg-type]
    assert eng.live
    res = eng.run_cycle([make_market("E-A", yes_bid="0.50", yes_ask="0.40")])
    assert res.acted == [] and venue.orders == []


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


def test_engine_live_grouped_legs_are_fok_and_stop_on_kill(settings) -> None:  # type: ignore[no-untyped-def]
    class Venue:
        def __init__(self) -> None:
            self.orders: list[tuple[object, ...]] = []

        def live_exposure(self, ticker: str, side: OutcomeSide) -> Decimal:
            return Decimal(0)

        def create_order(
            self, ticker: str, side: OutcomeSide, count: Decimal, price: Decimal, **kw: object
        ) -> dict[str, object]:
            self.orders.append((side, kw.get("time_in_force")))
            return {"order": {"status": "canceled"}}  # FOK killed

    ledger = Ledger(settings.paper_db_path, settings.paper_starting_cash)
    venue = Venue()
    settings.mode = Mode.live
    eng = Engine(settings, venue, [BookScannerStrategy(settings)], ledger, allow_live=True)  # type: ignore[arg-type]
    res = eng.run_cycle([make_market("E-A", yes_bid="0.50", yes_ask="0.40")])
    assert res.acted == []
    assert venue.orders == [(OutcomeSide.yes, "fill_or_kill")]  # second leg never sent
