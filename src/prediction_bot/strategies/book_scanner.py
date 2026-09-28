from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal

from prediction_bot.models import Market, OutcomeSide, Signal, fee_for
from prediction_bot.strategies.base import Strategy


class BookScannerStrategy(Strategy):
    """Structural-mispricing scanner (no external model).

    Flags a market when buying YES *and* NO at their asks costs less than $1 after
    fees (a locked, risk-free profit; rare but appears on thin demo books), and
    reports it as two signals. Also useful as a liveness check for the pipeline.
    """

    name = "book_scanner"

    def evaluate(self, markets: Sequence[Market]) -> list[Signal]:
        out: list[Signal] = []
        for m in markets:
            q = m.quote
            yes_ask, no_ask = q.ask_for(OutcomeSide.yes), q.ask_for(OutcomeSide.no)
            if yes_ask is None or no_ask is None:
                continue
            total = yes_ask + no_ask
            if total >= 1:
                continue
            # Both legs pay $1 on exactly one side; treat each leg's fair value as 1 - other ask.
            # Legs must be equal-sized and filled together or the position is not locked.
            rationale = f"yes_ask+no_ask={total:.4f} < 1"
            size = min(q.ask_size_for(OutcomeSide.yes), q.ask_size_for(OutcomeSide.no))
            legs = [
                self.make_signal(
                    m, side, Decimal(1) - other, rationale, max_size=size, group=m.ticker
                )
                for side, other in ((OutcomeSide.yes, no_ask), (OutcomeSide.no, yes_ask))
            ]
            if not all(legs):
                continue
            pair = [s for s in legs if s]
            size = min(s.size for s in pair)
            # The pair is only locked if the gap covers *both* legs' fees (rounded up per
            # leg exactly as the engine books them), not each fee against the full gap.
            mult = self.settings.taker_fee_multiplier
            fees = sum((fee_for(s.limit_price, size, mult) for s in pair), Decimal(0))
            pair_edge = (size * (Decimal(1) - total) - fees) / size
            if pair_edge < self.settings.min_edge:
                continue
            out.extend(replace(s, size=size, edge=pair_edge) for s in pair)
        return out
