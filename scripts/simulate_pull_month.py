#!/usr/bin/env python3
"""Simulate a month of odds credits under the v5 Phase 3-E planner (no network).

Steps every tick of November 2026 against the real `data/cadence_calendar.json`, the
real `data/release_calendar.json` and a dense synthetic slate (Sunday and Monday NFL,
Saturday NCAAF and EPL, Tuesday/Wednesday UCL). Credits are counted like the Odds API:
one per feed pull. The rotation runs on the :17 slot of even UTC hours, as the 2-hour
sportsbook slot does; plan targets run on any tick.

    python scripts/simulate_pull_month.py              # all four combinations
    python scripts/simulate_pull_month.py --grid fallback --cadence 10min
"""

from __future__ import annotations

import argparse
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from prediction_bot import kickoffs
from prediction_bot.cadence import CadenceCalendar
from prediction_bot.cli import _refresh_pull_plan
from prediction_bot.config import Settings
from prediction_bot.odds import OddsPacer
from prediction_bot.paper.ledger import Ledger
from prediction_bot.pull_plan import (
    mark_pulled_ids,
    plan_allowance,
    plan_hold,
    select_plan_pull,
)
from prediction_bot.release_calendar import ReleaseCalendar

UTC = timezone.utc
ROOT = Path(__file__).parents[1]
START = datetime(2026, 11, 1, tzinfo=UTC)
FEEDS = [
    "americanfootball_nfl",
    "americanfootball_ncaaf",
    "soccer_epl",
    "soccer_uefa_champs_league",
]
CADENCES = {"10min": (7, 17, 27, 37, 47, 57), "cron": (17, 47)}
# Move-trigger pulls: the live ledger averaged 3-4 a day (Oct 25 - Nov 5); the sim
# fires one on the :47 tick of these UTC hours, every day.
MOVE_HOURS = (14, 18, 22)
MONTH_CREDITS = 500


class Credits:
    """The Odds API's accounting: each pull of one feed costs one credit."""

    def __init__(self, left: int) -> None:
        self.left = left
        self.budget: int | None = None
        self.requests_remaining: str | None = str(left)
        self.requests_used: str | None = "0"
        self.requests_made = 0
        self.odds_requests_made = 0
        self.requests_last_by_sport: dict[str, int | None] = {}
        self.events_free = True
        self._events_requests_used_baseline: int | None = None

    def quota_remaining(self) -> int:
        self.requests_remaining = str(self.left)
        return self.left

    def pull(self, feed: str) -> bool:
        if self.budget is None or self.requests_made >= self.budget or self.left <= 0:
            return False
        self.left -= 1
        self.requests_made += 1
        self.odds_requests_made += 1
        self.requests_last_by_sport[feed] = 1
        self.requests_remaining = str(self.left)
        return True


def _game(ledger: Ledger, event: str, kickoff: datetime, offset_h: float = 3) -> None:
    ledger.conn.execute(
        "INSERT INTO market_snapshots (fetched_at,ticker,event_ticker,series,yes_bid,yes_ask,"
        "yes_bid_size,yes_ask_size,open_interest,volume,expected_expiration) "
        "VALUES (?,?,?,?,'0.48','0.50','100','100','10','10',?)",
        (
            (kickoff - timedelta(hours=30)).isoformat(),
            f"{event}-A",
            event,
            event.split("-", 1)[0],
            (kickoff + timedelta(hours=offset_h)).isoformat(),
        ),
    )
    if event.startswith("KXNCAAFGAME-"):
        # NCAAF expirations are placeholders, so no close is planned from them: the
        # books' kickoff (here from the free /events listing, days ahead) is needed.
        kickoffs.observe(ledger.conn, event, kickoff, "odds_events", kickoff - timedelta(days=4))


