"""The sharp lane: v3's sharp-backed sides as real paper entries, at every price.

Candidates are exactly the sides the practice book (`_feed_maker_shadows`) considers each
cycle: v3 candidates and v3 near misses blocked only by net_edge, entry_window,
price_floor or thin_market, with v3's fair (fresh sharp reference, `v3_devig_method`)
above the side's mid. The practice book itself is unchanged.

Two bands, by entry price: `longshot` (0.10 to 0.25) and `main` (above 0.25, up to 0.90).
`sharp_lane_bands` switches a band off.

Entry rule, per candidate:

  taker   fair - ask - taker fee >= `sharp_lane_take_edge`: filled at the ask now.
  maker   otherwise the practice book's price (bid + 1c at a 2c+ spread, the bid at a 1c
          spread); when fair - that price - maker fee >= `sharp_lane_min_edge` it rests
          as a paper order that fills only when the market trades through it
          (`Engine._resolve_paper_orders`). It is pulled after `sharp_lane_ttl_minutes`
          (never past the kickoff buffer), or with `edge_gone` once a later cycle prices
          the game again and the side no longer qualifies at the order's price.
  block   `sharp_lane_edge` (or `sharp_lane_band` when no price sits in an enabled band).

One position per game across both bands (the highest edge), one resting order per game,
no re-entry after a fill. Flat `sharp_lane_order_notional` per entry, capped by
`max_position_contracts`. Lane fills plus resting orders may use at most
`sharp_lane_max_open_fraction` of the bankroll. The lane has its own daily stop
(`sharp_lane_max_daily_loss`); the global daily stop ignores lane fills.

Every skipped candidate is a `blocks` row under strategy `sharp_lane`:

  sharp_lane_measure_only   the side comes from a measure-only feed
  sharp_lane_league_disabled  the sport is in `v2_disabled_sports`
  sharp_lane_band           no entry price sits in an enabled band
  sharp_lane_edge           not enough edge at the ask or at the resting price
  sharp_lane_game           another side of this game has more edge
  sharp_lane_daily_stop     the lane's own daily stop has tripped
  sharp_lane_v3_held        v3 holds a position or has an open paper order in the game
  sharp_lane_held           the lane already filled in this game (no re-entry)
  sharp_lane_resting        a lane order is already resting in this game
  sharp_lane_size           the notional buys no whole contract
  sharp_lane_kickoff        a resting order would expire before it is placed
  sharp_lane_fee            no fee schedule for the series
  sharp_lane_budget         lane fills and orders would exceed the open fraction
  sharp_lane_cash           not enough free paper cash

The first entry of each game is also logged as if it filled at once at its price
(`shadow_decisions`, reason `sharp_lane`), never touching cash, so filled orders can be
compared with all picks.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal

from prediction_bot.config import Settings
from prediction_bot.fees import FEE_MODEL_VERSION, FeeStyle, FixedFee, fee_for_ticker
from prediction_bot.models import Block, SharpLaneCandidate, Signal
from prediction_bot.paper.ledger import (
    SHARP_LANE_RULES,
    SHARP_LANE_STRATEGY,
    SHARP_LANE_TAG,
    Ledger,
    PaperOrder,
)

log = logging.getLogger(__name__)

ONE_CENT = Decimal("0.01")
LONGSHOT_MIN = Decimal("0.10")
LONGSHOT_MAX = Decimal("0.25")
MAIN_MAX = Decimal("0.90")
BANDS = ("longshot", "main")


@dataclass
class SharpLaneResult:
    orders_placed: list[Signal] = field(default_factory=list)
    filled: list[Signal] = field(default_factory=list)  # bought at the ask
    cancelled: int = 0
    blocked: dict[str, int] = field(default_factory=dict)
    halted: str | None = None


@dataclass(frozen=True)
class _Entry:
    candidate: SharpLaneCandidate
    style: FeeStyle
    price: Decimal
    rule: str  # ask, bid_plus_1 or at_bid
    band: str
    edge: Decimal  # fair - price - per-contract fee (unrounded)


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def band_of(price: Decimal) -> str | None:
    """`longshot` for 0.10..0.25, `main` above 0.25 up to 0.90, else None."""
    if LONGSHOT_MIN <= price <= LONGSHOT_MAX:
        return "longshot"
    if LONGSHOT_MAX < price <= MAIN_MAX:
        return "main"
    return None


def gap_bucket(fair: Decimal, mid: Decimal) -> str:
    """Sharp gap (fair - mid at decision) in cents: 0-1c, 1-2c, 2-3c or 3c+."""
    gap = (fair - mid) * 100
    if gap < 1:
        return "0-1c"
    if gap < 2:
        return "1-2c"
    if gap < 3:
        return "2-3c"
    return "3c+"


def maker_price(bid: Decimal | None, ask: Decimal | None) -> tuple[Decimal, str] | None:
    """The practice book's resting price: bid + 1c at a 2c+ spread, the bid at 1c."""
    if bid is None or ask is None:
        return None
    if ask - bid >= Decimal("0.02"):
        return bid + ONE_CENT, "bid_plus_1"
    if ask - bid == ONE_CENT:
        return bid, "at_bid"
    return None


