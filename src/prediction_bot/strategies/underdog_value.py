from __future__ import annotations

from dataclasses import replace
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

    def _both_sides(
        self, m: Market, fair: Decimal, rationale: str, twin: Market | None = None
    ) -> list[Signal]:
        lo, hi = self.settings.underdog_min_price, self.settings.underdog_max_price
        out: list[Signal] = []
        for side, side_fair in ((OutcomeSide.yes, fair), (OutcomeSide.no, Decimal(1) - fair)):
            eq = ((twin.ticker, side.opposite()),) if twin else ()
            sig = self.make_signal(m, side, side_fair, rationale, equivalents=eq)
            if sig is None or not (lo <= sig.limit_price <= hi):
                continue  # the entry price we'd actually pay must sit in the band
            payout = (Decimal(1) / sig.limit_price).quantize(Decimal("0.1"))
            out.append(replace(sig, rationale=f"underdog ({payout}x payout); {rationale}"))
        return out
