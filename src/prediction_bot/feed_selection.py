from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from prediction_bot.config import Settings
from prediction_bot.odds_planner import PlannedGame

if TYPE_CHECKING:
    import sqlite3

    from prediction_bot.paper.ledger import Ledger

SEED_LEAD_HOURS: dict[str, int] = {
    "americanfootball_nfl": 240,
    "americanfootball_ncaaf": 168,
    "mma_mixed_martial_arts": 240,
    "basketball_nba": 36,
    "icehockey_nhl": 30,
    "baseball_mlb": 24,
}
DEFAULT_LEAD_HOURS = 48
MIN_EVENTS_FOR_PERCENTILES = 5
ENTRY_LOOKAHEAD = timedelta(days=7)
ZERO_PULL_DEMOTION_AGE = timedelta(hours=48)


@dataclass(frozen=True)
class FeedStat:
    feed: str
    lead_p50_h: float
    lead_p90_h: float
    pulls_14d: int
    useful_pulls_14d: int
    provisional: bool


@dataclass(frozen=True)
class FeedScore:
    weighted_game_count: Decimal
    soonest_kickoff: datetime | None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: str | datetime) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def _seed_lead_hours(feed: str) -> int:
    feed = feed.removeprefix("outrights:")
    if feed.startswith("soccer_"):
        return 168
    if feed.startswith("golf_"):
        return 400
    return SEED_LEAD_HOURS.get(feed, DEFAULT_LEAD_HOURS)


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    """Nearest-rank percentiles: sorted value at ceil(p*n), using one-based ranks."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def compute_feed_stats_from_connection(
    conn: sqlite3.Connection, feeds: Iterable[str], now: datetime
) -> dict[str, FeedStat]:
    feed_names = sorted({feed.removeprefix("outrights:") for feed in feeds})
    if not feed_names:
        return {}
    now_utc = _utc(now)
    cutoff = now_utc - timedelta(days=14)
    source_feeds = sorted({*feed_names, *(f"outrights:{feed}" for feed in feed_names)})
    placeholders = ",".join("?" for _ in source_feeds)
    observations = conn.execute(
        "SELECT feed,event_id,created_at,commence_time FROM odds_snapshots "
        f"WHERE feed IN ({placeholders})",
        source_feeds,
    )
    event_leads: dict[str, dict[str, float]] = {feed: {} for feed in feed_names}
    for feed_value, event_value, created_value, commence_value in observations:
        feed = str(feed_value).removeprefix("outrights:")
        created_at = _timestamp(str(created_value))
        commence_time = _timestamp(str(commence_value))
        if (
            feed not in event_leads
            or created_at is None
            or commence_time is None
            or not cutoff <= created_at <= now_utc
        ):
            continue
        event_id = str(event_value)
        lead = (commence_time - created_at).total_seconds() / 3600
        event_leads[feed][event_id] = max(event_leads[feed].get(event_id, lead), lead)

    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feed_pulls'"
    ).fetchone()
    pull_counts = {feed: 0 for feed in feed_names}
    useful_counts = {feed: 0 for feed in feed_names}
    if table_exists:
        pulls = conn.execute(
            f"SELECT feed,created_at,candidates FROM feed_pulls WHERE feed IN ({placeholders})",
            source_feeds,
        )
        for feed_value, created_value, candidates_value in pulls:
            feed = str(feed_value).removeprefix("outrights:")
            created_at = _timestamp(str(created_value))
            if feed not in pull_counts or created_at is None or not cutoff <= created_at <= now_utc:
                continue
            pull_counts[feed] += 1
            useful_counts[feed] += int(candidates_value) > 0

    stats: dict[str, FeedStat] = {}
    for feed in feed_names:
        values = list(event_leads[feed].values())
        provisional = len(event_leads[feed]) < MIN_EVENTS_FOR_PERCENTILES
        if provisional:
            lead_p50 = lead_p90 = float(_seed_lead_hours(feed))
        else:
            lead_p50 = _nearest_rank(values, 0.5)
            lead_p90 = _nearest_rank(values, 0.9)
        stats[feed] = FeedStat(
            feed=feed,
            lead_p50_h=lead_p50,
            lead_p90_h=lead_p90,
            pulls_14d=pull_counts[feed],
            useful_pulls_14d=useful_counts[feed],
            provisional=provisional,
        )
    return stats


def compute_feed_stats(ledger: Ledger, feeds: Iterable[str], now: datetime) -> dict[str, FeedStat]:
    """Compute 14-day lead percentiles and pull counts for the requested feeds."""
    return compute_feed_stats_from_connection(ledger.conn, feeds, now)


def eligible(stat: FeedStat, settings: Settings) -> bool:
    return stat.lead_p90_h >= settings.min_lead_hours + 6 and (
        settings.rules_version == "v1" or stat.feed not in settings.v2_disabled_sports
    )


def liquidity_weights(
    ledger: Ledger,
    games_by_sport: Mapping[str, Sequence[PlannedGame]],
    settings: Settings,
) -> dict[str, Decimal]:
    """Return one liquidity weight per event using its latest market snapshots."""
    event_tickers = sorted(
        {
            game.event_ticker
            for games in games_by_sport.values()
            for game in games
            if game.event_ticker
        }
    )
    weights: dict[str, Decimal] = {}
    for event_ticker in event_tickers:
        rows = ledger.conn.execute(
            "SELECT ticker,yes_bid,yes_ask,yes_ask_size FROM market_snapshots "
            "WHERE event_ticker=? ORDER BY fetched_at DESC,id DESC",
            (event_ticker,),
        )
        seen_tickers: set[str] = set()
        tightest: tuple[Decimal, Decimal] | None = None
        for ticker_value, bid_value, ask_value, ask_size_value in rows:
            ticker = str(ticker_value)
            if ticker in seen_tickers:
                continue
            seen_tickers.add(ticker)
            if bid_value is None or ask_value is None:
                continue
            spread = Decimal(str(ask_value)) - Decimal(str(bid_value))
            ask_size = Decimal(str(ask_size_value))
            if tightest is None or spread < tightest[0]:
                tightest = spread, ask_size
        weights[event_ticker] = (
            Decimal("1")
            if tightest is not None
            and tightest[0] <= settings.v2_max_spread
            and tightest[1] >= settings.v3_min_ask_depth
            else Decimal("0.25")
        )
    return weights


def feed_scores(
    games_by_sport: Mapping[str, Sequence[PlannedGame]],
    weights: Mapping[str, Decimal | float],
    settings: Settings,
    now: datetime,
) -> dict[str, FeedScore]:
    now_utc = _utc(now)
    earliest = now_utc + timedelta(hours=settings.min_lead_hours)
    latest = now_utc + ENTRY_LOOKAHEAD
    scores: dict[str, FeedScore] = {}
    for feed, games in games_by_sport.items():
        qualifying = [
            (_utc(game.kickoff), Decimal(str(weights.get(game.event_ticker, Decimal("0.25")))))
            for game in games
            if earliest <= _utc(game.kickoff) <= latest
        ]
        scores[feed] = FeedScore(
            weighted_game_count=sum((weight for _, weight in qualifying), Decimal(0)),
            soonest_kickoff=min((kickoff for kickoff, _ in qualifying), default=None),
        )
    return scores


def learning_rule_demoted(ledger: Ledger, feed: str, now: datetime) -> bool:
    rows = ledger.conn.execute(
        "SELECT created_at,candidates FROM feed_pulls WHERE feed=? "
        "ORDER BY created_at DESC,id DESC LIMIT 3",
        (feed,),
    ).fetchall()
    if len(rows) != 3 or any(int(candidates) != 0 for _created_at, candidates in rows):
        return False
    most_recent = _timestamp(str(rows[0][0]))
    if most_recent is None:
        return False
    age = _utc(now) - most_recent
    return timedelta(0) <= age <= ZERO_PULL_DEMOTION_AGE


def select_entry_feeds(
    games_by_sport: Mapping[str, Sequence[PlannedGame]],
    stats: Mapping[str, FeedStat],
    weights: Mapping[str, Decimal | float],
    settings: Settings,
    ledger: Ledger,
    now: datetime,
) -> list[str]:
    scores = feed_scores(games_by_sport, weights, settings, now)
    candidates = [
        feed
        for feed, stat in stats.items()
        if feed in scores and eligible(stat, settings) and scores[feed].weighted_game_count > 0
    ]
    demoted = {feed for feed in candidates if learning_rule_demoted(ledger, feed, now)}
    return sorted(
        candidates,
        key=lambda feed: (
            feed in demoted,
            -scores[feed].weighted_game_count,
            scores[feed].soonest_kickoff,
            feed,
        ),
    )
