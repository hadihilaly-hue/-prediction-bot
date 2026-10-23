from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from prediction_bot.config import Settings
from prediction_bot.fees import FEE_MODEL_VERSION, maker_fee, taker_fee
from prediction_bot.models import Block, Market, OutcomeSide, Signal, fee_for
from prediction_bot.odds import ConsensusOdds
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
    halted: str | None = None  # reason no entries were taken this cycle, if any


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
        shadow_strategies: Sequence[Strategy] = (),
    ) -> None:
        self.settings = settings
        self.kalshi = kalshi
        self.strategies = list(strategies)
        self.ledger = ledger
        self.allow_live = allow_live
        self.shadow_strategies = list(shadow_strategies)

    @property
    def live(self) -> bool:
        return self.settings.is_live and self.allow_live

    def halt_reason(self) -> str | None:
        """Code-owned hard stops, checked before any strategy output is acted on."""
        if self.settings.kill_switch:
            return "kill switch is on (PBOT_KILL_SWITCH)"
        limit = self.settings.max_daily_loss
        if limit is None:
            return None
        day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        key = day.strftime("%Y-%m-%d")
        latched = self.ledger.halt_for_day(key)
        if latched:
            return f"{latched} (halted until {key} 23:59 UTC)"
        if self.live:
            # The paper ledger does not see live fills, so the daily-loss limit cannot be
            # measured in live mode yet; fail closed rather than trade with a dead stop.
            return "daily loss limit is set but live PnL is not tracked; refusing to trade live"
        today = self.ledger.worst_drawdown_since(day.replace(tzinfo=None))
        if today <= -limit:
            reason = f"daily loss limit hit: {today:.2f} <= -{limit:.2f}"
            self.ledger.record_halt(key, reason)
            return reason
        return None

    def run_cycle(self, markets: Sequence[Market]) -> CycleResult:
        res = CycleResult(markets_scanned=len(markets))
        for strat in self.strategies:
            try:
                res.signals.extend(strat.evaluate(markets))
            except Exception:  # keep other strategies alive
                log.exception("strategy %s failed", strat.name)
        for sig in res.signals:
            version = sig.rules_version or self.settings.rules_version
            self.ledger.record_shadow_decision(sig, version)
        for strat in self.strategies:
            get = getattr(strat, "seen_shadow_decisions", None)
            if get is not None:
                for sig in get():
                    self.ledger.record_shadow_decision(sig, "v2", reason="entry_window")
            get_line_moves = getattr(strat, "seen_line_move_decisions", None)
            if get_line_moves is not None:
                for sig in get_line_moves():
                    self.ledger.record_shadow_decision(sig, "v2", reason="line_move_follower")
        for strat in self.shadow_strategies:
            try:
                for sig in strat.evaluate(markets):
                    version = sig.rules_version or ("v2" if strat.name == "sportsbook_v2" else "v1")
                    self.ledger.record_shadow_decision(sig, version)
                get = getattr(strat, "seen_shadow_decisions", None)
                if get is not None:
                    for sig in get():
                        self.ledger.record_shadow_decision(sig, "v2", reason="entry_window")
                get_line_moves = getattr(strat, "seen_line_move_decisions", None)
                if get_line_moves is not None:
                    for sig in get_line_moves():
                        self.ledger.record_shadow_decision(sig, "v2", reason="line_move_follower")
            except Exception:
                log.exception("shadow strategy %s failed", strat.name)
        for method_name, write in (
            ("seen_blocks", self.ledger.record_blocks),
            ("seen_quotes", self.ledger.record_quote_snapshots),
            ("seen_edge_reviews", self.ledger.record_edge_reviews),
        ):
            items = [
                item
                for strat in self.strategies
                if (get := getattr(strat, method_name, None)) is not None
                for item in get()
            ]
            if items:
                try:
                    write(items)
                except Exception:
                    log.exception("failed to record %s", method_name)
        shadow_updates = [
            update
            for strat in self.strategies
            if (get := getattr(strat, "seen_maker_shadow_updates", None)) is not None
            for update in get()
        ]
        if shadow_updates:
            try:
                self.ledger.update_maker_shadows(shadow_updates)
            except Exception:
                log.exception("failed to update maker shadows")
        maker_shadows = [
            shadow
            for strat in self.strategies
            if (get := getattr(strat, "seen_maker_shadows", None)) is not None
            for shadow in get()
        ]
        if maker_shadows:
            try:
                self.ledger.record_maker_shadows(maker_shadows)
            except Exception:
                log.exception("failed to record maker shadows")
        # Snapshot every sportsbook consensus the strategies fetched this cycle, once
        # per (feed, event) — strategies sharing one odds client report the same data.
        seen: dict[str, dict[str, ConsensusOdds]] = {}
        for strat in self.strategies:
            get = getattr(strat, "seen_consensus", None)
            if get is None:
                continue
            for feed, events in get().items():
                seen.setdefault(feed, {}).update(events)
        if seen:
            try:
                self.ledger.record_consensus(seen)
            except Exception:  # a logging failure must never stop a cycle
                log.exception("failed to record odds snapshots")
        hit = {s.ticker for s in res.signals}
        self.ledger.remember_markets([m for m in markets if m.ticker in hit])
        res.halted = self.halt_reason()
        if res.halted:
            log.warning("HALTED, no entries: %s", res.halted)
            if self.live and self.settings.kill_switch:
                for o in self.kalshi.get_orders(status="resting"):
                    self.kalshi.cancel_order(str(o["order_id"]))
            for s in res.signals:
                self.ledger.record_signal(s, acted=False)
            return res
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

    def _exposure(self, sig: Signal) -> Decimal:
        """Open contracts on the signal's outcome, including equivalent legs on other tickers."""
        legs = ((sig.ticker, sig.side), *sig.equivalents)
        if self.live:
            return sum((self.kalshi.live_exposure(t, sd) for t, sd in legs), Decimal(0))
        return sum((self.ledger.position_count(t, sd, sig.strategy) for t, sd in legs), Decimal(0))

    def _admissible(self, sig: Signal, batch: Sequence[Signal]) -> bool:
        """Position cap and (paper) cash check, counting the whole batch as one action.

        Live exposure is per venue position; paper exposure is per strategy so that
        strategies run together keep independent, comparable books.
        """
        if sig.rules_version == "v2":
            reason = ""
            if self.live:
                reason = "paper_only"
            elif not sig.position_key or not sig.event_ticker:
                reason = "missing_position_key"
            elif any(held != sig.position_key for held in self.ledger.held_keys(sig.event_ticker)):
                reason = "opposite_position"
            elif sig.fee_type is None or sig.fee_multiplier is None:
                reason = "fee_unknown"
            elif sig.fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
                reason = "unknown_fee_type"
            else:
                try:
                    fee = self._fee_amount(sig, sig.limit_price, sig.size)
                except ValueError:
                    reason = "fee_unknown"
                else:
                    cost = sig.limit_price * sig.size + fee
                    bankroll = self.ledger.cash + sum(
                        (position.cost for position in self.ledger.positions()), Decimal(0)
                    )
                    utc_day = sig.created_at.astimezone(timezone.utc).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    if (
                        self.ledger.position_cost(sig.position_key) + cost
                        > bankroll * self.settings.v2_position_bankroll_fraction
                    ):
                        reason = "cap_position"
                    elif (
                        self.ledger.event_cost(sig.event_ticker) + cost
                        > bankroll * self.settings.v2_event_bankroll_fraction
                    ):
                        reason = "cap_event"
                    elif (
                        self.ledger.v2_new_risk_since(utc_day) + cost
                        > bankroll * self.settings.v2_daily_bankroll_fraction
                    ):
                        reason = "cap_day"
                    elif (
                        self.ledger.v2_total_open_cost() + cost
                        > bankroll * self.settings.v2_total_open_bankroll_fraction
                    ):
                        reason = "cap_total"
            if reason:
                self.ledger.record_blocks(
                    [
                        Block(
                            reason=reason,
                            strategy=sig.strategy,
                            ticker=sig.ticker,
                            event_ticker=sig.event_ticker or "",
                            detail=sig.position_key or "",
                            created_at=sig.created_at,
                        )
                    ]
                )
                log.info(
                    "BLOCK %s %s %s: %s",
                    sig.strategy,
                    sig.event_ticker,
                    reason,
                    sig.position_key,
                )
                return False
            if not self.live:
                cost = sig.limit_price * sig.size + self._fee_amount(sig, sig.limit_price, sig.size)
                if cost > self.ledger.cash:
                    return False
            return True
        legs = {(sig.ticker, sig.side), *sig.equivalents}
        same = [
            s
            for s in batch
            if (s.ticker, s.side) in legs and (self.live or s.strategy == sig.strategy)
        ]
        added = sum((s.size for s in same), Decimal(0))
        if self._exposure(sig) + added > self.settings.max_position_contracts:
            return False
        if not self.live:
            cost = sum(
                (
                    s.limit_price * s.size + fee_for(s.limit_price, s.size, self._fee_multiplier(s))
                    for s in batch
                ),
                Decimal(0),
            )
            if cost > self.ledger.cash:
                return False
        return True

    def _fee_multiplier(self, sig: Signal) -> Decimal:
        if sig.taker:
            return self.settings.taker_fee_multiplier
        return self.settings.maker_fee_multiplier

    def _fee_amount(self, sig: Signal, price: Decimal, count: Decimal) -> Decimal:
        if sig.rules_version != "v2":
            return fee_for(price, count, self._fee_multiplier(sig))
        if sig.fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
            raise ValueError(f"unknown fee type: {sig.fee_type}")
        if sig.fee_multiplier is None:
            raise ValueError("v2 signal has no series fee multiplier")
        if sig.taker:
            return taker_fee(price, count, sig.fee_multiplier, self.settings.fee_rounding)
        return maker_fee(
            price,
            count,
            sig.fee_multiplier,
            sig.fee_type,
            self.settings.fee_rounding,
        )

    def _act(self, sig: Signal, res: CycleResult, grouped: bool = False) -> bool:
        if self.live:
            # Grouped legs go fill-or-kill so a leg can never rest half-hedged; a
            # killed leg is reported as not acted and the rest of the group is skipped.
            tif = "fill_or_kill" if grouped else "good_till_canceled"
            try:
                order = self.kalshi.create_order(
                    sig.ticker,
                    sig.side,
                    sig.size,
                    sig.limit_price,
                    time_in_force=tif,
                    # A maker-priced signal must rest or die: if the book moved, paying
                    # taker fees at the crossed price loses the edge we measured.
                    post_only=not sig.taker,
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
        try:
            fee = self._fee_amount(sig, sig.limit_price, sig.size)
        except ValueError as exc:
            reason = "unknown_fee_type" if "unknown fee type" in str(exc) else "fee_unknown"
            self.ledger.record_blocks(
                [
                    Block(
                        reason=reason,
                        strategy=sig.strategy,
                        ticker=sig.ticker,
                        event_ticker=sig.event_ticker or "",
                        detail=str(exc),
                        created_at=sig.created_at,
                    )
                ]
            )
            self.ledger.record_signal(sig, acted=False)
            return False
        try:
            self.ledger.record_fill(
                sig.strategy,
                sig.ticker,
                sig.side,
                sig.limit_price,
                sig.size,
                fee,
                sig.fair_prob,
                rules_version=sig.rules_version,
                position_key=sig.position_key,
                event_ticker=sig.event_ticker,
                tags=sig.tags,
                commence_time=sig.commence_time,
                fee_model_version=FEE_MODEL_VERSION if sig.rules_version == "v2" else "v1-flat",
            )
        except ValueError as e:
            log.warning("skip %s: %s", sig.ticker, e)
            self.ledger.record_signal(sig, acted=False)
            return False
        self.ledger.record_signal(sig, acted=True)
        self.ledger.mark_edge_review_traded(sig)
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
