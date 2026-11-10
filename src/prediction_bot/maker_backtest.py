"""Maker backtest from the public trade tape: pure logic, no network, no clock.

Question: had the bot rested a bid on the sharp-favoured side at every v3 near-miss
decision point (`shadow_decisions` rows with reason `net_edge` or `entry_window`),
how often would it have filled, and what was the closing-line value of those fills
after the maker fee?

Pipeline (each step a pure function; scripts/maker_backtest.py does the I/O):

1. `build_orders` turns decision rows into candidate resting bids, one live order per
   position_key, using the book in `market_snapshots` and the repo's maker price rule.
2. `fetch_plan` lists the trade-tape windows the candidates need.
3. `run_backtest` scores each candidate against the cached tape with five fill rules
   (Q queue, X run-over, C = earlier of Q and X, T touch, A always) and stops each
   position at its first C fill.
4. `summarize` builds the headline table, breakdowns, verdict and the validity checks;
   `render_markdown` / `orders_csv_rows` format them.
"""

from __future__ import annotations

import bisect
import hashlib
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from prediction_bot.fees import FeeLookup, fee_for_ticker
from prediction_bot.maker_fill import queue_fill
from prediction_bot.models import OutcomeSide
from prediction_bot.venues.kalshi import PublicTrade

CENT = Decimal("0.01")
ONE = Decimal(1)
HUNDRED = Decimal(100)

DEFAULT_TTL_MINUTES = 480
DEFAULT_MAX_NOTIONAL = Decimal("25")
KICKOFF_BUFFER = timedelta(minutes=5)
MAX_BOOK_AGE = timedelta(minutes=5)
PRICE_TOLERANCE = Decimal("0.01")
KICKOFF_TOLERANCE = timedelta(minutes=30)
FETCH_MERGE_GAP_SECONDS = 6 * 3600

EDGE_THRESHOLDS = (Decimal("0"), Decimal("0.5"), Decimal("1.0"), Decimal("1.5"), Decimal("2.0"))
BREAKDOWN_EDGE = Decimal("1.0")
VERDICT_MIN_FILLS = 50
VERDICT_MIN_GAMES = 30

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


def utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def parse_time(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return utc(value)
    try:
        return utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def opt_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


# ---- inputs -----------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """One `shadow_decisions` row. `price` is the ask for `side` at decision time;
    `clv_kalshi` is cents (T-5 mid for the side minus `price`)."""

    id: int
    created_at: datetime
    ticker: str
    side: OutcomeSide
    position_key: str
    fair: Decimal
    price: Decimal
    window: str
    commence_time: datetime | None
    clv_kalshi: Decimal | None
    clv_status: str | None


@dataclass(frozen=True)
class Snapshot:
    """One `market_snapshots` row (YES-leg book)."""

    at: datetime
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    yes_bid_size: Decimal
    yes_ask_size: Decimal
    event_ticker: str = ""


def side_bid(snap: Snapshot, side: OutcomeSide) -> Decimal | None:
    if side is OutcomeSide.yes:
        return snap.yes_bid
    return None if snap.yes_ask is None else ONE - snap.yes_ask


def side_ask(snap: Snapshot, side: OutcomeSide) -> Decimal | None:
    if side is OutcomeSide.yes:
        return snap.yes_ask
    return None if snap.yes_bid is None else ONE - snap.yes_bid


def side_bid_size(snap: Snapshot, side: OutcomeSide) -> Decimal:
    """Size resting at the side's best bid: a NO bid at 1 - yes_ask queues behind
    yes_ask_size."""
    return snap.yes_bid_size if side is OutcomeSide.yes else snap.yes_ask_size


@dataclass(frozen=True)
class ShadowRow:
    """One live `maker_shadow` row, for the replay cross-check."""

    id: int
    created_at: datetime
    ticker: str
    side: OutcomeSide
    price: Decimal
    size: Decimal
    queue_ahead: Decimal | None
    expires_at: datetime
    fill_queue_at: datetime | None


class SnapshotIndex:
    """Snapshots per ticker, sorted, with bisect lookups."""

    def __init__(self, by_ticker: Mapping[str, Sequence[Snapshot]]) -> None:
        self.rows: dict[str, list[Snapshot]] = {
            ticker: sorted(rows, key=lambda s: s.at) for ticker, rows in by_ticker.items()
        }
        self.times: dict[str, list[datetime]] = {
            ticker: [s.at for s in rows] for ticker, rows in self.rows.items()
        }

    def latest_at_or_before(self, ticker: str, at: datetime) -> Snapshot | None:
        times = self.times.get(ticker)
        if not times:
            return None
        index = bisect.bisect_right(times, at) - 1
        return self.rows[ticker][index] if index >= 0 else None

    def between(self, ticker: str, after: datetime, before: datetime) -> list[Snapshot]:
        """Snapshots strictly after `after` and strictly before `before`."""
        times = self.times.get(ticker)
        if not times:
            return []
        lo = bisect.bisect_right(times, after)
        hi = bisect.bisect_left(times, before)
        return self.rows[ticker][lo:hi]


# ---- step 1: orders -----------------------------------------------------------


@dataclass(frozen=True)
class Order:
    decision: Decision
    event_ticker: str
    league: str
    book_at: datetime
    bid: Decimal
    ask: Decimal
    rule: str
    price: Decimal
    queue_ahead: Decimal
    size: Decimal
    start: datetime
    end: datetime
    kickoff: datetime
    kickoff_source: str
    maker_fee_c: Decimal
    taker_fee_c: Decimal | None
    edge_c: Decimal

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def side(self) -> OutcomeSide:
        return self.decision.side

    @property
    def ticker(self) -> str:
        return self.decision.ticker

    @property
    def position_key(self) -> str:
        return self.decision.position_key


@dataclass
class BuildResult:
    orders: list[Order]
    skips: Counter[str] = field(default_factory=Counter)
    kickoff_sources: Counter[str] = field(default_factory=Counter)
    decisions: int = 0


def per_contract_fee_cents(
    ticker: str, price: Decimal, contracts: Decimal, style: str, fees: FeeLookup
) -> Decimal:
    """The order's fee (rounded up to the cent, as Kalshi charges it) per contract, in cents."""
    total = fee_for_ticker(
        ticker,
        price,
        contracts,
        "maker" if style == "maker" else "taker",
        schedule=fees,
        rounding="cent",
    )
    return total / contracts * HUNDRED


def maker_price(bid: Decimal, ask: Decimal, bid_size: Decimal) -> tuple[str, Decimal, Decimal]:
    """The repo's maker rule (sportsbook_v2 shadow): a 1c spread joins the bid behind
    the size resting there; a 2c+ spread improves the bid by 1c with nothing ahead."""
    if ask - bid >= Decimal("0.02"):
        return "bid_plus_1", bid + CENT, Decimal(0)
    return "at_bid", bid, bid_size


def build_orders(
    decisions: Iterable[Decision],
    snapshots: SnapshotIndex,
    odds_kickoffs: Mapping[str, datetime],
    fees: FeeLookup,
    *,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    max_notional: Decimal = DEFAULT_MAX_NOTIONAL,
) -> BuildResult:
    """Candidate resting bids, in time order, at most one live per position_key.

    A decision is skipped (and counted by reason) when there is no book at most five
    minutes old whose side ask matches its price within 1c, when the book has no bid
    or is locked/crossed, when the kickoff is unknown or the odds kickoff disagrees
    with the row's by more than 30 minutes, when the window is already closed, when
    the fee schedule is unknown, or while an earlier order on the position is live.
    """
    ordered = sorted(decisions, key=lambda d: (d.created_at, d.id))
    result = BuildResult(orders=[], decisions=len(ordered))
    live_until: dict[str, datetime] = {}
    ttl = timedelta(minutes=ttl_minutes)
    for decision in ordered:
        busy = live_until.get(decision.position_key)
        if busy is not None and decision.created_at < busy:
            result.skips["position_busy"] += 1
            continue
        snap = snapshots.latest_at_or_before(decision.ticker, decision.created_at)
        if snap is None:
            result.skips["no_snapshot"] += 1
            continue
        if decision.created_at - snap.at > MAX_BOOK_AGE:
            result.skips["stale_snapshot"] += 1
            continue
        bid = side_bid(snap, decision.side)
        ask = side_ask(snap, decision.side)
        if ask is None or abs(ask - decision.price) > PRICE_TOLERANCE:
            result.skips["ask_mismatch"] += 1
            continue
        if bid is None or bid <= 0:
            result.skips["no_bid"] += 1
            continue
        if ask - bid < CENT:
            result.skips["locked_or_crossed"] += 1
            continue
        event_ticker = snap.event_ticker or decision.ticker.rsplit("-", 1)[0]
        odds_kickoff = odds_kickoffs.get(event_ticker)
        if odds_kickoff is not None:
            kickoff, source = odds_kickoff, "odds_snapshots"
            if (
                decision.commence_time is not None
                and abs(odds_kickoff - decision.commence_time) > KICKOFF_TOLERANCE
            ):
                result.skips["kickoff_mismatch"] += 1
                continue
        elif decision.commence_time is not None:
            kickoff, source = decision.commence_time, "decision_row"
        else:
            result.skips["no_kickoff"] += 1
            continue
        start = decision.created_at
        end = min(start + ttl, kickoff - KICKOFF_BUFFER)
        if end <= start:
            result.skips["window_closed"] += 1
            continue
        rule, price, queue_ahead = maker_price(bid, ask, side_bid_size(snap, decision.side))
        size = (max_notional / price).to_integral_value(rounding=ROUND_FLOOR)
        if size <= 0:
            result.skips["zero_size"] += 1
            continue
        try:
            maker_fee_c = per_contract_fee_cents(decision.ticker, price, size, "maker", fees)
            taker_size = (max_notional / ask).to_integral_value(rounding=ROUND_FLOOR)
            taker_fee_c = (
                per_contract_fee_cents(decision.ticker, ask, taker_size, "taker", fees)
                if taker_size > 0
                else None
            )
        except ValueError:
            result.skips["no_fee_schedule"] += 1
            continue
        edge_c = (decision.fair - price) * HUNDRED - maker_fee_c
        result.orders.append(
            Order(
                decision=decision,
                event_ticker=event_ticker,
                league=decision.ticker.split("-", 1)[0],
                book_at=snap.at,
                bid=bid,
                ask=ask,
                rule=rule,
                price=price,
                queue_ahead=queue_ahead,
                size=size,
                start=start,
                end=end,
                kickoff=kickoff,
                kickoff_source=source,
                maker_fee_c=maker_fee_c,
                taker_fee_c=taker_fee_c,
                edge_c=edge_c,
            )
        )
        result.kickoff_sources[source] += 1
        live_until[decision.position_key] = end
    return result


# ---- step 2: which tape to fetch ----------------------------------------------


def order_span(order: Order) -> tuple[int, int]:
    """The order's window as whole Unix seconds, widened outward."""
    lo = math.floor(order.start.timestamp())
    hi = math.ceil(order.end.timestamp())
    return lo, hi


def fetch_plan(
    orders: Iterable[Order], merge_gap: int = FETCH_MERGE_GAP_SECONDS
) -> dict[str, list[tuple[int, int]]]:
    """Per ticker, the merged windows that cover every order (gaps up to `merge_gap`
    seconds are bridged so neighbouring orders share requests)."""
    spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for order in orders:
        spans[order.ticker].append(order_span(order))
    plan: dict[str, list[tuple[int, int]]] = {}
    for ticker in sorted(spans):
        merged: list[tuple[int, int]] = []
        for lo, hi in sorted(spans[ticker]):
            if merged and lo <= merged[-1][1] + merge_gap:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        plan[ticker] = merged
    return plan


# ---- step 3: fills and outcomes -----------------------------------------------


def _in_window(at: datetime, start: datetime, end: datetime) -> bool:
    return start <= at < end


def queue_hits(trade: PublicTrade, side: OutcomeSide, price: Decimal) -> bool:
    """A taker trade that executes against a resting `side` bid at `price` (at it or
    through it): the same test as `maker_fill.queue_fill`."""
    if side is OutcomeSide.yes:
        return trade.taker_side is OutcomeSide.no and trade.yes_price <= price
    return trade.taker_side is OutcomeSide.yes and ONE - trade.yes_price <= price


def trades_through(trade: PublicTrade, side: OutcomeSide, price: Decimal) -> bool:
    """Any print strictly through our bid: a resting order cannot survive it."""
    if side is OutcomeSide.yes:
        return trade.yes_price < price
    return ONE - trade.yes_price < price


@dataclass(frozen=True)
class Fills:
    q_at: datetime | None
    x_at: datetime | None
    x_source: str | None
    c_at: datetime | None
    t_at: datetime | None
    a_at: datetime
    matched_volume: Decimal
    first_rule: str | None


def evaluate_fills(
    order: Order, trades: Sequence[PublicTrade], later_snapshots: Sequence[Snapshot]
) -> Fills:
    """All five fill rules for one order. Block trades never count.

    Q: `maker_fill.queue_fill` (volume at or through our price reaches queue + size).
    X: first trade strictly through our price, or first later snapshot whose side ask
       is at or below our price. C: earlier of Q and X. T: first qualifying taker
       trade at our price or better. A: at placement.
    """
    side, price = order.side, order.price
    usable = [
        t for t in trades if not t.is_block and _in_window(t.created_at, order.start, order.end)
    ]
    q_at = queue_fill(
        [t.to_trade() for t in usable],
        side,
        price,
        order.queue_ahead,
        order.size,
        order.start,
        order.end,
    )
    matched = sum((t.count for t in usable if queue_hits(t, side, price)), Decimal(0))
    t_at = next((t.created_at for t in usable if queue_hits(t, side, price)), None)
    x_trade = next((t.created_at for t in usable if trades_through(t, side, price)), None)
    x_snap = next(
        (
            s.at
            for s in later_snapshots
            if _in_window(s.at, order.start, order.end)
            and s.at > order.start
            and (ask := side_ask(s, side)) is not None
            and ask <= price
        ),
        None,
    )
    x_at: datetime | None
    x_source: str | None
    if x_trade is not None and (x_snap is None or x_trade <= x_snap):
        x_at, x_source = x_trade, "trade"
    elif x_snap is not None:
        x_at, x_source = x_snap, "snapshot"
    else:
        x_at, x_source = None, None
    if q_at is not None and (x_at is None or q_at <= x_at):
        c_at, first = q_at, "Q"
    elif x_at is not None:
        c_at, first = x_at, "X"
    else:
        c_at, first = None, None
    return Fills(
        q_at=q_at,
        x_at=x_at,
        x_source=x_source,
        c_at=c_at,
        t_at=t_at,
        a_at=order.start,
        matched_volume=matched,
        first_rule=first,
    )


@dataclass(frozen=True)
class OrderResult:
    order: Order
    tape: str  # ok | tape_unavailable | missing
    fills: Fills | None
    t5_mid: Decimal | None
    clv_close: Decimal | None
    settle: Decimal | None
    taker: Decimal | None

    @property
    def scored(self) -> bool:
        return self.tape == "ok" and self.fills is not None

    def filled(self, rule: str = "C") -> bool:
        if self.fills is None:
            return False
        return {
            "C": self.fills.c_at,
            "Q": self.fills.q_at,
            "X": self.fills.x_at,
            "T": self.fills.t_at,
            "A": self.fills.a_at,
        }[rule] is not None


def outcomes(
    order: Order, settlement: str | None
) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
    """(t5_mid, clv_close, settle, taker) in cents per contract after fees.

    The T-5 mid for our side is the decision price + clv_kalshi / 100, used only when
    clv_status is 'ok'. settle uses settlements.result ('yes'/'no') for our side.
    """
    d = order.decision
    t5_mid: Decimal | None = None
    clv_close: Decimal | None = None
    taker: Decimal | None = None
    if d.clv_status == "ok" and d.clv_kalshi is not None:
        t5_mid = d.price + d.clv_kalshi / HUNDRED
        clv_close = (t5_mid - order.price) * HUNDRED - order.maker_fee_c
        if order.taker_fee_c is not None:
            taker = (t5_mid - d.price) * HUNDRED - order.taker_fee_c
    settle: Decimal | None = None
    if settlement in ("yes", "no"):
        won = ONE if settlement == d.side.value else Decimal(0)
        settle = (won - order.price) * HUNDRED - order.maker_fee_c
    return t5_mid, clv_close, settle, taker


@dataclass
class BacktestRun:
    results: list[OrderResult]
    dropped: Counter[str] = field(default_factory=Counter)


def run_backtest(
    orders: Sequence[Order],
    trades_by_ticker: Mapping[str, Sequence[PublicTrade]],
    snapshots: SnapshotIndex,
    tape_status: Callable[[Order], str],
    settlements: Mapping[str, str],
) -> BacktestRun:
    """Score each position's candidate orders in time order and stop at its first C fill.

    A candidate whose tape is unavailable or missing is kept as unscored, and the
    position's later candidates are dropped (whether it filled is unknown).
    """
    by_position: dict[str, list[Order]] = defaultdict(list)
    for order in orders:
        by_position[order.position_key].append(order)
    run = BacktestRun(results=[])
    for key in sorted(by_position, key=lambda k: (by_position[k][0].start, k)):
        candidates = sorted(by_position[key], key=lambda o: (o.start, o.decision.id))
        for index, order in enumerate(candidates):
            rest = len(candidates) - index - 1
            t5, clv, settle, taker = outcomes(order, settlements.get(order.ticker))
            status = tape_status(order)
            if status != "ok":
                run.results.append(OrderResult(order, status, None, t5, clv, settle, taker))
                run.dropped[f"after_{status}"] += rest
                break
            fills = evaluate_fills(
                order,
                trades_by_ticker.get(order.ticker, ()),
                snapshots.between(order.ticker, order.start, order.end),
            )
            run.results.append(OrderResult(order, status, fills, t5, clv, settle, taker))
            if fills.c_at is not None:
                run.dropped["after_fill"] += rest
                break
    run.results.sort(key=lambda r: (r.order.start, r.order.decision.id))
    return run


# ---- step 4: statistics ---------------------------------------------------------


@dataclass(frozen=True)
class Estimate:
    n: int
    games: int
    mean: float | None
    se: float | None

    @property
    def lo(self) -> float | None:
        return None if self.mean is None or self.se is None else self.mean - 1.96 * self.se

    @property
    def hi(self) -> float | None:
        return None if self.mean is None or self.se is None else self.mean + 1.96 * self.se

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "games": self.games,
            "mean": _round(self.mean),
            "se": _round(self.se),
            "lo": _round(self.lo),
            "hi": _round(self.hi),
        }


