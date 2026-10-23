from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from prediction_bot.config import Settings
from prediction_bot.engine import Engine
from prediction_bot.models import OutcomeSide, Signal
from prediction_bot.paper.ledger import Ledger
from prediction_bot.strategies.base import Strategy


class V3Signals(Strategy):
    name = "sportsbook_v3"
    rules_version = "v3"

    def __init__(self, settings: Settings, signal: Signal) -> None:
        super().__init__(settings)
        self.signal = signal

    def evaluate(self, markets):  # type: ignore[no-untyped-def]
        return [self.signal]


class CandleClient:
    def __init__(self, candles=None, error: Exception | None = None) -> None:  # type: ignore[no-untyped-def]
        self.candles = candles or []
        self.error = error
        self.candle_calls = 0
        self.order_calls = 0

    def get_candlesticks(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        self.candle_calls += 1
        if self.error is not None:
            raise self.error
        return self.candles

    def create_order(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        self.order_calls += 1
        raise AssertionError("paper maker execution must not call an order endpoint")


def maker_signal(created: datetime, *, edge: str = "0.01", maker_price: str = "0.45") -> Signal:
    commence = created + timedelta(days=2)
    return Signal(
        strategy="sportsbook_v3",
        ticker="KXNFLGAME-26OCT20KCLV-KC",
        side=OutcomeSide.yes,
        fair_prob=Decimal("0.70"),
        limit_price=Decimal("0.50"),
        edge=Decimal(edge),
        size=Decimal(10),
        created_at=created,
        rules_version="v3",
        position_key="KXNFLGAME-26OCT20KCLV:kansas-city-chiefs",
        event_ticker="KXNFLGAME-26OCT20KCLV",
        tags=("value",),
        fee_type="quadratic_with_maker_fees",
        fee_multiplier=Decimal(1),
        maker_price=Decimal(maker_price),
        commence_time=commence,
    )


def settings_for_maker(**overrides) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(_env_file=None, rules_version="v3", v3_execution="maker_first", **overrides)


def test_maker_first_places_local_order_without_a_fill(tmp_path) -> None:  # type: ignore[no-untyped-def]
    created = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    signal = maker_signal(created)
    settings = settings_for_maker()
    ledger = Ledger(tmp_path / "maker-place.sqlite", Decimal("1000"))
    client = CandleClient()
    engine = Engine(
        settings,
        client,  # type: ignore[arg-type]
        [V3Signals(settings, signal)],
        ledger,
        clock=lambda: created,
    )

    result = engine.run_cycle([])

    assert result.acted == []
    assert result.orders_placed == [signal]
    assert ledger.conn.execute("SELECT status FROM paper_orders").fetchone()[0] == "open"
    assert ledger.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    assert client.order_calls == 0


def test_later_trade_through_candle_creates_maker_fill_at_candle_time(tmp_path) -> None:  # type: ignore[no-untyped-def]
    created = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    fill_at = created + timedelta(minutes=5)
    signal = maker_signal(created)
    settings = settings_for_maker()
    ledger = Ledger(tmp_path / "maker-resolve.sqlite", Decimal("1000"))
    client = CandleClient()
    Engine(
        settings,
        client,  # type: ignore[arg-type]
        [V3Signals(settings, signal)],
        ledger,
        clock=lambda: created,
    ).run_cycle([])
    client.candles = [
        {
            "end_period_ts": int(fill_at.timestamp()),
            "price": {"low_dollars": "0.44", "high_dollars": "0.52"},
        }
    ]
    result = Engine(
        settings,
        client,  # type: ignore[arg-type]
        [V3Signals(settings, signal)],
        ledger,
        clock=lambda: created + timedelta(minutes=10),
    ).run_cycle([])

    assert result.paper_fills_resolved == 1
    assert result.acted == []
    row = ledger.conn.execute(
        "SELECT created_at,price,count,fee,rules_version,tags FROM fills"
    ).fetchone()
    assert row == (
        fill_at.isoformat(),
        "0.45",
        "10",
        "0.05",
        "v3",
        "maker",
    )
    assert ledger.conn.execute("SELECT fill_rule FROM paper_orders").fetchone()[0] == (
        "trade_through"
    )
    assert client.order_calls == 0


def test_candle_fetch_failure_keeps_order_open_and_blocks_resting_position(tmp_path) -> None:  # type: ignore[no-untyped-def]
    created = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    signal = maker_signal(created)
    settings = settings_for_maker()
    ledger = Ledger(tmp_path / "maker-retry.sqlite", Decimal("1000"))
    client = CandleClient()
    Engine(
        settings,
        client,  # type: ignore[arg-type]
        [V3Signals(settings, signal)],
        ledger,
        clock=lambda: created,
    ).run_cycle([])
    client.error = RuntimeError("candles unavailable")

    result = Engine(
        settings,
        client,  # type: ignore[arg-type]
        [V3Signals(settings, signal)],
        ledger,
        clock=lambda: created + timedelta(minutes=5),
    ).run_cycle([])

    assert result.acted == []
    assert ledger.conn.execute("SELECT status FROM paper_orders").fetchone()[0] == "open"
    assert ledger.conn.execute("SELECT reason FROM blocks").fetchone()[0] == "order_resting"
    assert client.order_calls == 0


def test_expired_order_takes_taker_fallback_only_when_net_edge_holds(tmp_path) -> None:  # type: ignore[no-untyped-def]
    created = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    for edge, expected in (("0.03", "filled"), ("0.01", "open")):
        settings = settings_for_maker()
        ledger = Ledger(tmp_path / f"maker-fallback-{edge}.sqlite", Decimal("1000"))
        signal = maker_signal(created, edge=edge)
        order_id = ledger.record_paper_order(
            signal,
            Decimal("0.45"),
            Decimal("0.12"),
            created + timedelta(minutes=1),
        )
        ledger.expire_paper_order(order_id, created + timedelta(minutes=1))

        result = Engine(
            settings,
            CandleClient(),  # type: ignore[arg-type]
            [V3Signals(settings, signal)],
            ledger,
            clock=lambda: created + timedelta(minutes=2),
        ).run_cycle([])

        assert len(result.acted) == int(expected == "filled")
        statuses = [
            row[0] for row in ledger.conn.execute("SELECT status FROM paper_orders ORDER BY id")
        ]
        expected_statuses = ["expired"] if expected == "filled" else ["expired", "open"]
        assert statuses == expected_statuses


def test_crossing_maker_price_falls_back_to_taker_and_default_is_taker(tmp_path) -> None:  # type: ignore[no-untyped-def]
    created = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    for mode, maker_price in (("maker_first", "0.51"), ("taker", "0.45")):
        settings = Settings(_env_file=None, rules_version="v3", v3_execution=mode)
        signal = maker_signal(created, edge="0.03", maker_price=maker_price)
        ledger = Ledger(tmp_path / f"maker-cross-{mode}.sqlite", Decimal("1000"))

        result = Engine(
            settings,
            CandleClient(),  # type: ignore[arg-type]
            [V3Signals(settings, signal)],
            ledger,
            clock=lambda: created,
        ).run_cycle([])

        assert result.acted == [signal]
        assert result.orders_placed == []
        assert ledger.conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0
        assert ledger.conn.execute("SELECT rules_version,tags FROM fills").fetchone() == (
            "v3",
            "value",
        )


def test_paper_order_expiry_is_limited_by_ttl_and_kickoff(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = settings_for_maker()
    engine = Engine(
        settings,
        CandleClient(),  # type: ignore[arg-type]
        [],
        Ledger(tmp_path / "maker-expiry.sqlite", Decimal("100")),
    )
    created = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    long_lead = maker_signal(created)
    assert engine._paper_order_expiry(long_lead) == created + timedelta(minutes=480)
    near_kickoff = Signal(
        **{
            **long_lead.__dict__,
            "commence_time": created + timedelta(hours=1),
        }
    )
    assert engine._paper_order_expiry(near_kickoff) < near_kickoff.commence_time
    assert engine._paper_order_expiry(near_kickoff) < created + timedelta(minutes=480)
