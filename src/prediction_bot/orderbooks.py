"""Order-book depth snapshots (v5 Phase 4.1) for the queue-aware maker shadow.

Each paper run snapshots depth-20 books for, in priority order: tickers with an open
maker shadow, tickers we hold, and recent near-miss tickers whose game has not started.
A ticker snapshotted in the last `REFRESH` is skipped. Calls share the run's CLV
budget (`clv_max_calls_scanner` on scanner runs).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prediction_bot.paper.ledger import Ledger
    from prediction_bot.venues.kalshi import KalshiClient

log = logging.getLogger(__name__)
DEPTH = 20
REFRESH = timedelta(minutes=25)
NEAR_MISS_LOOKBACK = timedelta(hours=24)


def snapshot_targets(ledger: Ledger, now: datetime) -> list[str]:
    now = now.astimezone(timezone.utc)
    stamp = now.isoformat()
    ordered: list[str] = []
    seen: set[str] = set()

    def add(tickers: list[str]) -> None:
        for ticker in tickers:
            if ticker and ticker not in seen:
                seen.add(ticker)
                ordered.append(ticker)

    add(
        [
            str(r[0])
            for r in ledger.conn.execute(
                "SELECT DISTINCT ticker FROM maker_shadow WHERE status='open' ORDER BY ticker"
            )
        ]
    )
    add(sorted(ledger.open_tickers()))
    add(
        [
            str(r[0])
            for r in ledger.conn.execute(
                "SELECT ticker,MAX(created_at) FROM shadow_decisions WHERE would_trade=0 "
                "AND created_at>=? AND COALESCE(commence_time,commence_time_backfill)>? "
                "GROUP BY ticker ORDER BY MAX(created_at) DESC",
                ((now - NEAR_MISS_LOOKBACK).isoformat(), stamp),
            )
        ]
    )
    fresh = {
        str(r[0])
        for r in ledger.conn.execute(
            "SELECT DISTINCT ticker FROM orderbook_snapshots WHERE fetched_at>=?",
            ((now - REFRESH).isoformat(),),
        )
    }
    return [ticker for ticker in ordered if ticker not in fresh]


def snapshot_orderbooks(
    ledger: Ledger,
    kalshi: KalshiClient,
    *,
    now: datetime | None = None,
    max_calls: int = 50,
) -> int:
    """Fetch and store depth snapshots; returns the number of API calls made."""
    now = now or datetime.now(timezone.utc)
    calls = 0
    for ticker in snapshot_targets(ledger, now):
        if calls >= max_calls:
            break
        calls += 1
        try:
            levels = kalshi.get_orderbook_levels(ticker, depth=DEPTH)
        except Exception:
            log.exception("Orderbook snapshot failed for %s", ticker)
            continue
        ledger.record_orderbook_snapshot(ticker, levels, datetime.now(timezone.utc))
    return calls