def _round(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(value, places)


def clustered_mean(values: Sequence[tuple[str, float]]) -> Estimate:
    """Mean of `values` with a standard error clustered by the first element (game).

    SE = sqrt(G / (G - 1) * sum_g (sum_{i in g} (y_i - mean))^2) / n, the CR1 sandwich
    for a mean. None with fewer than two clusters.
    """
    n = len(values)
    groups: dict[str, float] = defaultdict(float)
    if n == 0:
        return Estimate(0, 0, None, None)
    mean = math.fsum(v for _, v in values) / n
    for game, v in values:
        groups[game] += v - mean
    g = len(groups)
    if g < 2:
        return Estimate(n, g, mean, None)
    meat = math.fsum(s * s for s in groups.values())
    se = math.sqrt(g / (g - 1) * meat) / n
    return Estimate(n, g, mean, se)


def _f(value: Decimal) -> float:
    return float(value)


def _estimate(
    rows: Iterable[OrderResult], value: Callable[[OrderResult], Decimal | None]
) -> Estimate:
    pairs = [(r.order.event_ticker, _f(v)) for r in rows if (v := value(r)) is not None]
    return clustered_mean(pairs)


@dataclass(frozen=True)
class Cohort:
    label: str
    orders: int
    games: int
    fills: int
    fill_rate: float | None
    clv_c: Estimate
    per_order: Estimate
    x_share: float | None
    clv_q: Estimate
    clv_x: Estimate
    clv_t: Estimate
    clv_a: Estimate
    taker: Estimate
    missed: Estimate
    settle_c: Estimate

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "orders": self.orders,
            "games": self.games,
            "fills": self.fills,
            "fill_rate": _round(self.fill_rate),
            "clv_close_c": self.clv_c.as_dict(),
            "per_order_placed": self.per_order.as_dict(),
            "x_share": _round(self.x_share),
            "clv_close_q": self.clv_q.as_dict(),
            "clv_close_x": self.clv_x.as_dict(),
            "clv_close_t": self.clv_t.as_dict(),
            "clv_close_a": self.clv_a.as_dict(),
            "taker": self.taker.as_dict(),
            "missed": self.missed.as_dict(),
            "settle_c": self.settle_c.as_dict(),
        }


