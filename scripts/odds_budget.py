#!/usr/bin/env python3
"""Estimate sportsbook Odds API credits from the last seven days of a paper ledger."""

from __future__ import annotations

import argparse
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from prediction_bot.odds_planner import PlannedGame, plan_odds_pulls

MONTH_DAYS = 30.4375
FREE_CREDITS = 500
PAID_CREDITS = 20_000
PAID_PRICE = 30


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def _tier(credits: float) -> str:
    if credits <= FREE_CREDITS:
        return "free"
    if credits <= PAID_CREDITS:
        return f"${PAID_PRICE}/month"
    return f">{PAID_CREDITS:,} credits"


def _read_ledger(
    path: Path, since: datetime
) -> tuple[list[tuple[datetime, str, str, datetime]], list[tuple[datetime, int]]]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        cutoff = since.replace(tzinfo=None).isoformat(timespec="seconds")
        snapshot_rows = conn.execute(
            "SELECT DISTINCT created_at,feed,event_id,commence_time FROM odds_snapshots "
            "WHERE created_at >= ?",
            (cutoff,),
        ).fetchall()
        run_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(runs)")}
        run_rows = (
            conn.execute(
                "SELECT ran_at,odds_used FROM runs WHERE ran_at >= ? AND odds_used IS NOT NULL",
                (cutoff,),
            ).fetchall()
            if "odds_used" in run_columns
            else []
        )
    events = [
        (_utc(str(created_at)), str(feed), str(event_id), _utc(str(commence_time)))
        for created_at, feed, event_id, commence_time in snapshot_rows
    ]
    runs = [(_utc(str(ran_at)), int(odds_used)) for ran_at, odds_used in run_rows]
    return events, runs


def _estimate(
    events: list[tuple[datetime, str, str, datetime]],
    runs: list[tuple[datetime, int]],
    since: datetime,
) -> None:
    grouped_events: dict[tuple[str, str], datetime] = {}
    by_run: dict[datetime, set[str]] = defaultdict(set)
    for created_at, feed, event_id, commence in events:
        grouped_events[(feed, event_id)] = commence
        by_run[created_at].add(feed)
    run_times = sorted({at for at, _ in runs} or set(by_run))
    observed_requests = [count for _, count in runs]
    if observed_requests:
        fixed_per_run = sum(observed_requests) / len(observed_requests)
        fixed_basis = "runs.odds_used"
    elif by_run:
        fixed_per_run = sum(len(feeds) for feeds in by_run.values()) / len(by_run)
        fixed_basis = "distinct odds_snapshots feeds per timestamp"
    else:
        fixed_per_run = 0.0
        fixed_basis = "no observed runs or feeds"

    sport_games: dict[str, list[PlannedGame]] = defaultdict(list)
    for (feed, _event_id), commence in grouped_events.items():
        sport_games[feed].append(PlannedGame(commence))

    simulated_by_cadence: dict[int, float] = {}
    for cadence in (3, 6, 12):
        sample_days = sorted({at.date() for at in run_times})
        if not sample_days:
            simulated_by_cadence[cadence] = 0.0
            continue
        selected_counts = []
        for day in sample_days:
            slot_start = datetime(day.year, day.month, day.day, 3, 17, tzinfo=timezone.utc)
            for run_index in range(cadence):
                now = slot_start + timedelta(hours=(24 / cadence) * run_index)
                available = {
                    sport: [game for game in games if game.kickoff > now]
                    for sport, games in sport_games.items()
                }
                selected = plan_odds_pulls(
                    available,
                    now,
                    PAID_CREDITS,
                    math.ceil(MONTH_DAYS),
                    runs_per_day=cadence,
                )
                selected_counts.append(len(selected))
        simulated_by_cadence[cadence] = sum(selected_counts) / len(selected_counts)

    since_str = min(
        (created_at for created_at, *_ in events),
        default=since,
    ).isoformat()
    print(f"Ledger-derived event data begins: {since_str}")
    print(f"Odds snapshot rows (distinct event/feed/time): {len(events)}")
    print(f"Observed sportsbook run timestamps: {len(run_times)}")
    print(f"Runs with odds_used: {len(runs)}")
    print(
        f"Fixed planner basis: {fixed_basis}; mean requests per odds-enabled run: "
        f"{fixed_per_run:.2f}"
    )
    feed_counts = Counter(feed for _created_at, feed, _event_id, _commence in events)
    print(
        "Observed events by sport: "
        + (", ".join(f"{feed}={count}" for feed, count in sorted(feed_counts.items())) or "none")
    )
    print(
        "Recorded odds_used per run: "
        + (", ".join(str(count) for _at, count in sorted(runs)) or "none")
    )
    print(
        f"Event-driven basis: {len(grouped_events)} distinct observed feed/events; "
        f"historical run timestamps={len(run_times)}; pending large-gap reverify not estimable "
        "from stored Odds snapshots alone"
    )
    print(
        f"Planning assumptions: {MONTH_DAYS:.4f} days/month; free={FREE_CREDITS}; "
        f"paid={PAID_CREDITS} credits for ${PAID_PRICE}/month"
    )
    print(
        f"Lookback cutoff: {since.isoformat()}; hypothetical run counts use the observed "
        "event sample and a full 20,000-credit planning budget"
    )
    print("Monthly estimates (uncapped demand; cadence rows include the current 3/day schedule):")
    print("scenario,planner,runs_per_day,requests_per_run,credits_per_month,tier")
    for scenario, cadence in (
        ("current schedule", 3),
        ("hypothetical", 3),
        ("hypothetical", 6),
        ("hypothetical", 12),
    ):
        fixed_month = fixed_per_run * cadence * MONTH_DAYS
        event_per_run = simulated_by_cadence[cadence]
        event_month = event_per_run * cadence * MONTH_DAYS
        print(
            f"{scenario},fixed,{cadence},{fixed_per_run:.2f},{fixed_month:.1f},{_tier(fixed_month)}"
        )
        print(
            f"{scenario},event_driven,{cadence},{event_per_run:.2f},"
            f"{event_month:.1f},{_tier(event_month)}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path("data/paper.sqlite"),
        help="ledger copy to read (default: data/paper.sqlite)",
    )
    parser.add_argument("--days", type=int, default=7, help="lookback days (default: 7)")
    args = parser.parse_args()
    if not args.ledger.is_file():
        parser.error(f"ledger does not exist: {args.ledger}")
    since = datetime.now(timezone.utc) - timedelta(days=max(1, args.days))
    events, runs = _read_ledger(args.ledger, since)
    print(f"Ledger input: {args.ledger.resolve()}")
    print(f"Lookback: {max(1, args.days)} days; cutoff={since.isoformat()}")
    _estimate(events, runs, since)


if __name__ == "__main__":
    main()
