"""Replay a real ledger's quote_snapshots and blocks through the change-only writers and
check that the readers answer the same on sampled (ticker, time) pairs.

    python scripts/check_change_only.py path/to/paper.sqlite [--samples 2000]

Reads the ledger read-only; writes a scratch ledger next to `--out` (default: a temp
dir). Exits 1 on the first mismatch.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from prediction_bot.dashboard import _blocks_week_start
from prediction_bot.models import Block, QuoteSnapshot
from prediction_bot.paper.ledger import Ledger


def _dec(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger", type=Path)
    parser.add_argument("--samples", type=int, default=2000)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    src = sqlite3.connect(f"file:{args.ledger.resolve()}?mode=ro", uri=True)
    out = args.out or Path(tempfile.mkdtemp()) / "replayed.sqlite"
    new = Ledger(out, Decimal(0))

    quotes = src.execute(
        "SELECT created_at,ticker,event_ticker,yes_bid,yes_ask,last_price,volume,open_interest "
        "FROM quote_snapshots ORDER BY created_at,id"
    ).fetchall()
    for created_at, ticker, event, bid, ask, last, volume, oi in quotes:
        at = datetime.fromisoformat(str(created_at))
        snap = QuoteSnapshot(
            str(ticker), str(event), _dec(bid), _dec(ask), _dec(last), Decimal(volume),
            Decimal(oi), created_at=at,
        )  # fmt: skip
        new.record_quote_snapshots([snap])
    blocks = src.execute(
        "SELECT created_at,rules_version,strategy,ticker,event_ticker,reason,detail,position_key "
        "FROM blocks ORDER BY created_at,id"
    ).fetchall()
    for created_at, version, strategy, ticker, event, reason, detail, key in blocks:
        new.record_blocks(
            [
                Block(
                    str(reason),
                    strategy,
                    ticker,
                    event,
                    detail,
                    version,
                    datetime.fromisoformat(str(created_at)),
                    key,
                )  # fmt: skip
            ]
        )
    n_new_q = new.conn.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0]
    n_new_b = new.conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
    print(f"quote_snapshots {len(quotes)} -> {n_new_q}; blocks {len(blocks)} -> {n_new_b}")

    rng = random.Random(0)
    tickers = [str(r[0]) for r in src.execute("SELECT DISTINCT ticker FROM quote_snapshots")]
    first, last = (
        datetime.fromisoformat(str(v))
        for v in src.execute(
            "SELECT MIN(created_at),MAX(created_at) FROM quote_snapshots"
        ).fetchone()
    )
    stamps = [str(r[0]) for r in quotes]
    checks = 0
    queries = (
        "SELECT yes_bid,yes_ask,last_price,volume,open_interest FROM quote_snapshots "
        "WHERE ticker=? AND created_at<=? ORDER BY created_at DESC,id DESC LIMIT 1",
        "SELECT yes_bid,yes_ask FROM quote_snapshots WHERE ticker=? AND created_at<=? "
        "AND yes_bid IS NOT NULL AND yes_ask IS NOT NULL ORDER BY created_at DESC,id DESC LIMIT 1",
        "SELECT yes_bid,yes_ask FROM quote_snapshots WHERE ticker=? AND created_at<? "
        "ORDER BY created_at DESC,id DESC LIMIT 1",
    )
    for i in range(args.samples):
        ticker = rng.choice(tickers)
        stamp = rng.choice(stamps) if i % 2 else (first + (last - first) * rng.random()).isoformat()
        for sql in queries:
            a = src.execute(sql, (ticker, stamp)).fetchone()
            b = new.conn.execute(sql, (ticker, stamp)).fetchone()
            if a != b:
                print(f"MISMATCH {ticker} {stamp}: {a} != {b}\n{sql}")
                return 1
            checks += 1
    per_day = "SELECT substr(created_at,1,10),reason,{} FROM blocks GROUP BY 1,2 ORDER BY 1,2"
    if (
        src.execute(per_day.format("COUNT(*)")).fetchall()
        != new.conn.execute(per_day.format("SUM(seen_count)")).fetchall()
    ):
        print("MISMATCH per-day block counts")
        return 1
    keys = [
        str(r[0])
        for r in src.execute(
            "SELECT DISTINCT position_key FROM blocks WHERE reason IN ('big_edge','large_gap') "
            "AND position_key IS NOT NULL"
        )
    ]
    for _ in range(args.samples):
        now = first + (last - first) * rng.random() + timedelta(hours=12)
        week = _blocks_week_start(now)
        recent = "SELECT reason,{} FROM blocks WHERE created_at>=? GROUP BY 1 ORDER BY 1"
        if (
            src.execute(recent.format("COUNT(*)"), (week,)).fetchall()
            != new.conn.execute(recent.format("SUM(seen_count)"), (week,)).fetchall()
        ):
            print(f"MISMATCH 7-day block counts at {now}")
            return 1
        end, start = (now - timedelta(hours=2)).isoformat(), (now - timedelta(hours=36)).isoformat()
        for key in rng.sample(keys, min(len(keys), 5)):
            a = src.execute(
                "SELECT 1 FROM blocks WHERE position_key=? AND reason IN ('big_edge','large_gap') "
                "AND created_at BETWEEN ? AND ? LIMIT 1",
                (key, start, end),
            ).fetchone()
            b = new.conn.execute(
                "SELECT 1 FROM blocks WHERE position_key=? AND reason IN ('big_edge','large_gap') "
                "AND created_at<=? AND COALESCE(last_seen_at,created_at)>=? LIMIT 1",
                (key, end, start),
            ).fetchone()
            if a != b:
                print(f"MISMATCH large-gap {key} at {now}")
                return 1
            checks += 1
    print(f"OK: {checks} reader checks matched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