def seed_slate(ledger: Ledger) -> None:
    """The Phase 3 November slate, plus Monday Night Football."""
    ledger.conn.executemany(
        "INSERT INTO feed_stats (computed_at,feed,lead_p50_h,lead_p90_h,pulls_14d,"
        "useful_pulls_14d,provisional,eligible) VALUES (?,?,100,200,0,0,0,1)",
        [((START - timedelta(days=1)).isoformat(), feed) for feed in FEEDS],
    )
    for day in range(30):
        date = START + timedelta(days=day)
        weekday = date.weekday()
        if weekday == 6:  # Sunday NFL: 1:00, 4:05, 4:25 pm ET and Sunday night (Mon UTC)
            for i, (h, m) in enumerate([(18, 0)] * 6 + [(21, 5), (21, 25)]):
                _game(ledger, f"KXNFLGAME-{day:02d}N{i}", date.replace(hour=h, minute=m))
            _game(
                ledger,
                f"KXNFLGAME-{day:02d}SNF",
                date.replace(hour=1, minute=20) + timedelta(days=1),
            )
            # Monday night: 8:15 pm ET Monday = 01:15Z Tuesday.
            _game(
                ledger,
                f"KXNFLGAME-{day:02d}MNF",
                date.replace(hour=1, minute=15) + timedelta(days=2),
            )
        if weekday == 5:  # Saturday college football and EPL
            for i, h in enumerate([16, 16, 17, 19, 20, 20, 23, 23, 2]):
                _game(
                    ledger, f"KXNCAAFGAME-{day:02d}C{i}", date.replace(hour=h, minute=30 * (i % 2))
                )
            for i, h in enumerate([12, 15, 15, 15, 17]):
                _game(ledger, f"KXEPLGAME-{day:02d}E{i}", date.replace(hour=h), offset_h=3)
        if weekday in (1, 2):  # UCL Tuesday/Wednesday
            for i, (h, m) in enumerate([(17, 45), (17, 45)] + [(20, 0)] * 6):
                _game(
                    ledger, f"KXUCLGAME-{day:02d}U{i}", date.replace(hour=h, minute=m), offset_h=3
                )
    ledger.conn.commit()


def _ticks(minutes: tuple[int, ...]) -> list[datetime]:
    out = []
    at = START
    while at < START + timedelta(days=30):
        out.extend(at.replace(minute=m) for m in minutes)
        at += timedelta(hours=1)
    return out