def cohort(label: str, rows: Sequence[OrderResult]) -> Cohort:
    """Statistics for scored orders. Fill rate and expectancy are per order placed;
    clv figures are per filled contract, after the maker fee."""
    scored = [r for r in rows if r.scored]
    fills = [r for r in scored if r.filled("C")]
    from_x = sum(1 for r in fills if r.fills is not None and r.fills.first_rule == "X")

    def clv_under(rule: str) -> Estimate:
        return _estimate((r for r in scored if r.filled(rule)), lambda r: r.clv_close)

    return Cohort(
        label=label,
        orders=len(scored),
        games=len({r.order.event_ticker for r in scored}),
        fills=len(fills),
        fill_rate=len(fills) / len(scored) if scored else None,
        clv_c=clv_under("C"),
        per_order=_estimate(
            scored,
            lambda r: (
                None if r.clv_close is None else (r.clv_close if r.filled("C") else Decimal(0))
            ),
        ),
        x_share=from_x / len(fills) if fills else None,
        clv_q=clv_under("Q"),
        clv_x=clv_under("X"),
        clv_t=clv_under("T"),
        clv_a=clv_under("A"),
        taker=_estimate(scored, lambda r: r.taker),
        missed=_estimate((r for r in scored if not r.filled("C")), lambda r: r.clv_close),
        settle_c=_estimate(fills, lambda r: r.settle),
    )


