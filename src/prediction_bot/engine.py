from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from prediction_bot.config import Settings
from prediction_bot.models import Market, OutcomeSide, Signal, fee_for
from prediction_bot.paper.ledger import Ledger
from prediction_bot.strategies.base import Strategy
from prediction_bot.venues.kalshi import KalshiClient

log = logging.getLogger(__name__)


@dataclass
class CycleResult:
    markets_scanned: int = 0
    signals: list[Signal] = field(default_factory=list)
    acted: list[Signal] = field(default_factory=list)
    settled: dict[str, Decimal] = field(default_factory=dict)
    live_orders: list[dict[str, object]] = field(default_factory=list)


class Engine:
    """Runs strategies over a market snapshot, then either paper-fills at the ask or
    (only when `settings.mode == live` AND `allow_live=True`) sends real limit orders."""

    def __init__(
        self,
        settings: Settings,
        kalshi: KalshiClient,
        strategies: Sequence[Strategy],
        ledger: Ledger,
        allow_live: bool = False,
    ) -> None:
        self.settings = settings
        self.kalshi = kalshi
        self.strategies = list(strategies)
        self.ledger = ledger
        self.allow_live = allow_live

    @property
    def live(self) -> bool:
        return self.settings.is_live and self.allow_live

    def run_cycle(self, markets: Sequence[Market]) -> CycleResult:
        res = CycleResult(markets_scanned=len(markets))
        for strat in self.strategies:
            try:
                res.signals.extend(strat.evaluate(markets))
            except Exception:  # keep other strategies alive
                log.exception("strategy %s failed", strat.name)
        for sig in res.signals:
            if self._act(sig, res):
                res.acted.append(sig)
        return res

    def _act(self, sig: Signal, res: CycleResult) -> bool:
        held = self.ledger.position_count(sig.ticker, sig.side)
        if held + sig.size > self.settings.max_position_contracts:
            self.ledger.record_signal(sig, acted=False)
            return False
        if self.live:
            order = self.kalshi.create_order(sig.ticker, sig.side, sig.size, sig.limit_price)
            res.live_orders.append(order)
            self.ledger.record_signal(sig, acted=True)
            log.info("LIVE order %s", order.get("order", order))
            return True
        fee = fee_for(sig.limit_price, sig.size, self.settings.taker_fee_multiplier)
        try:
            self.ledger.record_fill(
                sig.strategy, sig.ticker, sig.side, sig.limit_price, sig.size, fee, sig.fair_prob
            )
        except ValueError as e:
            log.warning("skip %s: %s", sig.ticker, e)
            self.ledger.record_signal(sig, acted=False)
            return False
        self.ledger.record_signal(sig, acted=True)
        return True

    def settle_open_positions(self) -> dict[str, Decimal]:
        """Look up every ticker we hold; settle those Kalshi has resolved."""
        out: dict[str, Decimal] = {}
        tickers = self.ledger.open_tickers()
        for i in range(0, len(tickers), 50):
            for m in self.kalshi.get_markets(status=None, tickers=tickers[i : i + 50]):
                if m.is_settled:
                    out[m.ticker] = self.ledger.settle(m.ticker, OutcomeSide(m.result))
        return out
