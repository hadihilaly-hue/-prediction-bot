"""Change-only writes for quote_snapshots and blocks answer every reader the same way.

The same random observation stream is recorded twice: once one row per observation (the
writer before the change, reproduced here as raw INSERTs) and once through Ledger's
change-only writers. Every reader query must agree on a sample of (ticker, time) pairs.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from prediction_bot.dashboard import _blocks_week_start
from prediction_bot.models import Block, QuoteSnapshot
from prediction_bot.paper.ledger import Ledger

UTC = timezone.utc
START = datetime(2026, 11, 1, 22, 0, tzinfo=UTC)  # crosses UTC midnights
TICKERS = [f"KXNFLGAME-26NOV0{i}-T{i}" for i in range(1, 6)]


def _quote_stream(rng: random.Random, steps: int) -> list[list[QuoteSnapshot]]:
    prices = [None, Decimal("0.41"), Decimal("0.42"), Decimal("0.43")]
    state = {t: [Decimal("0.42"), Decimal("0.44"), None, Decimal(10), Decimal(5)] for t in TICKERS}
    batches = []
    for step in range(steps):
        at = START + timedelta(minutes=10 * step)
        batch = []
        for ticker in TICKERS:
            if rng.random() < 0.25:
                continue  # not observed this tick
            row = state[ticker]
            if rng.random() < 0.35:  # something moves
                column = rng.randrange(5)
                if column < 3:
                    row[column] = rng.choice(prices)
                else:
                    row[column] = row[column] + rng.choice([0, 1, 1])
            batch.append(
                QuoteSnapshot(ticker, ticker.rsplit("-", 1)[0], *row, created_at=at)  # type: ignore[arg-type]
            )
        batches.append(batch)
    return batches


def _old_quote_insert(ledger: Ledger, batch: list[QuoteSnapshot]) -> None:
    ledger.conn.executemany(
        "INSERT INTO quote_snapshots (created_at, ticker, event_ticker, yes_bid, yes_ask,"
        " last_price, volume, open_interest) VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                s.created_at.isoformat(),
                s.ticker,
                s.event_ticker,
                None if s.yes_bid is None else str(s.yes_bid),
                None if s.yes_ask is None else str(s.yes_ask),
                None if s.last_price is None else str(s.last_price),
                str(s.volume),
                str(s.open_interest),
            )
            for s in batch
        ],
    )
    ledger.conn.commit()


def _pair(tmp_path: Path) -> tuple[Ledger, Ledger]:
    return Ledger(tmp_path / "old.sqlite", Decimal(100)), Ledger(
        tmp_path / "new.sqlite", Decimal(100)
    )


LATEST = (
    "SELECT yes_bid,yes_ask,last_price,volume,open_interest FROM quote_snapshots "
    "WHERE ticker=? AND created_at<=? ORDER BY created_at DESC,id DESC LIMIT 1"
)
LATEST_TWO_SIDED = (
    "SELECT yes_bid,yes_ask FROM quote_snapshots WHERE ticker=? AND created_at<=? "
    "AND yes_bid IS NOT NULL AND yes_ask IS NOT NULL ORDER BY created_at DESC,id DESC LIMIT 1"
)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_quote_readers_are_unchanged(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    batches = _quote_stream(rng, steps=400)
    old, new = _pair(tmp_path)
    for batch in batches:
        _old_quote_insert(old, batch)
        new.record_quote_snapshots(batch)
    n_old = old.conn.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0]
    n_new = new.conn.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0]
    assert n_new < 0.8 * n_old, (n_new, n_old)  # repeats were folded

    end = START + timedelta(minutes=10 * 400)
    observed = [s.created_at for b in batches for s in b]
    samples = [
        (rng.choice(TICKERS), START + (end - START) * rng.random() - timedelta(hours=1))
        for _ in range(400)
    ] + [(rng.choice(TICKERS), rng.choice(observed)) for _ in range(400)]
    for ticker, t in samples:
        stamp = t.isoformat()
        for sql in (LATEST, LATEST_TWO_SIDED):
            assert (
                old.conn.execute(sql, (ticker, stamp)).fetchone()
                == new.conn.execute(sql, (ticker, stamp)).fetchone()
            ), (sql, ticker, stamp)
        assert old.previous_quote_snapshot(ticker, t) == new.previous_quote_snapshot(ticker, t)
        assert old.quote_at(ticker, stamp) == new.quote_at(ticker, stamp)
    for ticker in TICKERS:
        assert old.first_quote(ticker) == new.first_quote(ticker)
    # Dashboard freshness: newest observation per table.
    fresh = "SELECT MAX(COALESCE(last_seen_at,created_at)) FROM quote_snapshots"
    assert old.conn.execute(fresh).fetchone() == new.conn.execute(fresh).fetchone()


def test_an_out_of_order_quote_is_inserted_not_merged(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "q.sqlite", Decimal(100))
    q = dict(
        ticker="T-1",
        event_ticker="T",
        yes_bid=Decimal("0.4"),
        yes_ask=Decimal("0.5"),
        last_price=None,
        volume=Decimal(1),
        open_interest=Decimal(1),
    )
    ledger.record_quote_snapshots([QuoteSnapshot(**q, created_at=START)])  # type: ignore[arg-type]
    ledger.record_quote_snapshots([QuoteSnapshot(**q, created_at=START - timedelta(hours=1))])  # type: ignore[arg-type]
    assert ledger.conn.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0] == 2


# ------------------------------------------------------------------------ blocks


def _block_stream(rng: random.Random, steps: int) -> list[list[Block]]:
    """Per ticker, the block reason usually persists (as real blocks do), so merged rows
    run for hours and often straddle the edges of the 36h-2h large-gap window."""
    reasons = ["mapping_failed", "no_odds_match", "net_edge", "big_edge", "large_gap"]
    current = {ticker: rng.choice(reasons) for ticker in TICKERS}
    batches = []
    for step in range(steps):
        at = START + timedelta(minutes=30 * step)
        batch = []
        for ticker in TICKERS:
            if rng.random() < 0.1:
                current[ticker] = rng.choice(reasons)
            if rng.random() < 0.2:
                continue
            reason = current[ticker]
            batch.append(
                Block(
                    reason=reason,
                    strategy="sportsbook_v3",
                    ticker=ticker,
                    event_ticker=ticker.rsplit("-", 1)[0],
                    detail=rng.choice(["", "", "", "x"]) if reason == "net_edge" else "",
                    rules_version="v3",
                    created_at=at,
                    position_key=f"{ticker}:yes",
                )
            )
        if rng.random() < 0.05:  # tickerless blocks never merge
            batch.append(Block("dq_stop", "sportsbook_v3", "", "", "3/10", "v3", at))
        batches.append(batch)
    return batches


def _old_block_insert(ledger: Ledger, batch: list[Block]) -> None:
    ledger.conn.executemany(
        "INSERT INTO blocks (created_at, rules_version, strategy, ticker, event_ticker,"
        " reason, detail, position_key) VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                b.created_at.isoformat(),
                b.rules_version,
                b.strategy,
                b.ticker,
                b.event_ticker,
                b.reason,
                b.detail,
                b.position_key,
            )
            for b in batch
        ],
    )
    ledger.conn.commit()


PENDING = (  # cli._sharp_followup pending tickers, as rewritten for merged rows
    "SELECT DISTINCT ticker FROM blocks WHERE rules_version='v3' "
    "AND reason IN ('big_edge','large_gap') AND created_at<=? "
    "AND COALESCE(last_seen_at,created_at)>=? ORDER BY ticker"
)


@pytest.mark.parametrize("seed", [4, 5, 6])
def test_block_readers_are_unchanged(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    batches = _block_stream(rng, steps=300)  # ~6 days
    old, new = _pair(tmp_path)
    for batch in batches:
        _old_block_insert(old, batch)
        new.record_blocks(batch)
    n_old = old.conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
    n_new = new.conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
    assert n_new < 0.7 * n_old, (n_new, n_old)

    # Rows never span a UTC day.
    assert not new.conn.execute(
        "SELECT 1 FROM blocks WHERE last_seen_at IS NOT NULL "
        "AND substr(last_seen_at,1,10)<>substr(created_at,1,10)"
    ).fetchone()
    # Per-day and all-time counts: COUNT(*) before, SUM(seen_count) after.
    per_day = "SELECT substr(created_at,1,10),reason,{} FROM blocks GROUP BY 1,2 ORDER BY 1,2"
    assert (
        old.conn.execute(per_day.format("COUNT(*)")).fetchall()
        == new.conn.execute(per_day.format("SUM(seen_count)")).fetchall()
    )
    end = START + timedelta(minutes=30 * 300)
    for _ in range(150):
        now = START + (end - START) * rng.random() + timedelta(hours=12)
        week = _blocks_week_start(now)
        recent = "SELECT reason,{} FROM blocks WHERE created_at>=? GROUP BY 1 ORDER BY 1"
        assert old.conn.execute(recent.format("COUNT(*)"), (week,)).fetchall() == (
            new.conn.execute(recent.format("SUM(seen_count)"), (week,)).fetchall()
        )
        window = ((now - timedelta(hours=2)).isoformat(), (now - timedelta(hours=36)).isoformat())
        old_pending = [
            r[0]
            for r in old.conn.execute(
                "SELECT DISTINCT ticker FROM blocks WHERE rules_version='v3' AND reason IN "
                "('big_edge','large_gap') AND created_at BETWEEN ? AND ? ORDER BY ticker",
                (window[1], window[0]),
            )
        ]
        assert old_pending == [r[0] for r in new.conn.execute(PENDING, window)]
        for ticker in TICKERS:
            key = f"{ticker}:yes"
            assert old.has_recent_large_gap_record(key, now) == new.has_recent_large_gap_record(
                key, now
            ), (key, now)


def test_a_block_repeated_across_midnight_starts_a_new_row(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "b.sqlite", Decimal(100))
    for minutes in (-20, -10, 10):
        ledger.record_blocks(
            [
                Block(
                    "net_edge",
                    "s",
                    "T-1",
                    "T",
                    created_at=datetime(2026, 11, 2, tzinfo=UTC) + timedelta(minutes=minutes),
                )
            ]
        )
    rows = ledger.conn.execute(
        "SELECT substr(created_at,1,10),seen_count FROM blocks ORDER BY id"
    ).fetchall()
    assert rows == [("2026-11-01", 2), ("2026-11-02", 1)]


def test_existing_ledgers_get_the_new_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE blocks (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, "
        "rules_version TEXT, strategy TEXT, ticker TEXT, event_ticker TEXT, "
        "reason TEXT NOT NULL, detail TEXT)"
    )
    conn.execute("INSERT INTO blocks (created_at,reason) VALUES ('2026-11-01T00:00:00','x')")
    conn.commit()
    conn.close()
    ledger = Ledger(path, Decimal(100))
    assert ledger.conn.execute("SELECT seen_count,last_seen_at FROM blocks").fetchone() == (
        1,
        None,
    )
