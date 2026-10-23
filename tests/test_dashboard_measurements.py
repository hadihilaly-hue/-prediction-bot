from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import cast

from prediction_bot.dashboard import dashboard_data, render_dashboard
from prediction_bot.models import MakerShadowCandidate, OutcomeSide, Signal
from prediction_bot.paper.ledger import Ledger


def test_dashboard_clv_cohorts_report_bucket_verdicts_and_exclusions(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "dashboard-clv-cohorts.sqlite", Decimal("1000"))
    created = datetime(2026, 10, 1, tzinfo=timezone.utc)
    commence = created + timedelta(hours=100)
    decisions: list[tuple[Signal, str]] = []
    scores: list[tuple[str, str | None, str, str]] = []
    for label, count, edge, bid, status in (
        ("supported", 50, "0.005", "1", "ok"),
        ("rejected", 50, "0.025", "-1", "ok"),
        ("open", 5, "0.015", "1", "ok"),
        ("excluded", 1, "0.010", None, "no_commence"),
    ):
        for index in range(count):
            event = f"KXNCAAFGAME-26OCT10{label}{index}"
            ticker = f"{event}-HOME"
            decisions.append(
                (
                    Signal(
                        strategy="sportsbook_v2",
                        ticker=ticker,
                        side=OutcomeSide.yes,
                        fair_prob=Decimal("0.70"),
                        limit_price=Decimal("0.50"),
                        edge=Decimal(edge),
                        size=Decimal(0),
                        created_at=created,
                        rules_version="v2",
                        position_key=f"{event}:{index}",
                        event_ticker=event,
                        commence_time=commence,
                    ),
                    "net_edge",
                )
            )
            scores.append((ticker, bid, "0.5", status))
    assert ledger.record_near_miss_decisions(decisions) == 106
    ledger.conn.executemany(
        "UPDATE shadow_decisions SET clv_kalshi_bid=?,clv_kalshi=?,clv_status=? WHERE ticker=?",
        [(bid, clv, status, ticker) for ticker, bid, clv, status in scores],
    )
    ledger.conn.commit()

    measurement = cast(dict[str, object], dashboard_data(ledger)["measurement"])
    quality = cast(dict[str, object], measurement["quality"])
    by_edge = cast(dict[str, object], quality["clv_by_edge"])
    buckets = cast(list[dict[str, object]], by_edge["buckets"])
    supported = next(row for row in buckets if row["edge_bucket"] == "0-1")
    rejected = next(row for row in buckets if row["edge_bucket"] == "2-3")
    opened = next(row for row in buckets if row["edge_bucket"] == "1-2")
    assert (supported["n"], supported["verdict"]) == (50, "supported")
    assert (rejected["n"], rejected["verdict"]) == (50, "rejected")
    assert (opened["n"], opened["verdict"]) == (5, "open")
    assert by_edge["excluded_by_status"] == [
        {"rules_version": "v2", "clv_status": "no_commence", "n": 1}
    ]
    by_window = cast(dict[str, object], quality["clv_by_window"])
    windows = cast(list[dict[str, object]], by_window["windows"])
    assert windows[0]["window"] == "72h+"
    assert windows[0]["n"] == 105
    assert windows[0]["mean_clv_kalshi"] == 0.5


