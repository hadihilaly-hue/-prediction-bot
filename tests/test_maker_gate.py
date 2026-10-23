from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from prediction_bot import cli
from prediction_bot.config import Settings
from prediction_bot.dashboard import dashboard_data, render_dashboard
from prediction_bot.engine import CycleResult, Engine
from prediction_bot.maker_gate import MakerGate, evaluate_maker_gate
from prediction_bot.models import OutcomeSide
from prediction_bot.paper.ledger import Ledger


def _settings(**overrides) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(_env_file=None, rules_version="v3", **overrides)


def _add_fill(
    ledger: Ledger,
    position_key: str,
    clv: str,
    *,
    version: str = "v3",
    tags: tuple[str, ...] | None = None,
    count: str = "1",
) -> None:
    ledger.record_fill(
        "sportsbook_v3",
        f"KXNFLGAME-{position_key}-HOME",
        OutcomeSide.yes,
        Decimal("0.50"),
        Decimal(count),
        Decimal(0),
        Decimal("0.60"),
        rules_version=version,
        position_key=position_key,
        tags=tags,
    )
    ledger.conn.execute(
        "UPDATE fills SET clv_kalshi=?,clv_status='ok' WHERE id=(SELECT MAX(id) FROM fills)",
        (clv,),
    )
    ledger.conn.commit()


def _add_maker_shadows(
    ledger: Ledger,
    count: int = 50,
    filled_clv: int = 20,
    clv: str = "2",
) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    for index in range(count):
        trade_through = now if index < filled_clv else None
        shadow_clv = clv if index < filled_clv else None
        ledger.conn.execute(
            "INSERT INTO maker_shadow (created_at,ticker,side,position_key,price,size,fair,"
            "maker_fee,status,status_at,trade_through_at,event_ticker,expires_at,rules_version,"
            "fill_trade_through_at,clv_kalshi,score_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                now,
                f"KXNFLGAME-MAKER{index}-HOME",
                "yes",
                f"maker-{index}",
                "0.45",
                "1",
                "0.60",
                "0.01",
                "filled" if trade_through else "expired",
                now,
                trade_through,
                f"KXNFLGAME-MAKER{index}",
                now,
                "v3",
                trade_through,
                shadow_clv,
                "scored",
            ),
        )
    ledger.conn.commit()


def _ready_ledger(ledger: Ledger, maker_mean: str = "2") -> None:
    for index in range(50):
        _add_fill(ledger, f"taker-{index}", "2")
    _add_maker_shadows(ledger, clv=maker_mean)


def test_empty_ledger_fails_taker_positions() -> None:
    gate = evaluate_maker_gate(Ledger(":memory:", Decimal("100")), _settings())
    assert not gate.passed
    assert gate.reason == "taker_positions"
    assert gate.taker_n == 0


def test_taker_ci_is_null_with_fewer_than_two_positions() -> None:
    ledger = Ledger(":memory:", Decimal("100"))
    _add_fill(ledger, "one", "2")
    gate = evaluate_maker_gate(ledger, _settings())
    assert gate.taker_ci_low is None
    assert gate.taker_ci_high is None


def test_position_clv_is_contract_weighted_and_positions_are_equal_weighted() -> None:
    ledger = Ledger(":memory:", Decimal("1000"))
    _add_fill(ledger, "position-a", "0", count="3")
    _add_fill(ledger, "position-a", "3", count="1")
    _add_fill(ledger, "position-b", "1")

    gate = evaluate_maker_gate(ledger, _settings())

    assert gate.taker_n == 2
    assert gate.taker_mean == Decimal("0.875")


def test_maker_gate_passes_with_complete_evidence() -> None:
    ledger = Ledger(":memory:", Decimal("1000"))
    _ready_ledger(ledger)

    gate = evaluate_maker_gate(ledger, _settings())

    assert gate.passed
    assert gate.reason == "passed"
    assert gate.taker_n == 50
    assert gate.taker_ci_low is not None and gate.taker_ci_low > 0
    assert gate.maker_scored == 50
    assert gate.maker_clv_n == 20
    assert gate.maker_mean == gate.taker_mean


def test_maker_mean_below_taker_fails_last_gate() -> None:
    ledger = Ledger(":memory:", Decimal("1000"))
    _ready_ledger(ledger, maker_mean="1.9")
    gate = evaluate_maker_gate(ledger, _settings())
    assert gate.reason == "maker_worse_than_taker"


def test_taker_ci_straddling_zero_fails() -> None:
    ledger = Ledger(":memory:", Decimal("1000"))
    for index in range(25):
        _add_fill(ledger, f"negative-{index}", "-1")
        _add_fill(ledger, f"positive-{index}", "1")
    _add_maker_shadows(ledger)
    gate = evaluate_maker_gate(ledger, _settings())
    assert gate.taker_ci_low is not None and gate.taker_ci_low < 0
    assert gate.reason == "taker_ci"


def test_maker_and_non_v3_fills_are_excluded_from_taker_evidence() -> None:
    ledger = Ledger(":memory:", Decimal("1000"))
    _add_fill(ledger, "maker", "20", tags=("maker",))
    _add_fill(ledger, "v2", "20", version="v2")
    _add_fill(ledger, "v1", "20", version="v1")
    gate = evaluate_maker_gate(ledger, _settings())
    assert gate.taker_n == 0
    assert gate.reason == "taker_positions"


def test_maker_gate_ignores_non_ok_taker_clv() -> None:
    ledger = Ledger(":memory:", Decimal("1000"))
    _add_fill(ledger, "valid", "2")
    _add_fill(ledger, "in-game", "100")
    ledger.conn.execute("UPDATE fills SET clv_status='in_game' WHERE position_key='in-game'")
    ledger.conn.commit()

    gate = evaluate_maker_gate(ledger, _settings())

    assert gate.taker_n == 1
    assert gate.taker_mean == Decimal("2")


