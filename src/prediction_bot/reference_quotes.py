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

from prediction_bot import kickoffs
from prediction_bot.clv import leg_outcome
from prediction_bot.odds import KALSHI_SERIES_TO_SPORT
from prediction_bot.teams import team_for_reference_name
from prediction_bot.venues.polymarket import PolyEvent, PolyMarket, PolymarketClient

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
MONEYLINE = "moneyline"
# A Polymarket book wider than this, or a mid this far from Kalshi's mid for the same
# outcome at the same time, is not a usable reference (spread/puck-line markets read
# 13-31c off Kalshi's moneyline).
MAX_BOOK_SPREAD = Decimal("0.05")
MAX_KALSHI_GAP = Decimal("0.10")
KALSHI_MID_MAX_AGE = timedelta(minutes=60)


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
    """`kickoffs.resolve`, else the latest Kalshi expiration minus the series offset."""
    found = kickoffs.resolve(ledger.conn, event)
    if found is not None:
        return found[0]
    row = ledger.conn.execute(
        "SELECT expected_expiration,MAX(fetched_at) FROM market_snapshots "
        "WHERE event_ticker=? AND expected_expiration IS NOT NULL",
        (event,),
    ).fetchone()
    if row and row[0]:
        try:
            return _utc(str(row[0])) - kickoffs.expiry_offset(
                ledger.conn, event.split("-", 1)[0], datetime.now(timezone.utc)
            )
        except ValueError:
            return None
    return None


def _teams_in_title(sport: str, title: str) -> set[str]:
    for separator in (" vs. ", " vs ", " @ ", " v "):
        if separator in title:
            names = [part.strip() for part in title.split(separator, 1)]
            return {t for name in names if (t := team_for_reference_name(sport, name))}
    return set()


def _normalized(text: str) -> str:
    return " ".join("".join(ch for ch in text.casefold() if ch.isalnum() or ch == " ").split())


def _is_game_winner(market: PolyMarket, title: str, typed: bool) -> bool:
    """The game-winner market: Gamma's moneyline type when the event's markets carry a
    type, else (two team outcomes only) the market whose question is the event title."""
    if typed:
        return market.market_type == MONEYLINE
    return _normalized(market.question) == _normalized(title)


def match_tokens(
    event: TrackedEvent, candidates: Sequence[PolyEvent]
) -> tuple[dict[str, str], list[str]]:
    """outcome id -> CLOB token whose price is P(outcome), plus unmapped names seen.

    Only game-winner markets count (`_is_game_winner`); spread and total markets also
    name both teams and must not be read as a win probability. Two-outcome markets whose
    outcomes are both teams give one token per team. Yes/No markets about one team or
    the draw give that outcome's YES token. The first market that maps an outcome wins.
    A match needs both teams and a start within `KICKOFF_TOLERANCE` of the Kalshi kickoff.
    """
    teams = {o for o in event.outcomes if o != "draw"}
    unknown: list[str] = []
    for poly in candidates:
        start = poly.start or next((m.start for m in poly.markets if m.start), None)
        if start is None or abs(start - event.kickoff) > KICKOFF_TOLERANCE:
            continue
        tokens: dict[str, str] = {}
        title_teams = _teams_in_title(event.sport, poly.title)
        typed = any(m.market_type for m in poly.markets)
        for market in poly.markets:
            names = [o.strip() for o in market.outcomes]
            if len(names) == 2 and {n.casefold() for n in names} != {"yes", "no"}:
                if not _is_game_winner(market, poly.title, typed):
                    continue
                mapped = [team_for_reference_name(event.sport, n) for n in names]
                unknown.extend(n for n, t in zip(names, mapped, strict=True) if t is None)
                if set(mapped) == teams:
                    for team, token in zip(mapped, market.token_ids, strict=True):
                        if team:
                            tokens.setdefault(team, token)
            elif [n.casefold() for n in names] == ["yes", "no"]:
                if typed and market.market_type != MONEYLINE:
                    continue
                subject = market.subject or market.question
                outcome = (
                    "draw"
                    if any(word in subject.casefold().split() for word in DRAW_NAMES)
                    else team_for_reference_name(event.sport, subject)
                )
                if outcome is None:
                    unknown.append(subject)
                elif outcome in event.outcomes and title_teams == teams:
                    tokens.setdefault(outcome, market.token_ids[0])
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
        kalshi_mids = _kalshi_mids(ledger, event.event_ticker, now)
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
            if ask - bid > MAX_BOOK_SPREAD:
                ledger.record_reference_unmatched(
                    event.event_ticker, SOURCE, "wide_book", f"{outcome}:{bid}-{ask}", now
                )
                continue
            kalshi_mid = kalshi_mids.get(outcome)
            if kalshi_mid is not None and abs((bid + ask) / 2 - kalshi_mid) > MAX_KALSHI_GAP:
                log.warning(
                    "Polymarket mismatch for %s %s: mid %s vs Kalshi %s",
                    event.event_ticker,
                    outcome,
                    (bid + ask) / 2,
                    kalshi_mid,
                )
                ledger.record_reference_unmatched(
                    event.event_ticker,
                    SOURCE,
                    "mismatch",
                    f"{outcome}:{(bid + ask) / 2} vs kalshi {kalshi_mid}",
                    now,
                )
                continue
            ledger.record_reference_quote(
                event.event_ticker, outcome, SOURCE, (bid + ask) / 2, bid, ask, token, now
            )
            written += 1
    return written


def _kalshi_mids(ledger: Ledger, event_ticker: str, now: datetime) -> dict[str, Decimal]:
    """outcome id -> Kalshi YES mid from each leg's latest market snapshot taken within
    `KALSHI_MID_MAX_AGE` before `now` (none when the snapshot is older)."""
    mids: dict[str, Decimal] = {}
    for ticker, raw_bid, raw_ask, fetched_at in ledger.conn.execute(
        "SELECT m.ticker,m.yes_bid,m.yes_ask,m.fetched_at FROM market_snapshots m "
        "JOIN (SELECT ticker,MAX(fetched_at) AS f FROM market_snapshots "
        "WHERE event_ticker=? AND fetched_at<=? GROUP BY ticker) l "
        "ON l.ticker=m.ticker AND l.f=m.fetched_at",
        (event_ticker, now.isoformat()),
    ):
        outcome = leg_outcome(str(ticker))
        if outcome is None or raw_bid is None or raw_ask is None:
            continue
        try:
            if now - _utc(str(fetched_at)) > KALSHI_MID_MAX_AGE:
                continue
            mids[outcome] = (Decimal(str(raw_bid)) + Decimal(str(raw_ask))) / 2
        except (ValueError, ArithmeticError):
            continue
    return mids


def side_reference_mid(rows: Mapping[str, Decimal], outcome: str, side: str) -> Decimal | None:
    """The side's probability from outcome mids: P(outcome) for YES, 1 - P for NO."""
    mid = rows.get(outcome)
    if mid is None:
        return None
    return mid if side == "yes" else Decimal(1) - mid
