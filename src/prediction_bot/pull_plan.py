"""Per-game odds pull targets (v5 Phase 3.1).

Every tracked Kalshi event in an eligible feed gets targets at T-24h, T-6h, T-90m and
T-10m (the sharp close Phase 0 scores against). One pull of a feed satisfies every due
target of that feed, so a slate kicking off together costs one credit per target.

Credits: the rotation keeps `odds_entry_share` of each day's budget for 72h+ entry
pulls; plan pulls may spend the rest, highest priority first (T-10m, T-90m, T-6h,
T-24h). A lower-priority target is funded only when the credits left today exceed the
higher-priority pull groups still ahead today. `OddsPacer` remains the hard cap.

Phase 3-E (`calendar` passed, `PBOT_ODDS_RELEASE_CALENDAR=on`): targets come from
`data/release_calendar.json` instead. Feeds with entries get their `kickoff` offsets
(lineups, inactives) and feed-level `clock` targets (status reports) in place of
T-24h/T-6h/T-90m; every feed keeps the T-10m sharp close. Targets of one feed within
20 minutes of each other merge into the higher-priority one, so they cost one credit.
Every row records its `source` (close, kickoff, release, default, grid) so pulls can
be evaluated by why they ran.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING

from prediction_bot.config import Settings
from prediction_bot.odds import KALSHI_SERIES_TO_SPORT

if TYPE_CHECKING:
    from prediction_bot.paper.ledger import Ledger
    from prediction_bot.release_calendar import ReleaseCalendar

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

# Phase 3-E (release calendar).
CALENDAR_HORIZON = timedelta(hours=48)
GAME_LOOKAHEAD = timedelta(days=9)  # known games that make a clock target worth paying for
RELEASE_WINDOW = timedelta(minutes=60)  # a release/kickoff/grid target expires after this
MERGE_WINDOW = timedelta(minutes=20)  # same-feed targets this close share one pull
CLOSE_MIN_LEAD = timedelta(minutes=2)  # nothing is taken later than kickoff - 2 min
FEED_EVENT_PREFIX = "feed:"  # event_ticker of feed-level rows (clock and grid targets)
# Credits kept back from plan pulls (same 25 as OddsPacer.MEASURE_RESERVE).
PLAN_HARD_RESERVE = 25


@dataclass(frozen=True)
class PlanPick:
    feed: str
    row_ids: tuple[int, ...]
    priority: int
    # The row the pull is attributed to (highest priority, then earliest window end).
    primary_id: int | None = None
    source: str | None = None


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
    conn: sqlite3.Connection,
    feeds: Iterable[str],
    now: datetime,
    horizon: timedelta = PLAN_HORIZON,
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
        if now < kickoff <= now + horizon
    }


def plan_pulls(
    ledger: Ledger,
    kickoffs: Mapping[str, tuple[str, datetime]],
    now: datetime,
    calendar: ReleaseCalendar | None = None,
) -> int:
    """Upsert planned targets and close out expired ones. Returns rows planned.

    Without `calendar` this is Phase 3 exactly; with it, see `_plan_event_targets`.
    """
    if calendar is not None:
        planned = sum(
            _plan_event_targets(ledger, calendar, event, feed, _utc(kickoff), _utc(now))
            for event, (feed, kickoff) in kickoffs.items()
        )
        close_out(ledger, now)
        return planned
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
    close_out(ledger, now)
    return planned


def close_out(ledger: Ledger, now: datetime) -> None:
    """Planned targets whose window ended: `skipped_budget` if they were due but
    unfunded, else `missed`."""
    ledger.conn.execute(
        "UPDATE pull_plan SET status=CASE WHEN budget_blocked_at IS NOT NULL "
        "THEN 'skipped_budget' ELSE 'missed' END WHERE status='planned' AND window_end<?",
        (_utc(now).isoformat(),),
    )
    ledger.conn.commit()


def _upsert(
    ledger: Ledger,
    *,
    event_ticker: str,
    feed: str,
    target: str,
    kickoff: datetime,
    target_at: datetime,
    start: datetime,
    end: datetime,
    priority: int,
    source: str,
    entry: str | None,
    max_per_day: int | None,
    now: datetime,
) -> int:
    """Insert a target, or refresh a still-planned one whose timing or priority changed."""
    cursor = ledger.conn.execute(
        "INSERT INTO pull_plan (event_ticker,feed,target,kickoff,target_at,window_start,"
        "window_end,priority,status,credits,planned_at,source,entry,max_per_day) "
        "VALUES (?,?,?,?,?,?,?,?,'planned',0,?,?,?,?) "
        "ON CONFLICT(event_ticker,target) DO UPDATE SET kickoff=excluded.kickoff,"
        "target_at=excluded.target_at,window_start=excluded.window_start,"
        "window_end=excluded.window_end,priority=excluded.priority,source=excluded.source,"
        "entry=excluded.entry,max_per_day=excluded.max_per_day "
        "WHERE pull_plan.status='planned' AND (pull_plan.kickoff<>excluded.kickoff "
        "OR pull_plan.window_start<>excluded.window_start "
        "OR pull_plan.window_end<>excluded.window_end "
        "OR pull_plan.priority<>excluded.priority OR pull_plan.source<>excluded.source)",
        (
            event_ticker,
            feed,
            target,
            kickoff.isoformat(),
            target_at.isoformat(),
            start.isoformat(),
            end.isoformat(),
            priority,
            now.isoformat(),
            source,
            entry,
            max_per_day,
        ),
    )
    return cursor.rowcount


def _plan_event_targets(
    ledger: Ledger,
    calendar: ReleaseCalendar,
    event_ticker: str,
    feed: str,
    kickoff: datetime,
    now: datetime,
) -> int:
    """One event's targets under the release calendar.

    - T-10m sharp close on every feed (window kickoff -20m to -2m, as in Phase 3).
    - Feeds with calendar entries: each `kickoff` entry, window [target, target + 60m]
      cut at kickoff - 2m. Their T-24h/T-6h/T-90m are not planned.
    - Feeds without entries: Phase 3's T-24h/T-6h/T-90m at `default_priority`.
    Still-planned rows of the event that no longer match are marked `superseded`.
    """
    from prediction_bot.release_calendar import plan_priority

    rows: list[tuple[str, datetime, datetime, datetime, int, str, str | None, int | None]] = []
    for name, offset, early, late, _priority in TARGETS:
        target_at = kickoff - offset
        if name == "T-10m":
            rows.append(
                (
                    name,
                    target_at,
                    target_at - early,
                    target_at + late,
                    plan_priority(calendar.close_priority),
                    "close",
                    "close",
                    None,
                )
            )
        elif not calendar.has_entries(feed):
            rows.append(
                (
                    name,
                    target_at,
                    target_at - early,
                    target_at + late,
                    plan_priority(calendar.default_priority),
                    "default",
                    f"default:{name}",
                    None,
                )
            )
    for entry in calendar.kickoff_entries(feed):
        target_at = kickoff - timedelta(minutes=entry.minutes_before)
        end = min(target_at + RELEASE_WINDOW, kickoff - CLOSE_MIN_LEAD)
        rows.append(
            (
                entry.target,
                target_at,
                target_at,
                end,
                plan_priority(entry.priority),
                "kickoff",
                entry.key,
                entry.max_per_day,
            )
        )
    planned = 0
    for name, target_at, start, end, priority, source, entry_key, cap in rows:
        if end < now:
            continue
        planned += _upsert(
            ledger,
            event_ticker=event_ticker,
            feed=feed,
            target=name,
            kickoff=kickoff,
            target_at=target_at,
            start=start,
            end=end,
            priority=priority,
            source=source,
            entry=entry_key,
            max_per_day=cap,
            now=now,
        )
    wanted = [name for name, *_rest in rows]
    ledger.conn.execute(
        "UPDATE pull_plan SET status='superseded',note=COALESCE(note,'calendar') "
        f"WHERE event_ticker=? AND status='planned' AND target NOT IN "
        f"({','.join('?' * len(wanted))})",
        (event_ticker, *wanted),
    )
    return planned


def plan_feed_targets(
    ledger: Ledger,
    calendar: ReleaseCalendar,
    feeds: Iterable[str],
    now: datetime,
    grid: bool = False,
) -> int:
    """Feed-level targets for the next 48h: `clock` entries, and with `grid` one 15:00
    local target per eligible feed on a local day with no other target.

    A clock target is planned only while the feed has a known game kicking off within
    the entry's `requires_game_within_hours` after it. Feed-level rows store the next
    such kickoff so the free /events check has a window to look at.
    """
    from prediction_bot.release_calendar import plan_priority

    now = _utc(now)
    feeds = sorted(set(feeds))
    games: dict[str, list[datetime]] = {}
    for _event, (feed, kickoff) in plan_kickoffs(ledger.conn, feeds, now, GAME_LOOKAHEAD).items():
        games.setdefault(feed, []).append(kickoff)
    for kickoffs in games.values():
        kickoffs.sort()
    planned = 0
    for feed in feeds:
        kickoffs = games.get(feed, [])
        for target in calendar.clock_targets(feed, now - RELEASE_WINDOW, now + CALENDAR_HORIZON):
            reach = timedelta(hours=target.entry.requires_game_within_hours)
            upcoming = [k for k in kickoffs if target.target_at < k <= target.target_at + reach]
            if not upcoming:
                continue
            planned += _upsert(
                ledger,
                event_ticker=f"{FEED_EVENT_PREFIX}{feed}",
                feed=feed,
                target=target.name,
                kickoff=upcoming[0],
                target_at=target.target_at,
                start=target.target_at,
                end=target.target_at + RELEASE_WINDOW,
                priority=plan_priority(target.entry.priority),
                source="release",
                entry=target.entry.key,
                max_per_day=target.entry.max_per_day,
                now=now,
            )
    if grid:
        planned += _plan_grid(ledger, calendar, feeds, games, now)
    ledger.conn.commit()
    return planned


def _plan_grid(
    ledger: Ledger,
    calendar: ReleaseCalendar,
    feeds: list[str],
    games: Mapping[str, list[datetime]],
    now: datetime,
) -> int:
    from prediction_bot.release_calendar import plan_priority

    planned = 0
    today = calendar.local_date(now)
    for day in (today, today + timedelta(days=1)):
        at = calendar.grid_at(day)
        if at + RELEASE_WINDOW < now:
            continue
        day_start = calendar.local_at(day, time(0))
        day_end = calendar.local_at(day + timedelta(days=1), time(0))
        for feed in feeds:
            other = ledger.conn.execute(
                "SELECT 1 FROM pull_plan WHERE feed=? AND source<>'grid' "
                "AND status<>'superseded' AND target_at>=? AND target_at<? LIMIT 1",
                (feed, day_start.isoformat(), day_end.isoformat()),
            ).fetchone()
            name = f"G-{day.isoformat()}"
            event = f"{FEED_EVENT_PREFIX}{feed}"
            if other is not None:
                ledger.conn.execute(
                    "UPDATE pull_plan SET status='superseded',note=COALESCE(note,'grid') "
                    "WHERE event_ticker=? AND target=? AND status='planned'",
                    (event, name),
                )
                continue
            later = [k for k in games.get(feed, []) if k > at]
            planned += _upsert(
                ledger,
                event_ticker=event,
                feed=feed,
                target=name,
                kickoff=later[0] if later else at,
                target_at=at,
                start=at,
                end=at + RELEASE_WINDOW,
                priority=plan_priority(calendar.grid_priority),
                source="grid",
                entry="grid",
                max_per_day=None,
                now=now,
            )
    return planned


def merge_near_targets(ledger: Ledger, now: datetime) -> int:
    """Merge same-feed targets within 20 minutes into the higher-priority one.

    Recomputed on every rebuild over planned rows (plus recently pulled ones, which
    are fixed). The T-10m close never merges: its window is the sharp-close window.
    A merged row is not funded on its own; when its survivor is pulled it is marked
    pulled with it and shares the credit. Returns the number of merged rows.
    """
    now = _utc(now)
    ledger.conn.execute(
        "UPDATE pull_plan SET merged_into=NULL WHERE status='planned' AND merged_into IS NOT NULL"
    )
    rows = ledger.conn.execute(
        "SELECT id,feed,priority,target_at,status FROM pull_plan WHERE source<>'close' "
        "AND ((status='planned' AND window_end>=?) OR (status='pulled' AND pulled_at>=?)) "
        "ORDER BY feed,CASE status WHEN 'pulled' THEN 0 ELSE 1 END,priority,target_at,id",
        (now.isoformat(), (now - RELEASE_WINDOW).isoformat()),
    ).fetchall()
    survivors: dict[str, list[tuple[int, datetime]]] = {}
    merges: list[tuple[int, int]] = []
    for row_id, feed, _priority, target_at, status in rows:
        at = _utc(str(target_at))
        near = [
            (abs((at - other).total_seconds()), sid)
            for sid, other in survivors.get(str(feed), [])
            if abs(at - other) <= MERGE_WINDOW
        ]
        if near and status == "planned":
            merges.append((min(near)[1], int(row_id)))
        else:
            survivors.setdefault(str(feed), []).append((int(row_id), at))
    if merges:
        ledger.conn.executemany("UPDATE pull_plan SET merged_into=? WHERE id=?", merges)
    _absorb_merged_into_pulled(ledger)
    ledger.conn.commit()
    return len(merges)


def _absorb_merged_into_pulled(ledger: Ledger) -> None:
    """Rows merged into a survivor that is already pulled count as pulled by it."""
    ledger.conn.execute(
        "UPDATE pull_plan SET status='pulled',credits=0,"
        "pulled_at=(SELECT s.pulled_at FROM pull_plan s WHERE s.id=pull_plan.merged_into),"
        "note=COALESCE(note,'merged:'||merged_into) "
        "WHERE status='planned' AND merged_into IN "
        "(SELECT id FROM pull_plan WHERE status='pulled')"
    )


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
    share = (
        1.0
        if settings.odds_grid == "fallback"
        else min(1.0, max(0.0, 1.0 - settings.odds_entry_share))
    )
    return max(0, math.floor(budget * share + 1e-9))


def plan_credits_today(ledger: Ledger, now: datetime) -> float:
    start, end = _day_bounds(_utc(now))
    row = ledger.conn.execute(
        "SELECT COALESCE(SUM(credits),0) FROM pull_plan WHERE pulled_at>=? AND pulled_at<?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return float(row[0])


def _due_rows(
    ledger: Ledger, now: datetime, feed: str | None = None, include_merged: bool = False
) -> list[tuple[int, str, int, str]]:
    stamp = _utc(now).isoformat()
    sql = (
        "SELECT id,feed,priority,window_end FROM pull_plan WHERE status='planned' "
        "AND window_start<=? AND window_end>=?"
        + ("" if include_merged else " AND merged_into IS NULL")
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
        "AND merged_into IS NULL AND window_start>? AND window_start<?",
        (priority, now.isoformat(), end.isoformat()),
    ):
        at = _utc(str(window_start))
        buckets.add((str(feed), int(at.timestamp() // GROUP_BUCKET.total_seconds())))
    return len(buckets)


def month_credits_left(ledger: Ledger, now: datetime) -> int | None:
    """The latest Odds API balance a run recorded this UTC month, if any."""
    now = _utc(now)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    row = ledger.conn.execute(
        "SELECT odds_remaining FROM runs WHERE odds_remaining IS NOT NULL AND ran_at>=? "
        "ORDER BY ran_at DESC,id DESC LIMIT 1",
        (month_start.isoformat(),),
    ).fetchone()
    return int(row[0]) if row is not None else None


def move_reserve_left(ledger: Ledger, settings: Settings, now: datetime) -> int:
    """Credits still held back this month for move-trigger pulls."""
    return max(0, settings.odds_move_reserve - ledger.odds_triggers_pulled_in_month(now))


def plan_floor(ledger: Ledger, settings: Settings, now: datetime) -> int:
    """Plan pulls stop when the month's balance would drop below this."""
    return PLAN_HARD_RESERVE + move_reserve_left(ledger, settings, now)