def window_label(created_at: datetime, kickoff: datetime) -> str:
    lead = kickoff - created_at
    if lead >= timedelta(hours=72):
        return "72h+"
    if lead >= timedelta(hours=24):
        return "24-72h"
    if lead >= timedelta(hours=2):
        return "2-24h"
    return "<2h"


def price_band(price: Decimal) -> str:
    if price < Decimal("0.40"):
        return "<0.40"
    if price <= Decimal("0.60"):
        return "0.40-0.60"
    return ">0.60"


def queue_bucket(queue: Decimal) -> str:
    if queue <= 0:
        return "0"
    if queue <= 100:
        return "1-100"
    if queue <= 1000:
        return "101-1000"
    return ">1000"


BREAKDOWNS: dict[str, tuple[Callable[[Order], str], tuple[str, ...] | None]] = {
    "league": (lambda o: o.league, None),
    "window": (
        lambda o: window_label(o.start, o.kickoff),
        ("72h+", "24-72h", "2-24h", "<2h"),
    ),
    "spread": (lambda o: "1c" if o.spread <= CENT else "2c+", ("1c", "2c+")),
    "price band": (lambda o: price_band(o.price), ("<0.40", "0.40-0.60", ">0.60")),
    "queue_ahead": (
        lambda o: queue_bucket(o.queue_ahead),
        ("0", "1-100", "101-1000", ">1000"),
    ),
}


# ---- validity checks ------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # PASS | FAIL | WARN
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def _sample_key(trade: PublicTrade) -> str:
    return hashlib.sha256(f"{trade.ticker}|{trade.trade_id}".encode()).hexdigest()


def tape_sample(trades: Iterable[PublicTrade], size: int = 200) -> list[PublicTrade]:
    """A deterministic pseudo-random sample of non-block trades."""
    pool = sorted((t for t in trades if not t.is_block), key=_sample_key)
    return pool[:size]


def check_tape_sanity(
    sample: Sequence[PublicTrade], unparsed: int, snapshots: SnapshotIndex
) -> Check:
    """Sides yes/no, 0 < yes_price < 1, count > 0, and >= 95% of prices inside that
    clock hour's snapshot bid/ask range (with 2c of slack)."""
    name = "tape sanity"
    if not sample:
        return Check(name, FAIL, "no trades in the cache to sample")
    bad = [
        t.trade_id
        for t in sample
        if t.taker_side not in (OutcomeSide.yes, OutcomeSide.no)
        or not Decimal(0) < t.yes_price < ONE
        or t.count <= 0
    ]
    inside = checked = 0
    slack = Decimal("0.02")
    for t in sample:
        hour = t.created_at.replace(minute=0, second=0, microsecond=0)
        snaps = snapshots.between(
            t.ticker, hour - timedelta(microseconds=1), hour + timedelta(hours=1)
        )
        bids = [s.yes_bid for s in snaps if s.yes_bid is not None]
        asks = [s.yes_ask for s in snaps if s.yes_ask is not None]
        if not bids or not asks:
            continue
        checked += 1
        if min(bids) - slack <= t.yes_price <= max(asks) + slack:
            inside += 1
    share = inside / checked if checked else None
    detail = (
        f"{len(sample)} sampled; {len(bad)} with bad side/price/count; {unparsed} raw rows "
        f"unparseable; {inside}/{checked} inside the hour's snapshot range"
        + ("" if share is None else f" ({share:.1%})")
    )
    ok = not bad and unparsed == 0 and share is not None and share >= 0.95
    return Check(name, PASS if ok else FAIL, detail)


def check_side_semantics(
    trades: Iterable[PublicTrade],
    snapshots: SnapshotIndex,
    max_lag: timedelta = timedelta(seconds=120),
    min_classified: int = 20,
    min_agree: float = 0.8,
) -> Check:
    """taker_side and taker_outcome_side agree wherever both are present, and prints
    at the snapshot bid are `no` takers (at the ask, `yes` takers)."""
    name = "side semantics"
    both = disagree = 0
    at_bid = at_bid_no = at_ask = at_ask_yes = 0
    for t in trades:
        if t.is_block:
            continue
        if t.raw_taker_side and t.raw_taker_outcome_side:
            both += 1
            if t.raw_taker_side != t.raw_taker_outcome_side:
                disagree += 1
        snap = snapshots.latest_at_or_before(t.ticker, t.created_at)
        if snap is None or t.created_at - snap.at > max_lag:
            continue
        if snap.yes_bid is None or snap.yes_ask is None or snap.yes_bid >= snap.yes_ask:
            continue
        if t.yes_price == snap.yes_bid:
            at_bid += 1
            at_bid_no += t.taker_side is OutcomeSide.no
        elif t.yes_price == snap.yes_ask:
            at_ask += 1
            at_ask_yes += t.taker_side is OutcomeSide.yes
    classified = at_bid + at_ask
    agree = at_bid_no + at_ask_yes
    detail = (
        f"{disagree}/{both} rows disagree between taker_side and taker_outcome_side; "
        f"prints within {int(max_lag.total_seconds())}s of a snapshot: "
        f"{at_bid_no}/{at_bid} at the bid were `no` takers, "
        f"{at_ask_yes}/{at_ask} at the ask were `yes` takers"
    )
    if disagree:
        return Check(name, FAIL, detail)
    if classified < min_classified:
        return Check(name, WARN, detail + f" (fewer than {min_classified} classified)")
    return Check(name, PASS if agree / classified >= min_agree else FAIL, detail)


