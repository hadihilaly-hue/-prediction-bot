from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import typer

from prediction_bot.cli import _strategies, _validate_order
from prediction_bot.config import Settings
from prediction_bot.engine import CycleResult
from prediction_bot.odds import OddsClient
from prediction_bot.odds_planner import PlannedGame
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


def _seed_trigger_market(ledger: Ledger, now: datetime, *, wide: bool = False) -> str:
    feed = "americanfootball_nfl"
    event = "KXNFLGAME-26OCT25HOMEAWAY"
    ticker = f"{event}-HOME"
    bid, ask = ("0.10", "0.90") if wide else ("0.46", "0.50")
    for fetched, old_bid, old_ask in (
        (now - timedelta(hours=3), "0.45", "0.49"),
        (now - timedelta(hours=2), "0.45", "0.49"),
        (now - timedelta(minutes=1), bid, ask),
    ):
        ledger.conn.execute(
            "INSERT INTO market_snapshots "
            "(fetched_at,ticker,event_ticker,series,yes_bid,yes_ask,yes_bid_size,"
            "yes_ask_size,last_price,open_interest,volume,expected_expiration) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fetched.isoformat(),
                ticker,
                event,
                "KXNFLGAME",
                old_bid,
                old_ask,
                "10",
                "10",
                "0.5",
                "0",
                "0",
                (now + timedelta(days=2)).isoformat(),
            ),
        )
    ledger.record_feed_pulls({feed: 1}, "v3", now - timedelta(hours=2))
    ledger.conn.commit()
    return event


