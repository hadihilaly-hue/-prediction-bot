from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from prediction_bot.clv_report import clv_report, edge_bucket, format_report, price_band, summarize
from prediction_bot.config import Settings
from prediction_bot.paper.ledger import Ledger

KICKOFF = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)


def _near_miss(
    ledger: Ledger,
    *,
    reason: str = "net_edge",
    net_edge: str = "-0.025",
    price: str = "0.50",
    clv_mid: str | None = "1.5",
    clv_bid: str | None = "1.0",
    window: str = "72h+",
    status: str = "ok",
    created_at: str = "2026-10-25T00:00:00+00:00",
    ticker: str = "KXNFLGAME-26NOV01ARIDAL-ARI",
    strategy: str = "sportsbook_v3",
    rules_version: str = "v3",
    would_trade: int = 0,
    model_side: int | None = 1,
    fair: str | None = None,
    mid: str | None = None,
    side: str = "yes",
    tags: str | None = None,
) -> None:
    ledger.conn.execute(
        "INSERT INTO shadow_decisions (created_at,rules_version,window,strategy,ticker,side,"
        "would_trade,reason,price,net_edge,clv_kalshi,clv_kalshi_bid,clv_status,commence_time,"
        "model_side,fair,mid,tags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            created_at,
            rules_version,
            window,
            strategy,
            ticker,
            side,
            would_trade,
            reason,
            price,
            net_edge,
            clv_mid,
            clv_bid,
            status,
            KICKOFF.isoformat(),
            model_side,
            fair,
            mid,
            tags,
        ),
    )


def test_summarize_applies_the_buchdahl_rule() -> None:
    assert summarize([])["verdict"] == "open"
    small = summarize([1.0, 2.0, 3.0])
    assert (small["n"], small["mean"], small["verdict"]) == (3, 2.0, "open")
    assert small["sd"] == pytest.approx(1.0)
    assert small["beat_close"] == 1.0
    assert summarize([1.0] * 25 + [2.0] * 25)["verdict"] == "supported"
    assert summarize([-1.0] * 25 + [-2.0] * 25)["verdict"] == "rejected"
    assert summarize([-1.0] * 25 + [1.0] * 25)["verdict"] == "open"
    one = summarize([0.0])
    assert one["sd"] is None and one["ci95"] is None and one["beat_close"] == 0.0


def test_buckets_compare_numerically_not_as_text() -> None:
    # SQLite compares these TEXT columns as strings, where '-0.026' < '-0.03'.
    assert edge_bucket(Decimal("-0.026")) == "-3..-2c"
    assert edge_bucket(Decimal("-0.051")) == "<-5c"
    assert edge_bucket(Decimal("-0.03")) == "-3..-2c"
    assert edge_bucket(Decimal("0")) == "0..+1c"
    assert edge_bucket(Decimal("0.013")) == ">=+1c"
    assert edge_bucket(None) == "unknown"
    assert [price_band(Decimal(p)) for p in ("0.24", "0.25", "0.40", "0.60", "0.80")] == [
        "<0.25",
        "0.25-0.40",
        "0.40-0.60",
        "0.60-0.80",
        ">=0.80",
    ]


def test_report_cohorts_filters_and_breakouts(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "paper.sqlite", Decimal("100"))
    _near_miss(ledger, net_edge="-0.026", clv_mid="-0.5", clv_bid="-1.0", price="0.30")
    _near_miss(ledger, net_edge="-0.012", clv_mid="1.5", clv_bid="1.0", price="0.65")
    _near_miss(ledger, status="in_game", clv_mid="9", clv_bid="9")  # excluded
    _near_miss(ledger, created_at="2026-10-20T00:00:00+00:00", clv_mid="9")  # before since
    _near_miss(ledger, reason="entry_window", window="24-72h", clv_mid="-2", clv_bid="-2.5")
    _near_miss(ledger, rules_version="v2")  # wrong rules version
    _near_miss(
        ledger,
        strategy="line_move_follower",
        rules_version="v2",
        reason="line_move_follower",
        would_trade=1,
        net_edge="",
        clv_mid="0.5",
        clv_bid="0.0",
    )
    ledger.conn.execute(
        "INSERT INTO fills (created_at,strategy,ticker,side,price,count,fee,fair_prob,"
        "commence_time,clv_kalshi,clv_kalshi_bid,clv_status,net_edge) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            (KICKOFF - timedelta(hours=30)).isoformat(),
            "sportsbook_v3",
            "KXMLSGAME-26NOV01CHIORL-TIE",
            "yes",
            "0.21",
            "10",
            "0.10",
            "0.25",
            KICKOFF.isoformat(),
            "3.0",
            "2.5",
            "ok",
            "0.03",
        ),
    )
    ledger.conn.commit()

    report = clv_report(ledger.conn, since="2026-10-24")
    net = report["cohorts"]["v3_net_edge"]["metrics"]
    assert net["clv_kalshi"]["all"]["n"] == 2
    assert net["clv_kalshi"]["all"]["mean"] == pytest.approx(0.5)
    assert net["clv_kalshi_bid"]["all"]["mean"] == pytest.approx(0.0)
    assert set(net["clv_kalshi"]["by_edge_bucket"]) == {"-3..-2c", "-2..-1c"}
    assert list(net["clv_kalshi"]["by_price_band"]) == ["0.25-0.40", "0.60-0.80"]
    assert net["clv_kalshi"]["by_sport"]["KXNFLGAME"]["n"] == 2
    assert net["clv_sharp_close"]["all"]["n"] == 0

    window = report["cohorts"]["v3_entry_window"]["metrics"]["clv_kalshi"]
    assert window["by_window"]["24-72h"]["mean"] == pytest.approx(-2.0)

    lmf = report["cohorts"]["line_move_follower"]["metrics"]["clv_kalshi"]
    assert lmf["all"]["n"] == 1 and list(lmf["by_edge_bucket"]) == ["unknown"]

    fills = report["cohorts"]["fills"]["metrics"]["clv_kalshi"]
    assert list(fills["by_window"]) == ["24-72h"]  # derived from created_at and commence
    assert list(fills["by_sport"]) == ["KXMLSGAME"]

    everything = clv_report(ledger.conn)["cohorts"]["v3_net_edge"]["metrics"]
    assert everything["clv_kalshi"]["all"]["n"] == 3
    text = format_report(report)
    assert "v3 near misses blocked by net_edge" in text
    assert "clv_sharp_close (devigged sharp-book close minus entry): no scored rows" in text


def test_clv_report_command_prints_json(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import prediction_bot.cli as cli

    path = tmp_path / "paper.sqlite"
    ledger = Ledger(path, Decimal("100"))
    _near_miss(ledger)
    ledger.conn.commit()
    settings = Settings(_env_file=None, paper_db_path=path)  # type: ignore[call-arg]
    monkeypatch.setattr(cli, "_settings", lambda: settings)

    cli.clv_report_command(since="2026-10-24", as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert data["since"] == "2026-10-24"
    assert data["cohorts"]["v3_net_edge"]["metrics"]["clv_kalshi_bid"]["all"]["n"] == 1
    assert data["rule"]["min_games"] == 30
