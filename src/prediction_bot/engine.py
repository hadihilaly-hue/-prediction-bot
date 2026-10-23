from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from prediction_bot.candles import fetch_candles
from prediction_bot.config import Settings
from prediction_bot.fees import FEE_MODEL_VERSION, maker_fee, taker_fee
from prediction_bot.maker_fill import trade_through_fill
from prediction_bot.models import Block, Market, OutcomeSide, Signal, fee_for
from prediction_bot.odds import KALSHI_SERIES_TO_SPORT, ConsensusOdds
from prediction_bot.paper.ledger import Ledger
from prediction_bot.strategies.base import Strategy
from prediction_bot.venues.kalshi import KalshiClient, KalshiError

log = logging.getLogger(__name__)


@dataclass
class CycleResult:
    markets_scanned: int = 0
    signals: list[Signal] = field(default_factory=list)
    acted: list[Signal] = field(default_factory=list)
    orders_placed: list[Signal] = field(default_factory=list)
    paper_fills_resolved: int = 0
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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.kalshi = kalshi
        self.strategies = list(strategies)
        self.ledger = ledger
        self.allow_live = allow_live
        self.shadow_strategies = list(shadow_strategies)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

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
        if self._maker_first_enabled():
            res.paper_fills_resolved = self._resolve_paper_orders(self.clock())
        for strat in self.strategies:
            try:
                res.signals.extend(strat.evaluate(markets))
            except Exception:  # keep other strategies alive
                log.exception("strategy %s failed", strat.name)
        for sig in res.signals:
            if sig.strategy != "book_scanner" and sig.rules_version is not None:
                self.ledger.record_shadow_decision(sig, sig.rules_version)
        for strat in self.strategies:
            get = getattr(strat, "seen_shadow_decisions", None)
            if get is not None:
                for sig in get():
                    if sig.strategy != "book_scanner":
                        self.ledger.record_shadow_decision(sig, "v2", reason="entry_window")
            get_line_moves = getattr(strat, "seen_line_move_decisions", None)
            if get_line_moves is not None:
                for sig in get_line_moves():
                    if sig.strategy != "book_scanner":
                        self.ledger.record_shadow_decision(sig, "v2", reason="line_move_follower")
        for strat in self.shadow_strategies:
            try:
                for sig in strat.evaluate(markets):
                    if sig.strategy != "book_scanner":
                        version = sig.rules_version or (
                            "v2" if strat.name == "sportsbook_v2" else "v1"
                        )
                        self.ledger.record_shadow_decision(sig, version)
                get = getattr(strat, "seen_shadow_decisions", None)
                if get is not None:
                    for sig in get():
                        if sig.strategy != "book_scanner":
                            self.ledger.record_shadow_decision(sig, "v2", reason="entry_window")
                get_line_moves = getattr(strat, "seen_line_move_decisions", None)
                if get_line_moves is not None:
                    for sig in get_line_moves():
                        if sig.strategy != "book_scanner":
                            self.ledger.record_shadow_decision(
                                sig, "v2", reason="line_move_follower"
                            )
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
                self.ledger.update_maker_shadows(
                    shadow_updates, v3_cancel_move=self.settings.v3_maker_cancel_move
                )
                if self._maker_first_enabled():
                    self.ledger.update_paper_orders_for_fair(
                        shadow_updates, self.settings.v3_maker_cancel_move
                    )
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
                    if sig not in res.orders_placed:
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

    def _maker_first_enabled(self) -> bool:
        return (
            not self.live
            and self.settings.v3_execution == "maker_first"
            and any(
                getattr(strategy, "rules_version", None) == "v3" for strategy in self.strategies
            )
        )

    def _resolve_paper_orders(self, now: datetime) -> int:
        now_utc = (
            now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
        )
        filled = 0
        for order in self.ledger.open_paper_orders():
            end = min(now_utc, order.expires_at)
            if end <= order.created_at:
                if now_utc >= order.expires_at:
                    self.ledger.expire_paper_order(order.id, order.expires_at)
                continue
            try:
                candles = fetch_candles(self.kalshi, order.ticker, order.created_at, end)
            except Exception:
                log.exception("maker candle fetch failed for %s; leaving order open", order.ticker)
                continue
            fill_at = trade_through_fill(candles, order.side, order.price, order.created_at, end)
            if fill_at is not None:
                if self.ledger.fill_paper_order(order.id, fill_at):
                    filled += 1
            elif now_utc >= order.expires_at:
                self.ledger.expire_paper_order(order.id, order.expires_at)
        return filled

    def _v3_paper_action(self, sig: Signal, grouped: bool = False) -> str:
        if self.live or sig.rules_version != "v3" or self.settings.v3_execution != "maker_first":
            return "taker"
        if sig.position_key is None:
            return "none"
        latest = self.ledger.latest_paper_order(sig.position_key)
        if latest is not None and latest.status == "open":
            return "resting"
        if latest is not None and latest.status == "filled" and self.settings.v3_no_reentry:
            return "held"
        taker_edge_holds = sig.edge >= self.settings.v2_min_net_edge
        maker_is_post_only = (
            not grouped and sig.maker_price is not None and sig.maker_price < sig.limit_price
        )
        if latest is not None and latest.status in {"expired", "cancelled"}:
            if taker_edge_holds:
                return "taker"
            return "maker" if maker_is_post_only else "none"
        if latest is not None and latest.status == "filled":
            return "maker" if maker_is_post_only else "taker" if taker_edge_holds else "none"
        if maker_is_post_only:
            return "maker"
        return "taker" if taker_edge_holds else "none"

    def _paper_order_expiry(self, sig: Signal) -> datetime:
        expires_at = sig.created_at + timedelta(minutes=self.settings.v3_maker_ttl_minutes)
        if sig.commence_time is None:
            return expires_at
        series = (sig.event_ticker or sig.ticker).split("-", 1)[0]
        sport = KALSHI_SERIES_TO_SPORT.get(series, "")
        buffer = self.settings.kickoff_buffer_minutes.get(
            sport, self.settings.kickoff_buffer_default_minutes
        )
        return min(expires_at, sig.commence_time - timedelta(minutes=buffer))

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
        if sig.rules_version in {"v2", "v3"}:
            reason = ""
            action = self._v3_paper_action(sig, grouped=len(batch) > 1)
            if self.live:
                reason = "paper_only"
            elif not sig.position_key or not sig.event_ticker:
                reason = "missing_position_key"
            elif action == "resting":
                reason = "order_resting"
            elif action == "held" or (
                sig.rules_version == "v3"
                and self.settings.v3_no_reentry
                and self.ledger.has_v3_position_activity(sig.position_key or "", sig.created_at)
            ):
                reason = "already_held"
            elif action == "none":
                reason = "net_edge"
            elif any(
                held != sig.position_key
                for held in self.ledger.held_keys(
                    sig.event_ticker, include_v3=sig.rules_version == "v3"
                )
            ):
                reason = "opposite_position"
            elif sig.fee_type is None or sig.fee_multiplier is None:
                reason = "fee_unknown"
            elif sig.fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
                reason = "unknown_fee_type"
            else:
                cost_price = (
                    sig.maker_price
                    if action == "maker" and sig.maker_price is not None
                    else sig.limit_price
                )
                try:
                    fee = (
                        maker_fee(
                            cost_price,
                            sig.size,
                            sig.fee_multiplier or Decimal(0),
                            sig.fee_type or "",
                            self.settings.fee_rounding,
                        )
                        if action == "maker"
                        else self._fee_amount(sig, cost_price, sig.size)
                    )
                except ValueError:
                    reason = "fee_unknown"
                else:
                    cost = cost_price * sig.size + fee
                    bankroll = self.ledger.cash + sum(
                        (position.cost for position in self.ledger.positions()), Decimal(0)
                    )
                    utc_day = sig.created_at.astimezone(timezone.utc).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    if (
                        self.ledger.position_cost(
                            sig.position_key, include_v3=sig.rules_version == "v3"
                        )
                        + cost
                        > bankroll * self.settings.v2_position_bankroll_fraction
                    ):
                        reason = "cap_position"
                    elif (
                        self.ledger.event_cost(
                            sig.event_ticker, include_v3=sig.rules_version == "v3"
                        )
                        + cost
                        > bankroll * self.settings.v2_event_bankroll_fraction
                    ):
                        reason = "cap_event"
                    elif (
                        self.ledger.v2_new_risk_since(utc_day, include_v3=sig.rules_version == "v3")
                        + cost
                        > bankroll * self.settings.v2_daily_bankroll_fraction
                    ):
                        reason = "cap_day"
                    elif (
                        self.ledger.v2_total_open_cost(include_v3=sig.rules_version == "v3") + cost
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
                action = self._v3_paper_action(sig, grouped=len(batch) > 1)
                cost_price = (
                    sig.maker_price
                    if action == "maker" and sig.maker_price is not None
                    else sig.limit_price
                )
                fee = (
                    maker_fee(
                        cost_price,
                        sig.size,
                        sig.fee_multiplier or Decimal(0),
                        sig.fee_type or "",
                        self.settings.fee_rounding,
                    )
                    if action == "maker"
                    else self._fee_amount(sig, cost_price, sig.size)
                )
                cost = cost_price * sig.size + fee
                if cost > self.ledger.available_cash(include_v3=sig.rules_version == "v3"):
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
        if sig.rules_version not in {"v2", "v3"}:
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
        paper_action = self._v3_paper_action(sig, grouped=grouped)
        if paper_action == "maker":
            assert sig.maker_price is not None
            try:
                fee = maker_fee(
                    sig.maker_price,
                    sig.size,
                    sig.fee_multiplier or Decimal(0),
                    sig.fee_type or "",
                    self.settings.fee_rounding,
                )
                self.ledger.record_paper_order(
                    sig,
                    sig.maker_price,
                    fee,
                    self._paper_order_expiry(sig),
                )
            except Exception as exc:
                log.warning("skip maker order %s: %s", sig.ticker, exc)
                self.ledger.record_signal(sig, acted=False)
                return False
            self.ledger.record_signal(sig, acted=True)
            res.orders_placed.append(sig)
            return True
        if paper_action in {"none", "resting", "held"}:
            self.ledger.record_signal(sig, acted=False)
            return False
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
                fee_model_version=FEE_MODEL_VERSION
                if sig.rules_version in {"v2", "v3"}
                else "v1-flat",
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