def test_odds_trigger_prints_one_moved_feed_and_dedupes(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import prediction_bot.cli as cli

    now = datetime.now(timezone.utc)
    path = tmp_path / "trigger.sqlite"
    ledger = Ledger(path, Decimal("100"))
    event = _seed_trigger_market(ledger, now)
    settings = Settings(
        _env_file=None,
        rules_version="v3",
        min_lead_hours=72,
        paper_db_path=path,
        odds_trigger_daily_cap=4,
    )
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_client", lambda _settings: object())
    monkeypatch.setattr(cli, "_fetch", lambda _client, _series: [])
    monkeypatch.setattr(
        cli,
        "_planner_games",
        lambda *_args: {
            "americanfootball_nfl": [PlannedGame(now + timedelta(hours=100), event_ticker=event)]
        },
    )

    cli.odds_trigger(series=["KXNFLGAME"])
    assert capsys.readouterr().out.strip() == "americanfootball_nfl"
    cli.odds_trigger(series=["KXNFLGAME"])
    assert capsys.readouterr().out == ""
    assert ledger.conn.execute("SELECT status FROM odds_triggers").fetchall() == [("planned",)]


def test_odds_trigger_daily_cap_is_shared_between_ledger_instances(
    tmp_path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    import prediction_bot.cli as cli

    now = datetime.now(timezone.utc)
    path = tmp_path / "trigger-cap.sqlite"
    first = Ledger(path, Decimal("100"))
    event = _seed_trigger_market(first, now)
    second = Ledger(path, Decimal("100"))
    for index in range(4):
        feed = f"feed-{index}"
        second.record_odds_trigger(feed, f"ticker-{index}", "0.50", now)
        second.finish_odds_trigger(feed, "pulled")
    settings = Settings(
        _env_file=None,
        rules_version="v3",
        min_lead_hours=72,
        paper_db_path=path,
        odds_trigger_daily_cap=4,
    )
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_client", lambda _settings: object())
    monkeypatch.setattr(cli, "_fetch", lambda _client, _series: [])
    monkeypatch.setattr(
        cli,
        "_planner_games",
        lambda *_args: {
            "americanfootball_nfl": [PlannedGame(now + timedelta(hours=100), event_ticker=event)]
        },
    )

    cli.odds_trigger(series=["KXNFLGAME"])
    assert capsys.readouterr().out == ""
    assert second.odds_triggers_pulled_today(now) == 4


def test_odds_trigger_ignores_zero_value_feed(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import prediction_bot.cli as cli

    now = datetime.now(timezone.utc)
    path = tmp_path / "trigger-zero.sqlite"
    ledger = Ledger(path, Decimal("100"))
    event = _seed_trigger_market(ledger, now, wide=True)
    settings = Settings(
        _env_file=None,
        rules_version="v3",
        min_lead_hours=72,
        paper_db_path=path,
    )
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_client", lambda _settings: object())
    monkeypatch.setattr(cli, "_fetch", lambda _client, _series: [])
    monkeypatch.setattr(
        cli,
        "_planner_games",
        lambda *_args: {
            "americanfootball_nfl": [PlannedGame(now + timedelta(hours=100), event_ticker=event)]
        },
    )

    cli.odds_trigger(series=["KXNFLGAME"])
    assert capsys.readouterr().out == ""
    assert ledger.conn.execute("SELECT COUNT(*) FROM odds_triggers").fetchone() == (0,)


def test_paper_trigger_makes_one_paid_odds_request(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import httpx

    import prediction_bot.cli as cli

    now = datetime.now(timezone.utc)
    path = tmp_path / "paper-trigger.sqlite"
    ledger = Ledger(path, Decimal("100"))
    event = _seed_trigger_market(ledger, now)
    feed = "americanfootball_nfl"
    ledger.record_odds_trigger(feed, f"{event}-HOME", "0.50", now)
    ledger.record_odds_pacer_carry(0.5, now)
    settings = Settings(
        _env_file=None,
        rules_version="v3",
        min_lead_hours=72,
        v3_execution="taker",
        paper_db_path=path,
        odds_api_key="key",
        odds_runs_per_day=12,
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                json=[{"commence_time": (now + timedelta(hours=100)).isoformat()}],
                headers={"x-requests-used": "10", "x-requests-remaining": "100"},
            )
        used = "11" if request.url.path.endswith("/odds") else "10"
        headers = {"x-requests-used": used, "x-requests-remaining": "100"}
        if request.url.path.endswith("/odds"):
            headers["x-requests-last"] = "1"
        return httpx.Response(200, json=[], headers=headers)

    odds = OddsClient("key", client=httpx.Client(transport=httpx.MockTransport(handler)))

    class EngineStub:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def settle_open_positions(self):  # type: ignore[no-untyped-def]
            return {}

        def run_cycle(self, markets):  # type: ignore[no-untyped-def]
            odds.h2h(feed)
            return CycleResult()

    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_client", lambda _settings: object())
    monkeypatch.setattr(cli, "_odds_client", lambda _settings: odds)
    monkeypatch.setattr(cli, "_fetch", lambda _client, _series: [])
    monkeypatch.setattr(
        cli,
        "_planner_games",
        lambda *_args: {feed: [PlannedGame(now + timedelta(hours=100), event_ticker=event)]},
    )
    monkeypatch.setattr(cli, "Engine", EngineStub)
    monkeypatch.setattr(cli, "score_maker_shadows", lambda *args, **kwargs: 0)
    monkeypatch.setattr(cli, "backfill_clv_commence_times", lambda *args, **kwargs: 0)
    monkeypatch.setattr(cli, "record_clv", lambda *args, **kwargs: 0)
    monkeypatch.setattr(cli, "refresh_clv_statuses", lambda *args, **kwargs: 0)

    cli.paper(strategy=["sportsbook_v3"], series=["KXNFLGAME"], cycles=1, trigger=feed)

    assert sum(request.url.path.endswith("/odds") for request in requests) == 1
    assert ledger.conn.execute("SELECT status FROM odds_triggers").fetchone() == ("pulled",)
    assert ledger.conn.execute("SELECT trigger_reason FROM runs").fetchone() == (f"move:{feed}",)
    assert ledger.odds_pacer_carry() == pytest.approx(-0.5)
