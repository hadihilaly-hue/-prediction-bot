"""I/O for the maker backtest: read the ledger (read-only), fill the trade cache from
Kalshi's public endpoints, and run the pure pipeline in `maker_backtest`.

scripts/maker_backtest.py is the command-line entry point.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from prediction_bot import maker_backtest as mb
from prediction_bot.models import OutcomeSide
from prediction_bot.trade_cache import (
    MISSING,
    OK,
    UNAVAILABLE,
    TradeCache,
    coverage_status,
    covered,
    touches,
)
from prediction_bot.venues.kalshi import (
    BudgetExhausted,
    PoliteFetcher,
    PublicTrade,
    TapeUnavailable,
    iter_segment,
    parse_public_trade,
    trade_segments,
)

log = logging.getLogger(__name__)

DEFAULT_SINCE = "2026-10-23"
DECISION_REASONS = ("net_edge", "entry_window")


# ---- ledger (read-only) ---------------------------------------------------------


def open_ledger_ro(path: Path | str) -> sqlite3.Connection:
    """Open the ledger read-only (`mode=ro`): the backtest never writes to it."""
    return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


class SeriesFees:
    """`series_fees` rows as a `FeeLookup`."""

    def __init__(self, rows: dict[str, tuple[str, Decimal]]) -> None:
        self.rows = rows

    def get(self, series_ticker: str) -> tuple[str, Decimal] | None:
        return self.rows.get(series_ticker)


@dataclass
class LedgerInputs:
    decisions: list[mb.Decision]
    pre_skips: Counter[str]
    snapshots: mb.SnapshotIndex
    odds_kickoffs: dict[str, datetime]
    fees: SeriesFees
    settlements: dict[str, str]
    shadows: list[mb.ShadowRow]


def load_decisions(
    conn: sqlite3.Connection, since: datetime, until: datetime | None
) -> tuple[list[mb.Decision], Counter[str]]:
    skips: Counter[str] = Counter()
    cols = _columns(conn, "shadow_decisions")
    commence = (
        "COALESCE(commence_time, commence_time_backfill)"
        if "commence_time_backfill" in cols
        else "commence_time"
    )
    rows = conn.execute(
        "SELECT id, created_at, ticker, side, position_key, fair, price, window, "
        f"{commence}, clv_kalshi, clv_status FROM shadow_decisions "
        "WHERE rules_version='v3' AND reason IN (?, ?) ORDER BY id",
        DECISION_REASONS,
    ).fetchall()
    out: list[mb.Decision] = []
    for row_id, created, ticker, side, key, fair, price, window, start, clv, status in rows:
        created_at = mb.parse_time(created)
        if created_at is None or created_at < since or (until is not None and created_at >= until):
            continue
        fair_d, price_d = mb.opt_decimal(fair), mb.opt_decimal(price)
        if fair_d is None or price_d is None or str(side) not in ("yes", "no"):
            skips["missing_fair_price_or_side"] += 1
            continue
        out.append(
            mb.Decision(
                id=int(row_id),
                created_at=created_at,
                ticker=str(ticker),
                side=OutcomeSide(str(side)),
                position_key=str(key) if key else f"{ticker}|{side}",
                fair=fair_d,
                price=price_d,
                window=str(window),
                commence_time=mb.parse_time(start),
                clv_kalshi=mb.opt_decimal(clv),
                clv_status=None if status is None else str(status),
            )
        )
    return out, skips


def load_snapshots(conn: sqlite3.Connection, tickers: Sequence[str]) -> mb.SnapshotIndex:
    by_ticker: dict[str, list[mb.Snapshot]] = {}
    unique = sorted(set(tickers))
    for i in range(0, len(unique), 500):
        chunk = unique[i : i + 500]
        marks = ",".join("?" * len(chunk))
        for fetched, ticker, event, bid, ask, bid_size, ask_size in conn.execute(
            "SELECT fetched_at, ticker, event_ticker, yes_bid, yes_ask, yes_bid_size, "
            f"yes_ask_size FROM market_snapshots WHERE ticker IN ({marks})",
            chunk,
        ):
            at = mb.parse_time(fetched)
            if at is None:
                continue
            by_ticker.setdefault(str(ticker), []).append(
                mb.Snapshot(
                    at=at,
                    yes_bid=mb.opt_decimal(bid),
                    yes_ask=mb.opt_decimal(ask),
                    yes_bid_size=mb.opt_decimal(bid_size) or Decimal(0),
                    yes_ask_size=mb.opt_decimal(ask_size) or Decimal(0),
                    event_ticker=str(event or ""),
                )
            )
    return mb.SnapshotIndex(by_ticker)


def load_odds_kickoffs(conn: sqlite3.Connection) -> dict[str, datetime]:
    """Per Kalshi event, the commence_time of the latest pre-game odds snapshot linked
    to it (`odds_snapshots.event_ticker`, filled since 2026-11-04)."""
    if "event_ticker" not in _columns(conn, "odds_snapshots"):
        return {}
    latest: dict[str, tuple[datetime, datetime]] = {}
    for event, created, commence in conn.execute(
        "SELECT event_ticker, created_at, commence_time FROM odds_snapshots "
        "WHERE event_ticker IS NOT NULL AND event_ticker != ''"
    ):
        created_at, start = mb.parse_time(created), mb.parse_time(commence)
        if created_at is None or start is None or created_at >= start:
            continue
        seen = latest.get(str(event))
        if seen is None or created_at > seen[0]:
            latest[str(event)] = (created_at, start)
    return {event: start for event, (_, start) in latest.items()}


def load_fees(conn: sqlite3.Connection) -> SeriesFees:
    return SeriesFees(
        {
            str(series): (str(fee_type), Decimal(str(mult)))
            for series, fee_type, mult in conn.execute(
                "SELECT series_ticker, fee_type, fee_multiplier FROM series_fees"
            )
        }
    )


def load_settlements(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(ticker): str(result).lower()
        for ticker, result in conn.execute("SELECT ticker, result FROM settlements")
    }


def load_shadows(conn: sqlite3.Connection) -> list[mb.ShadowRow]:
    cols = _columns(conn, "maker_shadow")
    queue = "queue_ahead" if "queue_ahead" in cols else "NULL"
    fill = "fill_queue_at" if "fill_queue_at" in cols else "NULL"
    out: list[mb.ShadowRow] = []
    for row_id, created, ticker, side, price, size, q, expires, filled in conn.execute(
        f"SELECT id, created_at, ticker, side, price, size, {queue}, expires_at, {fill} "
        "FROM maker_shadow ORDER BY id"
    ):
        created_at, expires_at = mb.parse_time(created), mb.parse_time(expires)
        price_d, size_d = mb.opt_decimal(price), mb.opt_decimal(size)
        if created_at is None or expires_at is None or price_d is None or size_d is None:
            continue
        out.append(
            mb.ShadowRow(
                id=int(row_id),
                created_at=created_at,
                ticker=str(ticker),
                side=OutcomeSide(str(side)),
                price=price_d,
                size=size_d,
                queue_ahead=mb.opt_decimal(q),
                expires_at=expires_at,
                fill_queue_at=mb.parse_time(filled),
            )
        )
    return out


def load_inputs(conn: sqlite3.Connection, since: datetime, until: datetime | None) -> LedgerInputs:
    decisions, pre_skips = load_decisions(conn, since, until)
    return LedgerInputs(
        decisions=decisions,
        pre_skips=pre_skips,
        snapshots=load_snapshots(conn, [d.ticker for d in decisions]),
        odds_kickoffs=load_odds_kickoffs(conn),
        fees=load_fees(conn),
        settlements=load_settlements(conn),
        shadows=load_shadows(conn),
    )


# ---- trade tape ---------------------------------------------------------------------


@dataclass
class FetchStats:
    calls: int = 0
    cutoff_ts: int | None = None
    budget_exhausted: bool = False
    segments_fetched: int = 0
    segments_unavailable: int = 0
    trades_added: int = 0
    unparsed_added: int = 0


def fetch_tape(
    cache: TradeCache,
    fetcher: PoliteFetcher,
    plan: dict[str, list[tuple[int, int]]],
) -> FetchStats:
    """Fetch every planned window the cache does not already cover, segment by segment
    (split at the historical cutoff). A refused segment is recorded as tape_unavailable;
    the request budget stops the run cleanly, leaving the window unrecorded so the next
    run fetches it again."""
    stats = FetchStats()
    try:
        stats.cutoff_ts = fetcher.cutoff_ts()
        for ticker, windows in plan.items():
            for lo, hi in windows:
                known = cache.windows(ticker)
                if coverage_status(known, lo, hi) != MISSING:
                    continue
                for path, a, b in trade_segments(lo, hi, stats.cutoff_ts):
                    if covered(known, a, b, OK) or touches(known, a, b, UNAVAILABLE):
                        continue
                    parsed: list[tuple[dict[str, Any], PublicTrade]] = []
                    unparsed: list[dict[str, Any]] = []
                    try:
                        for row in iter_segment(fetcher, path, ticker, a, b):
                            trade = parse_public_trade(row)
                            if trade is None:
                                unparsed.append(row)
                            else:
                                parsed.append((row, trade))
                    except TapeUnavailable as exc:
                        log.warning("%s unavailable for %s: %s", path, ticker, exc)
                        cache.mark_window(ticker, a, b, UNAVAILABLE)
                        stats.segments_unavailable += 1
                        continue
                    finally:
                        stats.trades_added += cache.add_trades(ticker, parsed)
                        cache.add_unparsed(ticker, unparsed)
                        stats.unparsed_added += len(unparsed)
                    cache.mark_window(ticker, a, b, OK)
                    stats.segments_fetched += 1
    except BudgetExhausted as exc:
        log.warning("%s; the next run resumes from the cache", exc)
        stats.budget_exhausted = True
    stats.calls = fetcher.calls
    return stats


# ---- run ------------------------------------------------------------------------------


@dataclass
class RunOutput:
    summary: mb.Summary
    results: list[mb.OrderResult]


def _compute(
    inputs: LedgerInputs,
    build: mb.BuildResult,
    cache: TradeCache,
    params: dict[str, Any],
    tape_info: dict[str, Any],
) -> RunOutput:
    windows = cache.all_windows()
    trades = cache.all_trades()

    def order_tape(order: mb.Order) -> str:
        lo, hi = mb.order_span(order)
        return coverage_status(windows.get(order.ticker, []), lo, hi)

    def shadow_tape(ticker: str, start: datetime, end: datetime) -> str:
        lo, hi = int(start.timestamp()), int(end.timestamp()) + 1
        return coverage_status(windows.get(ticker, []), lo, hi)

    run = mb.run_backtest(build.orders, trades, inputs.snapshots, order_tape, inputs.settlements)
    planned_tickers = sorted({o.ticker for o in build.orders})
    pool = [t for ticker in planned_tickers for t in trades.get(ticker, [])]
    sample = mb.tape_sample(pool)
    replay_check, replay_rows = mb.replay_shadows(inputs.shadows, trades, shadow_tape)
    checks = [
        mb.check_tape_sanity(sample, cache.unparsed_count(), inputs.snapshots),
        mb.check_side_semantics(pool, inputs.snapshots),
        replay_check,
        mb.check_no_overlap(run.results),
    ]
    statuses = Counter(order_tape(o) for o in build.orders)
    tape = {
        **tape_info,
        "trades cached for these tickers": len(pool),
        "candidate orders by tape status": dict(sorted(statuses.items())),
    }
    summary = mb.summarize(run, build, checks, params=params, tape=tape, shadow_replay=replay_rows)
    return RunOutput(summary=summary, results=run.results)


def run(
    db: Path,
    cache_path: Path,
    *,
    since: datetime,
    until: datetime | None,
    fetcher: PoliteFetcher | None,
    ttl_minutes: int = mb.DEFAULT_TTL_MINUTES,
    max_notional: Decimal = mb.DEFAULT_MAX_NOTIONAL,
) -> RunOutput:
    conn = open_ledger_ro(db)
    try:
        inputs = load_inputs(conn, since, until)
    finally:
        conn.close()
    build = mb.build_orders(
        inputs.decisions,
        inputs.snapshots,
        inputs.odds_kickoffs,
        inputs.fees,
        ttl_minutes=ttl_minutes,
        max_notional=max_notional,
    )
    build.skips.update(inputs.pre_skips)
    build.decisions += sum(inputs.pre_skips.values())
    plan = mb.fetch_plan(build.orders)
    # The replayed live shadows need their own windows of tape.
    for shadow in inputs.shadows:
        lo = int(shadow.created_at.timestamp())
        hi = int(shadow.expires_at.timestamp()) + 1
        plan.setdefault(shadow.ticker, []).append((lo, hi))
    cache = TradeCache(cache_path)
    try:
        tape_info: dict[str, Any] = {
            "planned windows": sum(len(v) for v in plan.values()),
            "tickers": len(plan),
        }
        if fetcher is not None:
            stats = fetch_tape(cache, fetcher, plan)
            tape_info.update(
                {
                    "requests this run": stats.calls,
                    "historical cutoff (unix)": stats.cutoff_ts,
                    "stopped at --max-calls": stats.budget_exhausted,
                    "segments fetched this run": stats.segments_fetched,
                    "segments refused (tape_unavailable) this run": stats.segments_unavailable,
                }
            )
        else:
            tape_info["requests this run"] = 0
        params = {
            "since": since.isoformat(),
            "until": None if until is None else until.isoformat(),
            "ttl_minutes": ttl_minutes,
            "max_order_notional": str(max_notional),
        }
        # The numbers come from the cache alone; a second pass checks they are stable.
        stable_info = {k: tape_info[k] for k in ("planned windows", "tickers")}
        first = _compute(inputs, build, cache, params, stable_info)
        second = _compute(inputs, build, cache, params, stable_info)
    finally:
        cache.close()
    determinism = mb.check_determinism(first.summary.numbers(), second.summary.numbers())
    summary = first.summary
    summary.checks.append(determinism)
    summary.fetch = {k: v for k, v in tape_info.items() if k not in stable_info}
    summary.verdict = mb.verdict_line(summary.headline, summary.invalid)
    return RunOutput(summary=summary, results=first.results)


def write_outputs(output: RunOutput, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary.md": out_dir / "summary.md",
        "summary.json": out_dir / "summary.json",
        "orders.csv": out_dir / "orders.csv",
    }
    paths["summary.md"].write_text(mb.render_markdown(output.summary))
    paths["summary.json"].write_text(
        json.dumps(output.summary.as_dict(), indent=2, sort_keys=True, default=str) + "\n"
    )
    with paths["orders.csv"].open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(mb.ORDER_COLUMNS))
        writer.writeheader()
        for result in output.results:
            writer.writerow(mb.order_row(result))
    return paths


def parse_day(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = mb.parse_time(value if "T" in value else f"{value}T00:00:00+00:00")
    if parsed is None:
        raise ValueError(f"not a date: {value}")
    return parsed


def day_after(value: datetime) -> datetime:
    return value + timedelta(days=1)