def replay_shadows(
    shadows: Sequence[ShadowRow],
    trades_by_ticker: Mapping[str, Sequence[PublicTrade]],
    tape_status: Callable[[str, datetime, datetime], str],
    tolerance: timedelta = timedelta(seconds=60),
) -> tuple[Check, list[dict[str, Any]]]:
    """Replay each live maker_shadow row with `queue_fill` on the cached tape and compare
    with its stored fill_queue_at. A stored time that the replay does not reproduce
    (within a minute) fails the check; other differences are listed."""
    rows: list[dict[str, Any]] = []
    mismatches = notes = 0
    for s in sorted(shadows, key=lambda r: r.id):
        status = tape_status(s.ticker, s.created_at, s.expires_at)
        replay: datetime | None = None
        if status == "ok":
            usable = [t.to_trade() for t in trades_by_ticker.get(s.ticker, ()) if not t.is_block]
            replay = queue_fill(
                usable,
                s.side,
                s.price,
                s.queue_ahead if s.queue_ahead is not None else Decimal(0),
                s.size,
                s.created_at,
                s.expires_at,
            )
        if status != "ok":
            verdict = f"tape {status}"
            notes += 1
            if s.fill_queue_at is not None:
                mismatches += 1
        elif s.fill_queue_at is None and replay is None:
            verdict = "match (no fill)"
        elif (
            s.fill_queue_at is not None
            and replay is not None
            and abs(replay - s.fill_queue_at) <= tolerance
        ):
            verdict = "match"
        elif s.fill_queue_at is None:
            verdict = "differs: replay fills, stored none"
            notes += 1
        else:
            verdict = "differs"
            mismatches += 1
        rows.append(
            {
                "id": s.id,
                "ticker": s.ticker,
                "side": s.side.value,
                "price": str(s.price),
                "queue_ahead": None if s.queue_ahead is None else str(s.queue_ahead),
                "stored_fill_queue_at": None
                if s.fill_queue_at is None
                else s.fill_queue_at.isoformat(),
                "replay_fill_queue_at": None if replay is None else replay.isoformat(),
                "result": verdict,
            }
        )
    detail = f"{len(rows)} shadows replayed; {mismatches} stored fills not reproduced"
    if notes:
        detail += f"; {notes} other differences (listed below)"
    if not rows:
        return Check("maker_shadow replay", WARN, "no maker_shadow rows"), rows
    return Check("maker_shadow replay", FAIL if mismatches else PASS, detail), rows


def check_no_overlap(results: Sequence[OrderResult]) -> Check:
    by_key: dict[str, list[Order]] = defaultdict(list)
    for r in results:
        by_key[r.order.position_key].append(r.order)
    overlaps = 0
    for orders in by_key.values():
        orders.sort(key=lambda o: o.start)
        for a, b in zip(orders, orders[1:], strict=False):
            if b.start < a.end:
                overlaps += 1
    return Check(
        "one order per position",
        FAIL if overlaps else PASS,
        f"{overlaps} overlapping order pairs across {len(by_key)} positions",
    )


def check_determinism(first: Mapping[str, Any], second: Mapping[str, Any]) -> Check:
    same = first == second
    return Check(
        "warm-cache rerun",
        PASS if same else FAIL,
        "a second pass over the cache gave identical numbers"
        if same
        else "a second pass over the cache gave different numbers",
    )


# ---- summary ----------------------------------------------------------------------


