"""Local sqlite cache of Kalshi's public trade tape for the maker backtest.

`trades` keeps every fetched trade (one row per ticker and trade_id, raw JSON
included); `windows` records which (ticker, min_ts, max_ts) ranges were fetched to
the end of their cursor, with status `ok` or `tape_unavailable`. A window that was
cut short by the request budget is not recorded, so the next run fetches it again.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

from prediction_bot.models import OutcomeSide
from prediction_bot.venues.kalshi import PublicTrade, parse_public_trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    ticker TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    created_time TEXT NOT NULL,
    yes_price TEXT NOT NULL,
    count TEXT NOT NULL,
    taker_side TEXT NOT NULL,
    is_block INTEGER NOT NULL,
    raw TEXT NOT NULL,
    PRIMARY KEY (ticker, trade_id)
);
CREATE TABLE IF NOT EXISTS unparsed (
    ticker TEXT NOT NULL,
    raw TEXT NOT NULL,
    PRIMARY KEY (ticker, raw)
);
CREATE TABLE IF NOT EXISTS windows (
    ticker TEXT NOT NULL,
    min_ts INTEGER NOT NULL,
    max_ts INTEGER NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (ticker, min_ts, max_ts, status)
);
"""

OK = "ok"
UNAVAILABLE = "tape_unavailable"
MISSING = "missing"


class TradeCache:
    def __init__(self, path: Path | str) -> None:
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def add_trades(self, ticker: str, rows: Iterable[tuple[dict[str, Any], PublicTrade]]) -> int:
        payload = [
            (
                ticker,
                trade.trade_id,
                trade.created_at.isoformat(),
                str(trade.yes_price),
                str(trade.count),
                trade.taker_side.value,
                1 if trade.is_block else 0,
                json.dumps(raw, sort_keys=True, separators=(",", ":")),
            )
            for raw, trade in rows
        ]
        self.conn.executemany(
            "INSERT OR IGNORE INTO trades (ticker, trade_id, created_time, yes_price, count,"
            " taker_side, is_block, raw) VALUES (?,?,?,?,?,?,?,?)",
            payload,
        )
        self.conn.commit()
        return len(payload)

    def add_unparsed(self, ticker: str, rows: Iterable[dict[str, Any]]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO unparsed (ticker, raw) VALUES (?,?)",
            [(ticker, json.dumps(raw, sort_keys=True, separators=(",", ":"))) for raw in rows],
        )
        self.conn.commit()

    def unparsed_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM unparsed").fetchone()[0])

    def trade_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0])

    def mark_window(self, ticker: str, min_ts: int, max_ts: int, status: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO windows (ticker, min_ts, max_ts, status) VALUES (?,?,?,?)",
            (ticker, min_ts, max_ts, status),
        )
        self.conn.commit()

    def windows(self, ticker: str) -> list[tuple[int, int, str]]:
        rows = self.conn.execute(
            "SELECT min_ts, max_ts, status FROM windows WHERE ticker=? ORDER BY min_ts, max_ts",
            (ticker,),
        ).fetchall()
        return [(int(lo), int(hi), str(status)) for lo, hi, status in rows]

    def all_windows(self) -> dict[str, list[tuple[int, int, str]]]:
        out: dict[str, list[tuple[int, int, str]]] = {}
        for ticker, lo, hi, status in self.conn.execute(
            "SELECT ticker, min_ts, max_ts, status FROM windows ORDER BY ticker, min_ts, max_ts"
        ):
            out.setdefault(str(ticker), []).append((int(lo), int(hi), str(status)))
        return out

    def trades(self, ticker: str) -> list[PublicTrade]:
        """Every cached trade for `ticker`, oldest first (ties by trade_id)."""
        out: list[PublicTrade] = []
        for raw, ticker_db, side, price, count in self.conn.execute(
            "SELECT raw, ticker, taker_side, yes_price, count FROM trades WHERE ticker=?",
            (ticker,),
        ):
            parsed = parse_public_trade(json.loads(raw))
            if parsed is None:  # pragma: no cover - only parsed rows are stored
                continue
            # The stored columns are the parse at fetch time; keep the ticker we asked for.
            parsed.ticker = str(ticker_db)
            parsed.taker_side = OutcomeSide(str(side))
            parsed.yes_price = Decimal(str(price))
            parsed.count = Decimal(str(count))
            out.append(parsed)
        return sorted(out, key=lambda t: (t.created_at, t.trade_id))

    def all_trades(self) -> dict[str, list[PublicTrade]]:
        tickers = [
            str(row[0])
            for row in self.conn.execute("SELECT DISTINCT ticker FROM trades ORDER BY ticker")
        ]
        return {ticker: self.trades(ticker) for ticker in tickers}

    def raw_rows(self) -> list[dict[str, Any]]:
        return [
            json.loads(raw)
            for (raw,) in self.conn.execute("SELECT raw FROM trades ORDER BY ticker, trade_id")
        ]


def covered(windows: list[tuple[int, int, str]], lo: int, hi: int, status: str = OK) -> bool:
    """True when the union of `status` windows covers [lo, hi]."""
    reach = lo
    for w_lo, w_hi, w_status in sorted(windows):
        if w_status != status:
            continue
        if w_lo > reach:
            break
        reach = max(reach, w_hi)
        if reach >= hi:
            return True
    return reach >= hi


def touches(windows: list[tuple[int, int, str]], lo: int, hi: int, status: str) -> bool:
    return any(s == status and w_lo < hi and w_hi > lo for w_lo, w_hi, s in windows)


def coverage_status(windows: list[tuple[int, int, str]], lo: int, hi: int) -> str:
    """`ok`, `tape_unavailable` or `missing` for the range [lo, hi]."""
    if touches(windows, lo, hi, UNAVAILABLE):
        return UNAVAILABLE
    if covered(windows, lo, hi, OK):
        return OK
    return MISSING
