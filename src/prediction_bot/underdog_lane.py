"""The underdog lane: `underdog_value` picks as real paper entries beside v3.

`underdog_value` (v1) buys a side priced 10c-25c when the de-vigged consensus of every
configured book puts it above the price by `min_edge` after the fee. Under v3 it only
ran in shadow. With `PBOT_UNDERDOG_LANE=paper` its picks are entered again, as two books
built from the same picks:

- **The lane book (cash).** A pick priced inside the spread (one cent above the bid) is
  placed as a resting paper order. It fills only when the market later trades through
  that price (`Engine._resolve_paper_orders`, 1-minute candles) and is pulled after
  `underdog_lane_ttl_minutes`, or sooner when a fresh odds pull no longer makes it a
  pick. A pick with no room inside the spread is priced at the ask and fills there.
- **The instant-fill book (no cash).** The first pick of each game is also logged as if
  it had filled at its price the moment it was made, which is how the v1 paper engine
  booked every entry. It is settled and scored like a fill but never moves cash.

Any gap between the two is the part of the old results that came from assuming a resting
order always fills.

Guards, in order. Each skipped pick is recorded in `blocks` under strategy
`underdog_value`:

  lane_league_disabled  the sport is in `v2_disabled_sports`
  lane_no_kickoff       the matched game has no usable kickoff time
  lane_entry_window     kickoff is closer than `entry_window_min_hours`
  lane_mapping          the alias tables disagree with the fuzzy team match, or v3
                        matched this Kalshi event to a different sportsbook game
  lane_big_edge         fair is further above the price than
                        `underdog_lane_max_gross_edge`: bad data more often than value
  lane_held             the lane already filled in this game (one side per game)
  lane_resting          a lane order is already resting in this game
  lane_fee              no fee schedule for the series
  lane_budget           lane fills and orders would exceed
                        `underdog_lane_max_open_fraction` of the bankroll
  lane_cash             not enough free paper cash

The lane has its own daily stop (`underdog_lane_max_daily_loss`), and the global
`max_daily_loss` ignores lane fills, so a bad underdog day pauses the lane only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from prediction_bot.config import Settings
from prediction_bot.fees import FEE_MODEL_VERSION, FeeStyle
from prediction_bot.models import Block, Signal
from prediction_bot.paper.ledger import (
    UNDERDOG_LANE_RULES,
    UNDERDOG_LANE_STRATEGY,
    UNDERDOG_LANE_TAG,
    Ledger,
)
from prediction_bot.strategies.sportsbook_arb import GamePick

log = logging.getLogger(__name__)

FeeAmount = Callable[[Signal, Decimal, Decimal, FeeStyle], Decimal]


@dataclass
class LaneResult:
    orders_placed: list[Signal] = field(default_factory=list)
    filled: list[Signal] = field(default_factory=list)  # picks priced at the ask
    cancelled: int = 0
    blocked: dict[str, int] = field(default_factory=dict)
    halted: str | None = None


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def lane_key(pick: GamePick) -> str:
    outcome = "-".join(pick.outcome.casefold().split())
    return Ledger.underdog_lane_key(pick.event_ticker, outcome)


def lane_halt_reason(settings: Settings, ledger: Ledger, now: datetime) -> str | None:
    """The lane's own daily stop: its settled PnL today has reached -limit."""
    limit = settings.underdog_lane_max_daily_loss
    if limit is None:
        return None
    day = _utc(now).replace(hour=0, minute=0, second=0, microsecond=0)
    low = ledger.worst_drawdown_since(day.replace(tzinfo=None), lane="only")
    if low <= -limit:
        return f"underdog lane daily loss limit hit: {low:.2f} <= -{limit:.2f}"
    return None


def _expiry(settings: Settings, pick: GamePick, created_at: datetime) -> datetime:
    expires_at = created_at + timedelta(minutes=settings.underdog_lane_ttl_minutes)
    if pick.commence_time is None:
        return expires_at
    buffer = settings.kickoff_buffer_minutes.get(
        pick.sport, settings.kickoff_buffer_default_minutes
    )
    return min(expires_at, _utc(pick.commence_time) - timedelta(minutes=buffer))


def _guard(
    settings: Settings,
    ledger: Ledger,
    pick: GamePick,
    event_links: Mapping[str, str],
    now: datetime,
) -> tuple[str, str] | None:
    """(reason, detail) of the first guard that stops `pick`, or None."""
    sig = pick.signal
    if pick.sport in settings.v2_disabled_sports:
        return "lane_league_disabled", pick.sport
    if pick.commence_time is None:
        return "lane_no_kickoff", pick.odds_event_id
    lead = _utc(pick.commence_time) - now
    min_hours = settings.entry_window_min_hours
    if min_hours > 0 and lead < timedelta(hours=min_hours):
        return "lane_entry_window", f"lead_hours={lead.total_seconds() / 3600:.1f}"
    linked = event_links.get(pick.odds_event_id)
    other_game = any(
        event == pick.event_ticker and odds_id != pick.odds_event_id
        for odds_id, event in event_links.items()
    )
    if not pick.teams_exact:
        return "lane_mapping", "teams"
    if (linked is not None and linked != pick.event_ticker) or other_game:
        return "lane_mapping", "event"
    gross = sig.fair_prob - sig.limit_price
    if gross > settings.underdog_lane_max_gross_edge:
        return "lane_big_edge", f"gross={gross:.4f}"
    state = ledger.underdog_lane_event_state(pick.event_ticker)
    if state is not None:
        return f"lane_{state}", ""
    return None


