from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from prediction_bot.config import Settings
from prediction_bot.models import (
    Block,
    EdgeReview,
    Market,
    OutcomeSide,
    QuoteSnapshot,
    Signal,
    fee_for,
)
from prediction_bot.odds import KALSHI_SERIES_TO_SPORT, ConsensusOdds, OddsClient, consensus
from prediction_bot.paper.ledger import Ledger
from prediction_bot.strategies.sportsbook_arb import DRAW_NAMES, SportsbookArbStrategy
from prediction_bot.teams import kalshi_name_ok, team_for_code, team_for_odds_name
from prediction_bot.venues.kalshi import KalshiClient

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def _parse_time(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def _ticker_date(event_ticker: str) -> date | None:
    match = re.match(r"^[^-]+-(\d{2}[A-Z]{3}\d{2})", event_ticker)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%y%b%d").date()
    except ValueError:
        return None


def _ticker_start_et(event_ticker: str) -> datetime | None:
    match = re.match(r"^KXMLBGAME-(\d{2}[A-Z]{3}\d{2})(\d{4})", event_ticker)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2), "%y%b%d%H%M").replace(tzinfo=ET)
    except ValueError:
        return None


class SportsbookV2Strategy(SportsbookArbStrategy):
    name = "sportsbook_v2"

    def __init__(
        self,
        settings: Settings,
        odds_client: OddsClient | None = None,
        kalshi_client: KalshiClient | None = None,
        ledger: Ledger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(settings, odds_client)
        self.kalshi = kalshi_client
        self.ledger = ledger
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._blocks: list[Block] = []
        self._quotes: list[QuoteSnapshot] = []
        self._reviews: list[EdgeReview] = []

    def seen_blocks(self) -> list[Block]:
        return list(self._blocks)

    def seen_quotes(self) -> list[QuoteSnapshot]:
        return list(self._quotes)

    def seen_edge_reviews(self) -> list[EdgeReview]:
        return list(self._reviews)

    def _block(self, reason: str, event_ticker: str, ticker: str, detail: str = "") -> None:
        block = Block(
            reason=reason,
            strategy=self.name,
            ticker=ticker,
            event_ticker=event_ticker,
            detail=detail,
            created_at=self.clock(),
        )
        self._blocks.append(block)
        log.info("BLOCK %s %s %s: %s", self.name, event_ticker, reason, detail)

    def _in_play_reasons(
        self,
        event_ticker: str,
        game: ConsensusOdds,
        commence: datetime,
        legs: Sequence[Market],
        now: datetime,
    ) -> list[tuple[str, str]]:
        reasons: list[tuple[str, str]] = []
        if now >= commence:
            reasons.append(("in_play:commence_past", "commence time has passed"))
        for book, update in game.book_updates.items():
            updated_at = _parse_time(update)
            if updated_at is not None and updated_at > commence:
                reasons.append(("in_play:book_updated_after_start", book))
                break
        first = self.ledger.first_consensus(game.event_id) if self.ledger else {}
        for name, prob in game.probs.items():
            original = first.get(name)
            if (
                original is not None
                and self.settings.in_play_first_consensus_min
                <= original
                <= self.settings.in_play_first_consensus_max
                and (
                    prob <= self.settings.in_play_consensus_low
                    or prob >= self.settings.in_play_consensus_high
                )
            ):
                reasons.append(("in_play:consensus_extreme", name))
                break
        for market in legs:
            initial = self.ledger.first_quote(market.ticker) if self.ledger else None
            if initial is None:
                continue
            first_bid, first_ask = initial
            current_asks = (
                (market.quote.yes_ask, first_ask),
                (
                    Decimal(1) - market.quote.yes_bid if market.quote.yes_bid is not None else None,
                    Decimal(1) - first_bid if first_bid is not None else None,
                ),
            )
            for current, original in current_asks:
                if (
                    current is not None
                    and original is not None
                    and self.settings.in_play_kalshi_price_min
                    < original
                    < self.settings.in_play_kalshi_price_max
                    and (
                        current <= self.settings.in_play_kalshi_price_min
                        or current >= self.settings.in_play_kalshi_price_max
                    )
                ):
                    reasons.append(("in_play:kalshi_extreme", market.ticker))
                    break
        previous = self.ledger.previous_consensus(game.event_id, now) if self.ledger else {}
        if any(
            abs(prob - previous[name]) > self.settings.in_play_consensus_jump
            for name, prob in game.probs.items()
            if name in previous
        ):
            reasons.append(("in_play:consensus_jump", game.event_id))
        return reasons

    def _start_mismatch(
        self, event_ticker: str, legs: Sequence[Market], commence: datetime
    ) -> str | None:
        """Check ticker timing; Kalshi expected_expiration approximates game end."""
        ticker_date = _ticker_date(event_ticker)
        commence_et = commence.astimezone(ET)
        if ticker_date is None or commence_et.date() not in (
            ticker_date,
            ticker_date + timedelta(days=1),
        ):
            return "ticker date differs from ET commence date"
        if event_ticker.startswith("KXMLBGAME-"):
            encoded = _ticker_start_et(event_ticker)
            if (
                encoded is not None
                and abs((encoded.astimezone(timezone.utc) - commence).total_seconds()) > 3600
            ):
                return "MLB ticker start differs from commence by more than 60 minutes"
        # Kalshi's expected expiration is an approximate game-end timestamp.
        for market in legs:
            if market.expected_expiration is None:
                continue
            expiry = _parse_time(market.expected_expiration)
            if expiry is None or expiry <= commence or expiry > commence + timedelta(hours=6):
                return "Kalshi expected expiration is outside the allowed game-end window"
        return None

    def _match_games(
        self, sport: str, team_ids: set[str], ticker_date: date
    ) -> list[ConsensusOdds]:
        matches: list[ConsensusOdds] = []
        for game in self.games_for(sport):
            home = team_for_odds_name(sport, game.home_team)
            away = team_for_odds_name(sport, game.away_team)
            commence = _parse_time(game.commence_time)
            if home is None or away is None or commence is None:
                continue
            game_date = commence.astimezone(ET).date()
            if {home, away} == team_ids and game_date in (
                ticker_date,
                ticker_date + timedelta(days=1),
            ):
                matches.append(game)
        return matches

    def _refetch_game(self, sport: str, event_id: str) -> ConsensusOdds | None:
        if self.odds is None:
            return None
        try:
            events = self.odds.h2h(sport, self.settings.odds_bookmakers, bypass_cache=True)
        except Exception:
            log.exception("sportsbook_v2: odds refetch failed for %s", sport)
            return None
        return next(
            (
                fresh
                for event in events
                if str(event.get("id")) == event_id
                and (fresh := consensus(event, self.settings.odds_bookmakers)) is not None
            ),
            None,
        )

    def _review_big_edge(
        self,
        market: Market,
        side: OutcomeSide,
        yes_fair: Decimal,
        event_ticker: str,
        sport: str,
        game: ConsensusOdds,
        outcome_name: str,
        now: datetime,
    ) -> tuple[Market, Decimal] | None:
        price = market.quote.ask_for(side)
        side_fair = yes_fair if side is OutcomeSide.yes else Decimal(1) - yes_fair
        if price is None or side_fair - price < self.settings.max_gross_edge:
            return market, yes_fair
        # 7¢ includes UCLAORE's 8.2¢ and SFSEA's 7.5¢ gross edges, both below 10¢.
        whitelisted = market.ticker in self.settings.big_edge_whitelist
        refreshed_market = None
        try:
            if self.kalshi is not None:
                refreshed_market = self.kalshi.get_market(market.ticker)
        except Exception:
            log.exception("sportsbook_v2: Kalshi refetch failed for %s", market.ticker)
        refreshed_game = self._refetch_game(sport, game.event_id) if whitelisted else None
        refreshed_fair = None
        if refreshed_game is not None:
            refreshed_yes_fair = (
                refreshed_game.probs.get("Draw")
                if outcome_name == "Draw"
                else refreshed_game.probs.get(outcome_name)
            )
            self._cache.setdefault(sport, [])
            self._cache[sport] = [
                entry for entry in self._cache[sport] if entry.event_id != refreshed_game.event_id
            ] + [refreshed_game]
            refreshed_fair = (
                refreshed_yes_fair
                if side is OutcomeSide.yes
                else Decimal(1) - refreshed_yes_fair
                if refreshed_yes_fair is not None
                else None
            )
        else:
            refreshed_fair = None
        refreshed_price = (
            refreshed_market.quote.ask_for(side) if refreshed_market is not None else None
        )
        confirmed = (
            whitelisted
            and refreshed_price is not None
            and refreshed_fair is not None
            and refreshed_fair - refreshed_price >= self.settings.max_gross_edge
        )
        self._reviews.append(
            EdgeReview(
                ticker=market.ticker,
                side=side,
                fair_prob=side_fair,
                price=price,
                gross_edge=side_fair - price,
                refetched_price=refreshed_price,
                refetched_fair=refreshed_fair,
                confirmed=confirmed,
                whitelisted=whitelisted,
                created_at=now,
            )
        )
        if not confirmed:
            self._block(
                "big_edge",
                event_ticker,
                market.ticker,
                "gross edge requires an explicit whitelist and confirming refetches",
            )
            return None
        assert refreshed_market is not None and refreshed_fair is not None
        yes_fair = refreshed_fair if side is OutcomeSide.yes else Decimal(1) - refreshed_fair
        return refreshed_market, yes_fair

    def evaluate(self, markets: Sequence[Market]) -> list[Signal]:
        self._cache.clear()
        self._blocks.clear()
        self._quotes = [
            QuoteSnapshot(
                ticker=m.ticker,
                event_ticker=m.event_ticker,
                yes_bid=m.quote.yes_bid,
                yes_ask=m.quote.yes_ask,
                last_price=m.last_price,
                volume=m.volume,
                open_interest=m.open_interest,
                created_at=self.clock(),
            )
            for m in markets
            if KALSHI_SERIES_TO_SPORT.get(m.event_ticker.split("-", 1)[0])
            not in (None, "mma_mixed_martial_arts")
        ]
        self._reviews.clear()
        by_event: dict[str, list[Market]] = defaultdict(list)
        for market in markets:
            by_event[market.event_ticker].append(market)
        now = _parse_time(self.clock()) or datetime.now(timezone.utc)
        candidates: dict[str, list[Signal]] = defaultdict(list)
        for event_ticker, legs in by_event.items():
            series = event_ticker.split("-", 1)[0]
            sport = KALSHI_SERIES_TO_SPORT.get(series)
            if sport is None or sport == "mma_mixed_martial_arts":
                self._block("unsupported_series", event_ticker, legs[0].ticker, series)
                continue
            ticker_date = _ticker_date(event_ticker)
            if ticker_date is None:
                self._block(
                    "no_odds_match", event_ticker, legs[0].ticker, "unparseable ticker date"
                )
                continue
            ids_by_ticker: dict[str, str] = {}
            names_by_id: dict[str, str] = {}
            mapping_error = False
            for market in legs:
                code = market.ticker.removeprefix(event_ticker + "-")
                is_draw = sport.startswith("soccer_") and (
                    code in {"TIE", "DRAW"} or market.subtitle.strip().lower() in DRAW_NAMES
                )
                if is_draw:
                    ids_by_ticker[market.ticker] = "draw"
                    continue
                team_id = team_for_code(series, code)
                if team_id is None:
                    self._block("mapping_failed", event_ticker, market.ticker, code)
                    mapping_error = True
                    break
                if not kalshi_name_ok(series, team_id, market.subtitle):
                    self._block("title_mismatch", event_ticker, market.ticker, market.subtitle)
                    mapping_error = True
                    break
                ids_by_ticker[market.ticker] = team_id
                names_by_id[team_id] = market.subtitle
            if mapping_error:
                continue
            team_ids = {team_id for team_id in ids_by_ticker.values() if team_id != "draw"}
            if len(team_ids) != 2:
                self._block("mapping_failed", event_ticker, legs[0].ticker, "expected two teams")
                continue
            games = self._match_games(sport, team_ids, ticker_date)
            if not games:
                self._block("no_odds_match", event_ticker, legs[0].ticker)
                continue
            if len(games) > 1:
                self._block("ambiguous_match", event_ticker, legs[0].ticker)
                continue
            game = games[0]
            for odds_name in (game.home_team, game.away_team):
                mapped_id = team_for_odds_name(sport, odds_name)
                if mapped_id is not None:
                    names_by_id[mapped_id] = odds_name
            commence = _parse_time(game.commence_time)
            if commence is None:
                self._block("no_odds_match", event_ticker, legs[0].ticker, "missing commence time")
                continue
            probs_by_id = {
                team_id: game.probs.get(names_by_id[team_id], Decimal(-1)) for team_id in team_ids
            }
            if "draw" in ids_by_ticker.values():
                probs_by_id["draw"] = game.probs.get("Draw", Decimal(-1))
            total = sum(
                (probs_by_id.get(ids_by_ticker[m.ticker], Decimal(-1)) for m in legs),
                Decimal(0),
            )
            if total < Decimal("0.98") or total > Decimal("1.02"):
                self._block("mapping_sum", event_ticker, legs[0].ticker, str(total))
                continue
            gate_reasons: list[tuple[str, str]] = []
            mismatch = self._start_mismatch(event_ticker, legs, commence)
            if mismatch:
                gate_reasons.append(("start_mismatch", mismatch))
            buffer = self.settings.kickoff_buffer_minutes.get(
                sport, self.settings.kickoff_buffer_default_minutes
            )
            if now >= commence - timedelta(minutes=buffer):
                gate_reasons.append(("kickoff_guard", f"commence={commence.isoformat()}"))
            gate_reasons.extend(self._in_play_reasons(event_ticker, game, commence, legs, now))
            if self.settings.min_lead_hours > 0 and commence - now < timedelta(
                hours=self.settings.min_lead_hours
            ):
                gate_reasons.append(("entry_window", f"commence={commence.isoformat()}"))
            if gate_reasons:
                for reason, detail in gate_reasons:
                    self._block(reason, event_ticker, legs[0].ticker, detail)
                continue
            ids = set(team_ids)
            for market in legs:
                outcome_id = ids_by_ticker[market.ticker]
                yes_fair = probs_by_id[outcome_id]
                for side in (OutcomeSide.yes, OutcomeSide.no):
                    outcome_name = "Draw" if outcome_id == "draw" else names_by_id[outcome_id]
                    active = self._review_big_edge(
                        market, side, yes_fair, event_ticker, sport, game, outcome_name, now
                    )
                    if active is None:
                        continue
                    active_market, active_fair = active
                    signal_fair = (
                        active_fair if side is OutcomeSide.yes else Decimal(1) - active_fair
                    )
                    signal = self.make_signal(
                        active_market,
                        side,
                        signal_fair,
                        f"{outcome_name} consensus {active_fair:.3f} from {game.books_used} books",
                        taker=True,
                    )
                    if signal is None:
                        continue
                    if side is OutcomeSide.yes:
                        outcome = outcome_id
                    elif "draw" not in ids_by_ticker.values():
                        outcome = next(
                            (team_id for team_id in ids if team_id != outcome_id), outcome_id
                        )
                    else:
                        outcome = f"not:{outcome_id}"
                    position_key = f"{event_ticker}:{outcome}"
                    tags = {"value"}
                    if (
                        self.settings.underdog_min_price
                        <= signal.limit_price
                        <= self.settings.underdog_max_price
                    ):
                        tags.add("underdog")
                    signal = replace(
                        signal,
                        created_at=now,
                        rules_version="v2",
                        position_key=position_key,
                        event_ticker=event_ticker,
                        tags=tuple(sorted(tags)),
                        commence_time=commence,
                    )
                    candidates[position_key].append(signal)
        out: list[Signal] = []
        for position_key, signals in candidates.items():
            selected = min(
                signals,
                key=lambda sig: (
                    sig.limit_price
                    + fee_for(sig.limit_price, Decimal(1), self.settings.taker_fee_multiplier),
                    0 if sig.side is OutcomeSide.yes else 1,
                    sig.ticker,
                ),
            )
            out.append(selected)
            for rejected in signals:
                if rejected is not selected:
                    event_ticker = rejected.event_ticker or ""
                    self._block(
                        "duplicate_position",
                        event_ticker,
                        rejected.ticker,
                        position_key,
                    )
        return out
