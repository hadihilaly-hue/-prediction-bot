"""One source of truth for kickoff times.

`event_kickoffs` holds one row per Kalshi event: the latest known kickoff, where it came
from and what it was before it last moved. Every reader that needs a kickoff (the pull
plan, CLV scoring, reference quotes, the odds planner) asks `resolve()` instead of
reading the kickoff stored on its own rows, which is frozen at decision time.

Sources, best first:
- 'odds' (a paid Odds API pull) and 'odds_events' (the free /events listing) rank equal;
  the most recently observed one wins.
- 'kalshi_expiry' (Kalshi `expected_expiration` minus `expiry_offset`) is an estimate. It
  fills events with no odds-based kickoff and never overwrites one.

Only pre-game observations count: the Odds API rewrites `commence_time` to the actual
start once a game is live, so an observation made at or after its own kickoff is ignored.
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from prediction_bot.odds import KALSHI_SERIES_TO_SPORT

if TYPE_CHECKING:
    from prediction_bot.config import Settings
    from prediction_bot.odds import OddsClient

log = logging.getLogger(__name__)

ODDS_SOURCES = ("odds", "odds_events")
KALSHI_EXPIRY = "kalshi_expiry"
SOURCES = (*ODDS_SOURCES, KALSHI_EXPIRY)
CHANGE_TOLERANCE = timedelta(seconds=60)
# Kalshi expected_expiration minus kickoff when a series has too few linked events.
OFFSET_LOOKBACK = timedelta(days=30)
OFFSET_MIN_EVENTS = 5
_STATIC_OFFSETS = {
    "soccer_usa_mls": timedelta(hours=2, minutes=50),
    "basketball_nba": timedelta(hours=2, minutes=50),
    "icehockey_nhl": timedelta(hours=2, minutes=50),
}
# Free /events refresh (Part 1.2b).
FREE_REFRESH_EVERY = timedelta(hours=3)
FREE_REFRESH_AHEAD = timedelta(days=10)
# Re-anchoring rows that are not yet scored / flagging scored ones.
REANCHOR_TOLERANCE = timedelta(minutes=5)
MISMATCH_TOLERANCE = timedelta(minutes=30)

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_kickoffs (
    event_ticker TEXT PRIMARY KEY,
    kickoff TEXT NOT NULL,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    odds_event_id TEXT,
    prev_kickoff TEXT,
    changed_at TEXT,
    kalshi_expiration TEXT
);
CREATE TABLE IF NOT EXISTS kickoff_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


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


def _parse(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return _utc(str(value))
    except ValueError:
        return None


# The latest pre-kickoff Kalshi expected_expiration of an event, from market_snapshots.
_LATEST_EXPIRATION = (
    "SELECT m.expected_expiration FROM market_snapshots m "
    "WHERE m.event_ticker=event_kickoffs.event_ticker AND m.expected_expiration IS NOT NULL "
    "AND m.fetched_at<event_kickoffs.kickoff ORDER BY m.fetched_at DESC LIMIT 1"
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {str(r[1]) for r in conn.execute("PRAGMA table_info(event_kickoffs)")}
    if "kalshi_expiration" not in columns:
        conn.execute("ALTER TABLE event_kickoffs ADD COLUMN kalshi_expiration TEXT")
    # Kept on the event row because the daily archive removes market_snapshots of
    # closed games after 7 days, and the expiry offset looks back 30.
    conn.execute(
        f"UPDATE event_kickoffs SET kalshi_expiration=({_LATEST_EXPIRATION}) "
        "WHERE kalshi_expiration IS NULL"
    )
    conn.commit()


def _state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kickoff_state WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kickoff_state (key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def observe(
    conn: sqlite3.Connection,
    event_ticker: str,
    kickoff: datetime | str,
    source: str,
    observed_at: datetime | str,
    odds_event_id: str | None = None,
) -> bool:
    """Record one kickoff observation. Returns True when the stored row changed.

    Does not commit; callers batch observations and commit once.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown kickoff source {source!r}")
    new = _parse(kickoff)
    seen = _parse(observed_at)
    if new is None or seen is None or seen >= new:
        return False  # live (or nonsense) observations never count
    row = conn.execute(
        "SELECT kickoff,source,observed_at,odds_event_id FROM event_kickoffs WHERE event_ticker=?",
        (event_ticker,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO event_kickoffs (event_ticker,kickoff,source,observed_at,odds_event_id) "
            "VALUES (?,?,?,?,?)",
            (event_ticker, new.isoformat(), source, seen.isoformat(), odds_event_id),
        )
        return True
    old_kickoff, old_source, old_seen_text, old_event_id = row
    old = _parse(old_kickoff)
    old_seen = _parse(old_seen_text)
    if source == KALSHI_EXPIRY and old_source in ODDS_SOURCES:
        return False
    if (
        (old_source in ODDS_SOURCES or source == KALSHI_EXPIRY)
        and old_seen is not None
        and seen < old_seen
    ):
        return False  # an older observation of an equal-rank source
    moved = old is None or abs(new - old) > CHANGE_TOLERANCE
    if moved:
        conn.execute(
            "UPDATE event_kickoffs SET kickoff=?,source=?,observed_at=?,odds_event_id=?,"
            "prev_kickoff=?,changed_at=? WHERE event_ticker=?",
            (
                new.isoformat(),
                source,
                seen.isoformat(),
                odds_event_id or old_event_id,
                old_kickoff,
                seen.isoformat(),
                event_ticker,
            ),
        )
    else:
        conn.execute(
            "UPDATE event_kickoffs SET source=?,observed_at=?,odds_event_id=? WHERE event_ticker=?",
            (source, seen.isoformat(), odds_event_id or old_event_id, event_ticker),
        )
    return True


def resolve(conn: sqlite3.Connection, event_ticker: str) -> tuple[datetime, str] | None:
    """(kickoff, source) for an event, or None when nothing is known."""
    row = conn.execute(
        "SELECT kickoff,source FROM event_kickoffs WHERE event_ticker=?", (event_ticker,)
    ).fetchone()
    if row is None:
        return None
    kickoff = _parse(row[0])
    return None if kickoff is None else (kickoff, str(row[1]))


def resolve_odds(conn: sqlite3.Connection, event_ticker: str) -> datetime | None:
    """The kickoff only when it comes from the books (never a Kalshi estimate)."""
    found = resolve(conn, event_ticker)
    return found[0] if found is not None and found[1] in ODDS_SOURCES else None


def sync_snapshots(conn: sqlite3.Connection) -> int:
    """Observe every linked `odds_snapshots` row not seen yet, oldest first.

    Catches rows written by any path (including the one-time backfill of the existing
    ledger) without relying on each writer to call `observe`. Returns rows processed.
    """
    last = int(_state(conn, "snapshots_synced_id") or 0)
    rows = conn.execute(
        "SELECT MAX(id),event_ticker,commence_time,created_at,event_id FROM odds_snapshots "
        "WHERE id>? AND event_ticker IS NOT NULL "
        "GROUP BY event_ticker,created_at ORDER BY created_at,MAX(id)",
        (last,),
    ).fetchall()
    top = conn.execute("SELECT MAX(id) FROM odds_snapshots").fetchone()[0]
    for _id, event_ticker, commence, created, event_id in rows:
        observe(conn, str(event_ticker), str(commence), "odds", str(created), str(event_id))
    if top is not None:
        _set_state(conn, "snapshots_synced_id", str(int(top)))
    conn.commit()
    return len(rows)


def _static_offset(feed: str | None) -> timedelta:
    if feed is not None and feed in _STATIC_OFFSETS:
        return _STATIC_OFFSETS[feed]
    return timedelta(hours=3)


def expiry_offsets(
    conn: sqlite3.Connection, series: Iterable[str], now: datetime
) -> dict[str, timedelta]:
    """Kalshi `expected_expiration` minus kickoff per series.

    The median over the series' events with an odds-based kickoff in the last 30 days
    when there are at least 5 of them, else the static table (soccer 3h; MLS, NBA and
    NHL 2h50m; everything else 3h).
    """
    now = _utc(now)
    wanted = sorted(set(series))
    out = {s: _static_offset(KALSHI_SERIES_TO_SPORT.get(s)) for s in wanted}
    if not wanted:
        return out
    samples: dict[str, list[float]] = {}
    rows = conn.execute(
        "SELECT event_ticker,kickoff,COALESCE(kalshi_expiration,"
        f"({_LATEST_EXPIRATION})) FROM event_kickoffs "
        "WHERE source IN ('odds','odds_events') AND kickoff>=? AND kickoff<=?",
        ((now - OFFSET_LOOKBACK).isoformat(), now.isoformat()),
    ).fetchall()
    for event_ticker, kickoff, expiration in rows:
        series_ticker = str(event_ticker).split("-", 1)[0]
        if series_ticker not in out:
            continue
        start, end = _parse(kickoff), _parse(expiration)
        if start is None or end is None:
            continue
        samples.setdefault(series_ticker, []).append((end - start).total_seconds())
    for series_ticker, values in samples.items():
        if len(values) >= OFFSET_MIN_EVENTS:
            out[series_ticker] = timedelta(seconds=statistics.median(values))
    return out


def expiry_offset(conn: sqlite3.Connection, series: str, now: datetime) -> timedelta:
    return expiry_offsets(conn, [series], now)[series]


def observe_kalshi_expiry(
    conn: sqlite3.Connection,
    expirations: Mapping[str, tuple[datetime | str, datetime | str]],
    now: datetime,
) -> None:
    """Estimate kickoffs for events with no odds-based kickoff, and keep every event's
    latest pre-kickoff expiration for `expiry_offsets`.

    `expirations` maps event_ticker -> (expected_expiration, fetched_at).
    """
    pending = {
        event: value for event, value in expirations.items() if resolve_odds(conn, event) is None
    }
    offsets = expiry_offsets(conn, {e.split("-", 1)[0] for e in pending}, now)
    for event, (expiration, fetched_at) in pending.items():
        end = _parse(expiration)
        if end is None:
            continue
        observe(conn, event, end - offsets[event.split("-", 1)[0]], KALSHI_EXPIRY, fetched_at)
    for event, (expiration, fetched_at) in expirations.items():
        end, seen = _parse(expiration), _parse(fetched_at)
        if end is None or seen is None:
            continue
        # Only expirations Kalshi listed before the game, as the offset measures.
        conn.execute(
            "UPDATE event_kickoffs SET kalshi_expiration=? WHERE event_ticker=? AND kickoff>?",
            (end.isoformat(), event, seen.isoformat()),
        )
    conn.commit()


# --- free /events refresh (1.2b) ----------------------------------------------------


def refresh_free(conn: sqlite3.Connection, odds: OddsClient, now: datetime) -> dict[str, int]:
    """Re-read kickoffs from the free /events listing, at most once per 3h per sport.

    Sports considered: those with a pending CLV row or a planned pull_plan target in the
    next 10 days. Only events whose Odds API id is already linked to a Kalshi event in
    `odds_snapshots` are observed. Returns sport -> events observed. Spends no credits:
    `OddsClient.events` disables itself if the call ever moves `x-requests-used`.
    """
    now = _utc(now)
    if not getattr(odds, "events_free", False):
        return {}
    links: dict[str, dict[str, str]] = {}
    for feed, event_id, event_ticker in conn.execute(
        "SELECT DISTINCT feed,event_id,event_ticker FROM odds_snapshots "
        "WHERE event_ticker IS NOT NULL"
    ):
        links.setdefault(str(feed), {})[str(event_id)] = str(event_ticker)
    sports = _sports_to_refresh(conn, now)
    out: dict[str, int] = {}
    for sport in sorted(sports):
        if sport not in links:
            continue
        last = _parse(_state(conn, f"free_refresh:{sport}"))
        if last is not None and now - last < FREE_REFRESH_EVERY:
            continue
        if odds.requests_used is None:
            try:
                odds.quota_remaining()  # free /sports call: sets the quota baseline
            except Exception as exc:
                log.warning("free kickoff refresh: quota read failed: %s", exc)
                return out
        try:
            odds._events_requests_used_baseline = int(str(odds.requests_used))
        except ValueError:
            log.warning("free kickoff refresh: unreadable quota; skipped")
            return out
        try:
            events = odds.events(sport, now, now + FREE_REFRESH_AHEAD)
        except Exception as exc:  # network errors never block the cycle
            log.warning("free kickoff refresh failed for %s: %s", sport, exc)
            continue
        if not odds.events_free:
            log.warning("odds api /events is not free; kickoff refresh disabled")
            break
        seen = 0
        for event in events:
            event_ticker = links[sport].get(str(event.get("id", "")))
            commence = event.get("commence_time")
            if event_ticker is None or not commence:
                continue
            observe(conn, event_ticker, str(commence), "odds_events", now, str(event["id"]))
            seen += 1
        _set_state(conn, f"free_refresh:{sport}", now.isoformat())
        conn.commit()
        out[sport] = seen
    return out


def _sports_to_refresh(conn: sqlite3.Connection, now: datetime) -> set[str]:
    sports: set[str] = {
        str(feed)
        for (feed,) in conn.execute(
            "SELECT DISTINCT feed FROM pull_plan WHERE status='planned' "
            "AND target_at>=? AND target_at<=?",
            (now.isoformat(), (now + FREE_REFRESH_AHEAD).isoformat()),
        )
    }
    tickers = [
        str(t)
        for (t,) in conn.execute(
            "SELECT ticker FROM fills WHERE clv_kalshi IS NULL "
            "UNION SELECT ticker FROM shadow_decisions WHERE clv_kalshi IS NULL "
            "AND COALESCE(clv_status,'pending')='pending' "
            "UNION SELECT ticker FROM maker_shadow WHERE clv_kalshi IS NULL"
        )
    ]
    for ticker in tickers:
        sport = KALSHI_SERIES_TO_SPORT.get(ticker.split("-", 1)[0])
        if sport is not None:
            sports.add(sport)
    return sports


# --- re-anchoring rows (1.3) --------------------------------------------------------

_ROW_SOURCES: dict[str, str] = {
    "fills": "SELECT id,ticker,COALESCE(event_ticker,''),"
    "COALESCE(commence_time,commence_time_backfill),clv_kalshi,clv_status FROM fills",
    "shadow_decisions": "SELECT id,ticker,'',COALESCE(commence_time,commence_time_backfill),"
    "clv_kalshi,clv_status FROM shadow_decisions",
    "maker_shadow": "SELECT id,ticker,event_ticker,commence_time,clv_kalshi,clv_status "
    "FROM maker_shadow",
}


def _event_of(ticker: str, event_ticker: str) -> str:
    return event_ticker or ticker.rsplit("-", 1)[0]


def kickoff_rows(
    conn: sqlite3.Connection, tolerance: timedelta = REANCHOR_TOLERANCE
) -> list[dict[str, Any]]:
    """Rows whose stored kickoff differs from the odds-resolved one by more than
    `tolerance`. Read-only."""
    resolved = {
        str(e): _parse(k)
        for e, k in conn.execute(
            "SELECT event_ticker,kickoff FROM event_kickoffs WHERE source IN ('odds','odds_events')"
        )
    }
    out: list[dict[str, Any]] = []
    for table, sql in _ROW_SOURCES.items():
        for row_id, ticker, event_ticker, stored, clv, status in conn.execute(sql):
            event = _event_of(str(ticker), str(event_ticker or ""))
            kickoff = resolved.get(event)
            start = _parse(stored)
            if kickoff is None or start is None or abs(kickoff - start) <= tolerance:
                continue
            out.append(
                {
                    "table": table,
                    "id": int(row_id),
                    "ticker": str(ticker),
                    "event_ticker": event,
                    "stored": start,
                    "resolved": kickoff,
                    "scored": clv is not None,
                    "status": status,
                }
            )
    for row_id, event, _feed, target, stored in conn.execute(
        "SELECT id,event_ticker,feed,target,kickoff FROM pull_plan WHERE status='planned' "
        "AND event_ticker NOT LIKE 'feed:%'"
    ):
        kickoff = resolved.get(str(event))
        start = _parse(stored)
        if kickoff is None or start is None or abs(kickoff - start) <= tolerance:
            continue
        out.append(
            {
                "table": "pull_plan",
                "id": int(row_id),
                "ticker": f"{event} {target}",
                "event_ticker": str(event),
                "stored": start,
                "resolved": kickoff,
                "scored": False,
                "status": "planned",
            }
        )
    return out


def _sport_buffer(settings: Settings | None, ticker: str) -> timedelta:
    if settings is None:
        return timedelta(minutes=15)
    sport = KALSHI_SERIES_TO_SPORT.get(ticker.split("-", 1)[0], "")
    return timedelta(
        minutes=settings.kickoff_buffer_minutes.get(sport, settings.kickoff_buffer_default_minutes)
    )


def reanchor(conn: sqlite3.Connection, settings: Settings | None = None) -> dict[str, int]:
    """Move unscored rows onto the resolved kickoff; flag scored rows that were off.

    - Not yet scored (clv_kalshi IS NULL), more than 5 minutes off an odds-based kickoff:
      the old value goes to `commence_time_decision` (first move only), `commence_time`
      takes the resolved one, `clv_status` is cleared so the next refresh recomputes it,
      and the ticker's CLV fetch attempts restart. An open maker shadow whose
      `expires_at` was cut at the old kickoff is cut at the new one.
    - Scored 'ok' more than 30 minutes off: `clv_status='kickoff_mismatch'`, numbers kept.
    Kalshi estimates never move a row. Returns counts per action.
    """
    counts = {"reanchored": 0, "mismatch": 0, "expiry_moved": 0}
    tickers: set[str] = set()
    for row in kickoff_rows(conn, REANCHOR_TOLERANCE):
        table = row["table"]
        if table == "pull_plan":
            continue
        new: datetime = row["resolved"]
        old: datetime = row["stored"]
        if row["scored"]:
            if row["status"] == "ok" and abs(new - old) > MISMATCH_TOLERANCE:
                conn.execute(
                    f"UPDATE {table} SET clv_status='kickoff_mismatch' WHERE id=?", (row["id"],)
                )
                counts["mismatch"] += 1
            continue
        conn.execute(
            f"UPDATE {table} SET commence_time_decision=COALESCE(commence_time_decision,?),"
            "commence_time=?,clv_status=NULL WHERE id=?",
            (old.isoformat(), new.isoformat(), row["id"]),
        )
        counts["reanchored"] += 1
        tickers.add(row["ticker"])
        if table == "maker_shadow":
            counts["expiry_moved"] += _move_maker_expiry(conn, row["id"], old, new, settings)
    for ticker in tickers:
        conn.execute("DELETE FROM clv_fetch_attempts WHERE ticker=?", (ticker,))
    conn.commit()
    return counts


def _move_maker_expiry(
    conn: sqlite3.Connection,
    row_id: int,
    old: datetime,
    new: datetime,
    settings: Settings | None,
) -> int:
    found = conn.execute(
        "SELECT ticker,created_at,expires_at,status,COALESCE(rules_version,'v2') "
        "FROM maker_shadow WHERE id=?",
        (row_id,),
    ).fetchone()
    if found is None:
        return 0
    ticker, created, expires, status, version = found
    expires_at, created_at = _parse(expires), _parse(created)
    if status != "open" or expires_at is None or created_at is None:
        return 0
    buffer = _sport_buffer(settings, str(ticker))
    if abs(expires_at - (old - buffer)) > timedelta(seconds=1):
        return 0  # the TTL cut it, not the kickoff
    moved = new - buffer
    if version == "v3" and settings is not None:
        moved = min(moved, created_at + timedelta(minutes=settings.v3_maker_ttl_minutes))
    conn.execute("UPDATE maker_shadow SET expires_at=? WHERE id=?", (moved.isoformat(), row_id))
    return 1