def simulate(
    grid: str, cadence: str, workdir: Path, with_moves: bool = True, entry_share: float = 0.5
) -> dict[str, Any]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, rules_version="v3", odds_grid=grid, odds_entry_share=entry_share
    )
    release = ReleaseCalendar.load(ROOT / "data" / "release_calendar.json")
    cadence_cal = CadenceCalendar.load(ROOT / "data" / "cadence_calendar.json")
    ledger = Ledger(workdir / f"month-{grid}-{cadence}.sqlite", Decimal("1000"))
    seed_slate(ledger)
    client = Credits(MONTH_CREDITS)
    rotation = OddsPacer(client, 12, ledger=ledger, cadence_mode="off", calendar=cadence_cal)  # type: ignore[arg-type]
    rotation.rotation_share = 0.0 if grid == "fallback" else settings.odds_entry_share
    rotation.move_reserve = settings.odds_move_reserve
    rotation.rotation_holds_floor = True
    counts: dict[str, int] = defaultdict(int)
    plan_by_day: dict[str, int] = defaultdict(int)
    for now in _ticks(CADENCES[cadence]):
        # Every run records the Odds API balance, as `pbot paper` does.
        ledger.conn.execute(
            "INSERT INTO runs (ran_at,strategies,scanned,signals,filled,settled,cash,"
            "open_cost,odds_remaining) VALUES (?,'book_scanner',0,0,0,0,'1000','0',?)",
            ((now - timedelta(seconds=1)).isoformat(), client.left),
        )
        _refresh_pull_plan(ledger, now, settings, release)
        hold = plan_hold(ledger, settings, release, now)
        if now.minute == 17 and now.hour % 2 == 0:  # the 2-hour sportsbook slot
            rotation.plan_hold = hold
            budget = rotation.start_run(now)
            for i in range(budget):
                counts["rotation"] += client.pull(f"rotation_{i}")
            pick = select_plan_pull(ledger, settings, now, calendar=release)
            if (
                pick is not None
                and rotation.grant_extra_credit(pick.feed, floor=rotation.plan_floor(now))
                and client.pull(pick.feed)
            ):
                mark_pulled_ids(ledger, pick.feed, now, 1.0)
                counts["plan"] += 1
                plan_by_day[now.date().isoformat()] += 1
            rotation.finish_run(now)
            continue
        if now.minute == 47 and now.hour in MOVE_HOURS and with_moves:
            mover = OddsPacer(client, 12, ledger=ledger, trigger_mode=True)  # type: ignore[arg-type]
            mover.move_reserve = settings.odds_move_reserve
            mover.rotation_holds_floor = True
            mover.plan_hold = hold
            mover.start_run(now)
            if client.pull("move"):
                counts["move"] += 1
                ledger.conn.execute(
                    "INSERT INTO odds_triggers (created_at,feed,ticker,ask,status) "
                    "VALUES (?,'move',?,'0.5','pulled')",
                    (now.isoformat(), f"M{counts['move']}"),
                )
            mover.finish_run(now)
        pick = select_plan_pull(ledger, settings, now, calendar=release)
        if pick is None:
            continue
        planner = OddsPacer(client, 12, ledger=ledger, plan_mode=True)  # type: ignore[arg-type]
        planner.move_reserve = settings.odds_move_reserve
        if planner.start_run(now) and client.pull(pick.feed):
            mark_pulled_ids(ledger, pick.feed, now, 1.0)
            counts["plan"] += 1
            plan_by_day[now.date().isoformat()] += 1
        planner.finish_run(now)
    _refresh_pull_plan(ledger, START + timedelta(days=33), settings, release)
    allowance = plan_allowance(ledger, settings, START + timedelta(days=29, hours=23))
    status = {
        f"{source}:{state}": int(n)
        for source, state, n in ledger.conn.execute(
            "SELECT source,status,COUNT(*) FROM pull_plan GROUP BY source,status"
        )
    }
    nfl_clock = {
        str(state): int(n)
        for state, n in ledger.conn.execute(
            "SELECT status,COUNT(*) FROM pull_plan WHERE feed='americanfootball_nfl' "
            "AND source='release' AND (target LIKE '%T11:35' OR target LIKE '%T14:55' "
            "OR target LIKE '%T18:50') GROUP BY status"
        )
    }
    lowest = ledger.conn.execute("SELECT MIN(odds_remaining) FROM runs").fetchone()[0]
    ledger.conn.close()
    return {
        "lowest_balance": min(int(lowest), client.left),
        "grid": grid,
        "cadence": cadence,
        "spent": MONTH_CREDITS - client.left,
        "spent_excluding_moves": MONTH_CREDITS - client.left - counts["move"],
        "rotation_pulls": counts["rotation"],
        "plan_pulls": counts["plan"],
        "move_pulls": counts["move"],
        # The plan's month allowance (its share of the usable credits) and its
        # busiest UTC day.
        "plan_allowance": round(allowance or 0, 1),
        "max_plan_pulls_in_a_day": max(plan_by_day.values(), default=0),
        "nfl_sun_mon_clock": nfl_clock,
        "targets": status,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid", choices=["rotation", "fallback"], action="append")
    parser.add_argument("--cadence", choices=sorted(CADENCES), action="append")
    parser.add_argument("--no-moves", action="store_true", help="simulate no move-trigger pulls")
    parser.add_argument("--entry-share", type=float, default=0.5, help="ODDS_ENTRY_SHARE")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        for grid in args.grid or ["rotation", "fallback"]:
            for cadence in args.cadence or ["10min", "cron"]:
                result = simulate(
                    grid,
                    cadence,
                    Path(tmp),
                    with_moves=not args.no_moves,
                    entry_share=args.entry_share,
                )
                print(result)


if __name__ == "__main__":
    main()