def _rate(candidate: SharpLaneCandidate, price: Decimal, style: FeeStyle) -> Decimal:
    return fee_for_ticker(
        candidate.ticker,
        price,
        Decimal(1),
        style,
        schedule=FixedFee(candidate.fee_type, candidate.fee_multiplier),
        rounding=None,
    )


def lane_key(candidate: SharpLaneCandidate) -> str:
    return Ledger.sharp_lane_key(candidate.position_key)


def lane_halt_reason(settings: Settings, ledger: Ledger, now: datetime) -> str | None:
    """The lane's own daily stop: its settled PnL today has reached -limit."""
    limit = settings.sharp_lane_max_daily_loss
    if limit is None:
        return None
    day = _utc(now).replace(hour=0, minute=0, second=0, microsecond=0)
    low = ledger.worst_drawdown_since(day.replace(tzinfo=None), lane="sharp")
    if low <= -limit:
        return f"sharp lane daily loss limit hit: {low:.2f} <= -{limit:.2f}"
    return None


def _entry(settings: Settings, candidate: SharpLaneCandidate) -> _Entry | tuple[str, str]:
    """The lane's entry for `candidate`, or (block reason, detail)."""
    bands = {band for band in settings.sharp_lane_bands if band in BANDS}
    ask = candidate.ask
    in_band = False
    if ask is not None:
        band = band_of(ask)
        if band in bands:
            in_band = True
            try:
                edge = candidate.fair - ask - _rate(candidate, ask, "taker")
            except ValueError as exc:
                return "sharp_lane_fee", str(exc)
            if edge >= settings.sharp_lane_take_edge:
                return _Entry(candidate, "taker", ask, "ask", band, edge)
    priced = maker_price(candidate.bid, ask)
    rule = "none"
    if priced is not None:
        price, rule = priced
        band = band_of(price)
        if band in bands:
            in_band = True
            try:
                edge = candidate.fair - price - _rate(candidate, price, "maker")
            except ValueError as exc:
                return "sharp_lane_fee", str(exc)
            if edge >= settings.sharp_lane_min_edge:
                return _Entry(candidate, "maker", price, rule, band, edge)
    shown = band_of(priced[0]) if priced is not None else band_of(ask) if ask else None
    detail = _detail(candidate, shown or "none", "maker" if priced else "taker", rule)
    return ("sharp_lane_edge" if in_band else "sharp_lane_band"), detail


def _detail(candidate: SharpLaneCandidate, band: str, style: str, rule: str) -> str:
    gates = ",".join(candidate.gates) or "none"
    return f"band={band} {style} rule={rule} gates={gates}"


def _tags(entry: _Entry) -> tuple[str, ...]:
    candidate = entry.candidate
    return tuple(
        sorted(
            {
                SHARP_LANE_TAG,
                entry.style,
                f"band_{entry.band}",
                f"rule_{entry.rule}",
                f"gap_{gap_bucket(candidate.fair, candidate.mid)}",
                *candidate.gates,
            }
        )
    )


def _expiry(settings: Settings, candidate: SharpLaneCandidate, created_at: datetime) -> datetime:
    expires_at = created_at + timedelta(minutes=settings.sharp_lane_ttl_minutes)
    buffer = settings.kickoff_buffer_minutes.get(
        candidate.sport, settings.kickoff_buffer_default_minutes
    )
    return min(expires_at, _utc(candidate.commence_time) - timedelta(minutes=buffer))


def _still_qualifies(
    settings: Settings, order: PaperOrder, candidates: Sequence[SharpLaneCandidate]
) -> bool:
    for candidate in candidates:
        if candidate.ticker != order.ticker or candidate.side is not order.side:
            continue
        if candidate.measure_only or candidate.sport in settings.v2_disabled_sports:
            continue
        try:
            edge = candidate.fair - order.price - _rate(candidate, order.price, "maker")
        except ValueError:
            continue
        if edge >= settings.sharp_lane_min_edge:
            return True
    return False


