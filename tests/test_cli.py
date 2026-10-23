from __future__ import annotations

from decimal import Decimal

import pytest
import typer

from prediction_bot.cli import _strategies, _validate_order
from prediction_bot.config import Settings
from prediction_bot.engine import CycleResult
from prediction_bot.paper.ledger import Ledger


class OddsStub:
    requests_used = None
    requests_remaining = None


def test_v1_paper_does_not_implicitly_add_v2_shadow(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import prediction_bot.cli as cli

    settings = Settings(
        _env_file=None,
        rules_version="v1",
        shadow_strategies=[],
        paper_db_path=str(tmp_path / "paper.sqlite"),
    )
    requested: list[list[str]] = []

    class EngineStub:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def settle_open_positions(self):  # type: ignore[no-untyped-def]
            return {}

        def run_cycle(self, markets):  # type: ignore[no-untyped-def]
            return CycleResult()

    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_client", lambda _settings: object())
    monkeypatch.setattr(cli, "_odds_client", lambda _settings: None)
    monkeypatch.setattr(
        cli,
        "_strategies",
        lambda _settings, names, *args, **kwargs: requested.append(list(names)) or [],
    )
    monkeypatch.setattr(cli, "_fetch", lambda _client, _series: [])
    monkeypatch.setattr(cli, "Engine", EngineStub)
    monkeypatch.setattr(cli, "record_clv", lambda *args, **kwargs: 0)

    cli.paper(strategy=["sportsbook_arb"], cycles=1)

    assert requested == [["sportsbook_arb"], []]


def test_scanner_only_paper_uses_configured_clv_budget(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import prediction_bot.cli as cli

    settings = Settings.model_construct(
        rules_version="v1",
        shadow_strategies=[],
        paper_db_path=tmp_path / "scanner-clv.sqlite",
        clv_max_calls_scanner=300,
    )
    budgets: list[int] = []

    class EngineStub:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def settle_open_positions(self):  # type: ignore[no-untyped-def]
            return {}

        def run_cycle(self, markets):  # type: ignore[no-untyped-def]
            return CycleResult()

    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_client", lambda _settings: object())
    monkeypatch.setattr(cli, "_odds_client", lambda _settings: None)

    def strategies_stub(*_args: object, **_kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr(cli, "_strategies", strategies_stub)
    monkeypatch.setattr(cli, "_fetch", lambda _client, _series: [])
    monkeypatch.setattr(cli, "Engine", EngineStub)

    def record_clv_stub(*_args: object, **kwargs: object) -> int:
        max_calls = kwargs.get("max_calls")
        if isinstance(max_calls, int):
            budgets.append(max_calls)
        return 0

    monkeypatch.setattr(cli, "record_clv", record_clv_stub)

    cli.paper(strategy=["book_scanner"], cycles=1)

    assert budgets == [300]


def test_v3_shadows_reuse_the_same_odds_client(settings) -> None:  # type: ignore[no-untyped-def]
    odds = object()
    strategies = _strategies(
        settings,
        ["sportsbook_v3", "sportsbook_v2", "sportsbook_arb", "underdog_value"],
        odds=odds,  # type: ignore[arg-type]
        kalshi=object(),  # type: ignore[arg-type]
        ledger=Ledger(":memory:", Decimal("100")),
    )
    assert [strategy.name for strategy in strategies] == [
        "sportsbook_v3",
        "sportsbook_v2",
        "sportsbook_arb",
        "underdog_value",
    ]
    assert all(getattr(strategy, "odds", None) is odds for strategy in strategies)


def test_v3_only_adds_shadows_for_the_sportsbook_v3_cycle(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import prediction_bot.cli as cli

    settings = Settings(
        _env_file=None,
        rules_version="v3",
        shadow_strategies=[],
        paper_db_path=str(tmp_path / "paper.sqlite"),
    )
    requested: list[list[str]] = []

    class EngineStub:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def settle_open_positions(self):  # type: ignore[no-untyped-def]
            return {}

        def run_cycle(self, markets):  # type: ignore[no-untyped-def]
            return CycleResult()

    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_client", lambda _settings: object())
    odds = OddsStub()
    monkeypatch.setattr(cli, "_odds_client", lambda _settings: odds)
    monkeypatch.setattr(
        cli,
        "_strategies",
        lambda _settings, names, *args, **kwargs: requested.append(list(names)) or [],
    )
    monkeypatch.setattr(cli, "_fetch", lambda _client, _series: [])
    monkeypatch.setattr(cli, "Engine", EngineStub)
    monkeypatch.setattr(cli, "record_clv", lambda *args, **kwargs: 0)

    cli.paper(strategy=["sportsbook_v3"], cycles=1)
    cli.paper(strategy=["book_scanner"], cycles=1)

    assert requested == [
        ["sportsbook_v3"],
        ["sportsbook_v2", "sportsbook_arb", "underdog_value"],
        ["book_scanner"],
        [],
    ]


@pytest.mark.parametrize(
    ("price", "count"),
    [
        ("abc", "1"),
        ("NaN", "1"),
        ("0.5", "Infinity"),
        ("0", "1"),
        ("-0.5", "1"),
        ("1.0", "1"),
        ("0.5", "0"),
        ("0.5", "-3"),
        ("0.01", "101"),  # notional 1.01 <= 10 but count > max_position_contracts (100)
        ("0.5", "40"),  # notional 20 > 10
    ],
)
def test_validate_order_rejects_bad_values(settings, price: str, count: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(typer.BadParameter):
        _validate_order(price, count, settings)


def test_validate_order_accepts_sane_values(settings) -> None:  # type: ignore[no-untyped-def]
    assert _validate_order("0.40", "5", settings) == (Decimal("0.40"), Decimal(5))
