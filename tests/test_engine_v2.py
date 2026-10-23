from __future__ import annotations

from decimal import Decimal

from prediction_bot.engine import Engine
from prediction_bot.models import OutcomeSide, Signal
from prediction_bot.paper.ledger import Ledger
from prediction_bot.strategies.base import Strategy
from tests.conftest import make_market


class FixedSignals(Strategy):
    name = "sportsbook_v2"

    def __init__(self, settings, signals: list[Signal], name: str = "sportsbook_v2") -> None:  # type: ignore[no-untyped-def]
        super().__init__(settings)
        self.signals = signals
        self.name = name

    def evaluate(self, markets):  # type: ignore[no-untyped-def]
        return self.signals


def v2_signal(
    *,
    ticker: str = "KXNFLGAME-26OCT25KCLV-LV",
    key: str = "KXNFLGAME-26OCT25KCLV:las-vegas-raiders",
    size: str = "10",
) -> Signal:
    return Signal(
        strategy="sportsbook_v2",
        ticker=ticker,
        side=OutcomeSide.yes,
        fair_prob=Decimal("0.8"),
        limit_price=Decimal("0.5"),
        edge=Decimal("0.28"),
        size=Decimal(size),
        rules_version="v2",
        position_key=key,
        event_ticker="KXNFLGAME-26OCT25KCLV",
        tags=("value",),
        fee_type="quadratic_with_maker_fees",
        fee_multiplier=Decimal(1),
    )


def test_v2_position_cap_applies_across_cycles(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "position.sqlite", Decimal("1000"))
    market = make_market("KXNFLGAME-26OCT25KCLV-LV", "0.49", "0.5", subtitle="Las Vegas")
    first = v2_signal(size="10")
    engine = Engine(settings, object(), [FixedSignals(settings, [first])], ledger)  # type: ignore[arg-type]
    assert engine.run_cycle([market]).acted == [first]
    assert ledger.position_cost(first.position_key or "") == Decimal("5.18")
    assert ledger.conn.execute("SELECT fee_paid FROM fills").fetchone()[0] == "0.18"

    too_large = v2_signal(size="50")
    engine = Engine(settings, object(), [FixedSignals(settings, [too_large])], ledger)  # type: ignore[arg-type]
    assert engine.run_cycle([market]).acted == []
    assert ledger.conn.execute("SELECT reason FROM blocks").fetchone()[0] == "cap_position"


def test_v2_blocks_opposite_position_in_same_event(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "opposite.sqlite", Decimal("1000"))
    first = v2_signal()
    market = make_market(first.ticker, "0.49", "0.5", subtitle="Las Vegas")
    engine = Engine(settings, object(), [FixedSignals(settings, [first])], ledger)  # type: ignore[arg-type]
    assert engine.run_cycle([market]).acted == [first]

    opposite = v2_signal(
        ticker="KXNFLGAME-26OCT25KCLV-KC",
        key="KXNFLGAME-26OCT25KCLV:kansas-city-chiefs",
    )
    other_market = make_market(opposite.ticker, "0.49", "0.5", subtitle="Kansas City")
    engine = Engine(settings, object(), [FixedSignals(settings, [opposite])], ledger)  # type: ignore[arg-type]
    assert engine.run_cycle([other_market]).acted == []
    assert ledger.conn.execute("SELECT reason FROM blocks ORDER BY id DESC LIMIT 1").fetchone()[
        0
    ] == ("opposite_position")


def test_v2_event_cost_cap_is_independent_of_position_cap(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings.v2_position_bankroll_fraction = Decimal("0.10")
    settings.v2_event_bankroll_fraction = Decimal("0.01")
    ledger = Ledger(tmp_path / "event.sqlite", Decimal("1000"))
    signal = v2_signal(size="10")
    market = make_market(signal.ticker, "0.49", "0.5", subtitle="Las Vegas")
    engine = Engine(settings, object(), [FixedSignals(settings, [signal])], ledger)  # type: ignore[arg-type]
    assert len(engine.run_cycle([market]).acted) == 1
    engine = Engine(settings, object(), [FixedSignals(settings, [signal])], ledger)  # type: ignore[arg-type]
    assert engine.run_cycle([market]).acted == []
    assert ledger.conn.execute("SELECT reason FROM blocks").fetchone()[0] == "cap_event"


def test_rules_version_v2_is_paper_only(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from prediction_bot.config import Mode

    settings.mode = Mode.live
    settings.kalshi_api_base = "https://api.elections.kalshi.com/trade-api/v2"
    ledger = Ledger(tmp_path / "live.sqlite", Decimal("1000"))
    signal = v2_signal()
    market = make_market(signal.ticker, "0.49", "0.5", subtitle="Las Vegas")
    engine = Engine(
        settings,
        object(),
        [FixedSignals(settings, [signal])],
        ledger,
        allow_live=True,
    )  # type: ignore[arg-type]
    assert engine.run_cycle([market]).acted == []
    assert ledger.conn.execute("SELECT reason FROM blocks").fetchone()[0] == "paper_only"


def test_v2_unknown_fee_metadata_fails_closed(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "unknown-fee.sqlite", Decimal("1000"))
    signal = v2_signal()
    signal = Signal(
        **{
            **signal.__dict__,
            "fee_type": None,
            "fee_multiplier": None,
        }
    )
    market = make_market(signal.ticker, "0.49", "0.5", subtitle="Las Vegas")
    engine = Engine(settings, object(), [FixedSignals(settings, [signal])], ledger)  # type: ignore[arg-type]

    assert engine.run_cycle([market]).acted == []
    assert ledger.conn.execute("SELECT reason FROM blocks").fetchone()[0] == "fee_unknown"


def test_shadow_strategy_is_recorded_without_acting(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "shadow.sqlite", Decimal("1000"))
    signal = Signal(
        strategy="sportsbook_arb",
        ticker="KXNFLGAME-26OCT25KCLV-LV",
        side=OutcomeSide.yes,
        fair_prob=Decimal("0.8"),
        limit_price=Decimal("0.5"),
        edge=Decimal("0.29"),
        size=Decimal(2),
        rules_version=None,
        position_key="KXNFLGAME-26OCT25KCLV:las-vegas-raiders",
        event_ticker="KXNFLGAME-26OCT25KCLV",
    )
    shadow = FixedSignals(settings, [signal], name="sportsbook_arb")
    market = make_market(signal.ticker, "0.49", "0.5", subtitle="Las Vegas")
    engine = Engine(
        settings,
        object(),
        [],
        ledger,
        shadow_strategies=[shadow],
    )  # type: ignore[arg-type]

    assert engine.run_cycle([market]).acted == []
    assert ledger.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    row = ledger.conn.execute(
        "SELECT rules_version,strategy,would_trade,reason FROM shadow_decisions"
    ).fetchone()
    assert row == ("v1", "sportsbook_arb", 1, "")
