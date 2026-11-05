"""Second reference price (v5 Phase 5.1): Polymarket mids for tracked Kalshi events.

On scanner runs, events with an open maker shadow, an open position or a recent near
miss whose game has not started are matched to a Polymarket game market. The match uses
the team aliases in `teams.py` and a kickoff within `KICKOFF_TOLERANCE`. Mids per
outcome go into `reference_quotes`. Events kicking off within `CLOSE_WINDOW` are polled
first, so a near-close sample exists for `clv_polymarket`. Nothing here trades.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from prediction_bot.clv import leg_outcome
from prediction_bot.odds import KALSHI_SERIES_TO_SPORT
from prediction_bot.pull_plan import pregame_offset
from prediction_bot.teams import team_for_reference_name
from prediction_bot.venues.polymarket import PolyEvent, PolymarketClient

if TYPE_CHECKING:
    from prediction_bot.config import Settings
    from prediction_bot.paper.ledger import Ledger

log = logging.getLogger(__name__)
SOURCE = "polymarket"
KICKOFF_TOLERANCE = timedelta(hours=3)
CLOSE_WINDOW = timedelta(minutes=40)
REFRESH = timedelta(minutes=25)
LOOKBACK = timedelta(hours=24)
DRAW_NAMES = {"draw", "tie"}


@dataclass(frozen=True)
class TrackedEvent:
    event_ticker: str
    sport: str
    kickoff: datetime
    outcomes: frozenset[str]  # team ids, plus "draw" for 3-way soccer


def _utc(value: str | datetime) -> datetime:
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def tracked_events(ledger: Ledger, now: datetime) -> list[TrackedEvent]:
    """Events worth a reference quote, closest kickoff first, upcoming games only."""
    now = _utc(now)
    stamp = now.isoformat()
    events: set[str] = set()
    for (ticker,) in ledger.conn.execute(
        "SELECT DISTINCT ticker FROM maker_shadow WHERE status='open'"
    ):
        events.add(str(ticker).rsplit("-", 1)[0])
    for ticker in ledger.open_tickers():
        events.add(str(ticker).rsplit("-", 1)[0])
    for (ticker,) in ledger.conn.execute(
        "SELECT DISTINCT ticker FROM shadow_decisions WHERE would_trade=0 AND created_at>=? "
        "AND COALESCE(commence_time,commence_time_backfill)>?",
        ((now - LOOKBACK).isoformat(), stamp),
    ):
        events.add(str(ticker).rsplit("-", 1)[0])
    out = []
    for event in sorted(events):
        sport = KALSHI_SERIES_TO_SPORT.get(event.split("-", 1)[0])
        if sport is None:
            continue
        legs = [
            str(row[0])
            for row in ledger.conn.execute(
                "SELECT DISTINCT ticker FROM market_snapshots WHERE event_ticker=?", (event,)
            )
        ]
        outcomes = frozenset(o for leg in legs if (o := leg_outcome(leg)) is not None)
        kickoff = _event_kickoff(ledger, event, sport)
        if len({o for o in outcomes if o != "draw"}) != 2 or kickoff is None or kickoff <= now:
            continue
        out.append(TrackedEvent(event, sport, kickoff, outcomes))
    return sorted(out, key=lambda e: (e.kickoff, e.event_ticker))


def _event_kickoff(ledger: Ledger, event: str, sport: str) -> datetime | None:
    row = ledger.conn.execute(
        "SELECT MAX(commence_time) FROM odds_snapshots WHERE event_ticker=?", (event,)
    ).fetchone()
    if row and row[0]:
        try:
            return _utc(str(row[0]))
        except ValueError:
            pass
    row = ledger.conn.execute(
        "SELECT MAX(expected_expiration) FROM market_snapshots WHERE event_ticker=?", (event,)
    ).fetchone()
    if row and row[0]:
        try:
            return _utc(str(row[0])) - pregame_offset(sport)
        except ValueError:
            return None
    return None


def _teams_in_title(sport: str, title: str) -> set[str]:
    for separator in (" vs. ", " vs ", " @ ", " v "):
        if separator in title:
            names = [part.strip() for part in title.split(separator, 1)]
            return {t for name in names if (t := team_for_reference_name(sport, name))}
    return set()


def match_tokens(
    event: TrackedEvent, candidates: Sequence[PolyEvent]
) -> tuple[dict[str, str], list[str]]:
    """outcome id -> CLOB token whose price is P(outcome), plus unmapped names seen.

    Two-outcome markets whose outcomes are both teams give one token per team.
    Yes/No markets about one team or the draw give that outcome's YES token. A match
    needs both teams and a start within `KICKOFF_TOLERANCE` of the Kalshi kickoff.
    """
    teams = {o for o in event.outcomes if o != "draw"}
    unknown: list[str] = []
    for poly in candidates:
        start = poly.start or next((m.start for m in poly.markets if m.start), None)
        if start is None or abs(start - event.kickoff) > KICKOFF_TOLERANCE:
            continue
        tokens: dict[str, str] = {}
        title_teams = _teams_in_title(event.sport, poly.title)
        for market in poly.markets:
            names = [o.strip() for o in market.outcomes]
            if len(names) == 2 and {n.casefold() for n in names} != {"yes", "no"}:
                mapped = [team_for_reference_name(event.sport, n) for n in names]
                unknown.extend(n for n, t in zip(names, mapped, strict=True) if t is None)
                if set(mapped) == teams:
                    tokens.update(
                        {t: tok for t, tok in zip(mapped, market.token_ids, strict=True) if t}
                    )
            elif [n.casefold() for n in names] == ["yes", "no"]:
                subject = market.subject or market.question
                outcome = (
                    "draw"
                    if any(word in subject.casefold().split() for word in DRAW_NAMES)
                    else team_for_reference_name(event.sport, subject)
                )
                if outcome is None:
                    unknown.append(subject)
                elif outcome in event.outcomes and title_teams == teams:
                    tokens[outcome] = market.token_ids[0]
        if tokens and (teams <= set(tokens) or set(tokens) == set(event.outcomes)):
            return tokens, unknown
    return {}, unknown


def poll_reference_quotes(
    ledger: Ledger,
    client: PolymarketClient,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> int:
    """Store Polymarket mids for tracked events; returns quotes written."""
    now = _utc(now or datetime.now(timezone.utc))
    cap = settings.polymarket_max_requests
    start_requests = client.requests
    recent = {
        str(r[0])
        for r in ledger.conn.execute(
            "SELECT DISTINCT event_ticker FROM reference_quotes WHERE source=? AND fetched_at>=?",
            (SOURCE, (now - REFRESH).isoformat()),
        )
    }
    events = [
        e
        for e in tracked_events(ledger, now)
        if e.kickoff - now <= CLOSE_WINDOW or e.event_ticker not in recent
    ]
    by_sport: dict[str, list[PolyEvent]] = {}
    written = 0
    for event in events:
        if client.requests - start_requests >= cap:
            break
        tag = settings.polymarket_tags.get(event.sport)
        if tag is None:
            continue
        if event.sport not in by_sport:
            try:
                by_sport[event.sport] = client.events(tag)
            except Exception:
                log.exception("Polymarket discovery failed for %s", event.sport)
                by_sport[event.sport] = []
        tokens, unknown = match_tokens(event, by_sport[event.sport])
        if not tokens:
            ledger.record_reference_unmatched(
                event.event_ticker, SOURCE, "no_market", ",".join(sorted(set(unknown))), now
            )
            continue
        for outcome, token in sorted(tokens.items()):
            if client.requests - start_requests >= cap:
                break
            try:
                bid, ask = client.book(token)
            except Exception:
                log.exception("Polymarket book failed for %s", token)
                continue
            if bid is None or ask is None or bid > ask:
                continue
            ledger.record_reference_quote(
                event.event_ticker, outcome, SOURCE, (bid + ask) / 2, bid, ask, token, now
            )
            written += 1
    return written


def side_reference_mid(rows: Mapping[str, Decimal], outcome: str, side: str) -> Decimal | None:
    """The side's probability from outcome mids: P(outcome) for YES, 1 - P for NO."""
    mid = rows.get(outcome)
    if mid is None:
        return None
    return mid if side == "yes" else Decimal(1) - mid
