#!/usr/bin/env python3
"""Replay the pull planner over the last N days of a paper ledger (v5 Phase 3-E).

Rebuilds the plan tick by tick from the ledger's own `market_snapshots` (each tick
sees only snapshots fetched by then) and counts the credits each planner would have
spent, next to what the live bot actually spent on the same days:

- `phase3`: Phase 3's T-24h/T-6h/T-90m/T-10m targets on the :17/:47 slots;
- `calendar`: Phase 3-E release-calendar targets on the 10-minute Worker ticks.

Each planner runs twice: `capped` uses the ledger's recorded `cadence_days` budgets
and run balances with today's default flags (Phase 3: the plan's share of each day;
calendar: the plan's month-to-date allowance and the reserves), `demand` lifts every
budget so the count is what the targets would ask for. Feed
eligibility is the ledger's latest `feed_stats`, so a feed that became eligible
mid-window is treated as eligible throughout. Local data only; no network.

    git show paper-ledger:paper.sqlite.gz | gunzip > /tmp/paper.sqlite
    python scripts/replay_pull_plan.py --ledger /tmp/paper.sqlite --days 14
"""

from __future__ import annotations

import argparse
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from prediction_bot.cli import _refresh_pull_plan
from prediction_bot.config import Settings
from prediction_bot.paper.ledger import Ledger
from prediction_bot.pull_plan import mark_pulled_ids, select_plan_pull
from prediction_bot.release_calendar import ReleaseCalendar

UTC = timezone.utc
ROOT = Path(__file__).parents[1]


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _ticks(start: datetime, end: datetime, minutes: tuple[int, ...]) -> list[datetime]:
    out = []
    at = start.replace(minute=0, second=0, microsecond=0)
    while at <= end:
        out.extend(t for m in minutes if start <= (t := at.replace(minute=m)) <= end)
        at += timedelta(hours=1)
    return out


def replay(
    source: sqlite3.Connection,
    start: datetime,
    end: datetime,
    calendar: ReleaseCalendar | None,
    minutes: tuple[int, ...],
    capped: bool,
    workdir: Path,
) -> tuple[Counter[str], Counter[str]]:
    """Plan credits by UTC day and by source for one planner."""
    name = f"{'cal' if calendar else 'p3'}-{'capped' if capped else 'demand'}.sqlite"
    ledger = Ledger(workdir / name, Decimal("1000"))
    settings = Settings(_env_file=None, rules_version="v3")  # type: ignore[call-arg]
    latest = source.execute(
        "SELECT feed,eligible,MAX(computed_at) FROM feed_stats GROUP BY feed"
    ).fetchall()
    ledger.conn.executemany(
        "INSERT INTO feed_stats (computed_at,feed,lead_p50_h,lead_p90_h,pulls_14d,"
        "useful_pulls_14d,provisional,eligible) VALUES (?,?,0,0,0,0,0,?)",
        [((start - timedelta(days=1)).isoformat(), feed, flag) for feed, flag, _at in latest],
    )
    budgets = dict(source.execute("SELECT day,budget FROM cadence_days").fetchall())
    day = start.date()
    while day <= end.date():
        budget = float(budgets.get(day.isoformat()) or 0) if capped else 10_000.0
        ledger.record_cadence_day(
            day, static_w=1, dyn_count=None, dyn_json=None, blended_w=1,
            budget=budget, mode="replay", at=start,
        )  # fmt: skip
        day += timedelta(days=1)
    snapshots = source.execute(
        "SELECT fetched_at,ticker,event_ticker,series,expected_expiration FROM market_snapshots "
        "WHERE fetched_at>=? AND fetched_at<=? ORDER BY fetched_at",
        ((start - timedelta(days=2)).isoformat(), end.isoformat()),
    ).fetchall()
    commences = source.execute(
        "SELECT created_at,feed,event_ticker,commence_time FROM odds_snapshots "
        "WHERE event_ticker IS NOT NULL AND created_at>=? AND created_at<=? ORDER BY created_at",
        ((start - timedelta(days=2)).isoformat(), end.isoformat()),
    ).fetchall()
    # `capped`: the live run balances, so the month allowance and floors see what the
    # bot saw (the rotation's real spending included).
    balances = (
        source.execute(
            "SELECT ran_at,odds_remaining FROM runs WHERE odds_remaining IS NOT NULL "
            "AND ran_at>=? AND ran_at<=? ORDER BY ran_at",
            ((start - timedelta(days=31)).isoformat(), end.isoformat()),
        ).fetchall()
        if capped
        else []
    )
    snap_i = odds_i = run_i = 0
    by_day: Counter[str] = Counter()
    by_source: Counter[str] = Counter()
    for now in _ticks(start, end, minutes):
        stamp = now.isoformat()
        batch = []
        while snap_i < len(snapshots) and snapshots[snap_i][0] <= stamp:
            batch.append(snapshots[snap_i])
            snap_i += 1
        ledger.conn.executemany(
            "INSERT INTO market_snapshots (fetched_at,ticker,event_ticker,series,yes_bid_size,"
            "yes_ask_size,open_interest,volume,expected_expiration) VALUES (?,?,?,?,0,0,0,0,?)",
            batch,
        )
        obatch = []
        while odds_i < len(commences) and commences[odds_i][0] <= stamp:
            obatch.append(commences[odds_i])
            odds_i += 1
        ledger.conn.executemany(
            "INSERT INTO odds_snapshots (created_at,feed,event_id,home_team,away_team,"
            "commence_time,team,prob,books_used,event_ticker) "
            "VALUES (?,?,'','','',?,'','0',0,?)",
            [(c, f, ct, e) for c, f, e, ct in obatch],
        )
        rbatch = []
        while run_i < len(balances) and balances[run_i][0] <= stamp:
            rbatch.append(balances[run_i])
            run_i += 1
        ledger.conn.executemany(
            "INSERT INTO runs (ran_at,strategies,scanned,signals,filled,settled,cash,open_cost,"
            "odds_remaining) VALUES (?,'replay',0,0,0,0,'0','0',?)",
            rbatch,
        )
        _refresh_pull_plan(ledger, now, settings, calendar)
        pick = select_plan_pull(ledger, settings, now, calendar=calendar)
        if pick is not None:
            mark_pulled_ids(ledger, pick.feed, now, 1.0)
            by_day[now.date().isoformat()] += 1
            by_source[pick.source or "default"] += 1
    ledger.conn.close()
    return by_day, by_source