def test_dashboard_measurements_are_position_based(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "dashboard.sqlite", Decimal("1000"))
    ticker = "KXNFLGAME-26OCT25KCLV-LV"
    ledger.record_fill(
        "sportsbook_v2",
        ticker,
        OutcomeSide.yes,
        Decimal("0.50"),
        Decimal("10"),
        Decimal("0.10"),
        Decimal("0.70"),
        rules_version="v2",
        position_key="KXNFLGAME-26OCT25KCLV:las-vegas-raiders",
        event_ticker="KXNFLGAME-26OCT25KCLV",
        tags=("underdog", "value"),
        commence_time=datetime(2026, 10, 25, 20, tzinfo=timezone.utc),
    )
    ledger.conn.execute(
        "INSERT INTO settlements (ticker,settled_at,result,pnl) VALUES (?,?,?,?)",
        (ticker, datetime.now(timezone.utc).isoformat(), "yes", "4.90"),
    )
    ledger.conn.execute(
        "UPDATE fills SET clv_kalshi='5',clv_gameday='3',clv_exit='7',"
        "clv_status='ok' WHERE ticker=?",
        (ticker,),
    )
    ledger.record_maker_shadows(
        [
            MakerShadowCandidate(
                created_at=datetime(2026, 10, 24, 20, tzinfo=timezone.utc),
                ticker=ticker,
                side=OutcomeSide.yes,
                event_ticker="KXNFLGAME-26OCT25KCLV",
                position_key="KXNFLGAME-26OCT25KCLV:las-vegas-raiders:maker",
                price=Decimal("0.50"),
                size=Decimal("2"),
                fair=Decimal("0.70"),
                maker_fee=Decimal("0.10"),
                expires_at=datetime(2026, 10, 25, 19, 50, tzinfo=timezone.utc),
                commence_time=datetime(2026, 10, 25, 20, tzinfo=timezone.utc),
            )
        ]
    )
    ledger.conn.execute(
        "UPDATE maker_shadow SET status='filled',clv_kalshi='4' WHERE ticker=?",
        (ticker,),
    )
    ledger.conn.commit()
    ledger.record_run(["sportsbook_v2"], 1, 1, 1, 0, odds_used=2, odds_remaining=10)

    data = dashboard_data(ledger)
    measurement = data["measurement"]
    assert len(measurement["positions"]) == 1
    position = measurement["positions"][0]
    assert position["version"] == "v2"
    assert position["summary"] == "Las Vegas Raiders win in KXNFLGAME-26OCT25KCLV"
    assert position["strategy_tags"] == "sportsbook_v2, underdog, value"
    assert position["clv"] == 5
    assert position["clv_gameday"] == 3
    assert position["clv_exit"] == 7
    assert measurement["brier_positions"]["n"] == 1
    assert measurement["brier_fills"]["market_value"] == 0.25
    assert measurement["brier_positions"]["market_value"] == 0.25
    assert measurement["by_version"]["v2"]["brier_fills"]["n"] == 1
    assert measurement["by_version"]["v3"]["brier_fills"]["n"] == 0
    assert measurement["by_version"]["v1"]["brier_fills"]["n"] == 0
    assert measurement["by_version"]["v2"]["clv"]["league"]["KXNFLGAME"]["n"] == 1
    assert (
        measurement["by_version"]["v2"]["clv"]["league"]["KXNFLGAME"]["evidence_note"]
        == "too few to be evidence"
    )
    assert measurement["by_version"]["v2"]["monte_carlo"]["runs"] == 20_000
    assert measurement["by_version"]["v2"]["calibration"]["70-79%"]["observed_win_rate"] == 1.0
    assert measurement["by_version"]["v2"]["calibration"]["70-79%"]["predicted_win_rate"] == 0.7
    assert measurement["quality"]["maker_shadow"] == {"filled": 1}
    assert measurement["quality"]["maker_shadow_pnl"]["value"] == 0.9
    assert measurement["quality"]["maker_shadow_clv"]["mean"] == 4.0
    assert measurement["quality"]["maker_shadow_scoring"]["trade_through"]["scored"] == 0
    assert measurement["quality"]["odds_used"] == 2
    assert measurement["quality"]["odds_remaining"] == 10

    ledger.record_run(["book_scanner"], 1, 0, 0, 0)
    latest_measurement = dashboard_data(ledger)["measurement"]
    assert latest_measurement["quality"]["odds_used"] == 2
    assert latest_measurement["quality"]["odds_remaining"] == 10


def test_dashboard_renders_captioned_measurement_tables(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "dashboard-render.sqlite", Decimal("1000"))
    html = render_dashboard(ledger)

    for caption in (
        "Calibration by ten-cent bucket",
        "Brier scores use 2,000 game-bootstrap resamples",
        "Monte Carlo estimates the chance",
        "Settled position P&L with all outcomes",
        "Block reasons show the last seven days",
        "Maker-shadow counts by status",
        "Shadow decisions by entry window",
    ):
        assert caption in html
    assert 'data-rules="v2"' in html
    assert 'data-rules="v1"' in html
    assert 'id="evidence"' in html
    assert "How much evidence is there?" in html
    assert "Expected return if the Kalshi closing price is the true chance" in html
    assert "closing-price gain by how far ahead of the game" in html
    for heading in (
        "Expected wins",
        "Actual wins",
        "Bot Brier",
        "Market Brier",
        "Bot 95% CI",
        "P(≥ realized)",
        "Mean simulated P&L",
        "Last 7 days",
        "Would trade",
    ):
        assert heading in html
    assert "JSON.stringify(V.calibration" not in html
    assert "JSON.stringify(M.quality" not in html


