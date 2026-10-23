from __future__ import annotations

from decimal import Decimal

import pytest
import typer

from prediction_bot.cli import _validate_order
from prediction_bot.config import Settings
from prediction_bot.engine import CycleResult


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
