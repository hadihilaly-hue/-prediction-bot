from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

ONE_CENT = Decimal("0.01")


class OutcomeSide(str, Enum):
    yes = "yes"
    no = "no"

    def opposite(self) -> OutcomeSide:
        return OutcomeSide.no if self is OutcomeSide.yes else OutcomeSide.yes


@dataclass(frozen=True)
class PriceLevel:
    price: Decimal  # dollars, 0-1
    size: Decimal  # contracts


@dataclass(frozen=True)
class Quote:
    """Best prices for a binary market, expressed in YES-dollar terms."""

    ticker: str
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    yes_bid_size: Decimal = Decimal(0)
    yes_ask_size: Decimal = Decimal(0)

    @property
    def mid(self) -> Decimal | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def spread(self) -> Decimal | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    def mid_for(self, side: OutcomeSide) -> Decimal | None:
        """The side's mid: the YES mid for YES, one minus it for NO."""
        mid = self.mid
        if mid is None:
            return None
        return mid if side is OutcomeSide.yes else Decimal(1) - mid

    def ask_for(self, side: OutcomeSide) -> Decimal | None:
        """Price you pay to buy one contract of `side` right now (crossing the spread)."""
        if side is OutcomeSide.yes:
            return self.yes_ask
        return None if self.yes_bid is None else Decimal(1) - self.yes_bid

    def bid_for(self, side: OutcomeSide) -> Decimal | None:
        """Best resting bid for `side` — the level a maker buy posts above."""
        if side is OutcomeSide.yes:
            return self.yes_bid
        return None if self.yes_ask is None else Decimal(1) - self.yes_ask

    def ask_size_for(self, side: OutcomeSide) -> Decimal:
        return self.yes_ask_size if side is OutcomeSide.yes else self.yes_bid_size

    def bid_size_for(self, side: OutcomeSide) -> Decimal:
        return self.yes_bid_size if side is OutcomeSide.yes else self.yes_ask_size


@dataclass(frozen=True)
class Market:
    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    status: str
    close_time: datetime | None
    quote: Quote
    expected_expiration: datetime | None = None  # ~ event end (game finish) for sports
    volume: Decimal = Decimal(0)
    open_interest: Decimal = Decimal(0)
    result: str = ""  # "yes" / "no" / "" when unsettled
    category: str = ""
    raw: dict[str, object] = field(default_factory=dict, repr=False, compare=False)
    last_price: Decimal | None = None

    @property
    def is_settled(self) -> bool:
        return self.result in ("yes", "no")


@dataclass(frozen=True)
class Signal:
    """A strategy's opinion: buy `side` of `ticker` at up to `limit_price`."""

    strategy: str
    ticker: str
    side: OutcomeSide
    fair_prob: Decimal  # strategy's probability that `side` wins
    limit_price: Decimal  # max price to pay for `side`
    edge: Decimal  # fair_prob - limit_price - fees (prob points)
    size: Decimal  # contracts
    rationale: str = ""
    group: str | None = None  # legs sharing a group are acted on all-or-nothing
    # other (ticker, side) legs that pay out on the same outcome; exposure counts them too
    equivalents: tuple[tuple[str, OutcomeSide], ...] = ()
    # False when limit_price is a resting (maker) quote inside the spread: fees are
    # charged at the maker rate and the order only fills when someone trades into it.
    taker: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    rules_version: str | None = None
    position_key: str | None = None
    event_ticker: str | None = None
    tags: tuple[str, ...] | None = None
    commence_time: datetime | None = None
    fee_type: str | None = None
    fee_multiplier: Decimal | None = None
    fair_pre_haircut: Decimal | None = None
    available_size: Decimal | None = None
    open_interest: Decimal | None = None
    spread: Decimal | None = None
    devig_json: str | None = None
    reference_source: str | None = None
    book_count: int | None = None
    book_spread: Decimal | None = None
    book_probs_json: str | None = None
    maker_price: Decimal | None = None
    # The side's Kalshi mid when the decision was made (scoreboard: favoured side).
    mid: Decimal | None = None


@dataclass(frozen=True)
class Trade:
    """One public Kalshi trade. `taker_side` is the side the taker bought, so a
    `no` taker trade at `yes_price` hits resting YES bids at or above it."""

    ticker: str
    created_at: datetime
    count: Decimal
    yes_price: Decimal
    taker_side: OutcomeSide


@dataclass(frozen=True)
class MakerShadowCandidate:
    created_at: datetime
    ticker: str
    side: OutcomeSide
    event_ticker: str
    position_key: str
    price: Decimal
    size: Decimal
    fair: Decimal
    maker_fee: Decimal
    expires_at: datetime
    commence_time: datetime | None = None
    rules_version: str = "v2"
    # v5 Phase 4: contracts displayed ahead of us at `price` on our side when placed
    # (0 if the price improves the book; None if no book was seen), and how the
    # price was chosen: `bid_plus_1` (spread >= 2c) or `at_bid` (1c spread).
    queue_ahead: Decimal | None = None
    maker_price_rule: str | None = None
    # PR 2: the taker gates that would have blocked this side (`blocked_net_edge`, ...,
    # or `taker_pass`) and what created it (`maker_feed`).
    tags: tuple[str, ...] | None = None
    origin: str | None = None


@dataclass(frozen=True)
class MakerShadowUpdate:
    at: datetime
    ticker: str
    side: OutcomeSide
    ask: Decimal | None
    last_price: Decimal | None
    fair: Decimal | None


@dataclass(frozen=True)
class Block:
    reason: str
    strategy: str
    ticker: str
    event_ticker: str
    detail: str = ""
    rules_version: str = "v2"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    position_key: str | None = None


@dataclass(frozen=True)
class QuoteSnapshot:
    ticker: str
    event_ticker: str
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    last_price: Decimal | None
    volume: Decimal
    open_interest: Decimal
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class EdgeReview:
    ticker: str
    side: OutcomeSide
    fair_prob: Decimal
    price: Decimal
    gross_edge: Decimal
    refetched_price: Decimal | None
    refetched_fair: Decimal | None
    confirmed: bool
    whitelisted: bool
    traded: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    position_key: str | None = None
