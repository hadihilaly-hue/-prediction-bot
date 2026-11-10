#!/usr/bin/env python3
"""Estimate sportsbook Odds API credits from the last seven days of a paper ledger."""

from __future__ import annotations

import argparse
import math
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from prediction_bot.config import Settings
from prediction_bot.feed_selection import compute_feed_stats_from_connection, eligible
from prediction_bot.odds import KALSHI_SERIES_TO_OUTRIGHT, KALSHI_SERIES_TO_SPORT
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
) -> tuple[
    list[tuple[datetime, str, str, datetime]],
    list[tuple[int, datetime, int]],
]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        cutoff = since.replace(tzinfo=None).isoformat(timespec="seconds")
        month_start = since.astimezone(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        month_cutoff = month_start.replace(tzinfo=None).isoformat(timespec="seconds")
        snapshot_rows = conn.execute(
            "SELECT DISTINCT created_at,feed,event_id,commence_time FROM odds_snapshots "
            "WHERE created_at >= ?",
            (cutoff,),
        ).fetchall()
        run_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(runs)")}
        run_rows = (
            conn.execute(
                "SELECT id,ran_at,odds_used FROM runs "
                "WHERE ran_at >= ? AND odds_used IS NOT NULL ORDER BY ran_at,id",
                (month_cutoff,),
            ).fetchall()
            if "odds_used" in run_columns
            else []
        )
    events = [
        (_utc(str(created_at)), str(feed), str(event_id), _utc(str(commence_time)))
        for created_at, feed, event_id, commence_time in snapshot_rows
    ]
    runs = [
        (int(run_id), _utc(str(ran_at)), int(odds_used)) for run_id, ran_at, odds_used in run_rows
    ]
    return events, runs


def _cumulative_deltas(
    runs: Sequence[tuple[int, datetime, int]],
) -> list[tuple[int, int, datetime, int]]:
    """Return non-reset deltas as (run_id, previous_run_id, run_time, credits)."""
    previous_by_month: dict[str, tuple[int, int]] = {}
    deltas: list[tuple[int, int, datetime, int]] = []
    for run_id, ran_at, cumulative in sorted(runs, key=lambda row: (row[1], row[0])):
        month = ran_at.astimezone(timezone.utc).strftime("%Y-%m")
        previous = previous_by_month.get(month)
        previous_by_month[month] = (run_id, cumulative)
        if previous is None:
            continue
        delta = cumulative - previous[1]
        if delta < 0:
            continue
        deltas.append((run_id, previous[0], ran_at, delta))
    return deltas


def _canonical_feed(feed: str) -> str:
    return feed.removeprefix("outrights:")


def _run_windows(
    runs: Sequence[tuple[int, datetime, int]],
) -> list[tuple[int, int | None, datetime, datetime, int | None, str]]:
    previous_by_month: dict[str, tuple[int, datetime, int]] = {}
    windows: list[tuple[int, int | None, datetime, datetime, int | None, str]] = []
    for run_id, ran_at, cumulative in sorted(runs, key=lambda row: (row[1], row[0])):
        utc_run = ran_at.astimezone(timezone.utc)
        month = utc_run.strftime("%Y-%m")
        previous = previous_by_month.get(month)
        month_start = utc_run.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if previous is None:
            start_at = month_start
            previous_id = None
            delta = None
            status = "first run of month"
        else:
            previous_id, start_at, previous_cumulative = previous
            delta = cumulative - previous_cumulative
            status = "month reset" if delta < 0 else "ok"
            if delta < 0:
                delta = None
        windows.append((run_id, previous_id, start_at, utc_run, delta, status))
        previous_by_month[month] = (run_id, utc_run, cumulative)
    return windows


def _feed_report_sections(
    path: Path,
    runs: Sequence[tuple[int, datetime, int]],
    since: datetime,
    now: datetime,
) -> str:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        snapshots = [
            (_utc(str(created_at)), _canonical_feed(str(feed)))
            for created_at, feed in conn.execute("SELECT created_at,feed FROM odds_snapshots")
        ]
        table_names = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        feed_pulls: list[tuple[datetime, str, int, int | None]] = []
        gate_candidate_pulls: list[tuple[datetime, str, int]] = []
        has_gate_candidate_column = False
        if "feed_pulls" in table_names:
            pull_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(feed_pulls)")}
            requests_column = "requests_last" if "requests_last" in pull_columns else "NULL"
            has_gate_candidate_column = "gate_candidates" in pull_columns
            feed_pulls = [
                (
                    _utc(str(created_at)),
                    _canonical_feed(str(feed)),
                    int(candidates),
                    None if requests_last is None else int(requests_last),
                )
                for created_at, feed, candidates, requests_last in conn.execute(
                    f"SELECT created_at,feed,candidates,{requests_column} FROM feed_pulls"
                )
            ]
            if has_gate_candidate_column:
                gate_candidate_pulls = [
                    (_utc(str(created_at)), _canonical_feed(str(feed)), int(count))
                    for created_at, feed, count in conn.execute(
                        "SELECT created_at,feed,gate_candidates FROM feed_pulls "
                        "WHERE gate_candidates IS NOT NULL"
                    )
                ]
        # Change-only blocks (Part 2 ledger fix): a row repeated within a UTC day keeps
        # its first created_at and moves last_seen_at, so a row stands for an observation at
        # each end; both ends are replayed here.
        block_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(blocks)")}
        last_seen = (
            "COALESCE(last_seen_at,created_at)" if "last_seen_at" in block_columns else "created_at"
        )
        blocks = [
            (
                _utc(str(at)),
                str(event_ticker or ""),
                str(reason),
                str(rules_version or ""),
            )
            for created_at, seen_at, event_ticker, reason, rules_version in conn.execute(
                f"SELECT created_at,{last_seen},event_ticker,reason,rules_version FROM blocks "
                "WHERE reason IN ('net_edge','entry_window')"
            )
            for at in ((created_at,) if seen_at == created_at else (created_at, seen_at))
        ]
        signals = [
            (_utc(str(created_at)), str(event_ticker or ""), str(rules_version or ""))
            for created_at, event_ticker, rules_version in conn.execute(
                "SELECT created_at,event_ticker,rules_version FROM signals"
            )
        ]
        skips = []
        if "feed_skips" in table_names:
            skips = [
                (_utc(str(created_at)), _canonical_feed(str(feed)), str(reason))
                for created_at, feed, reason in conn.execute(
                    "SELECT created_at,feed,reason FROM feed_skips"
                )
            ]
        report_windows = [window for window in _run_windows(runs) if window[3] >= since]
        feeds_by_run: dict[int, set[str]] = {}
        for run_id, _previous_id, start_at, end_at, _delta, _status in report_windows:
            feeds_by_run[run_id] = {
                feed for created_at, feed in snapshots if start_at < created_at <= end_at
            }

        lines = [
            "credits per run by feed (distinct odds_snapshots.feed in each run window):",
            "run_id,previous_run_id,credits,feeds",
        ]
        for run_id, previous_id, _start_at, _end_at, delta, status in report_windows:
            credits = str(delta) if delta is not None else f"n/a ({status})"
            feeds = "|".join(sorted(feeds_by_run[run_id])) or "none"
            lines.append(
                f"{run_id},{previous_id if previous_id is not None else 'none'},{credits},{feeds}"
            )

        useful_pulls: Counter[str] = Counter()
        total_pulls: Counter[str] = Counter()
        source = "feed_pulls per run window; blocks+signals reconstructed when absent"
        series_to_feed = {**KALSHI_SERIES_TO_SPORT, **KALSHI_SERIES_TO_OUTRIGHT}
        for _run_id, _previous_id, start_at, end_at, _delta, _status in report_windows:
            window_start = max(start_at, since)
            run_pulls = [
                (feed, candidates)
                for created_at, feed, candidates, _requests_last in feed_pulls
                if window_start < created_at <= end_at
            ]
            if run_pulls:
                for feed, candidates in run_pulls:
                    total_pulls[feed] += 1
                    useful_pulls[feed] += int(candidates > 0)
                continue

            entry_window_events = {
                (event_ticker, rules_version)
                for created_at, event_ticker, reason, rules_version in blocks
                if window_start < created_at <= end_at and reason == "entry_window" and event_ticker
            }
            candidates: Counter[str] = Counter()
            for created_at, event_ticker, reason, rules_version in blocks:
                if (
                    reason != "net_edge"
                    or not window_start < created_at <= end_at
                    or not event_ticker
                    or (event_ticker, rules_version) in entry_window_events
                ):
                    continue
                feed = series_to_feed.get(event_ticker.split("-", 1)[0])
                if feed:
                    candidates[feed] += 1
            for created_at, event_ticker, rules_version in signals:
                if (
                    not window_start < created_at <= end_at
                    or not event_ticker
                    or (event_ticker, rules_version) in entry_window_events
                ):
                    continue
                feed = series_to_feed.get(event_ticker.split("-", 1)[0])
                if feed:
                    candidates[feed] += 1
            run_feeds = {
                feed for created_at, feed in snapshots if window_start < created_at <= end_at
            }
            for feed in run_feeds:
                total_pulls[feed] += 1
                useful_pulls[feed] += int(candidates[feed] > 0)
        lines.extend(
            [
                "useful-pull ratio per feed "
                f"(source: {source}; lookback starts {since.isoformat()}):",
                "feed,pulls,useful_pulls,ratio",
            ]
        )
        for feed in sorted(total_pulls):
            pulls = total_pulls[feed]
            ratio = useful_pulls[feed] / pulls if pulls else 0.0
            lines.append(f"{feed},{pulls},{useful_pulls[feed]},{ratio:.3f}")
        if not total_pulls:
            lines.append("none")

        credits_by_feed: Counter[str] = Counter()
        for run_id, _previous_id, start_at, end_at, _delta, _status in report_windows:
            window_start = max(start_at, since)
            rows = [
                (feed, requests_last)
                for created_at, feed, _candidates, requests_last in feed_pulls
                if window_start < created_at <= end_at
            ]
            if rows:
                covered: set[str] = set()
                for feed, requests_last in rows:
                    covered.add(feed)
                    credits_by_feed[feed] += (
                        requests_last
                        if requests_last is not None
                        else int(feed in feeds_by_run[run_id])
                    )
                for feed in feeds_by_run[run_id] - covered:
                    credits_by_feed[feed] += 1
            else:
                for feed in feeds_by_run[run_id]:
                    credits_by_feed[feed] += 1
        lines.extend(
            [
                "credits by feed (SUM(requests_last), fallback to feed attribution):",
                "feed,credits",
            ]
        )
        for feed in sorted(credits_by_feed):
            lines.append(f"{feed},{credits_by_feed[feed]}")
        if not credits_by_feed:
            lines.append("none")

        if has_gate_candidate_column:
            gate_totals: Counter[str] = Counter()
            gate_pulls: Counter[str] = Counter()
            for _run_id, _previous_id, start_at, end_at, _delta, _status in report_windows:
                window_start = max(start_at, since)
                for created_at, feed, count in gate_candidate_pulls:
                    if window_start < created_at <= end_at:
                        gate_totals[feed] += count
                        gate_pulls[feed] += 1
            lines.extend(
                [
                    "mean gate candidates per feed pull:",
                    "feed,mean_gate_candidates",
                ]
            )
            for feed in sorted(gate_totals):
                lines.append(f"{feed},{gate_totals[feed] / gate_pulls[feed]:.3f}")
            if not gate_totals:
                lines.append("none")

        skip_counts: Counter[tuple[str, str]] = Counter()
        for created_at, feed, reason in skips:
            if created_at >= since:
                skip_counts[(feed, reason)] += 1
        lines.extend(["feed skips by reason:", "feed,reason,count"])
        for (feed, reason), count in sorted(skip_counts.items()):
            lines.append(f"{feed},{reason},{count}")
        if not skip_counts:
            lines.append("none")

        stats_cutoff = now - timedelta(days=14)
        stat_feeds = sorted(
            {feed for created_at, feed in snapshots if stats_cutoff <= created_at <= now}
            | {
                feed
                for created_at, feed, _candidates, _requests_last in feed_pulls
                if stats_cutoff <= created_at <= now
            }
        )
        stats = compute_feed_stats_from_connection(conn, stat_feeds, now)
        settings = Settings(_env_file=None, rules_version="v3", min_lead_hours=72)
        lines.extend(
            [
                "feed stats and eligibility (nearest-rank percentiles; min_lead_hours=72):",
                "feed,lead_p50_h,lead_p90_h,provisional,pulls_14d,useful_pulls_14d,eligible",
            ]
        )
        for feed, stat in sorted(stats.items()):
            lines.append(
                f"{feed},{stat.lead_p50_h:.1f},{stat.lead_p90_h:.1f},"
                f"{int(stat.provisional)},{stat.pulls_14d},{stat.useful_pulls_14d},"
                f"{int(eligible(stat, settings))}"
            )
        if not stats:
            lines.append("none")
    return "\n".join(lines)


def _estimate(
    events: list[tuple[datetime, str, str, datetime]],
    runs: list[tuple[int, datetime, int]],
    since: datetime,
) -> None:
    grouped_events: dict[tuple[str, str], datetime] = {}
    by_run: dict[datetime, set[str]] = defaultdict(set)
    for created_at, feed, event_id, commence in events:
        grouped_events[(feed, event_id)] = commence
        by_run[created_at].add(feed)
    observed_runs = [run for run in runs if run[1] >= since]
    deltas = [delta for delta in _cumulative_deltas(runs) if delta[2] >= since]
    run_times = sorted({at for _run_id, at, _cumulative in observed_runs} or set(by_run))
    if deltas:
        fixed_per_run = sum(delta[3] for delta in deltas) / len(deltas)
        fixed_basis = "runs.odds_used cumulative deltas"
    elif runs:
        fixed_per_run = 0.0
        fixed_basis = "no complete cumulative delta in lookback"
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
    print(f"Runs with odds_used: {len(observed_runs)}")
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
        "Recorded cumulative odds_used: "
        + (
            ", ".join(f"run {run_id}={cumulative}" for run_id, _at, cumulative in observed_runs)
            or "none"
        )
    )
    print("Per-run cumulative deltas: run_id,previous_run_id,delta")
    for run_id, previous_id, _at, delta in deltas:
        print(f"{run_id},{previous_id},{delta}")
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
    print(
        "Event-driven requests_per_run formula: mean("
        "len(plan_odds_pulls(games_by_sport_at_slot, slot, remaining_credits, "
        "days_left_in_month, runs_per_day))) over simulated slots"
    )
    print(
        f"Event-driven formula inputs: remaining_credits={PAID_CREDITS}, "
        f"days_left_in_month={math.ceil(MONTH_DAYS)}, slot_start=03:17 UTC, "
        f"observed_event_feeds={len(grouped_events)}"
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


PLAN_TARGET_ORDER = ("T-10m", "T-90m", "T-6h", "T-24h")
PLAN_SOURCE_ORDER = ("close", "kickoff", "release", "default", "grid")


def _target_bucket(target: str) -> str:
    """Release targets are named by date (`R-2026-11-08T11:35`): bucket by local time."""
    if target.startswith("R-") and "T" in target:
        return f"R-{target.rsplit('T', 1)[1]}"
    if target.startswith("G-"):
        return "G-15:00"
    return target


def pull_plan_section(path: Path, since: datetime) -> str:
    """Phase 3 per-game targets: credits by target bucket and useful-pull ratio per feed.

    A target is useful when it was pulled (by a plan credit or riding along on another
    pull); the ratio is pulled / (pulled + skipped_budget + missed + no_events).
    """
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
        if "pull_plan" not in tables:
            return "Pull plan: no pull_plan table in this ledger (pre-Phase 3)."
        has_source = "source" in _columns(conn, "pull_plan")
        rows = conn.execute(
            "SELECT target,feed,status,credits,"
            + ("source" if has_source else "'default'")
            + " FROM pull_plan WHERE planned_at>=?",
            (since.isoformat(),),
        ).fetchall()
    by_target: dict[str, Counter[str]] = defaultdict(Counter)
    credits_by_target: dict[str, float] = defaultdict(float)
    by_feed: dict[str, Counter[str]] = defaultdict(Counter)
    credits_by_feed: dict[str, float] = defaultdict(float)
    by_source: dict[str, Counter[str]] = defaultdict(Counter)
    credits_by_source: dict[str, float] = defaultdict(float)
    for target, feed, status, credits, source in rows:
        by_source[str(source)][str(status)] += 1
        credits_by_source[str(source)] += float(credits or 0)
        target = _target_bucket(str(target))
        by_target[str(target)][str(status)] += 1
        by_feed[str(feed)][str(status)] += 1
        credits_by_target[str(target)] += float(credits or 0)
        credits_by_feed[str(feed)] += float(credits or 0)
    closed = ("pulled", "skipped_budget", "missed", "no_events")

    def ratio(counts: Counter[str]) -> str:
        done = sum(counts[key] for key in closed)
        return f"{counts['pulled'] / done:.0%}" if done else "-"

    lines = ["Pull plan by target (since lookback cutoff):"]
    lines.append("target,planned,pulled,skipped_budget,missed,plan_credits,useful_ratio")
    for target in [*PLAN_TARGET_ORDER, *sorted(set(by_target) - set(PLAN_TARGET_ORDER))]:
        counts = by_target.get(target, Counter())
        lines.append(
            f"{target},{counts['planned']},{counts['pulled']},{counts['skipped_budget']},"
            f"{counts['missed']},{credits_by_target.get(target, 0.0):.1f},{ratio(counts)}"
        )
    lines.append("Pull plan by source:")
    lines.append("source,planned,pulled,skipped_budget,missed,superseded,plan_credits,useful_ratio")
    for source in [*PLAN_SOURCE_ORDER, *sorted(set(by_source) - set(PLAN_SOURCE_ORDER))]:
        if source not in by_source:
            continue
        counts = by_source[source]
        lines.append(
            f"{source},{counts['planned']},{counts['pulled']},{counts['skipped_budget']},"
            f"{counts['missed']},{counts['superseded']},{credits_by_source[source]:.1f},"
            f"{ratio(counts)}"
        )
    lines.append("Pull plan by feed:")
    lines.append("feed,targets,pulled,plan_credits,useful_ratio")
    for feed in sorted(by_feed):
        counts = by_feed[feed]
        lines.append(
            f"{feed},{sum(counts.values())},{counts['pulled']},"
            f"{credits_by_feed[feed]:.1f},{ratio(counts)}"
        )
    return "\n".join(lines)


PULL_SOURCE_ORDER = (
    "release",
    "kickoff",
    "close",
    "default",
    "grid",
    "rotation",
    "move",
    "measure",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _as_float(value: object) -> float | None:
    try:
        return float(str(value)) if value is not None else None
    except ValueError:
        return None


def pull_source_rows(
    conn: sqlite3.Connection, since: datetime
) -> list[tuple[str, str, str, int, int, int, int, int, float | None, float | None]]:
    """(source, feed, iso_week, credits, candidates, gate_candidates, v3 rows,
    v3 rows with net edge, share net edge >= 0, mean ask-minus-fair gap in cents).

    One `feed_pulls` row is one credit. The edge columns come from the pull's v3
    `shadow_decisions` (sportsbook_v3), tagged with the same `pull_source`.
    """
    if "pull_source" not in _columns(conn, "feed_pulls"):
        return []
    stamp = since.isoformat()
    pulls: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for created_at, feed, source, candidates, gate in conn.execute(
        "SELECT created_at,feed,pull_source,candidates,gate_candidates FROM feed_pulls "
        "WHERE pull_source IS NOT NULL AND created_at>=?",
        (stamp,),
    ):
        year, week, _day = _utc(str(created_at)).isocalendar()
        key = (str(source), str(feed), f"{year}-W{week:02d}")
        pulls[key][0] += 1
        pulls[key][1] += int(candidates or 0)
        pulls[key][2] += int(gate or 0)
    edges: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    gaps: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    counts: Counter[tuple[str, str, str]] = Counter()
    if "pull_source" in _columns(conn, "shadow_decisions"):
        for created_at, ticker, source, price, fair, net_edge in conn.execute(
            "SELECT created_at,ticker,pull_source,price,fair,net_edge FROM shadow_decisions "
            "WHERE pull_source IS NOT NULL AND rules_version='v3' AND strategy='sportsbook_v3' "
            "AND created_at>=?",
            (stamp,),
        ):
            feed = KALSHI_SERIES_TO_SPORT.get(str(ticker).split("-", 1)[0], "?")
            year, week, _day = _utc(str(created_at)).isocalendar()
            key = (str(source), feed, f"{year}-W{week:02d}")
            counts[key] += 1
            edge = _as_float(net_edge)
            if edge is not None:
                edges[key].append(edge)
            ask, fair_value = _as_float(price), _as_float(fair)
            if ask is not None and fair_value is not None:
                gaps[key].append((ask - fair_value) * 100)
    out = []
    for key in sorted(set(pulls) | set(counts)):
        credits, candidates, gate = pulls.get(key, [0, 0, 0])
        edge_values, gap_values = edges.get(key, []), gaps.get(key, [])
        out.append(
            (
                *key,
                credits,
                candidates,
                gate,
                counts.get(key, 0),
                len(edge_values),
                sum(e >= 0 for e in edge_values) / len(edge_values) if edge_values else None,
                sum(gap_values) / len(gap_values) if gap_values else None,
            )
        )
    return out


@dataclass
class _SourceTotals:
    credits: int = 0
    candidates: int = 0
    gate_candidates: int = 0
    v3_rows: int = 0
    edge_rows: int = 0
    edge_ge_0: float = 0.0
    gap_rows: int = 0
    gap_sum: float = 0.0

    def line(self, key: Sequence[str]) -> str:
        share = f"{self.edge_ge_0 / self.edge_rows:.1%}" if self.edge_rows else "-"
        gap = f"{self.gap_sum / self.gap_rows:+.2f}" if self.gap_rows else "-"
        return (
            f"{','.join(key)},{self.credits},{self.candidates},{self.gate_candidates},"
            f"{self.v3_rows},{share},{gap}"
        )


def pull_source_section(path: Path, since: datetime) -> str:
    """v5 Phase 3-E: credits and candidate quality by why the pull ran."""
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = pull_source_rows(conn, since)
    if not rows:
        return "Pulls by source: no tagged feed_pulls rows (pre-Phase 3-E ledger or none yet)."
    order = {name: i for i, name in enumerate(PULL_SOURCE_ORDER)}

    def table(width: int) -> list[str]:
        totals: dict[tuple[str, ...], _SourceTotals] = defaultdict(_SourceTotals)
        for source, feed, week, credits, cands, gate, n_v3, n_edge, share, gap in rows:
            key = (source, feed, week)[:1] if width == 0 else (source, (feed, week)[width - 1])
            t = totals[key]
            t.credits += credits
            t.candidates += cands
            t.gate_candidates += gate
            t.v3_rows += n_v3
            if share is not None:
                t.edge_rows += n_edge
                t.edge_ge_0 += share * n_edge
            if gap is not None:
                t.gap_rows += n_v3
                t.gap_sum += gap * n_v3
        return [
            totals[key].line(key)
            for key in sorted(totals, key=lambda k: (order.get(k[0], 99), k[1:]))
        ]

    header = "credits,candidates,gate_candidates,v3_rows,share_net_edge_ge_0,mean_gap_c"
    return "\n".join(
        [
            "Pulls by source (v5 Phase 3-E; one feed_pulls row = one credit):",
            f"source,{header}",
            *table(0),
            "Pulls by source and feed:",
            f"source,feed,{header}",
            *table(1),
            "Pulls by source and ISO week:",
            f"source,week,{header}",
            *table(2),
        ]
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
    print(_feed_report_sections(args.ledger, runs, since, datetime.now(timezone.utc)))
    print(pull_plan_section(args.ledger, since))
    print(pull_source_section(args.ledger, since))


if __name__ == "__main__":
    main()