def _entry_pulls_today(
    ledger: Ledger, calendar: ReleaseCalendar, feed: str, entry: str, now: datetime
) -> int:
    day = calendar.local_date(now)
    start = calendar.local_at(day, time(0))
    end = calendar.local_at(day + timedelta(days=1), time(0))
    row = ledger.conn.execute(
        "SELECT COUNT(DISTINCT pulled_at) FROM pull_plan WHERE feed=? AND entry=? "
        "AND status='pulled' AND credits>0 AND pulled_at>=? AND pulled_at<?",
        (feed, entry, start.isoformat(), end.isoformat()),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def soccer_feeds_allowed(
    ledger: Ledger, calendar: ReleaseCalendar, now: datetime
) -> set[str] | None:
    """On a local Saturday/Sunday, the soccer feeds the plan may fund (None = no cap).

    The cap keeps the feeds with the most games kicking off that local day (ties by
    feed name); feeds already funded today stay allowed.
    """
    cap = calendar.soccer_weekend_feed_cap
    day = calendar.local_date(now)
    if cap <= 0 or day.weekday() < 5:
        return None
    start = calendar.local_at(day, time(0))
    end = calendar.local_at(day + timedelta(days=1), time(0))
    funded = {
        str(feed)
        for (feed,) in ledger.conn.execute(
            "SELECT DISTINCT feed FROM pull_plan WHERE feed LIKE 'soccer_%' AND credits>0 "
            "AND pulled_at>=? AND pulled_at<?",
            (start.isoformat(), end.isoformat()),
        )
    }
    counts = [
        (-int(n), str(feed))
        for feed, n in ledger.conn.execute(
            "SELECT feed,COUNT(DISTINCT event_ticker) FROM pull_plan WHERE feed LIKE 'soccer_%' "
            "AND event_ticker NOT LIKE 'feed:%' AND kickoff>=? AND kickoff<? GROUP BY feed",
            (start.isoformat(), end.isoformat()),
        )
    ]
    ranked = [feed for _n, feed in sorted(counts)]
    allowed = set(funded)
    for feed in ranked:
        if len(allowed) >= cap:
            break
        allowed.add(feed)
    return allowed


def select_plan_pull(
    ledger: Ledger,
    settings: Settings,
    now: datetime,
    exclude: Iterable[str] = (),
    calendar: ReleaseCalendar | None = None,
) -> PlanPick | None:
    """The funded feed to pull now for its due targets, or None.

    Due targets that cannot be funded are stamped `budget_blocked_at`, so if they
    expire unpulled they are recorded as `skipped_budget` rather than `missed`.
    With `calendar`, a row past its entry's `max_per_day` and soccer feeds over the
    weekend cap are held back too, with the reason in `note`. Plan pulls never spend
    the month's last `plan_floor` credits (hard reserve plus move reserve).
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
    held: dict[int, str] = {}
    if calendar is not None:
        allowed = soccer_feeds_allowed(ledger, calendar, now)
        for feed, rows in list(by_feed.items()):
            if allowed is not None and feed.startswith("soccer_") and feed not in allowed:
                held.update({i: "soccer_weekend_cap" for i, _p, _e in rows})
                del by_feed[feed]
                continue
            keep = []
            for row_id, priority, window_end in rows:
                entry, cap = ledger.conn.execute(
                    "SELECT entry,max_per_day FROM pull_plan WHERE id=?", (row_id,)
                ).fetchone()
                if cap is not None and _entry_pulls_today(
                    ledger, calendar, feed, str(entry), now
                ) >= int(cap):
                    held[row_id] = "max_per_day"
                else:
                    keep.append((row_id, priority, window_end))
            if keep:
                by_feed[feed] = keep
            else:
                del by_feed[feed]
    remaining = plan_daily_cap(ledger, settings, now) - plan_credits_today(ledger, now)
    left = month_credits_left(ledger, now)
    if left is not None and left - 1 < plan_floor(ledger, settings, now):
        remaining = 0
    ordered = sorted(
        by_feed.items(),
        key=lambda item: (min(p for _i, p, _e in item[1]), min(e for _i, _p, e in item[1])),
    )
    blocked: list[int] = []
    pick: PlanPick | None = None
    for feed, rows in ordered:
        priority = min(p for _i, p, _e in rows)
        if pick is None and remaining - _groups_ahead(ledger, now, priority) >= 1:
            primary = min(rows, key=lambda r: (r[1], r[2], r[0]))[0]
            source_row = ledger.conn.execute(
                "SELECT source FROM pull_plan WHERE id=?", (primary,)
            ).fetchone()
            pick = PlanPick(
                feed,
                tuple(i for i, _p, _e in rows),
                priority,
                primary_id=primary,
                source=str(source_row[0]) if source_row and source_row[0] else None,
            )
        else:
            blocked.extend(i for i, _p, _e in rows)
    blocked.extend(held)
    if blocked:
        # Rows merged into a blocked survivor are blocked with it.
        merged = [
            int(r[0])
            for r in ledger.conn.execute(
                "SELECT id FROM pull_plan WHERE status='planned' AND merged_into IN "
                f"({','.join('?' * len(blocked))})",
                blocked,
            )
        ]
        ledger.conn.executemany(
            "UPDATE pull_plan SET budget_blocked_at=COALESCE(budget_blocked_at,?),"
            "note=COALESCE(note,?) WHERE id=?",
            [(now.isoformat(), held.get(row_id), row_id) for row_id in [*blocked, *merged]],
        )
        ledger.conn.commit()
    return pick


def primary_target(ledger: Ledger, feed: str, now: datetime) -> tuple[int, str] | None:
    """(id, source) of the due row a pull of `feed` now is attributed to."""
    rows = _due_rows(ledger, now, feed)
    if not rows:
        return None
    row_id = min(rows, key=lambda r: (r[2], r[3], r[0]))[0]
    source = ledger.conn.execute("SELECT source FROM pull_plan WHERE id=?", (row_id,)).fetchone()
    return row_id, str(source[0]) if source and source[0] else "default"


def mark_pulled(
    ledger: Ledger, feed: str, now: datetime, credits: float, paid_by: str | None = None
) -> int:
    """Mark the feed's due targets pulled, sharing `credits` across them.

    `credits` is 1 for a plan-funded pull and 0 when the targets rode along on a pull
    made for another reason (rotation, trigger, measurement), named by `paid_by`.
    Rows merged into a pulled row are pulled with it.
    """
    return len(mark_pulled_ids(ledger, feed, now, credits, paid_by))


def mark_pulled_ids(
    ledger: Ledger, feed: str, now: datetime, credits: float, paid_by: str | None = None
) -> list[int]:
    rows = [row_id for row_id, *_ in _due_rows(ledger, now, feed, include_merged=True)]
    if not rows:
        return []
    merged = [
        int(r[0])
        for r in ledger.conn.execute(
            "SELECT id FROM pull_plan WHERE status='planned' AND merged_into IN "
            f"({','.join('?' * len(rows))})",
            rows,
        )
        if int(r[0]) not in rows
    ]
    ids = [*rows, *merged]
    share = credits / len(ids)
    note = f"rode_along:{paid_by}" if credits == 0 and paid_by else None
    ledger.conn.executemany(
        "UPDATE pull_plan SET status='pulled',pulled_at=?,credits=?,note=COALESCE(note,?) "
        "WHERE id=?",
        [(_utc(now).isoformat(), share, note, row_id) for row_id in ids],
    )
    ledger.conn.commit()
    return ids


def record_pull_outcome(
    ledger: Ledger, row_ids: Iterable[int], candidates: int | None, gate_candidates: int | None
) -> None:
    """Copy the pull's candidate counts (from `feed_pulls`) onto the rows it satisfied."""
    ledger.conn.executemany(
        "UPDATE pull_plan SET candidates=?,gate_candidates=? WHERE id=?",
        [(candidates, gate_candidates, row_id) for row_id in row_ids],
    )
    ledger.conn.commit()


# Why a pull of a feed ran: a plan row's source (close, kickoff, release, default, grid)
# when the plan paid for it, else the reason the run pulled it.
PULL_SOURCES = ("close", "kickoff", "release", "default", "grid", "move", "measure", "rotation")
_TAGGED_TABLES = ("signals", "fills", "shadow_decisions")


def tag_pull_rows(
    ledger: Ledger,
    feed: str,
    since: datetime,
    source: str,
    pull_plan_id: int | None,
) -> tuple[int | None, int | None]:
    """Stamp `pull_source` / `pull_plan_id` on this cycle's rows for `feed`.

    Covers the feed's `feed_pulls` row and every `signals`, `fills` and
    `shadow_decisions` row written since `since` for that feed's Kalshi series. Only
    rows not yet tagged are touched, so a row is written once and never re-tagged.
    Returns the pull's (candidates, gate_candidates) from `feed_pulls`.
    """
    stamp = _utc(since).isoformat()
    prefixes = [series for series, sport in KALSHI_SERIES_TO_SPORT.items() if sport == feed]
    ledger.conn.execute(
        "UPDATE feed_pulls SET pull_source=?,pull_plan_id=? "
        "WHERE feed=? AND created_at>=? AND pull_source IS NULL",
        (source, pull_plan_id, feed, stamp),
    )
    if prefixes:
        like = " OR ".join("ticker LIKE ?" for _ in prefixes)
        patterns = [f"{series}-%" for series in prefixes]
        for table in _TAGGED_TABLES:
            ledger.conn.execute(
                f"UPDATE {table} SET pull_source=?,pull_plan_id=? "
                f"WHERE created_at>=? AND pull_source IS NULL AND ({like})",
                (source, pull_plan_id, stamp, *patterns),
            )
    row = ledger.conn.execute(
        "SELECT candidates,gate_candidates FROM feed_pulls WHERE feed=? AND created_at>=? "
        "ORDER BY id DESC LIMIT 1",
        (feed, stamp),
    ).fetchone()
    ledger.conn.commit()
    if row is None:
        return None, None
    return (
        int(row[0]) if row[0] is not None else None,
        int(row[1]) if row[1] is not None else None,
    )
