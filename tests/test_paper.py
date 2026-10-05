from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from prediction_bot.cli import predictions_markdown
from prediction_bot.config import Mode
from prediction_bot.engine import Engine
from prediction_bot.models import Market, OutcomeSide, Signal, fee_for
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
    assert led.position_count("T1", OutcomeSide.yes, "s") == 10
    assert led.position_count("T1", OutcomeSide.yes, "other") == 0
    assert led.position_count("T1", OutcomeSide.yes, "") == 0
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
    assert (
        "| T1 — YES: Kalshi says 40%, we say 60%; bought at 40¢ | yes | 0.4000 | 0.6000"
        " | +0.1800 | 5 | yes | lost (no) |" in md
    )
    assert "| T2 — YES:" in md and "| no | open |" in md and "| T1 |\n" in md
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


def test_predictions_and_trades_read_as_plain_english(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from prediction_bot.dashboard import dashboard_data, render_dashboard

    led = Ledger(tmp_path / "l.sqlite", Decimal("100"))
    led.remember_markets([make_market("E-A", "0.90", "0.92", title="Boise St. wins.")])
    led.record_signal(
        Signal("s", "E-A", OutcomeSide.no, Decimal("0.10"), Decimal("0.08"), Decimal("0.01"), 5),
        acted=True,
    )
    led.record_fill(
        "s", "E-A", OutcomeSide.no, Decimal("0.08"), Decimal(5), Decimal("0.01"), Decimal("0.10")
    )
    led.record_signal(
        Signal("s", "E-B", OutcomeSide.yes, Decimal("0.60"), Decimal("0.55"), Decimal("0.02"), 1),
        acted=False,
    )
    led.record_signal(  # live-style: acted but no fill recorded
        Signal("s", "E-C", OutcomeSide.yes, Decimal("0.60"), Decimal("0.55"), Decimal("0.02"), 1),
        acted=True,
    )
    live, unknown, old = led.predictions()
    assert old.summary == "Boise St. wins — NO: Kalshi says 8%, we say 10%; bought at 8¢"
    assert unknown.summary == "E-B — YES: Kalshi says 55%, we say 60%; would buy at 55¢"
    assert live.summary == "E-C — YES: Kalshi says 55%, we say 60%; placed order at 55¢"
    (t,) = led.trades()
    assert t.summary == old.summary
    led.remember_markets([make_market("E-C", "0.5", "0.55", title="")])  # no title known yet
    assert led.untitled_tickers() == ["E-B", "E-C"]

    # book_scanner fair values are 1 - other leg's ask, not probabilities
    led.record_fill(
        "book_scanner",
        "E-B",
        OutcomeSide.no,
        Decimal("0.50"),
        Decimal(1),
        Decimal(0),
        Decimal("0.60"),
    )
    assert led.trades()[0].summary == (
        "E-B — NO leg of a YES+NO arbitrage: bought at 50¢; pair costs 90¢ for a $1 payout"
    )

    data = dashboard_data(led)
    assert data["predictions"][2]["summary"] == old.summary  # type: ignore[index]
    assert data["trades"][1]["summary"] == t.summary  # type: ignore[index]

    led.remember_markets([make_market("E-B", "0.5", "0.55", title="<b>x</b></script>")])
    page = render_dashboard(led)
    assert page.count("<script") == 2 and "<b>x<\\/b><\\/script>" in page


def test_settle_backfills_titles_for_old_rows(settings) -> None:  # type: ignore[no-untyped-def]
    class Venue:
        asked: list[list[str] | None] = []

        def get_markets(self, status: str | None, tickers: list[str] | None) -> list[Market]:
            self.asked.append(tickers)
            return [
                make_market("E-A", "0.5", "0.5", title="A wins", result="yes"),
                make_market("E-B", "0.5", "0.5", title="B wins"),
            ]

    ledger = Ledger(settings.paper_db_path, settings.paper_starting_cash)
    ledger.record_fill(
        "s", "E-A", OutcomeSide.yes, Decimal("0.5"), Decimal(1), Decimal(0), Decimal("0.6")
    )
    ledger.settle("E-A", OutcomeSide.yes)  # settled before titles existed
    ledger.record_fill(
        "s", "E-B", OutcomeSide.yes, Decimal("0.5"), Decimal(1), Decimal(0), Decimal("0.6")
    )
    venue = Venue()
    eng = Engine(settings, venue, [], ledger)  # type: ignore[arg-type]
    assert eng.settle_open_positions() == {}
    assert venue.asked == [["E-A", "E-B"]]
    assert {t.ticker: t.title for t in ledger.trades()} == {"E-A": "A wins", "E-B": "B wins"}
    assert ledger.untitled_tickers() == []


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


def test_book_scanner_requires_gap_to_cover_both_fees(settings) -> None:  # type: ignore[no-untyped-def]
    settings.min_edge = Decimal("0.01")
    strat = BookScannerStrategy(settings)
    # yes ask 0.49, no ask 0.48 -> 3c gap; each leg's fee is 2c, so per-leg edge is 1c
    # but buying both costs 1.01 per pair: must be rejected.
    losing = make_market("E-A", yes_bid="0.52", yes_ask="0.49")
    assert strat.evaluate([losing]) == []
    # 6c gap covers both fees (0.0175 each at 20 contracts) -> 2.5c locked per pair.
    winning = make_market("E-B", yes_bid="0.55", yes_ask="0.49")
    sigs = strat.evaluate([winning])
    assert len(sigs) == 2
    assert all(s.edge == Decimal("0.025") for s in sigs)
    ledger = Ledger(settings.paper_db_path, settings.paper_starting_cash)
    eng = Engine(settings, object(), [strat], ledger)  # type: ignore[arg-type]
    eng.run_cycle([winning])
    (yes, no) = ledger.positions()
    assert yes.cost + no.cost < yes.count  # a locked pair costs less than its $1 payout


def test_book_scanner_edge_uses_size_aware_fees(settings) -> None:  # type: ignore[no-untyped-def]
    # YES 0.01 / NO 0.96: per-contract fees (ceil to 1c each) say edge 0.02, but at the
    # 10-contract common size fees total $0.04 -> 0.026/contract, which clears 0.025.
    settings.min_edge = Decimal("0.025")
    sigs = BookScannerStrategy(settings).evaluate(
        [make_market("E-A", yes_bid="0.04", yes_ask="0.01")]
    )
    assert [s.size for s in sigs] == [10, 10]
    assert all(s.edge == Decimal("0.026") for s in sigs)


def test_book_scanner_skips_empty_levels(settings) -> None:  # type: ignore[no-untyped-def]
    strat = BookScannerStrategy(settings)
    m = make_market("E-A", yes_bid="0.50", yes_ask="0.40", yes_bid_size="0")
    assert strat.evaluate([m]) == []


def test_engine_grouped_legs_are_all_or_nothing(settings) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(settings.paper_db_path, settings.paper_starting_cash)
    eng = Engine(settings, object(), [BookScannerStrategy(settings)], ledger)  # type: ignore[arg-type]
    # hold 15 YES already; cap 30 means the 20-lot YES leg would breach, so NO must not fill
    ledger.record_fill(
        "book_scanner",
        "E-A",
        OutcomeSide.yes,
        Decimal("0.40"),
        Decimal(15),
        Decimal(0),
        Decimal("0.5"),
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
    res = eng.run_cycle([make_market("E-A", yes_bid="0.50", yes_ask="0.40", title="Team A wins")])
    assert len(res.acted) == 2
    assert ledger.cash < settings.paper_starting_cash
    assert all(t.title == "Team A wins" for t in ledger.trades())
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


def test_paper_position_cap_is_per_strategy(tmp_path, settings) -> None:  # type: ignore[no-untyped-def]
    from prediction_bot.strategies.base import Strategy

    class Fixed(Strategy):
        def __init__(self, settings, name: str) -> None:  # type: ignore[no-untyped-def]
            super().__init__(settings)
            self.name = name

        def evaluate(self, markets):  # type: ignore[no-untyped-def]
            return [
                Signal(
                    self.name,
                    "T1",
                    OutcomeSide.yes,
                    Decimal("0.5"),
                    Decimal("0.30"),
                    Decimal("0.1"),
                    Decimal(30),
                    "x",
                    group="shared",
                )
            ]

    settings.max_position_contracts = Decimal(30)
    ledger = Ledger(tmp_path / "l.sqlite", Decimal("1000"))
    eng = Engine(settings, object(), [Fixed(settings, "a"), Fixed(settings, "b")], ledger)  # type: ignore[arg-type]
    res = eng.run_cycle([])
    # both strategies fill their own 30 contracts even when their legs share a group;
    # neither is blocked by the other's book
    assert [s.strategy for s in res.acted] == ["a", "b"]
    assert ledger.position_count("T1", OutcomeSide.yes, "a") == 30
    assert ledger.position_count("T1", OutcomeSide.yes, "b") == 30
    assert eng.run_cycle([]).acted == []


def test_kill_switch_and_daily_loss_halt_entries(tmp_path, settings) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "l.sqlite", Decimal("1000"))
    strat = BookScannerStrategy(settings)
    market = make_market("E-A", yes_bid="0.50", yes_ask="0.40")

    settings.kill_switch = True
    eng = Engine(settings, object(), [strat], ledger)  # type: ignore[arg-type]
    res = eng.run_cycle([market])
    assert res.signals and res.acted == [] and res.halted is not None
    assert "kill switch" in res.halted
    assert all(not p.acted for p in ledger.predictions())

    settings.kill_switch = False
    settings.max_daily_loss = Decimal("5")
    ledger.record_fill(
        "book_scanner",
        "L1",
        OutcomeSide.yes,
        Decimal("0.80"),
        Decimal(10),
        Decimal(0),
        Decimal("0.9"),
    )
    ledger.settle("L1", OutcomeSide.no)  # -8.00 today
    res = eng.run_cycle([market])
    assert res.acted == [] and res.halted is not None and "daily loss" in res.halted

    # a winning settlement later the same day must not reopen trading: the halt is latched
    ledger.record_fill(
        "book_scanner",
        "W1",
        OutcomeSide.yes,
        Decimal("0.10"),
        Decimal(10),
        Decimal(0),
        Decimal("0.9"),
    )
    ledger.settle("W1", OutcomeSide.yes)  # +9.00, day now +1.00
    assert ledger.realized_pnl_since(datetime(2000, 1, 1)) > 0
    res = eng.run_cycle([market])
    assert res.acted == [] and res.halted is not None and "halted until" in res.halted

    fresh = Ledger(tmp_path / "l2.sqlite", Decimal("1000"))
    settings.max_daily_loss = Decimal("50")
    res = Engine(settings, object(), [strat], fresh).run_cycle([market])  # type: ignore[arg-type]
    assert res.halted is None and res.acted


def test_performance_by_strategy(tmp_path) -> None:  # type: ignore[no-untyped-def]
    led = Ledger(tmp_path / "l.sqlite", Decimal("100"))
    led.record_fill(
        "a", "T1", OutcomeSide.yes, Decimal("0.40"), Decimal(10), Decimal("0.20"), Decimal("0.6")
    )
    led.record_fill(
        "b", "T1", OutcomeSide.no, Decimal("0.50"), Decimal(10), Decimal("0.30"), Decimal("0.5")
    )
    led.record_fill(
        "b", "T2", OutcomeSide.yes, Decimal("0.30"), Decimal(5), Decimal("0.10"), Decimal("0.4")
    )
    led.settle("T1", OutcomeSide.yes)
    by = led.performance_by_strategy()
    assert set(by) == {"a", "b"}
    assert by["a"].realized_pnl == Decimal("10") - Decimal("4.00") - Decimal("0.20")
    assert by["a"].wins == 1 and by["a"].settled_count == 1
    assert by["b"].realized_pnl == -Decimal("5.00") - Decimal("0.30")
    assert by["b"].wins == 0 and by["b"].settled_count == 1 and by["b"].open_positions == 1
    total = led.performance()
    assert total.realized_pnl == by["a"].realized_pnl + by["b"].realized_pnl
    assert total.fees_paid == Decimal("0.60")