def run_sharp_lane(
    settings: Settings,
    ledger: Ledger,
    candidates: Sequence[SharpLaneCandidate],
    *,
    priced_events: set[str],
    now: datetime,
) -> SharpLaneResult:
    """Act on one cycle of sharp-lane candidates. Paper only; the caller checks that."""
    result = SharpLaneResult()
    now = _utc(now)

    # A resting order whose game was priced again this cycle and no longer qualifies at
    # the order's price has lost its reason. Games not priced again keep it to its TTL.
    for order in ledger.open_sharp_lane_orders():
        stale = order.event_ticker in priced_events and not _still_qualifies(
            settings, order, candidates
        )
        if stale and ledger.cancel_paper_order(order.id, now, "edge_gone"):
            result.cancelled += 1

    blocks: list[Block] = []

    def block(candidate: SharpLaneCandidate, reason: str, detail: str) -> None:
        result.blocked[reason] = result.blocked.get(reason, 0) + 1
        blocks.append(
            Block(
                reason=reason,
                strategy=SHARP_LANE_STRATEGY,
                ticker=candidate.ticker,
                event_ticker=candidate.event_ticker,
                detail=detail,
                rules_version=SHARP_LANE_RULES,
                created_at=now,
                position_key=lane_key(candidate),
            )
        )

    def entry_detail(entry: _Entry) -> str:
        return _detail(entry.candidate, entry.band, entry.style, entry.rule)

    result.halted = lane_halt_reason(settings, ledger, now)
    best: dict[str, _Entry] = {}
    for candidate in candidates:
        if candidate.measure_only:
            block(candidate, "sharp_lane_measure_only", candidate.sport)
            continue
        if candidate.sport in settings.v2_disabled_sports:
            block(candidate, "sharp_lane_league_disabled", candidate.sport)
            continue
        entry = _entry(settings, candidate)
        if isinstance(entry, tuple):
            block(candidate, *entry)
            continue
        if result.halted:
            block(candidate, "sharp_lane_daily_stop", entry_detail(entry))
            continue
        held = best.get(candidate.event_ticker)
        if held is None or (entry.edge, candidate.ticker, candidate.side.value) > (
            held.edge,
            held.candidate.ticker,
            held.candidate.side.value,
        ):
            if held is not None:
                block(held.candidate, "sharp_lane_game", entry_detail(held))
            best[candidate.event_ticker] = entry
        else:
            block(candidate, "sharp_lane_game", entry_detail(entry))
    if result.halted:
        log.warning("sharp lane halted, no entries: %s", result.halted)

    for event_ticker in sorted(best):
        entry = best[event_ticker]
        candidate = entry.candidate
        detail = entry_detail(entry)
        if ledger.held_keys(event_ticker, include_v3=True):
            block(candidate, "sharp_lane_v3_held", detail)
            continue
        state = ledger.sharp_lane_event_state(event_ticker)
        if state is not None:
            block(candidate, f"sharp_lane_{state}", detail)
            continue
        size = min(
            (settings.sharp_lane_order_notional / entry.price).to_integral_value(
                rounding=ROUND_DOWN
            ),
            settings.max_position_contracts,
        )
        if size <= 0:
            block(candidate, "sharp_lane_size", detail)
            continue
        created_at = _utc(candidate.created_at)
        expires_at = _expiry(settings, candidate, created_at)
        if entry.style == "maker" and expires_at <= now:
            block(candidate, "sharp_lane_kickoff", detail)
            continue
        try:
            fee = fee_for_ticker(
                candidate.ticker,
                entry.price,
                size,
                entry.style,
                schedule=FixedFee(candidate.fee_type, candidate.fee_multiplier),
                rounding=settings.fee_rounding,
            )
        except ValueError as exc:
            block(candidate, "sharp_lane_fee", str(exc))
            continue
        cost = entry.price * size + fee
        bankroll = ledger.cash + sum((p.cost for p in ledger.positions()), Decimal(0))
        if ledger.sharp_lane_open_cost() + cost > bankroll * settings.sharp_lane_max_open_fraction:
            block(candidate, "sharp_lane_budget", detail)
            continue
        if cost > ledger.available_cash(include_v3=True):
            block(candidate, "sharp_lane_cash", detail)
            continue
        base = candidate.signal
        lane_sig = Signal(
            strategy=SHARP_LANE_STRATEGY,
            ticker=candidate.ticker,
            side=candidate.side,
            fair_prob=candidate.fair,
            limit_price=entry.price,
            edge=entry.edge,
            size=size,
            rationale=f"sharp lane {entry.band} {entry.style} ({entry.rule})",
            taker=entry.style == "taker",
            created_at=created_at,
            rules_version=SHARP_LANE_RULES,
            position_key=lane_key(candidate),
            event_ticker=event_ticker,
            tags=_tags(entry),
            commence_time=candidate.commence_time,
            fee_type=candidate.fee_type,
            fee_multiplier=candidate.fee_multiplier,
            mid=candidate.mid,
            devig_json=base.devig_json if base is not None else None,
        )
        try:
            if entry.style == "taker":
                ledger.record_fill(
                    SHARP_LANE_STRATEGY,
                    candidate.ticker,
                    candidate.side,
                    entry.price,
                    size,
                    fee,
                    candidate.fair,
                    at=created_at,
                    rules_version=SHARP_LANE_RULES,
                    position_key=lane_sig.position_key,
                    event_ticker=event_ticker,
                    tags=lane_sig.tags,
                    commence_time=candidate.commence_time,
                    fee_model_version=FEE_MODEL_VERSION,
                    net_edge=candidate.fair - entry.price - fee / size,
                )
                result.filled.append(lane_sig)
            else:
                ledger.record_paper_order(lane_sig, entry.price, fee, expires_at)
                result.orders_placed.append(lane_sig)
        except ValueError as exc:
            log.warning("sharp lane skipped %s: %s", candidate.ticker, exc)
            ledger.record_signal(lane_sig, acted=False)
            continue
        ledger.record_sharp_lane_entry(lane_sig, fee)
        ledger.record_signal(lane_sig, acted=True)
    if blocks:
        ledger.record_blocks(blocks)
    return result
