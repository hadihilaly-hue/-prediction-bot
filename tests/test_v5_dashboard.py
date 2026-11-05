from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from prediction_bot.config import Settings
from prediction_bot.dashboard import _v5_health_data, render_dashboard
from prediction_bot.paper.ledger import Ledger

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)


def _fill(
    ledger: Ledger, ticker: str, side: str, price: str, count: str, fee: str, fair: str
) -> None:
    ledger.conn.execute(
        "INSERT INTO fills (created_at,strategy,ticker,side,price,count,fee,fair_prob) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("2026-11-01T00:00:00+00:00", "sportsbook_v3", ticker, side, price, count, fee, fair),
    )


def test_pnl_vs_model_recomputes_each_fill_and_reports_variance(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "pnl.sqlite", Decimal("1000"))
    _fill(ledger, "KXNFLGAME-A-MIA", "yes", "0.40", "10", "0.17", "0.45")  # won
    _fill(ledger, "KXNFLGAME-B-MIN", "no", "0.60", "10", "0.17", "0.62")  # lost
    _fill(ledger, "KXNFLGAME-C-NYJ", "yes", "0.50", "10", "0.18", "0.55")  # still open
    for ticker, result in (("KXNFLGAME-A-MIA", "yes"), ("KXNFLGAME-B-MIN", "yes")):
        ledger.conn.execute(
            "INSERT INTO settlements VALUES (?,?,?,?)", (ticker, "2026-11-02", result, "0")
        )
    pnl = _v5_health_data(ledger, NOW)["pnl_vs_model"]
    assert isinstance(pnl, dict)
    assert pnl["n"] == 2  # the open fill is left out
    # Expected: (0.45-0.40)*10-0.17 + (0.62-0.60)*10-0.17 = 0.33 + 0.03.
    assert pnl["expected"] == pytest.approx(0.36)
    # Realized: 10*(1-0.40)-0.17 won; -(10*0.60+0.17) lost.
    assert pnl["realized"] == pytest.approx(5.83 - 6.17)
    assert pnl["gap"] == pytest.approx(-0.34 - 0.36)
    sd = (100 * 0.45 * 0.55 + 100 * 0.62 * 0.38) ** 0.5
    assert pnl["model_sd"] == pytest.approx(sd)
    assert pnl["gap_in_sd"] == pytest.approx(-0.70 / sd)


def test_freshness_reports_age_and_book_lag_per_feed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "fresh.sqlite", Decimal("1000"))
    for pulled, updated in (("05:00", "04:58"), ("05:30", "05:26"), ("05:45", None)):
        ledger.conn.execute(
            "INSERT INTO odds_snapshots (created_at,feed,event_id,home_team,away_team,"
            "commence_time,team,prob,books_used,max_book_update) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                f"2026-11-05T{pulled}:00+00:00",
                "americanfootball_nfl",
                "e",
                "h",
                "a",
                "2026-11-09T18:00:00Z",
                "h",
                "0.5",
                3,
                None if updated is None else f"2026-11-05T{updated}:00Z",
            ),
        )
    ledger.conn.execute(
        "INSERT INTO market_snapshots (fetched_at,ticker,event_ticker,series,yes_bid_size,"
        "yes_ask_size,open_interest,volume,expected_expiration) VALUES (?,?,?,?,?,?,?,?,?)",
        ("2026-11-05T03:00:00", "T", "E", "KXNFLGAME", "1", "1", "0", "0", "x"),
    )
    rows = {row["feed"]: row for row in _v5_health_data(ledger, NOW)["freshness"]}  # type: ignore[union-attr]
    nfl = rows["Odds: americanfootball_nfl"]
    assert nfl["age_min"] == pytest.approx(15)
    assert nfl["book_age_n"] == 2 and nfl["book_age_p50_min"] == pytest.approx(3)
    assert rows["Kalshi markets"]["age_min"] == pytest.approx(180)  # naive read as UTC
    assert rows["Polymarket reference"]["latest"] is None
    assert rows["Polymarket reference"]["age_min"] is None


def test_runs_per_day_counts_slots_against_48_and_today_so_far(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "runs.sqlite", Decimal("1000"))
    runs = [
        (datetime(2026, 11, 4, 0, 17, tzinfo=timezone.utc), None),
        (datetime(2026, 11, 4, 0, 19, tzinfo=timezone.utc), None),  # same slot twice
        (datetime(2026, 11, 4, 0, 47, tzinfo=timezone.utc), None),
        (datetime(2026, 11, 4, 1, 5, tzinfo=timezone.utc), "plan:americanfootball_nfl"),
        (datetime(2026, 11, 5, 5, 17, tzinfo=timezone.utc), None),
    ]
    for at, reason in runs:
        ledger.record_run(["book_scanner"], 0, 0, 0, 0, at=at, trigger_reason=reason)
    days = {row["day"]: row for row in _v5_health_data(ledger, NOW)["runs_per_day"]}  # type: ignore[union-attr]
    assert days["2026-11-04"] == {
        "day": "2026-11-04",
        "runs": 4,
        "slots_ran": 2,
        "triggered": 1,
        "scheduled": 48,
    }
    # By 06:00 UTC the 00:17 ... 05:47 slots are due: 12.
    assert days["2026-11-05"]["scheduled"] == 12 and days["2026-11-05"]["slots_ran"] == 1


def test_rejections_are_counted_per_reason_per_day(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "blocks.sqlite", Decimal("1000"))
    for created_at, reason in (
        ("2026-11-04T01:00:00+00:00", "net_edge"),
        ("2026-11-04T02:00:00+00:00", "net_edge"),
        ("2026-11-05T01:00:00+00:00", "thin_market"),
        ("2026-10-20T01:00:00+00:00", "net_edge"),  # older than 7 days
    ):
        ledger.conn.execute(
            "INSERT INTO blocks (created_at,reason) VALUES (?,?)", (created_at, reason)
        )
    assert _v5_health_data(ledger, NOW)["rejections"] == [
        {"day": "2026-11-05", "reason": "thin_market", "n": 1},
        {"day": "2026-11-04", "reason": "net_edge", "n": 2},
    ]


def test_dashboard_renders_the_measurement_health_panels(tmp_path) -> None:  # type: ignore[no-untyped-def]
    page = render_dashboard(
        Ledger(tmp_path / "page.sqlite", Decimal("1000")),
        Settings(_env_file=None),  # type: ignore[call-arg]
    )
    assert "<h2>Measurement health</h2>" in page
    for table_id in ("v5cards", "v5clv", "v5maker", "v5fresh", "v5runs", "v5reject"):
        assert f'id="{table_id}"' in page
    assert "Profit and loss is not evidence" in page
    assert "main > * { min-width:0; }" in page
    assert "minmax(min(460px,100%),1fr)" in page  # two-column block fits a phone


def test_readme_states_the_evidence_rule_and_links_the_snapshots() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "## Current rules (v5)" in readme and "## How to read the evidence" in readme
    assert "n ≥ 50" in readme and "P&L is not evidence" in readme
    assert "(docs/evidence/)" in readme
    assert "03:17" not in readme and "ceil(0.07" not in readme
    assert "covers NFL/NCAAF/NBA/NHL" not in readme and "WebSocket" not in readme
