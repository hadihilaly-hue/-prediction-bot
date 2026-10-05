from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_UP, Decimal
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

    def ask_for(self, side: OutcomeSide) -> Decimal | None:
        """Price you pay to buy one contract of `side`."""
        if side is OutcomeSide.yes:
            return self.yes_ask
        return None if self.yes_bid is None else Decimal(1) - self.yes_bid

    def ask_size_for(self, side: OutcomeSide) -> Decimal:
        return self.yes_ask_size if side is OutcomeSide.yes else self.yes_bid_size


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
    created_at: datetime = field(default_factory=datetime.utcnow)


def fee_for(price: Decimal, count: Decimal, multiplier: Decimal) -> Decimal:
    """Kalshi-style fee: ceil_to_cent(mult * C * P * (1 - P))."""
    raw = multiplier * count * price * (Decimal(1) - price)
    return raw.quantize(ONE_CENT, rounding=ROUND_UP)
