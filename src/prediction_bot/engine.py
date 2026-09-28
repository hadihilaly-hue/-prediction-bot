from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from prediction_bot.config import Settings
from prediction_bot.models import Market, OutcomeSide, Signal, fee_for
from prediction_bot.paper.ledger import Ledger
from prediction_bot.strategies.base import Strategy
from prediction_bot.venues.kalshi import KalshiClient, KalshiError

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
        hit = {s.ticker for s in res.signals}
        self.ledger.remember_markets([m for m in markets if m.ticker in hit])
        for batch in self._batches(res.signals):
            if not all(self._admissible(s, batch) for s in batch):
                for s in batch:
                    self.ledger.record_signal(s, acted=False)
                continue
            for sig in batch:
                if self._act(sig, res, grouped=len(batch) > 1):
                    res.acted.append(sig)
                elif self.live and len(batch) > 1:
                    done = [s for s in batch if s in res.acted]
                    if done:
                        log.error(
                            "UNHEDGED: leg %s/%s failed after %d filled leg(s) of group %s",
                            sig.ticker,
                            sig.side.value,
                            len(done),
                            sig.group,
                        )
                    break
        return res

    @staticmethod
    def _batches(signals: Sequence[Signal]) -> list[list[Signal]]:
        """Group legs that must be acted on together; ungrouped signals stand alone."""
        groups: dict[str, list[Signal]] = {}
        out: list[list[Signal]] = []
        for s in signals:
            if s.group is None:
                out.append([s])
            elif s.group in groups:
                groups[s.group].append(s)
            else:
                groups[s.group] = [s]
                out.append(groups[s.group])
        return out

    def _exposure(self, ticker: str, side: OutcomeSide) -> Decimal:
        if self.live:
            return self.kalshi.live_exposure(ticker, side)
        return self.ledger.position_count(ticker, side)

    def _admissible(self, sig: Signal, batch: Sequence[Signal]) -> bool:
        """Position cap and (paper) cash check, counting the whole batch as one action."""
        same = [s for s in batch if s.ticker == sig.ticker and s.side == sig.side]
        added = sum((s.size for s in same), Decimal(0))
        if self._exposure(sig.ticker, sig.side) + added > self.settings.max_position_contracts:
            return False
        if not self.live:
            cost = sum(
                (
                    s.limit_price * s.size
                    + fee_for(s.limit_price, s.size, self.settings.taker_fee_multiplier)
                    for s in batch
                ),
                Decimal(0),
            )
            if cost > self.ledger.cash:
                return False
        return True

    def _act(self, sig: Signal, res: CycleResult, grouped: bool = False) -> bool:
        if self.live:
            # Grouped legs go fill-or-kill so a leg can never rest half-hedged; a
            # killed leg is reported as not acted and the rest of the group is skipped.
            tif = "fill_or_kill" if grouped else "good_till_canceled"
            try:
                order = self.kalshi.create_order(
                    sig.ticker, sig.side, sig.size, sig.limit_price, time_in_force=tif
                )
            except KalshiError as e:
                log.warning("live order rejected %s/%s: %s", sig.ticker, sig.side.value, e)
                self.ledger.record_signal(sig, acted=False)
                return False
            detail = order.get("order", order)
            res.live_orders.append(order)
            if grouped and isinstance(detail, dict) and detail.get("status") != "executed":
                log.warning("FOK leg not filled %s/%s: %s", sig.ticker, sig.side.value, detail)
                self.ledger.record_signal(sig, acted=False)
                return False
            self.ledger.record_signal(sig, acted=True)
            log.info("LIVE order %s", detail)
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
        """Look up every ticker we hold or predicted; settle those Kalshi has resolved.

        Also stores titles for any ledger rows (including settled ones) still missing one.
        """
        out: dict[str, Decimal] = {}
        unsettled = set(self.ledger.unsettled_tickers())
        tickers = sorted(unsettled | set(self.ledger.untitled_tickers()))
        for i in range(0, len(tickers), 50):
            batch = self.kalshi.get_markets(status=None, tickers=tickers[i : i + 50])
            self.ledger.remember_markets(batch)
            for m in batch:
                if m.is_settled and m.ticker in unsettled:
                    out[m.ticker] = self.ledger.settle(m.ticker, OutcomeSide(m.result))
        return out
