from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from prediction_bot.models import Market, OutcomeSide, Signal
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
            # Both legs pay $1 on exactly one side; treat each leg's fair value as 1 - other ask
            rationale = f"yes_ask+no_ask={total:.4f} < 1"
            for side, other in ((OutcomeSide.yes, no_ask), (OutcomeSide.no, yes_ask)):
                sig = self.make_signal(m, side, Decimal(1) - other, rationale)
                if sig:
                    out.append(sig)
        return out