def test_dashboard_shows_phase_four_experiment_metrics(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "phase-four-dashboard.sqlite", Decimal("1000"))
    ticker = "KXNFLGAME-26OCT25KCLV-LV"
    ledger.record_fill(
        "sportsbook_v2",
        ticker,
        OutcomeSide.yes,
        Decimal("0.30"),
        Decimal("10"),
        Decimal("0.10"),
        Decimal("0.60"),
        rules_version="v2",
        position_key="KXNFLGAME-26OCT25KCLV:las-vegas-raiders",
        event_ticker="KXNFLGAME-26OCT25KCLV",
        tags=("exp_road_dog",),
        commence_time=datetime(2026, 10, 25, 20, 25, tzinfo=timezone.utc),
    )
    ledger.conn.execute(
        "INSERT INTO settlements (ticker,settled_at,result,pnl) VALUES (?,?,?,?)",
        (ticker, datetime.now(timezone.utc).isoformat(), "yes", "6.90"),
    )
    ledger.conn.execute(
        "UPDATE fills SET clv_kalshi='2.5',clv_status='in_game' WHERE ticker=?",
        (ticker,),
    )
    shadow = Signal(
        strategy="line_move_follower",
        ticker=ticker,
        side=OutcomeSide.yes,
        fair_prob=Decimal("0.60"),
        limit_price=Decimal("0.50"),
        edge=Decimal("0.10"),
        size=Decimal(1),
        created_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
        position_key="KXNFLGAME-26OCT25KCLV:las-vegas-raiders",
        event_ticker="KXNFLGAME-26OCT25KCLV",
        commence_time=datetime(2026, 10, 25, 20, 25, tzinfo=timezone.utc),
    )
    ledger.record_shadow_decision(shadow, "v2", reason="line_move_follower")
    ledger.conn.execute(
        "UPDATE shadow_decisions SET clv_kalshi='3.5',clv_status='ok' "
        "WHERE strategy='line_move_follower'"
    )
    ledger.conn.commit()

    measurement = dashboard_data(ledger)["measurement"]
    assert measurement["quality"]["exp_road_dog"]["positions"] == 1
    assert measurement["quality"]["exp_road_dog"]["pnl"] == 6.9
    assert measurement["quality"]["exp_road_dog"]["clv_n"] == 0
    assert measurement["quality"]["exp_road_dog"]["clv_mean"] is None
    assert measurement["quality"]["line_move_follower"]["observations"] == 1
    assert measurement["quality"]["line_move_follower"]["settled_clv_mean"] == 3.5
    html = render_dashboard(ledger)
    assert "Line-move follower is shadow-only" in html
    assert "Road-dog experiment evaluation at 50 and 100 positions" in html


def test_dashboard_data_includes_v3_fills_in_their_own_version(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "dashboard-v3.sqlite", Decimal("1000"))
    ticker = "KXNFLGAME-26OCT25KCLV-LV"
    ledger.record_fill(
        "sportsbook_v3",
        ticker,
        OutcomeSide.yes,
        Decimal("0.50"),
        Decimal("2"),
        Decimal("0"),
        Decimal("0.70"),
        rules_version="v3",
        position_key="KXNFLGAME-26OCT25KCLV:las-vegas-raiders",
        event_ticker="KXNFLGAME-26OCT25KCLV",
        net_edge=Decimal("0.015"),
    )
    ledger.conn.execute(
        "INSERT INTO settlements (ticker,settled_at,result,pnl) VALUES (?,?,?,?)",
        (ticker, datetime.now(timezone.utc).isoformat(), "yes", "1"),
    )
    ledger.conn.execute(
        "UPDATE fills SET clv_exit='2.5',clv_kalshi_bid='2.5',clv_status='ok' WHERE ticker=?",
        (ticker,),
    )
    ledger.conn.commit()

    measurement = dashboard_data(ledger)["measurement"]
    assert measurement["positions"][0]["version"] == "v3"
    assert measurement["positions"][0]["clv_exit"] == 2.5
    assert measurement["by_version"]["v3"]["brier_fills"]["n"] == 1
    assert measurement["by_version"]["v2"]["brier_fills"]["n"] == 0
    assert measurement["by_version"]["v1"]["brier_fills"]["n"] == 0
    quality = cast(dict[str, object], measurement["quality"])
    clv_by_edge = cast(dict[str, object], quality["clv_by_edge"])
    edge_buckets = cast(list[dict[str, object]], clv_by_edge["buckets"])
    v3_bucket = next(
        row for row in edge_buckets if row["edge_bucket"] == "1-2" and row["rules_version"] == "v3"
    )
    assert v3_bucket["n"] == 1
    assert v3_bucket["mean_clv_kalshi_bid"] == 2.5


def test_dashboard_v1_clv_aggregate_excludes_in_game_fill(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "dashboard-clv-hygiene.sqlite", Decimal("1000"))
    for index, (ticker, clv, status) in enumerate(
        (
            ("KXNFLGAME-26OCT01PRE-HOME", "5", "ok"),
            ("KXNFLGAME-26OCT01LIVE-HOME", "38.5", "in_game"),
            ("KXNFLGAME-26OCT01UNKNOWN-HOME", "20", "no_commence"),
        )
    ):
        ledger.record_fill(
            "sportsbook_v1",
            ticker,
            OutcomeSide.yes,
            Decimal("0.50"),
            Decimal(1),
            Decimal(0),
            Decimal("0.60"),
            rules_version="v1",
            position_key=f"position-{index}",
        )
        ledger.conn.execute(
            "UPDATE fills SET clv_kalshi=?,clv_status=? WHERE ticker=?",
            (clv, status, ticker),
        )
    ledger.conn.commit()

    measurement = dashboard_data(ledger)["measurement"]
    clv = measurement["by_version"]["v1"]["clv"]["league"]["KXNFLGAME"]

    assert clv["n"] == 1
    assert clv["mean"] == 5
    assert measurement["quality"]["clv_exclusions"]["v1"]["in_game"] == 1
    assert measurement["quality"]["clv_exclusions"]["v1"]["no_commence"] == 1
    html = render_dashboard(ledger)
    assert "Excluded from closing-price numbers:" in html