def actual(source: sqlite3.Connection, start: datetime, end: datetime) -> dict[str, Counter[str]]:
    """What the live bot spent: feed pulls by UTC day, split by why where the ledger
    says (move triggers, measurement pulls, Phase 3 plan credits; the rest is the
    2-hour rotation)."""
    window = (start.isoformat(), end.isoformat())
    out: dict[str, Counter[str]] = defaultdict(Counter)
    for (at,) in source.execute(
        "SELECT created_at FROM feed_pulls WHERE created_at>=? AND created_at<=?", window
    ):
        out["all"][str(at)[:10]] += 1
    for (at,) in source.execute(
        "SELECT created_at FROM odds_triggers WHERE status='pulled' "
        "AND created_at>=? AND created_at<=?",
        window,
    ):
        out["move"][str(at)[:10]] += 1
    tables = {str(r[0]) for r in source.execute("SELECT name FROM sqlite_master")}
    if "measure_pulls" in tables:
        for (at,) in source.execute(
            "SELECT created_at FROM measure_pulls WHERE created_at>=? AND created_at<=?", window
        ):
            out["measure"][str(at)[:10]] += 1
    if "pull_plan" in tables:
        for at, credits in source.execute(
            "SELECT pulled_at,credits FROM pull_plan WHERE pulled_at>=? AND pulled_at<=?", window
        ):
            out["plan"][str(at)[:10]] += float(credits or 0)  # type: ignore[assignment]
    for day, n in out["all"].items():
        out["rotation"][day] = n - out["move"][day] - out["measure"][day] - out["plan"][day]
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args()
    uri = f"{args.ledger.resolve().as_uri()}?mode=ro"
    source = sqlite3.connect(uri, uri=True)
    last = _utc(str(source.execute("SELECT MAX(fetched_at) FROM market_snapshots").fetchone()[0]))
    end = last.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    calendar = ReleaseCalendar.load(ROOT / "data" / "release_calendar.json")
    print(f"Replay window: {start.isoformat()} to {end.isoformat()} ({args.days} days)")
    results: dict[str, tuple[Counter[str], Counter[str]]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for label, cal, minutes in (
            ("phase3", None, (17, 47)),
            ("calendar", calendar, (7, 17, 27, 37, 47, 57)),
        ):
            for capped in (True, False):
                key = f"{label}_{'capped' if capped else 'demand'}"
                results[key] = replay(source, start, end, cal, minutes, capped, Path(tmp))
    live = actual(source, start, end)
    days = sorted({d for by_day, _src in results.values() for d in by_day} | set(live["all"]))
    cols = [*results, "live_all", "live_rotation", "live_move", "live_measure", "live_plan"]
    print("day," + ",".join(cols))
    for day in days:
        row = [str(results[k][0][day]) for k in results] + [
            f"{live[k][day]:g}" for k in ("all", "rotation", "move", "measure", "plan")
        ]
        print(day + "," + ",".join(row))
    totals = [str(sum(results[k][0].values())) for k in results] + [
        f"{sum(live[k].values()):g}" for k in ("all", "rotation", "move", "measure", "plan")
    ]
    print("total," + ",".join(totals))
    for key, (_by_day, by_source) in results.items():
        print(f"{key} by source: " + ", ".join(f"{s}={n}" for s, n in sorted(by_source.items())))


if __name__ == "__main__":
    main()
