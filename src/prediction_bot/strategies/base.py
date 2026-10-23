from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from decimal import Decimal

from prediction_bot.config import Settings
from prediction_bot.models import ONE_CENT, Market, OutcomeSide, Signal, fee_for


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
        taker: bool | None = None,
        max_size: Decimal | None = None,
        group: str | None = None,
        check_edge: bool = True,
        check_depth: bool = True,
        equivalents: tuple[tuple[str, OutcomeSide], ...] = (),
    ) -> Signal | None:
        """Build a signal to buy `side` if edge at the entry price clears `min_edge`.

        `taker=None` follows `settings.entry_style`: maker posts at bid+1c inside the
        spread when it exists (and falls back to the ask when it doesn't), taker pays
        the ask. `check_edge=False` skips the per-contract threshold so callers that
        evaluate a multi-leg position as a whole (with size-aware fees) can apply
        their own.
        """
        ask = market.quote.ask_for(side)
        if ask is None or ask <= 0 or ask >= 1:
            return None
        s = self.settings
        if taker is None:
            taker = s.entry_style == "taker"
        price = ask
        if not taker:
            bid = market.quote.bid_for(side)
            if bid is not None and bid > 0 and bid + ONE_CENT < ask:
                price = bid + ONE_CENT
            else:
                taker = True  # no room inside the spread: our resting price IS the ask
        # Liquidity gate: the level we'd hit (taker) or join (maker) must not be a
        # dead quote. A maker order isn't bounded by depth, so it isn't sized down.
        if check_depth:
            depth = market.quote.ask_size_for(side) if taker else market.quote.bid_size_for(side)
            if depth < s.min_book_depth:
                return None
        mult = s.taker_fee_multiplier if taker else s.maker_fee_multiplier
        fee_per_contract = fee_for(price, Decimal(1), mult)
        edge = fair_prob - price - fee_per_contract
        if check_edge and edge < s.min_edge:
            return None
        size = self._size(price, market.quote.ask_size_for(side) if taker else Decimal("1e9"))
        if max_size is not None:
            size = min(size, max_size)
        if size <= 0:
            return None
        return Signal(
            strategy=self.name,
            ticker=market.ticker,
            side=side,
            fair_prob=fair_prob,
            limit_price=price,
            edge=edge,
            size=size,
            rationale=rationale,
            group=group,
            equivalents=equivalents,
            taker=taker,
        )

    def _size(self, price: Decimal, available: Decimal) -> Decimal:
        by_notional = (self.settings.max_order_notional / price).to_integral_value(
            rounding="ROUND_DOWN"
        )
        cap = min(by_notional, self.settings.max_position_contracts)
        cap = min(cap, available.to_integral_value(rounding="ROUND_DOWN"))
        return max(cap, Decimal(0))