@dataclass
class Summary:
    params: dict[str, Any]
    checks: list[Check]
    headline: list[Cohort]
    breakdowns: dict[str, list[Cohort]]
    verdict: str
    build: dict[str, Any]
    tape: dict[str, Any]
    shadow_replay: list[dict[str, Any]]
    fetch: dict[str, Any] = field(default_factory=dict)

    @property
    def invalid(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    def numbers(self) -> dict[str, Any]:
        """Everything numeric, for the warm-cache determinism comparison."""
        return {
            "headline": [c.as_dict() for c in self.headline],
            "breakdowns": {k: [c.as_dict() for c in v] for k, v in self.breakdowns.items()},
            "verdict": self.verdict,
            "build": self.build,
            "tape": self.tape,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "params": self.params,
            "valid": not self.invalid,
            "checks": [c.as_dict() for c in self.checks],
            **self.numbers(),
            "fetch": self.fetch,
            "shadow_replay": self.shadow_replay,
        }


def _edge_label(threshold: Decimal) -> str:
    return f"edge >= {threshold:.1f}c"


def headline_cohorts(results: Sequence[OrderResult]) -> list[Cohort]:
    rows = [
        cohort(_edge_label(t), [r for r in results if r.order.edge_c >= t]) for t in EDGE_THRESHOLDS
    ]
    rows.append(cohort("control: edge < 0", [r for r in results if r.order.edge_c < 0]))
    return rows


def breakdown_cohorts(results: Sequence[OrderResult]) -> dict[str, list[Cohort]]:
    base = [r for r in results if r.order.edge_c >= BREAKDOWN_EDGE]
    out: dict[str, list[Cohort]] = {}
    for name, (key, order) in BREAKDOWNS.items():
        groups: dict[str, list[OrderResult]] = defaultdict(list)
        for r in base:
            groups[key(r.order)].append(r)
        labels = list(order) if order is not None else sorted(groups)
        out[name] = [cohort(label, groups.get(label, [])) for label in labels]
    return out


def verdict_line(headline: Sequence[Cohort], invalid: Sequence[Check]) -> str:
    target = next(c for c in headline if c.label == _edge_label(BREAKDOWN_EDGE))
    missing: list[str] = []
    if invalid:
        missing.append("validity checks failed: " + ", ".join(c.name for c in invalid))
    if target.clv_c.n < VERDICT_MIN_FILLS:
        missing.append(f"{target.clv_c.n} of {VERDICT_MIN_FILLS} C fills with a closing line")
    if target.clv_c.games < VERDICT_MIN_GAMES:
        missing.append(f"{target.clv_c.games} of {VERDICT_MIN_GAMES} games among those fills")
    lo = target.clv_c.lo
    if lo is None:
        missing.append("no interval for clv_close yet")
    elif lo <= 0:
        missing.append(f"clv_close interval lower end is {lo:+.2f}c, not above 0")
    if not missing:
        return (
            f"PASS: edge >= 1.0c has {target.clv_c.n} C fills from {target.clv_c.games} games "
            f"and clv_close {target.clv_c.mean:+.2f}c (95% CI {lo:+.2f}c to "
            f"{target.clv_c.hi:+.2f}c)"
        )
    return "NOT YET: " + "; ".join(missing)


def summarize(
    run: BacktestRun,
    build: BuildResult,
    checks: Sequence[Check],
    *,
    params: Mapping[str, Any],
    tape: Mapping[str, Any],
    shadow_replay: Sequence[dict[str, Any]] = (),
) -> Summary:
    headline = headline_cohorts(run.results)
    invalid = [c for c in checks if c.status == FAIL]
    unscored = Counter(r.tape for r in run.results if not r.scored)
    build_info = {
        "decisions": build.decisions,
        "candidate_orders": len(build.orders),
        "skips": dict(sorted(build.skips.items())),
        "kickoff_sources": dict(sorted(build.kickoff_sources.items())),
        "orders_scored": sum(1 for r in run.results if r.scored),
        "orders_unscored": dict(sorted(unscored.items())),
        "candidates_dropped": dict(sorted(run.dropped.items())),
    }
    return Summary(
        params=dict(params),
        checks=list(checks),
        headline=headline,
        breakdowns=breakdown_cohorts(run.results),
        verdict=verdict_line(headline, invalid),
        build=build_info,
        tape=dict(tape),
        shadow_replay=list(shadow_replay),
    )


# ---- rendering ----------------------------------------------------------------------


def _c(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}c"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _est(e: Estimate, interval: bool = True) -> str:
    if e.mean is None:
        return "-"
    text = _c(e.mean)
    if interval and e.lo is not None and e.hi is not None:
        text += f" ({e.lo:+.2f} to {e.hi:+.2f})"
    return f"{text} [n={e.n}]"


HEADLINE_HEADER = (
    "| cohort | orders | games | C fills | fill rate | clv_close on C fills (95% CI) "
    "| per order placed (95% CI) | C fills from X | clv Q | clv X | clv T | clv A "
    "| taker | missed |\n"
    "|---|--:|--:|--:|--:|---|---|--:|---|---|---|---|---|---|"
)

BREAKDOWN_HEADER = (
    "| group | orders | games | C fills | fill rate | clv_close on C fills (95% CI) "
    "| per order placed | taker |\n|---|--:|--:|--:|--:|---|---|---|"
)

LIMITS = (
    "- Decision points exist only at odds pulls; between pulls the bot had no fair "
    "and placed nothing.",
    "- queue_ahead is the size at the best bid when we join, taken from the nearest "
    "market snapshot (at most 5 minutes old); orders that join later are ahead of us "
    "only if they arrive before us, which the snapshot cannot show.",
    "- Our own order is assumed not to change anyone else's behaviour (no one reacts "
    "to the new best bid at bid + 1c).",
    "- The fair is the power-devig sharp fair stored on the decision row.",
    "- Candidates are chained by the C rule: a position stops at its first C fill. "
    "The Q, X, T and A columns are measured on those same orders.",
    "- The X rule also uses market snapshots, which are minutes apart; a run-over "
    "between two snapshots that left no trade on the tape is missed.",
    "- clv_close uses the T-5 mid recorded for the decision row (clv_status = 'ok' "
    "only); orders without one count in fill rates but not in clv figures.",
)


def render_markdown(summary: Summary) -> str:
    lines: list[str] = ["# Maker backtest from the public trade tape", ""]
    if summary.invalid:
        lines += [
            "**INVALID** — " + "; ".join(f"{c.name}: {c.detail}" for c in summary.invalid),
            "",
        ]
    p = summary.params
    lines += [
        f"Decisions since {p.get('since')} until {p.get('until') or 'the latest row'}; "
        f"edge = (fair - our price) x 100 - maker fee; all figures in cents per contract "
        "after fees; intervals are mean ± 1.96 × SE clustered by game.",
        "",
        "## Validity checks",
        "",
        "| check | result | detail |",
        "|---|---|---|",
    ]
    for check in summary.checks:
        lines.append(f"| {check.name} | {check.status} | {check.detail} |")
    side_failed = any(
        check.name == "side semantics" and check.status == FAIL for check in summary.checks
    )
    lines += ["", "## Headline", ""]
    if side_failed:
        lines += [
            "Not computed: the trade tape's side fields do not mean what the fill rules "
            "assume. See the side semantics check above.",
            "",
        ]
    else:
        lines.append(HEADLINE_HEADER)
        for c in summary.headline:
            lines.append(
                f"| {c.label} | {c.orders} | {c.games} | {c.fills} | {_pct(c.fill_rate)} "
                f"| {_est(c.clv_c)} | {_est(c.per_order)} | {_pct(c.x_share)} "
                f"| {_est(c.clv_q, False)} | {_est(c.clv_x, False)} | {_est(c.clv_t, False)} "
                f"| {_est(c.clv_a, False)} | {_est(c.taker, False)} | {_est(c.missed, False)} |"
            )
    lines += ["", f"**Verdict:** {summary.verdict}", ""]
    if not side_failed:
        lines += [f"## Breakdowns at edge >= {BREAKDOWN_EDGE:.1f}c", ""]
        for name, rows in summary.breakdowns.items():
            lines += [f"### By {name}", "", BREAKDOWN_HEADER]
            for c in rows:
                lines.append(
                    f"| {c.label} | {c.orders} | {c.games} | {c.fills} | {_pct(c.fill_rate)} "
                    f"| {_est(c.clv_c)} | {_est(c.per_order, False)} "
                    f"| {_est(c.taker, False)} |"
                )
            lines.append("")
        settle_rows = [c for c in summary.headline if c.settle_c.n]
        if settle_rows:
            lines += [
                "### Settlement P&L on C fills (noisy; for reference)",
                "",
                "| cohort | settle (95% CI) |",
                "|---|---|",
            ]
            lines += [f"| {c.label} | {_est(c.settle_c)} |" for c in settle_rows]
            lines.append("")
    b = summary.build
    lines += [
        "## Orders and tape",
        "",
        f"- Decision rows: {b['decisions']}; candidate orders: {b['candidate_orders']}; "
        f"scored: {b['orders_scored']}.",
        f"- Skipped decision rows: {_counts(b['skips'])}.",
        f"- Kickoff source: {_counts(b['kickoff_sources'])}.",
        f"- Unscored orders (tape): {_counts(b['orders_unscored'])}.",
        f"- Candidates dropped: {_counts(b['candidates_dropped'])}.",
    ]
    for key, value in {**summary.fetch, **summary.tape}.items():
        lines.append(f"- {key}: {value}")
    lines.append("")
    if summary.shadow_replay:
        lines += [
            "## maker_shadow replay",
            "",
            "| id | ticker | side | price | queue_ahead | stored fill_queue_at | replay | result |",
            "|--:|---|---|--:|--:|---|---|---|",
        ]
        for r in summary.shadow_replay:
            lines.append(
                f"| {r['id']} | {r['ticker']} | {r['side']} | {r['price']} "
                f"| {r['queue_ahead'] or '-'} | {r['stored_fill_queue_at'] or '-'} "
                f"| {r['replay_fill_queue_at'] or '-'} | {r['result']} |"
            )
        lines.append("")
    lines += ["## Limits", "", *LIMITS, ""]
    lines += [
        "## Rules",
        "",
        "- Q (queue): taker volume at or through our price reaches queue_ahead + our size.",
        "- X (run over): a trade strictly through our price, or a later snapshot whose ask "
        "for our side is at or below our price.",
        "- C (headline): the earlier of Q and X.",
        "- T (touch): the first taker trade at our price or better (optimistic).",
        "- A (always): filled at placement (no adverse selection; an upper bound).",
        "- taker: the same decisions taken at the ask, (T-5 mid - ask) x 100 - taker fee.",
        "- missed: clv_close of the orders that did not fill under C.",
        "- Block trades never count.",
        "",
    ]
    return "\n".join(lines)


def _counts(values: Mapping[str, Any]) -> str:
    return ", ".join(f"{k} {v}" for k, v in values.items()) or "none"


def _iso(value: datetime | None) -> str:
    return "" if value is None else value.isoformat()


def _num(value: Decimal | None, places: int = 4) -> str:
    if value is None:
        return ""
    return str(value.quantize(Decimal(1).scaleb(-places)))


ORDER_COLUMNS = (
    "decision_id",
    "position_key",
    "event_ticker",
    "league",
    "ticker",
    "side",
    "created_at",
    "book_at",
    "kickoff",
    "kickoff_source",
    "expires_at",
    "rule",
    "bid",
    "ask",
    "price",
    "fair",
    "size",
    "queue_ahead",
    "edge_c",
    "maker_fee_c",
    "taker_fee_c",
    "tape",
    "matched_volume",
    "q_at",
    "x_at",
    "x_source",
    "c_at",
    "t_at",
    "first_rule",
    "minutes_to_fill",
    "filled_c",
    "t5_mid",
    "clv_close",
    "settle",
    "taker",
)


def order_row(r: OrderResult) -> dict[str, str]:
    o, d, f = r.order, r.order.decision, r.fills
    minutes = (
        ""
        if f is None or f.c_at is None
        else str(
            Decimal(str((f.c_at - o.start).total_seconds() / 60)).quantize(
                CENT, rounding=ROUND_CEILING
            )
        )
    )
    return {
        "decision_id": str(d.id),
        "position_key": o.position_key,
        "event_ticker": o.event_ticker,
        "league": o.league,
        "ticker": o.ticker,
        "side": o.side.value,
        "created_at": _iso(o.start),
        "book_at": _iso(o.book_at),
        "kickoff": _iso(o.kickoff),
        "kickoff_source": o.kickoff_source,
        "expires_at": _iso(o.end),
        "rule": o.rule,
        "bid": _num(o.bid),
        "ask": _num(o.ask),
        "price": _num(o.price),
        "fair": _num(d.fair, 6),
        "size": str(o.size),
        "queue_ahead": str(o.queue_ahead),
        "edge_c": _num(o.edge_c),
        "maker_fee_c": _num(o.maker_fee_c),
        "taker_fee_c": _num(o.taker_fee_c),
        "tape": r.tape,
        "matched_volume": "" if f is None else str(f.matched_volume),
        "q_at": _iso(None if f is None else f.q_at),
        "x_at": _iso(None if f is None else f.x_at),
        "x_source": "" if f is None or f.x_source is None else f.x_source,
        "c_at": _iso(None if f is None else f.c_at),
        "t_at": _iso(None if f is None else f.t_at),
        "first_rule": "" if f is None or f.first_rule is None else f.first_rule,
        "minutes_to_fill": minutes,
        "filled_c": "1" if r.filled("C") else "0",
        "t5_mid": _num(r.t5_mid),
        "clv_close": _num(r.clv_close),
        "settle": _num(r.settle),
        "taker": _num(r.taker),
    }
