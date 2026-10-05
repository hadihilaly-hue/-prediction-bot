from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from decimal import Decimal

from prediction_bot.config import Settings
from prediction_bot.models import Market, OutcomeSide, Signal, fee_for


class Strategy(ABC):
    """A strategy turns a snapshot of markets into buy signals.

    Subclasses estimate `fair_prob` for a side; `make_signal` converts that into an
    order sized by edge after fees and the configured risk limits.
    """

    name: str = "base"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    def evaluate(self, markets: Sequence[Market]) -> list[Signal]: ...

    def make_signal(
        self,
        market: Market,
        side: OutcomeSide,
        fair_prob: Decimal,
        rationale: str,
        taker: bool = True,
        max_size: Decimal | None = None,
        group: str | None = None,
        check_edge: bool = True,
        equivalents: tuple[tuple[str, OutcomeSide], ...] = (),
    ) -> Signal | None:
        """Build a signal to buy `side` at the current ask if edge clears `min_edge`.

        `check_edge=False` skips the per-contract threshold so callers that evaluate a
        multi-leg position as a whole (with size-aware fees) can apply their own.
        """
        ask = market.quote.ask_for(side)
        if ask is None or ask <= 0 or ask >= 1:
            return None
        s = self.settings
        mult = s.taker_fee_multiplier if taker else s.maker_fee_multiplier
        fee_per_contract = fee_for(ask, Decimal(1), mult)
        edge = fair_prob - ask - fee_per_contract
        if check_edge and edge < s.min_edge:
            return None
        size = self._size(ask, market.quote.ask_size_for(side))
        if max_size is not None:
            size = min(size, max_size)
        if size <= 0:
            return None
        return Signal(
            strategy=self.name,
            ticker=market.ticker,
            side=side,
            fair_prob=fair_prob,
            limit_price=ask,
            edge=edge,
            size=size,
            rationale=rationale,
            group=group,
            equivalents=equivalents,
        )

    def _size(self, price: Decimal, available: Decimal) -> Decimal:
        by_notional = (self.settings.max_order_notional / price).to_integral_value(
            rounding="ROUND_DOWN"
        )
        cap = min(by_notional, self.settings.max_position_contracts)
        cap = min(cap, available.to_integral_value(rounding="ROUND_DOWN"))
        return max(cap, Decimal(0))