def run_underdog_lane(
    settings: Settings,
    ledger: Ledger,
    picks: Sequence[GamePick],
    *,
    evaluated_events: set[str],
    event_links: Mapping[str, str],
    now: datetime,
    fee_amount: FeeAmount,
) -> LaneResult:
    """Act on one cycle of `underdog_value` picks. Paper only; the caller checks that."""
    result = LaneResult()
    now = _utc(now)

    # A resting order whose game was priced again this cycle and is no longer a pick has
    # lost the reason it was placed. Games without a fresh pull are left to their TTL.
    current = {lane_key(pick) for pick in picks}
    for order in ledger.open_underdog_lane_orders():
        stale = order.event_ticker in evaluated_events and order.position_key not in current
        if stale and ledger.cancel_paper_order(order.id, now, "edge_gone"):
            result.cancelled += 1

    result.halted = lane_halt_reason(settings, ledger, now)
    if result.halted:
        log.warning("underdog lane halted, no entries: %s", result.halted)
        return result

    # One side per game: the pick with the most edge.
    best: dict[str, GamePick] = {}
    for pick in picks:
        held = best.get(pick.event_ticker)
        if held is None or (pick.signal.edge, pick.signal.ticker) > (
            held.signal.edge,
            held.signal.ticker,
        ):
            best[pick.event_ticker] = pick

    blocks: list[Block] = []

    def block(pick: GamePick, reason: str, detail: str, key: str) -> None:
        result.blocked[reason] = result.blocked.get(reason, 0) + 1
        blocks.append(
            Block(
                reason=reason,
                strategy=UNDERDOG_LANE_STRATEGY,
                ticker=pick.signal.ticker,
                event_ticker=pick.event_ticker,
                detail=detail,
                rules_version=UNDERDOG_LANE_RULES,
                created_at=now,
                position_key=key,
            )
        )

    for event_ticker in sorted(best):
        pick = best[event_ticker]
        sig = pick.signal
        key = lane_key(pick)
        stopped = _guard(settings, ledger, pick, event_links, now)
        if stopped is not None:
            block(pick, stopped[0], stopped[1], key)
            continue
        style: FeeStyle = "taker" if sig.taker else "maker"
        lane_sig = replace(
            sig,
            rules_version=UNDERDOG_LANE_RULES,
            position_key=key,
            event_ticker=event_ticker,
            commence_time=pick.commence_time,
            tags=tuple(sorted({UNDERDOG_LANE_TAG, style})),
        )
        try:
            fee = fee_amount(lane_sig, sig.limit_price, sig.size, style)
        except ValueError as exc:
            block(pick, "lane_fee", str(exc), key)
            continue
        cost = sig.limit_price * sig.size + fee
        bankroll = ledger.cash + sum((p.cost for p in ledger.positions()), Decimal(0))
        if (
            ledger.underdog_lane_open_cost() + cost
            > bankroll * settings.underdog_lane_max_open_fraction
        ):
            block(pick, "lane_budget", f"cost={cost:.2f}", key)
            continue
        if cost > ledger.available_cash(include_v3=True):
            block(pick, "lane_cash", f"cost={cost:.2f}", key)
            continue
        try:
            if sig.taker:
                ledger.record_fill(
                    UNDERDOG_LANE_STRATEGY,
                    sig.ticker,
                    sig.side,
                    sig.limit_price,
                    sig.size,
                    fee,
                    sig.fair_prob,
                    at=lane_sig.created_at,
                    rules_version=UNDERDOG_LANE_RULES,
                    position_key=key,
                    event_ticker=event_ticker,
                    tags=lane_sig.tags,
                    commence_time=pick.commence_time,
                    fee_model_version=FEE_MODEL_VERSION,
                    net_edge=sig.fair_prob - sig.limit_price - fee / sig.size,
                )
                result.filled.append(lane_sig)
            else:
                ledger.record_paper_order(
                    lane_sig,
                    sig.limit_price,
                    fee,
                    _expiry(settings, pick, _utc(lane_sig.created_at)),
                )
                result.orders_placed.append(lane_sig)
        except ValueError as exc:
            log.warning("underdog lane skipped %s: %s", sig.ticker, exc)
            ledger.record_signal(lane_sig, acted=False)
            continue
        ledger.record_underdog_lane_entry(lane_sig, fee)
        ledger.record_signal(lane_sig, acted=True)
    if blocks:
        ledger.record_blocks(blocks)
    return result
