from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from zoneinfo import ZoneInfo

from prediction_bot.config import Settings
from prediction_bot.devig import devig_all, longshot_haircut
from prediction_bot.fees import SeriesFeeCache, maker_fee, taker_fee, taker_fee_rate
from prediction_bot.models import (
    Block,
    EdgeReview,
    MakerShadowCandidate,
    MakerShadowUpdate,
    Market,
    OutcomeSide,
    QuoteSnapshot,
    Signal,
)
from prediction_bot.odds import KALSHI_SERIES_TO_SPORT, ConsensusOdds, OddsClient, consensus
from prediction_bot.paper.ledger import Ledger
from prediction_bot.strategies.sportsbook_arb import DRAW_NAMES, SportsbookArbStrategy
from prediction_bot.teams import kalshi_name_ok, team_for_code, team_for_odds_name
from prediction_bot.venues.kalshi import KalshiClient

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
SHARP_BOOKS = frozenset({"pinnacle", "circa", "betonlineag"})


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


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
    rules_version = "v2"

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
        self.fee_cache = (
            SeriesFeeCache(ledger, kalshi_client, self.clock)
            if ledger is not None and kalshi_client is not None
            else None
        )
        self._blocks: list[Block] = []
        self._quotes: list[QuoteSnapshot] = []
        self._reviews: list[EdgeReview] = []
        self._maker_shadows: list[MakerShadowCandidate] = []
        self._shadow_updates: dict[tuple[str, OutcomeSide], MakerShadowUpdate] = {}
        self._entry_window_decisions: list[Signal] = []
        self._line_move_decisions: list[Signal] = []

    def seen_blocks(self) -> list[Block]:
        return list(self._blocks)

    def seen_quotes(self) -> list[QuoteSnapshot]:
        return list(self._quotes)

    def seen_edge_reviews(self) -> list[EdgeReview]:
        return list(self._reviews)

    def seen_maker_shadows(self) -> list[MakerShadowCandidate]:
        return list(self._maker_shadows)

    def seen_maker_shadow_updates(self) -> list[MakerShadowUpdate]:
        return list(self._shadow_updates.values())

    def seen_shadow_decisions(self) -> list[Signal]:
        return list(self._entry_window_decisions)

    def seen_line_move_decisions(self) -> list[Signal]:
        return list(self._line_move_decisions)

    def _record_line_move(
        self,
        event_ticker: str,
        game: ConsensusOdds,
        legs: Sequence[Market],
        ids_by_ticker: dict[str, str],
        names_by_id: dict[str, str],
        commence: datetime,
        now: datetime,
    ) -> None:
        if self.ledger is None:
            return
        previous = self.ledger.previous_consensus(game.event_id, now)
        if not previous:
            return
        ids = set(ids_by_ticker.values())
        for market in legs:
            outcome_id = ids_by_ticker[market.ticker]
            team_name = names_by_id.get(outcome_id)
            current_team_prob = game.probs.get(team_name or "")
            previous_team_prob = previous.get(team_name or "")
            quote = self.ledger.previous_quote_snapshot(market.ticker, now)
            if (
                team_name is None
                or current_team_prob is None
                or previous_team_prob is None
                or quote is None
                or market.quote.yes_bid is None
                or market.quote.yes_ask is None
                or quote[0] is None
                or quote[1] is None
            ):
                continue
            reference_delta = current_team_prob - previous_team_prob
            if abs(reference_delta) < self.settings.exp_line_move_points:
                continue
            current_yes_mid = (market.quote.yes_bid + market.quote.yes_ask) / 2
            previous_yes_mid = (quote[0] + quote[1]) / 2
            side = OutcomeSide.yes if reference_delta > 0 else OutcomeSide.no
            current_mid = (
                current_yes_mid if side is OutcomeSide.yes else Decimal(1) - current_yes_mid
            )
            previous_mid = (
                previous_yes_mid if side is OutcomeSide.yes else Decimal(1) - previous_yes_mid
            )
            directional_move = current_mid - previous_mid
            if directional_move >= abs(reference_delta) / 3:
                continue
            if side is OutcomeSide.yes:
                position_outcome = outcome_id
                side_fair = current_team_prob
            elif "draw" not in ids:
                position_outcome = next(
                    (team_id for team_id in ids if team_id != outcome_id), outcome_id
                )
                side_fair = Decimal(1) - current_team_prob
            else:
                position_outcome = f"not:{outcome_id}"
                side_fair = Decimal(1) - current_team_prob
            position_key = f"{event_ticker}:{position_outcome}"
            if any(sig.position_key == position_key for sig in self._line_move_decisions):
                continue
            self._line_move_decisions.append(
                Signal(
                    strategy="line_move_follower",
                    ticker=market.ticker,
                    side=side,
                    fair_prob=side_fair,
                    limit_price=current_mid,
                    edge=abs(reference_delta),
                    size=Decimal(1),
                    rationale=(
                        f"reference moved {reference_delta:+.3f}; Kalshi side moved "
                        f"{directional_move:+.3f} in that direction"
                    ),
                    created_at=now,
                    rules_version="v2",
                    position_key=position_key,
                    event_ticker=event_ticker,
                    tags=("line_move_follower",),
                    commence_time=commence,
                )
            )

    def _road_dog_candidate(
        self,
        sport: str,
        event_ticker: str,
        game: ConsensusOdds,
        commence: datetime,
        outcome_id: str,
        side: OutcomeSide,
        price: Decimal,
        net_edge: Decimal,
        now: datetime,
    ) -> bool:
        if not self.settings.exp_road_dog_enabled or net_edge < 0:
            return False
        if not Decimal("0.15") <= price <= Decimal("0.45"):
            return False
        hours = Decimal(str((commence - now).total_seconds() / 3600))
        if not Decimal("72") <= hours <= Decimal("168"):
            return False
        away_id = team_for_odds_name(sport, game.away_team)
        home_id = team_for_odds_name(sport, game.home_team)
        if away_id is None or home_id is None:
            return False
        selected_outcome = (
            outcome_id
            if side is OutcomeSide.yes
            else home_id
            if outcome_id == away_id
            else away_id
            if outcome_id == home_id
            else f"not:{outcome_id}"
        )
        if selected_outcome != away_id:
            return False
        start_et = commence.astimezone(ET)
        attention = (
            19 <= start_et.hour <= 23
            or (sport == "americanfootball_nfl" and start_et.weekday() in {0, 3})
            or event_ticker.casefold()
            in {event.casefold() for event in self.settings.exp_road_dog_high_attention_events}
        )
        team_names = {game.home_team.casefold(), game.away_team.casefold()}
        team_ids = {home_id, away_id}
        for rivalry in self.settings.exp_road_dog_rivalries:
            pair = [
                token.strip()
                for token in re.split(r"\s*(?:,|;|:|\||\bvs\.?\b|-)\s*", rivalry, flags=re.I)
                if token.strip()
            ]
            if len(pair) == 2:
                rivalry_ids: set[str] = set()
                for token in pair:
                    team_id = team_for_odds_name(sport, token) or team_for_code(
                        event_ticker.split("-", 1)[0], token.upper()
                    )
                    if team_id in team_ids:
                        rivalry_ids.add(team_id)
                    elif token.casefold() in team_names:
                        rivalry_ids.add(token.casefold())
                if rivalry_ids == team_ids:
                    attention = True
        return attention

    def _block(
        self,
        reason: str,
        event_ticker: str,
        ticker: str,
        detail: str = "",
        position_key: str | None = None,
    ) -> None:
        block = Block(
            reason=reason,
            strategy=self.name,
            ticker=ticker,
            event_ticker=event_ticker,
            detail=detail,
            rules_version=self.rules_version,
            created_at=self.clock(),
            position_key=position_key,
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

    @staticmethod
    def _book_probabilities(
        sport: str, game: ConsensusOdds, expected_outcomes: set[str]
    ) -> dict[str, dict[str, dict[str, Decimal]]]:
        books: dict[str, dict[str, dict[str, Decimal]]] = {}
        for bookmaker, odds in game.book_odds.items():
            try:
                methods = devig_all(odds)
            except (ValueError, ArithmeticError):
                continue
            mapped: dict[str, dict[str, Decimal]] = {}
            valid = True
            for method, probabilities in methods.items():
                for name, probability in probabilities.items():
                    outcome = (
                        "draw"
                        if name.strip().lower() in DRAW_NAMES
                        else team_for_odds_name(sport, name)
                    )
                    if outcome not in expected_outcomes:
                        valid = False
                        break
                    mapped.setdefault(outcome, {})[method] = probability
                if not valid:
                    break
            if valid and set(mapped) == expected_outcomes:
                books[bookmaker] = mapped
        return books

    def _reference_for_side(
        self,
        books: dict[str, dict[str, dict[str, Decimal]]],
        outcome: str,
        side: OutcomeSide,
        sport: str,
    ) -> tuple[dict[str, Decimal], dict[str, dict[str, Decimal]], str, Decimal] | None:
        if self.rules_version == "v3":
            return self._v3_reference_for_side(books, outcome, side)
        side_books: dict[str, dict[str, Decimal]] = {}
        for bookmaker, outcomes in books.items():
            probabilities = outcomes.get(outcome)
            if probabilities is None:
                continue
            side_books[bookmaker] = {
                method: probability if side is OutcomeSide.yes else Decimal(1) - probability
                for method, probability in probabilities.items()
            }
        minimum = self.settings.min_books_by_sport.get(sport, self.settings.min_books)
        multiplicative = {
            bookmaker: values["multiplicative"]
            for bookmaker, values in side_books.items()
            if "multiplicative" in values
        }
        if len(multiplicative) < minimum:
            return None
        spread = max(multiplicative.values()) - min(multiplicative.values())
        source = "pinnacle" if "pinnacle" in side_books else "median"
        methods = (
            set(side_books["pinnacle"])
            if source == "pinnacle"
            else {method for values in side_books.values() for method in values}
        )
        reference = {
            method: side_books["pinnacle"][method]
            if source == "pinnacle"
            else _median([values[method] for values in side_books.values() if method in values])
            for method in methods
        }
        per_book = {
            bookmaker: values
            for bookmaker, values in side_books.items()
            if "multiplicative" in values
        }
        return reference, per_book, source, spread

    @staticmethod
    def _sharp_dispersion(side_books: dict[str, dict[str, Decimal]]) -> Decimal:
        sharp = [
            values["multiplicative"]
            for bookmaker, values in side_books.items()
            if bookmaker in SHARP_BOOKS and "multiplicative" in values
        ]
        return max(sharp) - min(sharp) if len(sharp) >= 2 else Decimal(0)

    def _v3_reference_for_side(
        self,
        books: dict[str, dict[str, dict[str, Decimal]]],
        outcome: str,
        side: OutcomeSide,
    ) -> tuple[dict[str, Decimal], dict[str, dict[str, Decimal]], str, Decimal] | None:
        side_books: dict[str, dict[str, Decimal]] = {}
        for bookmaker, outcomes in books.items():
            probabilities = outcomes.get(outcome)
            if probabilities is None:
                continue
            side_books[bookmaker] = {
                method: probability if side is OutcomeSide.yes else Decimal(1) - probability
                for method, probability in probabilities.items()
            }

        reference_books: dict[str, dict[str, Decimal]]
        if "pinnacle" in side_books:
            reference_books = {"pinnacle": side_books["pinnacle"]}
            source = "pinnacle"
        else:
            sharp_books = {
                bookmaker: values
                for bookmaker, values in side_books.items()
                if bookmaker in SHARP_BOOKS
            }
            if sharp_books:
                reference_books = sharp_books
                source = "sharp_median"
            else:
                if not self.settings.v3_allow_soft_reference:
                    return None
                soft_books: dict[str, dict[str, Decimal]] = {
                    bookmaker: side_books[bookmaker]
                    for bookmaker in ("draftkings", "fanduel")
                    if bookmaker in side_books
                }
                if not soft_books:
                    return None
                reference_books = soft_books
                source = "soft_reference"

        methods = set.intersection(*(set(values) for values in reference_books.values()))
        reference = {
            method: (
                next(iter(reference_books.values()))[method]
                if len(reference_books) == 1
                else _median([values[method] for values in reference_books.values()])
            )
            for method in methods
        }
        if not reference:
            return None
        return reference, side_books, source, self._sharp_dispersion(side_books)

    def _fresh_books(
        self,
        game: ConsensusOdds,
        books: dict[str, dict[str, dict[str, Decimal]]],
        now: datetime,
    ) -> tuple[dict[str, dict[str, dict[str, Decimal]]], bool]:
        cutoff = timedelta(hours=float(self.settings.v3_max_book_age_hours))
        stale = {
            bookmaker
            for bookmaker, updated in game.book_updates.items()
            if (updated_at := _parse_time(updated)) is not None and now - updated_at > cutoff
        }
        return (
            {bookmaker: values for bookmaker, values in books.items() if bookmaker not in stale},
            bool(stale),
        )

    def _fair_after_haircut(self, fair: Decimal, hours_until_start: Decimal) -> Decimal:
        if self.rules_version != "v3":
            return longshot_haircut(fair, hours_until_start, self.settings.longshot_haircut)
        mode = self.settings.v3_longshot_haircut
        return fair if mode == "off" else longshot_haircut(fair, hours_until_start, mode)

    def _set_shadow_update(
        self, market: Market, side: OutcomeSide, at: datetime, fair: Decimal | None = None
    ) -> None:
        key = (market.ticker, side)
        previous = self._shadow_updates.get(key)
        self._shadow_updates[key] = MakerShadowUpdate(
            at=at,
            ticker=market.ticker,
            side=side,
            ask=market.quote.ask_for(side),
            last_price=market.last_price,
            fair=fair if fair is not None else previous.fair if previous else None,
        )

    def _size_v2_signal(
        self,
        signal: Signal,
        *,
        reserved_position: dict[str, Decimal],
        reserved_event: dict[str, Decimal],
        reserved_day: Decimal,
        reserved_total: Decimal,
        reserved_cash: Decimal,
    ) -> Signal | None:
        if self.ledger is None or signal.position_key is None or signal.event_ticker is None:
            self._block("size_zero", signal.event_ticker or "", signal.ticker, "ledger unavailable")
            return None
        open_cost = sum((position.cost for position in self.ledger.positions()), Decimal(0))
        bankroll = self.ledger.cash + open_cost
        fee_rate = taker_fee_rate(signal.limit_price, signal.fee_multiplier or Decimal(0))
        effective_price = signal.limit_price + fee_rate
        if bankroll <= 0 or effective_price >= 1:
            self._block(
                "size_zero", signal.event_ticker, signal.ticker, "invalid bankroll or price"
            )
            return None
        kelly = (signal.fair_prob - effective_price) / (1 - effective_price)
        stake = max(Decimal(0), kelly * self.settings.v2_kelly_fraction * bankroll)
        utc_day = (
            self.clock().astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        )
        position_fraction = self.settings.v2_position_bankroll_fraction
        include_v3 = self.rules_version == "v3"
        tags = signal.tags or ()
        if "exp_road_dog" in tags and "value" not in tags:
            position_fraction = self.settings.exp_road_dog_position_bankroll_fraction
        position_room = (
            bankroll * position_fraction
            - self.ledger.position_cost(signal.position_key, include_v3=include_v3)
            - reserved_position.get(signal.position_key, Decimal(0))
        )
        event_room = (
            bankroll * self.settings.v2_event_bankroll_fraction
            - self.ledger.event_cost(signal.event_ticker, include_v3=include_v3)
            - reserved_event.get(signal.event_ticker, Decimal(0))
        )
        day_room = (
            bankroll * self.settings.v2_daily_bankroll_fraction
            - self.ledger.v2_new_risk_since(utc_day, include_v3=include_v3)
            - reserved_day
        )
        total_room = (
            bankroll * self.settings.v2_total_open_bankroll_fraction
            - self.ledger.v2_total_open_cost(include_v3=include_v3)
            - reserved_total
        )
        cash_room = self.ledger.cash - reserved_cash
        budget = min(stake, position_room, event_room, day_room, total_room, cash_room)
        desired = max(
            Decimal(0),
            (budget / effective_price).to_integral_value(rounding=ROUND_DOWN),
        )
        depth = signal.available_size or Decimal(0)
        size = min(desired, depth.to_integral_value(rounding=ROUND_DOWN))
        if size < desired:
            self._block(
                "depth_limited",
                signal.event_ticker or "",
                signal.ticker,
                f"{desired} contracts reduced to displayed depth {size}",
            )
        while size > 0:
            fee = taker_fee(
                signal.limit_price,
                size,
                signal.fee_multiplier or Decimal(0),
                self.settings.fee_rounding,
            )
            cost = signal.limit_price * size + fee
            if cost <= budget:
                break
            size -= 1
        if self.rules_version == "v3" and "large_gap" in (signal.tags or ()):
            size = (size / 2).to_integral_value(rounding=ROUND_DOWN)
        if size <= 0:
            self._block("size_zero", signal.event_ticker, signal.ticker, str(budget))
            return None
        return replace(signal, size=size)

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
        position_key: str | None = None,
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
                position_key=position_key,
            )
        )
        if not confirmed:
            self._block(
                "big_edge",
                event_ticker,
                market.ticker,
                "gross edge requires an explicit whitelist and confirming refetches",
                position_key,
            )
            return None
        assert refreshed_market is not None and refreshed_fair is not None
        capped_side_fair = min(side_fair, refreshed_fair)
        yes_fair = capped_side_fair if side is OutcomeSide.yes else Decimal(1) - capped_side_fair
        return refreshed_market, yes_fair

    def _review_large_gap_v3(
        self,
        market: Market,
        side: OutcomeSide,
        yes_fair: Decimal,
        evaluated_yes_fair: Decimal,
        event_ticker: str,
        position_key: str,
        sharp_dispersion: Decimal,
        sharp_book_count: int,
        now: datetime,
    ) -> tuple[Market, Decimal] | None:
        price = market.quote.ask_for(side)
        side_fair = (
            evaluated_yes_fair if side is OutcomeSide.yes else Decimal(1) - evaluated_yes_fair
        )
        if price is None or side_fair - price <= self.settings.max_gross_edge:
            return market, yes_fair

        reason = ""
        refreshed_market: Market | None = None
        refreshed_price: Decimal | None = None
        if side_fair - price > self.settings.v3_large_gap_max:
            reason = f"gross edge exceeds v3_large_gap_max={self.settings.v3_large_gap_max}"
        elif self.settings.v3_large_gap_mode == "block":
            reason = "v3_large_gap_mode=block"
        elif self.ledger is None or not self.ledger.has_recent_large_gap_record(position_key, now):
            reason = "no same-position big_edge/large_gap record from 2-36 hours earlier"
        elif self.kalshi is None:
            reason = "fresh Kalshi orderbook refetch unavailable"
        else:
            try:
                refreshed_quote = self.kalshi.get_orderbook(market.ticker)
                refreshed_price = refreshed_quote.ask_for(side)
                if refreshed_price is not None:
                    refreshed_market = replace(market, quote=refreshed_quote)
            except Exception:
                log.exception(
                    "sportsbook_v3: Kalshi orderbook refetch failed for %s", market.ticker
                )
                reason = "fresh Kalshi orderbook refetch failed"
        if not reason:
            if refreshed_price is None:
                reason = "fresh Kalshi orderbook has no ask"
            elif side_fair - refreshed_price <= self.settings.max_gross_edge:
                reason = "refreshed gross edge is not above max_gross_edge"
        if not reason and sharp_book_count < 2:
            reason = "fewer than two sharp books are available"
        if not reason and sharp_dispersion >= Decimal("0.02"):
            reason = f"sharp-book dispersion is not under 0.02: {sharp_dispersion}"

        confirmed = not reason
        self._reviews.append(
            EdgeReview(
                ticker=market.ticker,
                side=side,
                fair_prob=side_fair,
                price=price,
                gross_edge=side_fair - price,
                refetched_price=refreshed_price,
                refetched_fair=side_fair if refreshed_price is not None else None,
                confirmed=confirmed,
                whitelisted=False,
                created_at=now,
                position_key=position_key,
            )
        )
        if not confirmed:
            self._block("big_edge", event_ticker, market.ticker, reason, position_key)
            return None
        assert refreshed_market is not None
        return refreshed_market, yes_fair

    def evaluate(self, markets: Sequence[Market]) -> list[Signal]:
        self._cache.clear()
        self._blocks.clear()
        self._maker_shadows.clear()
        self._shadow_updates.clear()
        self._entry_window_decisions.clear()
        self._line_move_decisions.clear()
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
        for market in markets:
            for side in (OutcomeSide.yes, OutcomeSide.no):
                self._set_shadow_update(market, side, now)
        candidates: dict[str, list[Signal]] = defaultdict(list)
        entry_window_events: set[str] = set()
        events_evaluated = 0
        for event_ticker, legs in by_event.items():
            series = event_ticker.split("-", 1)[0]
            sport = KALSHI_SERIES_TO_SPORT.get(series)
            if sport is None or sport == "mma_mixed_martial_arts":
                self._block("unsupported_series", event_ticker, legs[0].ticker, series)
                continue
            events_evaluated += 1
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
                    reason = "mapping_mismatch" if self.rules_version == "v3" else "title_mismatch"
                    self._block(reason, event_ticker, market.ticker, market.subtitle)
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
            if len(games) > 1 and series == "KXMLBGAME":
                ticker_start = _ticker_start_et(event_ticker)
                if ticker_start is not None:
                    games = [
                        game
                        for game in games
                        if (game_start := _parse_time(game.commence_time)) is not None
                        and abs(game_start.astimezone(ET) - ticker_start) <= timedelta(minutes=60)
                    ]
            if len(games) > 1:
                self._block("ambiguous_match", event_ticker, legs[0].ticker)
                continue
            if not games:
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
                reason = "mapping_mismatch" if self.rules_version == "v3" else "mapping_sum"
                self._block(reason, event_ticker, legs[0].ticker, str(total))
                continue
            if sport in self.settings.v2_disabled_sports:
                self._block("league_disabled", event_ticker, legs[0].ticker, sport)
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
                if any(reason != "entry_window" for reason, _ in gate_reasons):
                    continue
                entry_window_events.add(event_ticker)
            self._record_line_move(
                event_ticker, game, legs, ids_by_ticker, names_by_id, commence, now
            )
            if self.fee_cache is None:
                self._block("fee_unknown", event_ticker, legs[0].ticker, series)
                continue
            fee_schedule = self.fee_cache.get(series)
            if fee_schedule is None:
                self._block("fee_unknown", event_ticker, legs[0].ticker, series)
                continue
            fee_type, fee_multiplier = fee_schedule
            if fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
                self._block("unknown_fee_type", event_ticker, legs[0].ticker, fee_type)
                continue
            expected_outcomes = set(ids_by_ticker.values())
            book_probabilities = self._book_probabilities(sport, game, expected_outcomes)
            minimum_books = self.settings.min_books_by_sport.get(sport, self.settings.min_books)
            stale_books = False
            if self.rules_version == "v3":
                book_probabilities, stale_books = self._fresh_books(game, book_probabilities, now)
            if len(book_probabilities) < minimum_books:
                reason = "stale_books" if stale_books else "few_books"
                self._block(
                    reason,
                    event_ticker,
                    legs[0].ticker,
                    f"found {len(book_probabilities)}, require {minimum_books}",
                )
                continue
            ids = set(team_ids)
            for market in legs:
                outcome_id = ids_by_ticker[market.ticker]
                if (
                    self.rules_version == "v3"
                    and market.open_interest < self.settings.v3_min_open_interest
                ):
                    self._block(
                        "thin_market",
                        event_ticker,
                        market.ticker,
                        f"open_interest={market.open_interest}, "
                        f"require {self.settings.v3_min_open_interest}",
                    )
                    continue
                for side in (OutcomeSide.yes, OutcomeSide.no):
                    outcome_name = "Draw" if outcome_id == "draw" else names_by_id[outcome_id]
                    position_outcome = (
                        outcome_id
                        if side is OutcomeSide.yes
                        else next((team_id for team_id in ids if team_id != outcome_id), outcome_id)
                        if "draw" not in ids_by_ticker.values()
                        else f"not:{outcome_id}"
                    )
                    position_key = f"{event_ticker}:{position_outcome}"
                    if (
                        self.rules_version == "v3"
                        and self.settings.v3_no_reentry
                        and self.ledger is not None
                        and self.ledger.has_v3_position_activity(position_key, now)
                    ):
                        self._block(
                            "already_held",
                            event_ticker,
                            market.ticker,
                            position_key,
                            position_key,
                        )
                        continue
                    reference = self._reference_for_side(
                        book_probabilities, outcome_id, side, sport
                    )
                    if reference is None:
                        self._block("few_books", event_ticker, market.ticker, outcome_name)
                        continue
                    method_fairs, per_book, reference_source, book_spread = reference
                    if book_spread > self.settings.max_book_spread:
                        self._block(
                            "book_spread" if self.rules_version == "v3" else "dispersion",
                            event_ticker,
                            market.ticker,
                            str(book_spread),
                        )
                        continue
                    fair_pre = (
                        min(method_fairs.values())
                        if self.rules_version != "v3"
                        or self.settings.v3_devig_method == "worst_case"
                        else method_fairs[self.settings.v3_devig_method]
                    )
                    hours_until_start = Decimal(str((commence - now).total_seconds() / 3600))
                    fair = self._fair_after_haircut(fair_pre, hours_until_start)
                    self._set_shadow_update(market, side, now, fair)
                    price = market.quote.ask_for(side)
                    if price is None:
                        self._block("missing_quote", event_ticker, market.ticker, side.value)
                        continue
                    gross_edge = fair - price
                    net_edge = gross_edge - taker_fee_rate(price, fee_multiplier)
                    threshold = self.settings.v2_min_net_edge
                    if price < self.settings.v2_price_floor:
                        self._block("price_floor", event_ticker, market.ticker, str(price))
                        continue
                    if price > self.settings.v2_price_ceiling:
                        self._block("price_ceiling", event_ticker, market.ticker, str(price))
                        continue
                    road_dog_candidate = self._road_dog_candidate(
                        sport, event_ticker, game, commence, outcome_id, side, price, net_edge, now
                    )
                    if net_edge < threshold and not road_dog_candidate:
                        self._block("net_edge", event_ticker, market.ticker, str(net_edge))
                        continue
                    if price < self.settings.v2_longshot_price and gross_edge < (
                        self.settings.v2_longshot_min_gross
                    ):
                        self._block("longshot_edge", event_ticker, market.ticker, str(gross_edge))
                        continue
                    yes_fair = fair_pre if side is OutcomeSide.yes else Decimal(1) - fair_pre
                    active = (
                        self._review_large_gap_v3(
                            market,
                            side,
                            yes_fair,
                            fair if side is OutcomeSide.yes else Decimal(1) - fair,
                            event_ticker,
                            position_key,
                            book_spread,
                            sum(book in SHARP_BOOKS for book in per_book),
                            now,
                        )
                        if self.rules_version == "v3"
                        else self._review_big_edge(
                            market,
                            side,
                            yes_fair,
                            event_ticker,
                            sport,
                            game,
                            outcome_name,
                            now,
                            position_key,
                        )
                    )
                    if active is None:
                        continue
                    active_market, active_yes_fair = active
                    if active_yes_fair != yes_fair or (
                        self.rules_version == "v3" and active_market is not market
                    ):
                        fair_pre = (
                            active_yes_fair
                            if side is OutcomeSide.yes
                            else Decimal(1) - active_yes_fair
                        )
                        fair = self._fair_after_haircut(fair_pre, hours_until_start)
                        price = active_market.quote.ask_for(side)
                        if price is None:
                            self._block("missing_quote", event_ticker, market.ticker, side.value)
                            continue
                        gross_edge = fair - price
                        net_edge = gross_edge - taker_fee_rate(price, fee_multiplier)
                        if price < self.settings.v2_price_floor:
                            self._block("price_floor", event_ticker, market.ticker, str(price))
                            continue
                        if price > self.settings.v2_price_ceiling:
                            self._block("price_ceiling", event_ticker, market.ticker, str(price))
                            continue
                        road_dog_candidate = self._road_dog_candidate(
                            sport,
                            event_ticker,
                            game,
                            commence,
                            outcome_id,
                            side,
                            price,
                            net_edge,
                            now,
                        )
                        if net_edge < threshold and not road_dog_candidate:
                            self._block("net_edge", event_ticker, market.ticker, str(net_edge))
                            continue
                        if (
                            price < self.settings.v2_longshot_price
                            and gross_edge < self.settings.v2_longshot_min_gross
                        ):
                            self._block(
                                "longshot_edge", event_ticker, market.ticker, str(gross_edge)
                            )
                            continue
                    road_dog_candidate = self._road_dog_candidate(
                        sport, event_ticker, game, commence, outcome_id, side, price, net_edge, now
                    )
                    is_main_candidate = net_edge >= threshold
                    if not is_main_candidate and not road_dog_candidate:
                        self._block("net_edge", event_ticker, market.ticker, str(net_edge))
                        continue
                    side_bid = active_market.quote.bid_for(side)
                    spread = price - side_bid if side_bid is not None else None
                    large_gap = (
                        self.rules_version == "v3" and fair - price > self.settings.max_gross_edge
                    )
                    signal = Signal(
                        strategy=self.name,
                        ticker=active_market.ticker,
                        side=side,
                        fair_prob=fair,
                        limit_price=price,
                        edge=net_edge,
                        size=Decimal(1),
                        rationale=(
                            f"{outcome_name} {reference_source} reference, "
                            f"{len(per_book)} books, fair {fair:.3f}"
                        ),
                        taker=True,
                        created_at=now,
                        rules_version=self.rules_version,
                        fee_type=fee_type,
                        fee_multiplier=fee_multiplier,
                        fair_pre_haircut=fair_pre,
                        available_size=active_market.quote.ask_size_for(side),
                        spread=spread,
                        devig_json=json.dumps(
                            {name: str(probability) for name, probability in method_fairs.items()},
                            sort_keys=True,
                        ),
                        reference_source=reference_source,
                        book_count=len(per_book),
                        book_spread=book_spread,
                        book_probs_json=json.dumps(
                            {
                                bookmaker: {
                                    "multiplicative_fair": str(values["multiplicative"]),
                                    "minimum_fair": str(min(values.values())),
                                }
                                for bookmaker, values in per_book.items()
                            },
                            sort_keys=True,
                        ),
                        commence_time=commence,
                    )
                    tags = {"value"} if is_main_candidate else set()
                    if reference_source == "soft_reference":
                        tags.add("soft_reference")
                    if large_gap:
                        tags.add("large_gap")
                    if road_dog_candidate:
                        tags.add("exp_road_dog")
                    if (
                        self.settings.underdog_min_price
                        <= signal.limit_price
                        <= self.settings.underdog_max_price
                    ):
                        tags.add("underdog")
                    signal = replace(
                        signal,
                        position_key=position_key,
                        event_ticker=event_ticker,
                        tags=tuple(sorted(tags)),
                    )
                    candidates[position_key].append(signal)
        if self.rules_version == "v3" and events_evaluated >= 10:
            quality_blocks = sum(
                block.reason in {"mapping_mismatch", "book_spread"} for block in self._blocks
            )
            fraction = Decimal(quality_blocks) / Decimal(events_evaluated)
            if fraction > self.settings.v3_dq_stop_fraction:
                self._block(
                    "dq_stop",
                    "",
                    "",
                    f"{quality_blocks}/{events_evaluated} mapping/dispersion blocks",
                )
                return []
        out: list[Signal] = []
        reserved_position: dict[str, Decimal] = {}
        reserved_event: dict[str, Decimal] = {}
        reserved_day = Decimal(0)
        reserved_total = Decimal(0)
        reserved_cash = Decimal(0)
        for position_key, signals in candidates.items():
            selected_shadow = min(
                signals,
                key=lambda sig: (
                    sig.limit_price
                    + taker_fee_rate(sig.limit_price, sig.fee_multiplier or Decimal(0)),
                    0 if sig.side is OutcomeSide.yes else 1,
                    sig.ticker,
                ),
            )
            sized_shadow = self._size_v2_signal(
                selected_shadow,
                reserved_position={},
                reserved_event={},
                reserved_day=Decimal(0),
                reserved_total=Decimal(0),
                reserved_cash=Decimal(0),
            )
            if (
                sized_shadow is not None
                and sized_shadow.event_ticker is not None
                and sized_shadow.commence_time is not None
                and sized_shadow.event_ticker not in entry_window_events
            ):
                shadow_market = next(m for m in markets if m.ticker == sized_shadow.ticker)
                bid = shadow_market.quote.bid_for(sized_shadow.side)
                ask = shadow_market.quote.ask_for(sized_shadow.side)
                shadow_price = bid + Decimal("0.01") if bid is not None else None
                if shadow_price is not None and ask is not None and shadow_price < ask:
                    buffer = self.settings.kickoff_buffer_minutes.get(
                        KALSHI_SERIES_TO_SPORT.get(sized_shadow.event_ticker.split("-", 1)[0], ""),
                        self.settings.kickoff_buffer_default_minutes,
                    )
                    self._maker_shadows.append(
                        MakerShadowCandidate(
                            created_at=now,
                            ticker=sized_shadow.ticker,
                            side=sized_shadow.side,
                            event_ticker=sized_shadow.event_ticker or "",
                            position_key=position_key,
                            price=shadow_price,
                            size=sized_shadow.size,
                            fair=sized_shadow.fair_prob,
                            maker_fee=maker_fee(
                                shadow_price,
                                sized_shadow.size,
                                sized_shadow.fee_multiplier or Decimal(0),
                                sized_shadow.fee_type or "",
                                self.settings.fee_rounding,
                            ),
                            expires_at=sized_shadow.commence_time - timedelta(minutes=buffer),
                            commence_time=sized_shadow.commence_time,
                        )
                    )
            trade_candidates = sorted(
                (
                    sig
                    for sig in signals
                    if sig.spread is not None and sig.spread <= self.settings.v2_max_spread
                ),
                key=lambda sig: (
                    sig.limit_price
                    + taker_fee_rate(sig.limit_price, sig.fee_multiplier or Decimal(0)),
                    0 if sig.side is OutcomeSide.yes else 1,
                    sig.ticker,
                ),
            )
            if not trade_candidates:
                self._block(
                    "wide_spread",
                    selected_shadow.event_ticker or "",
                    selected_shadow.ticker,
                    str(selected_shadow.spread)
                    if selected_shadow.spread is not None
                    else "missing bid",
                )
            else:
                sized = None
                for candidate in trade_candidates:
                    sized = self._size_v2_signal(
                        candidate,
                        reserved_position=reserved_position,
                        reserved_event=reserved_event,
                        reserved_day=reserved_day,
                        reserved_total=reserved_total,
                        reserved_cash=reserved_cash,
                    )
                    if sized is not None:
                        break
                if sized is not None:
                    if sized.event_ticker in entry_window_events:
                        self._entry_window_decisions.append(sized)
                    else:
                        out.append(sized)
                        fee = taker_fee(
                            sized.limit_price,
                            sized.size,
                            sized.fee_multiplier or Decimal(0),
                            self.settings.fee_rounding,
                        )
                        cost = sized.limit_price * sized.size + fee
                        reserved_position[position_key] = (
                            reserved_position.get(position_key, Decimal(0)) + cost
                        )
                        reserved_event[sized.event_ticker or ""] = (
                            reserved_event.get(sized.event_ticker or "", Decimal(0)) + cost
                        )
                        reserved_day += cost
                        reserved_total += cost
                        reserved_cash += cost
            for rejected in signals:
                if rejected is not selected_shadow:
                    event_ticker = rejected.event_ticker or ""
                    self._block(
                        "duplicate_position",
                        event_ticker,
                        rejected.ticker,
                        position_key,
                    )
        return out
