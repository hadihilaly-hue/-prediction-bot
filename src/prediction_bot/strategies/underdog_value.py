from __future__ import annotations

from decimal import Decimal

from prediction_bot.models import Market, OutcomeSide, Signal
from prediction_bot.strategies.sportsbook_arb import SportsbookArbStrategy


class UnderdogValueStrategy(SportsbookArbStrategy):
    """Sportsbook-consensus value bets restricted to the underdog price band.

    Same fair-value model as `sportsbook_arb` (de-vigged consensus of several books), but
    it only buys sides priced inside [`underdog_min_price`, `underdog_max_price`]: a side
    at 25c pays 4:1, so a modest edge there is worth far more per dollar than the same
    edge on a 95c favourite, and favourites that are "basically certain" are skipped
    entirely. The edge requirement still applies -- an underdog is only bought when the
    books say it is more likely than Kalshi's price implies.
    """

    name = "underdog_value"

    def _both_sides(self, m: Market, fair: Decimal, rationale: str) -> list[Signal]:
        lo, hi = self.settings.underdog_min_price, self.settings.underdog_max_price
        out: list[Signal] = []
        for side, side_fair in ((OutcomeSide.yes, fair), (OutcomeSide.no, Decimal(1) - fair)):
            ask = m.quote.ask_for(side)
            if ask is None or not (lo <= ask <= hi):
                continue
            payout = (Decimal(1) / ask).quantize(Decimal("0.1"))
            sig = self.make_signal(m, side, side_fair, f"underdog ({payout}x payout); {rationale}")
            if sig:
                out.append(sig)
        return out