def test_maker_gate_enforces_scored_and_filled_clv_minima() -> None:
    ledger = Ledger(":memory:", Decimal("1000"))
    for index in range(50):
        _add_fill(ledger, f"taker-{index}", "2")
    _add_maker_shadows(ledger, count=49, filled_clv=20)
    assert evaluate_maker_gate(ledger, _settings()).reason == "maker_scored"

    ledger = Ledger(":memory:", Decimal("1000"))
    for index in range(50):
        _add_fill(ledger, f"taker-{index}", "2")
    _add_maker_shadows(ledger, count=50, filled_clv=19)
    assert evaluate_maker_gate(ledger, _settings()).reason == "maker_filled_clv"


def test_auto_resolution_and_live_fallback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    passed_gate = MakerGate(
        50, Decimal(2), Decimal(1), Decimal(3), 50, 20, Decimal(2), True, "passed"
    )
    monkeypatch.setattr(cli, "evaluate_maker_gate", lambda *_args: passed_gate)
    ledger = Ledger(":memory:", Decimal("1000"))
    settings = _settings(v3_execution="auto")

    resolved, gate = cli._resolve_v3_execution(settings, ledger, ["sportsbook_v3"])
    assert resolved.v3_execution == "maker_first"
    assert gate == passed_gate

    resolved_live, gate_live = cli._resolve_v3_execution(
        _settings(v3_execution="auto", mode="live"), ledger, ["sportsbook_v3"]
    )
    assert resolved_live.v3_execution == "taker"
    assert gate_live is None


def test_paper_auto_logs_once_per_run_and_explicit_modes_do_not(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class EngineStub:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def settle_open_positions(self):  # type: ignore[no-untyped-def]
            return {}

        def run_cycle(self, _markets):  # type: ignore[no-untyped-def]
            return CycleResult()

    monkeypatch.setattr(cli, "_client", lambda _settings: object())
    monkeypatch.setattr(cli, "_odds_client", lambda _settings: None)
    monkeypatch.setattr(cli, "_strategies", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(cli, "_fetch", lambda *_args: [])
    monkeypatch.setattr(cli, "Engine", EngineStub)
    monkeypatch.setattr(cli, "score_maker_shadows", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(cli, "record_clv", lambda *_args, **_kwargs: 0)

    auto_path = tmp_path / "auto.sqlite"
    monkeypatch.setattr(
        cli,
        "_settings",
        lambda: _settings(v3_execution="auto", paper_db_path=str(auto_path)),
    )
    cli.paper(strategy=["sportsbook_v3"], cycles=2, interval=0)
    auto_ledger = Ledger(auto_path, Decimal("1000"))
    assert auto_ledger.conn.execute("SELECT COUNT(*) FROM execution_mode_log").fetchone()[0] == 2
    assert auto_ledger.conn.execute(
        "SELECT requested,resolved FROM execution_mode_log ORDER BY id"
    ).fetchall() == [("auto", "taker"), ("auto", "taker")]

    for mode in ("taker", "maker_first"):
        path = tmp_path / f"{mode}.sqlite"
        monkeypatch.setattr(
            cli,
            "_settings",
            lambda mode=mode, path=path: _settings(v3_execution=mode, paper_db_path=str(path)),
        )
        cli.paper(strategy=["sportsbook_v3"], cycles=1, interval=0)
        explicit_ledger = Ledger(path, Decimal("1000"))
        assert (
            explicit_ledger.conn.execute("SELECT COUNT(*) FROM execution_mode_log").fetchone()[0]
            == 0
        )


def test_taker_mode_still_expires_open_paper_orders(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expires = created + timedelta(hours=1)
    now = created + timedelta(hours=2)
    ledger = Ledger(":memory:", Decimal("1000"))
    ledger.conn.execute(
        "INSERT INTO paper_orders (created_at,strategy,rules_version,ticker,side,position_key,"
        "event_ticker,price,size,fair,maker_fee,expires_at,commence_time,status,status_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?)",
        (
            created.isoformat(),
            "sportsbook_v3",
            "v3",
            "KXNFLGAME-TEST-HOME",
            "yes",
            "position",
            "KXNFLGAME-TEST",
            "0.45",
            "1",
            "0.60",
            "0.01",
            expires.isoformat(),
            (created + timedelta(days=2)).isoformat(),
            created.isoformat(),
        ),
    )
    monkeypatch.setattr("prediction_bot.engine.fetch_candles", lambda *_args: [])
    settings = _settings(v3_execution="taker")
    result = Engine(
        settings,
        object(),  # type: ignore[arg-type]
        [],
        ledger,
        clock=lambda: now,
    ).run_cycle([])
    assert result.paper_fills_resolved == 0
    assert ledger.conn.execute("SELECT status FROM paper_orders").fetchone()[0] == "expired"


def test_dashboard_quality_includes_maker_gate() -> None:
    data = dashboard_data(Ledger(":memory:", Decimal("100")), _settings())
    measurement = data["measurement"]
    assert isinstance(measurement, dict)
    quality = measurement["quality"]
    assert isinstance(quality, dict)
    gate = quality["maker_gate"]
    assert isinstance(gate, dict)
    assert gate["reason"] == "taker_positions"
    assert gate["configured"] == "taker"
    assert gate["thresholds"] == {
        "min_positions": 50,
        "min_scored": 50,
        "min_filled_clv": 20,
    }
    assert gate["resolved"] is None
    assert gate["requested"] is None
    assert "G.configured === 'auto' && G.passed" in render_dashboard(
        Ledger(":memory:", Decimal("100")), _settings()
    )
