"""Per-game odds pull targets (v5 Phase 3.1).

Every tracked Kalshi event in an eligible feed gets targets at T-24h, T-6h, T-90m and
T-10m (the sharp close Phase 0 scores against). One pull of a feed satisfies every due
target of that feed, so a slate kicking off together costs one credit per target.

Credits: the rotation keeps `odds_entry_share` of each day's budget for 72h+ entry
pulls; plan pulls may spend the rest, highest priority first (T-10m, T-90m, T-6h,
T-24h). A lower-priority target is funded only when the credits left today exceed the
higher-priority pull groups still ahead today. `OddsPacer` remains the hard cap.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from prediction_bot.config import Settings
from prediction_bot.odds import KALSHI_SERIES_TO_SPORT

if TYPE_CHECKING:
    from prediction_bot.paper.ledger import Ledger

# name, offset before kickoff, early tolerance, late tolerance, priority (0 = first)
TARGETS: tuple[tuple[str, timedelta, timedelta, timedelta, int], ...] = (
    # The sharp close: books' last update must fall inside kickoff - 20 min.
    ("T-10m", timedelta(minutes=10), timedelta(minutes=10), timedelta(minutes=8), 0),
    ("T-90m", timedelta(minutes=90), timedelta(minutes=30), timedelta(minutes=30), 1),
    ("T-6h", timedelta(hours=6), timedelta(hours=1), timedelta(hours=1), 2),
    ("T-24h", timedelta(hours=24), timedelta(hours=1), timedelta(hours=1), 3),
)
PLAN_HORIZON = timedelta(hours=26)
SNAPSHOT_MAX_AGE = timedelta(days=2)
GROUP_BUCKET = timedelta(minutes=30)
RESERVE_FRACTION = 0.95


@dataclass(frozen=True)
class PlanPick:
    feed: str
    row_ids: tuple[int, ...]
    priority: int


def _utc(value: datetime | str) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def pregame_offset(feed: str) -> timedelta:
    """Kalshi `expected_expiration` minus kickoff (same table as cli._planner_games)."""
    if feed.startswith("soccer_"):
        return timedelta(hours=2)
    if feed == "basketball_nba":
        return timedelta(hours=2.5)
    return timedelta(hours=3)


def eligible_feeds(conn: sqlite3.Connection) -> set[str]:
    """Feeds whose latest `feed_stats` row is eligible for the entry window."""
    return {
        str(feed)
        for feed, flag in conn.execute(
            "SELECT s.feed,s.eligible FROM feed_stats s WHERE s.computed_at="
            "(SELECT MAX(t.computed_at) FROM feed_stats t WHERE t.feed=s.feed)"
        )
        if int(flag) == 1
    }


def plan_kickoffs(
    conn: sqlite3.Connection, feeds: Iterable[str], now: datetime
) -> dict[str, tuple[str, datetime]]:
    """event_ticker -> (feed, kickoff) for events kicking off within `PLAN_HORIZON`.

    Kickoff is the Odds API `commence_time` from a linked snapshot when one exists,
    else the latest `market_snapshots.expected_expiration` minus the pre-game offset.
    """
    now = _utc(now)
    wanted = set(feeds)
    out: dict[str, tuple[str, datetime]] = {}
    cutoff = (now - SNAPSHOT_MAX_AGE).isoformat()
    for event_ticker, expiration in conn.execute(
        "SELECT event_ticker,MAX(expected_expiration) FROM market_snapshots "
        "WHERE expected_expiration IS NOT NULL AND fetched_at>=? GROUP BY event_ticker",
        (cutoff,),
    ):
        feed = KALSHI_SERIES_TO_SPORT.get(str(event_ticker).split("-", 1)[0])
        if feed is None or feed not in wanted:
            continue
        try:
            kickoff = _utc(str(expiration)) - pregame_offset(feed)
        except ValueError:
            continue
        out[str(event_ticker)] = (feed, kickoff)
    for event_ticker, commence in conn.execute(
        "SELECT event_ticker,MAX(commence_time) FROM odds_snapshots "
        "WHERE event_ticker IS NOT NULL AND created_at>=? GROUP BY event_ticker",
        (cutoff,),
    ):
        if str(event_ticker) in out:
            try:
                out[str(event_ticker)] = (out[str(event_ticker)][0], _utc(str(commence)))
            except ValueError:
                continue
    return {
        event: (feed, kickoff)
        for event, (feed, kickoff) in out.items()
        if now < kickoff <= now + PLAN_HORIZON
    }


def plan_pulls(ledger: Ledger, kickoffs: Mapping[str, tuple[str, datetime]], now: datetime) -> int:
    """Upsert planned targets and close out expired ones. Returns rows planned."""
    now = _utc(now)
    stamp = now.isoformat()
    planned = 0
    for event_ticker, (feed, kickoff) in kickoffs.items():
        kickoff = _utc(kickoff)
        for name, offset, early, late, priority in TARGETS:
            target_at = kickoff - offset
            start, end = target_at - early, target_at + late
            if end < now:
                continue
            cursor = ledger.conn.execute(
                "INSERT INTO pull_plan (event_ticker,feed,target,kickoff,target_at,window_start,"
                "window_end,priority,status,credits,planned_at) "
                "VALUES (?,?,?,?,?,?,?,?,'planned',0,?) "
                "ON CONFLICT(event_ticker,target) DO UPDATE SET kickoff=excluded.kickoff,"
                "target_at=excluded.target_at,window_start=excluded.window_start,"
                "window_end=excluded.window_end WHERE pull_plan.status='planned' "
                "AND pull_plan.kickoff<>excluded.kickoff",
                (
                    event_ticker,
                    feed,
                    name,
                    kickoff.isoformat(),
                    target_at.isoformat(),
                    start.isoformat(),
                    end.isoformat(),
                    priority,
                    stamp,
                ),
            )
            planned += cursor.rowcount
    ledger.conn.execute(
        "UPDATE pull_plan SET status=CASE WHEN budget_blocked_at IS NOT NULL "
        "THEN 'skipped_budget' ELSE 'missed' END WHERE status='planned' AND window_end<?",
        (stamp,),
    )
    ledger.conn.commit()
    return planned


def _day_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _days_left_in_month(today: date) -> int:
    nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    return (nxt - today).days


def plan_daily_cap(ledger: Ledger, settings: Settings, now: datetime) -> int:
    """Credits plan pulls may spend today: (1 - odds_entry_share) of the day's budget.

    The day's budget is the cadence planner's figure for today when recorded, else the
    latest known month balance spread over the days left (with the 5% reserve).
    """
    now = _utc(now)
    day = ledger.cadence_day(now.date())
    budget = float(day["budget"]) if day and day.get("budget") else 0.0  # type: ignore[arg-type]
    if budget <= 0:
        row = ledger.conn.execute(
            "SELECT odds_remaining FROM runs WHERE odds_remaining IS NOT NULL "
            "ORDER BY ran_at DESC,id DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            budget = float(row[0]) * RESERVE_FRACTION / max(1, _days_left_in_month(now.date()))
    share = min(1.0, max(0.0, 1.0 - settings.odds_entry_share))
    return max(0, math.floor(budget * share + 1e-9))


def plan_credits_today(ledger: Ledger, now: datetime) -> float:
    start, end = _day_bounds(_utc(now))
    row = ledger.conn.execute(
        "SELECT COALESCE(SUM(credits),0) FROM pull_plan WHERE pulled_at>=? AND pulled_at<?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return float(row[0])


def _due_rows(
    ledger: Ledger, now: datetime, feed: str | None = None
) -> list[tuple[int, str, int, str]]:
    stamp = _utc(now).isoformat()
    sql = (
        "SELECT id,feed,priority,window_end FROM pull_plan WHERE status='planned' "
        "AND window_start<=? AND window_end>=?"
    )
    params: tuple[str, ...] = (stamp, stamp)
    if feed is not None:
        sql += " AND feed=?"
        params = (*params, feed)
    return [
        (int(r[0]), str(r[1]), int(r[2]), str(r[3]))
        for r in ledger.conn.execute(sql + " ORDER BY id", params)
    ]


def _groups_ahead(ledger: Ledger, now: datetime, priority: int) -> int:
    """Distinct (feed, 30-min bucket) pulls of higher-priority targets still ahead today."""
    now = _utc(now)
    _start, end = _day_bounds(now)
    buckets: set[tuple[str, int]] = set()
    for feed, window_start in ledger.conn.execute(
        "SELECT feed,window_start FROM pull_plan WHERE status='planned' AND priority<? "
        "AND window_start>? AND window_start<?",
        (priority, now.isoformat(), end.isoformat()),
    ):
        at = _utc(str(window_start))
        buckets.add((str(feed), int(at.timestamp() // GROUP_BUCKET.total_seconds())))
    return len(buckets)


def select_plan_pull(
    ledger: Ledger, settings: Settings, now: datetime, exclude: Iterable[str] = ()
) -> PlanPick | None:
    """The funded feed to pull now for its due targets, or None.

    Due targets that cannot be funded are stamped `budget_blocked_at`, so if they
    expire unpulled they are recorded as `skipped_budget` rather than `missed`.
    """
    if settings.odds_pull_plan == "off":
        return None
    now = _utc(now)
    excluded = set(exclude)
    by_feed: dict[str, list[tuple[int, int, str]]] = {}
    for row_id, feed, priority, window_end in _due_rows(ledger, now):
        if feed not in excluded:
            by_feed.setdefault(feed, []).append((row_id, priority, window_end))
    if not by_feed:
        return None
    remaining = plan_daily_cap(ledger, settings, now) - plan_credits_today(ledger, now)
    ordered = sorted(
        by_feed.items(),
        key=lambda item: (min(p for _i, p, _e in item[1]), min(e for _i, _p, e in item[1])),
    )
    blocked: list[int] = []
    pick: PlanPick | None = None
    for feed, rows in ordered:
        priority = min(p for _i, p, _e in rows)
        if pick is None and remaining - _groups_ahead(ledger, now, priority) >= 1:
            pick = PlanPick(feed, tuple(i for i, _p, _e in rows), priority)
        else:
            blocked.extend(i for i, _p, _e in rows)
    if blocked:
        ledger.conn.executemany(
            "UPDATE pull_plan SET budget_blocked_at=COALESCE(budget_blocked_at,?) WHERE id=?",
            [(now.isoformat(), row_id) for row_id in blocked],
        )
        ledger.conn.commit()
    return pick


def mark_pulled(ledger: Ledger, feed: str, now: datetime, credits: float) -> int:
    """Mark the feed's due targets pulled, sharing `credits` across them.

    `credits` is 1 for a plan-funded pull and 0 when the targets rode along on a pull
    made for another reason (rotation, trigger, measurement).
    """
    rows = _due_rows(ledger, now, feed)
    if not rows:
        return 0
    share = credits / len(rows)
    ledger.conn.executemany(
        "UPDATE pull_plan SET status='pulled',pulled_at=?,credits=? WHERE id=?",
        [(_utc(now).isoformat(), share, row_id) for row_id, *_ in rows],
    )
    ledger.conn.commit()
    return len(rows)
