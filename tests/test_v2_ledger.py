from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from prediction_bot.models import (
    Block,
    EdgeReview,
    OutcomeSide,
    QuoteSnapshot,
    Signal,
)
from prediction_bot.odds import ConsensusOdds
from prediction_bot.paper.ledger import Ledger


def test_existing_ledger_gets_additive_v2_columns(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, strategy TEXT NOT NULL,
            ticker TEXT NOT NULL, side TEXT NOT NULL, fair_prob TEXT NOT NULL,
            limit_price TEXT NOT NULL, edge TEXT NOT NULL, size TEXT NOT NULL,
            rationale TEXT NOT NULL, acted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE fills (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, strategy TEXT NOT NULL,
            ticker TEXT NOT NULL, side TEXT NOT NULL, price TEXT NOT NULL,
            count TEXT NOT NULL, fee TEXT NOT NULL, fair_prob TEXT NOT NULL
        );
        CREATE TABLE odds_snapshots (
            id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, feed TEXT NOT NULL,
            event_id TEXT NOT NULL, home_team TEXT NOT NULL, away_team TEXT NOT NULL,
            commence_time TEXT NOT NULL, team TEXT NOT NULL, prob TEXT NOT NULL,
            books_used INTEGER NOT NULL
        );
        """
    )
    conn.close()

    ledger = Ledger(path, Decimal("100"))
    expected = {
        "signals": {
            "rules_version",
            "position_key",
            "event_ticker",
            "tags",
            "commence_time",
            "fee_type",
            "fee_multiplier",
        },
        "fills": {
            "rules_version",
            "position_key",
            "event_ticker",
            "tags",
            "commence_time",
            "fee_model_version",
        },
        "odds_snapshots": {"max_book_update"},
    }
    for table, columns in expected.items():
        actual = {str(row[1]) for row in ledger.conn.execute(f"PRAGMA table_info({table})")}
        assert columns <= actual
    assert ledger.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
    ledger.conn.close()


def test_v2_metadata_and_snapshot_helpers_use_aware_utc(tmp_path) -> None:
    ledger = Ledger(tmp_path / "v2.sqlite", Decimal("100"))
    start = datetime(2026, 10, 25, 20, tzinfo=timezone.utc)
    naive = datetime(2026, 10, 23, 7)
    signal = Signal(
        strategy="sportsbook_v2",
        ticker="KXNFLGAME-26OCT25KCLV-LV",
        side=OutcomeSide.yes,
        fair_prob=Decimal("0.8"),
        limit_price=Decimal("0.7"),
        edge=Decimal("0.09"),
        size=Decimal("5"),
        created_at=naive,
        rules_version="v2",
        position_key="KXNFLGAME-26OCT25KCLV:las-vegas-raiders",
        event_ticker="KXNFLGAME-26OCT25KCLV",
        tags=("value", "underdog"),
        commence_time=start,
        fee_type="quadratic_with_maker_fees",
        fee_multiplier=Decimal("0.5"),
    )
    ledger.record_signal(signal, acted=True)
    ledger.record_fill(
        signal.strategy,
        signal.ticker,
        signal.side,
        signal.limit_price,
        signal.size,
        Decimal("0.01"),
        signal.fair_prob,
        at=naive,
        rules_version=signal.rules_version,
        position_key=signal.position_key,
        event_ticker=signal.event_ticker,
        tags=signal.tags,
        commence_time=signal.commence_time,
        fee_model_version="kalshi-v1",
    )
    row = ledger.conn.execute(
        "SELECT created_at, rules_version, position_key, event_ticker, tags, commence_time,"
        " fee_type, fee_multiplier"
        " FROM signals"
    ).fetchone()
    assert row == (
        "2026-10-23T07:00:00+00:00",
        "v2",
        signal.position_key,
        signal.event_ticker,
        "underdog,value",
        "2026-10-25T20:00:00+00:00",
        "quadratic_with_maker_fees",
        "0.5",
    )
    assert ledger.position_cost(signal.position_key or "") == Decimal("3.51")
    assert ledger.event_cost(signal.event_ticker or "") == Decimal("3.51")
    assert ledger.held_keys(signal.event_ticker or "") == {signal.position_key}

    ledger.record_quote_snapshots(
        [
            QuoteSnapshot(
                ticker=signal.ticker,
                event_ticker=signal.event_ticker or "",
                yes_bid=Decimal("0.68"),
                yes_ask=Decimal("0.7"),
                last_price=Decimal("0.69"),
                volume=Decimal("50"),
                open_interest=Decimal("10"),
                created_at=naive,
            )
        ]
    )
    assert ledger.first_quote(signal.ticker) == (Decimal("0.68"), Decimal("0.7"))

    game = ConsensusOdds(
        event_id="game-1",
        home_team="Kansas City Chiefs",
        away_team="Las Vegas Raiders",
        commence_time=start.isoformat(),
        probs={"Kansas City Chiefs": Decimal("0.4"), "Las Vegas Raiders": Decimal("0.6")},
        books_used=2,
        book_updates={"book-a": "2026-10-23T06:55:00Z"},
    )
    ledger.record_consensus({"americanfootball_nfl": {"game-1": game}}, at=naive)
    assert ledger.first_consensus("game-1") == game.probs
    assert (
        ledger.previous_consensus("game-1", datetime(2026, 10, 24, tzinfo=timezone.utc))
        == game.probs
    )
    assert (
        ledger.conn.execute(
            "SELECT max_book_update FROM odds_snapshots ORDER BY id LIMIT 1"
        ).fetchone()[0]
        == "2026-10-23T06:55:00+00:00"
    )

    block = Block(
        reason="kickoff_guard",
        strategy="sportsbook_v2",
        ticker=signal.ticker,
        event_ticker=signal.event_ticker or "",
        created_at=naive,
    )
    review = EdgeReview(
        ticker=signal.ticker,
        side=OutcomeSide.yes,
        fair_prob=Decimal("0.8"),
        price=Decimal("0.7"),
        gross_edge=Decimal("0.1"),
        refetched_price=Decimal("0.7"),
        refetched_fair=Decimal("0.8"),
        confirmed=False,
        whitelisted=False,
        created_at=naive,
    )
    assert ledger.record_blocks([block]) == 1
    assert ledger.record_edge_reviews([review]) == 1
    assert ledger.conn.execute("SELECT reason FROM blocks").fetchone()[0] == "kickoff_guard"
    assert ledger.conn.execute("SELECT confirmed FROM edge_reviews").fetchone()[0] == 0
    ledger.conn.close()


def test_legacy_naive_and_new_aware_settlement_timestamps_compare_as_utc(tmp_path) -> None:
    ledger = Ledger(tmp_path / "timestamps.sqlite", Decimal("100"))
    ledger.conn.execute(
        "INSERT INTO settlements (ticker, settled_at, result, pnl) VALUES (?,?,?,?)",
        ("old", "2026-10-23T00:00:00", "no", "-2"),
    )
    ledger.conn.commit()
    ledger.record_run(
        [],
        scanned=0,
        signals=0,
        filled=0,
        settled=0,
        at=datetime(2026, 10, 23, 1, tzinfo=timezone.utc),
    )
    ledger.conn.execute(
        "INSERT INTO settlements (ticker, settled_at, result, pnl) VALUES (?,?,?,?)",
        ("new", "2026-10-23T01:00:00+00:00", "yes", "1"),
    )
    ledger.conn.commit()
    assert ledger.realized_pnl_since(datetime(2026, 10, 23, tzinfo=timezone.utc)) == Decimal("-1")
    assert ledger.worst_drawdown_since(datetime(2026, 10, 23, tzinfo=timezone.utc)) == Decimal("-2")
    assert ledger.runs()[0].ran_at == "2026-10-23T01:00:00+00:00"
    ledger.conn.close()


def test_v2_risk_queries_include_v3_only_when_requested(tmp_path) -> None:
    ledger = Ledger(tmp_path / "versioned-risk.sqlite", Decimal("100"))
    for version in ("v2", "v3"):
        ledger.record_fill(
            f"sportsbook_{version}",
            f"KXNFLGAME-26OCT25KCLV-LV-{version}",
            OutcomeSide.yes,
            Decimal("0.5"),
            Decimal("2"),
            Decimal("0.1"),
            Decimal("0.8"),
            at=datetime(2026, 10, 23, 7, tzinfo=timezone.utc),
            rules_version=version,
            position_key="KXNFLGAME-26OCT25KCLV:las-vegas-raiders",
            event_ticker="KXNFLGAME-26OCT25KCLV",
        )

    since = datetime(2026, 10, 23, tzinfo=timezone.utc)
    assert ledger.position_cost("KXNFLGAME-26OCT25KCLV:las-vegas-raiders") == Decimal("1.1")
    assert ledger.position_cost(
        "KXNFLGAME-26OCT25KCLV:las-vegas-raiders", include_v3=True
    ) == Decimal("2.2")
    assert ledger.v2_total_open_cost() == Decimal("1.1")
    assert ledger.v2_total_open_cost(include_v3=True) == Decimal("2.2")
    assert ledger.v2_new_risk_since(since) == Decimal("1.1")
    assert ledger.v2_new_risk_since(since, include_v3=True) == Decimal("2.2")
