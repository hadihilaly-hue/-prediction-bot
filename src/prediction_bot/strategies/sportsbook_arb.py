from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from prediction_bot.config import Settings
from prediction_bot.models import Market, OutcomeSide, Signal
from prediction_bot.odds import (
    KALSHI_SERIES_TO_SPORT,
    ConsensusOdds,
    OddsClient,
    best_team,
    consensus,
)
from prediction_bot.strategies.base import Strategy

log = logging.getLogger(__name__)


class SportsbookArbStrategy(Strategy):
    """Buy a team on Kalshi when its price lags the de-vigged sportsbook consensus.

    Kalshi game-winner events (`KX<LEAGUE>GAME-<date><teams>`) have one market per
    team whose `yes_sub_title` is the team name. We match that name against The Odds
    API's home/away teams for the same sport and use the consensus win probability as
    fair value. Fees and `min_edge` are applied in `Strategy.make_signal`.
    """

    name = "sportsbook_arb"

    def __init__(self, settings: Settings, odds_client: OddsClient | None = None) -> None:
        super().__init__(settings)
        if odds_client is None and settings.odds_api_key:
            odds_client = OddsClient(settings.odds_api_key, settings.odds_api_base)
        self.odds = odds_client
        self._cache: dict[str, list[ConsensusOdds]] = {}

    def games_for(self, sport: str) -> list[ConsensusOdds]:
        """Odds are fetched at most once per `evaluate` call (cache cleared on each)."""
        if sport not in self._cache:
            if self.odds is None:
                return []
            events = self.odds.h2h(sport, self.settings.odds_bookmakers)
            self._cache[sport] = [
                c for e in events if (c := consensus(e, self.settings.odds_bookmakers))
            ]
        return self._cache[sport]

    def evaluate(self, markets: Sequence[Market]) -> list[Signal]:
        if self.odds is None:
            log.warning("sportsbook_arb: PBOT_ODDS_API_KEY not set; no signals")
            return []
        self._cache.clear()
        by_event: dict[str, list[Market]] = defaultdict(list)
        for m in markets:
            series = m.event_ticker.split("-")[0]
            if series in KALSHI_SERIES_TO_SPORT:
                by_event[m.event_ticker].append(m)

        out: list[Signal] = []
        for legs in by_event.values():
            sport = KALSHI_SERIES_TO_SPORT[legs[0].event_ticker.split("-")[0]]
            game = self._match_game(legs, self.games_for(sport))
            if game is None:
                continue
            for m in legs:
                team = best_team(m.subtitle, [game.home_team, game.away_team])
                if team is None:
                    continue
                fair = game.probs.get(team)
                if fair is None:
                    continue
                rationale = (
                    f"{team} consensus {fair:.3f} from {game.books_used} books"
                    f" ({game.home_team} vs {game.away_team})"
                )
                yes_sig = self.make_signal(m, OutcomeSide.yes, fair, rationale)
                no_sig = self.make_signal(m, OutcomeSide.no, Decimal(1) - fair, rationale)
                out.extend(s for s in (yes_sig, no_sig) if s)
        return out

    # Kalshi's expected_expiration_time sits a few hours after kickoff; close_time
    # (fallback) is a couple of days later. Games further from the anchor than the
    # window are a different fixture.
    EXPIRATION_WINDOW = timedelta(hours=18)
    CLOSE_WINDOW = timedelta(days=4)

    @classmethod
    def _match_game(cls, legs: list[Market], games: list[ConsensusOdds]) -> ConsensusOdds | None:
        """Pick the game whose teams match the legs. Repeated matchups (e.g. a series)
        are disambiguated by the game start nearest the market's expected expiration."""
        names = [m.subtitle for m in legs if m.subtitle]
        need = min(2, len(names))
        anchor, window = None, cls.EXPIRATION_WINDOW
        for m in legs:
            if m.expected_expiration:
                anchor = m.expected_expiration
                break
            if m.close_time and anchor is None:
                anchor, window = m.close_time, cls.CLOSE_WINDOW
        candidates: list[tuple[timedelta, ConsensusOdds]] = []
        for g in games:
            hits = sum(1 for n in names if best_team(n, [g.home_team, g.away_team]))
            if hits < need:
                continue
            gap = cls._start_gap(g.commence_time, anchor, window)
            if gap is None:
                continue
            candidates.append((gap, g))
        if not candidates:
            return None
        if len(candidates) > 1 and anchor is None:
            return None  # same matchup listed twice and no date to disambiguate
        return min(candidates, key=lambda c: c[0])[1]

    @classmethod
    def _start_gap(
        cls, commence: str, anchor: datetime | None, window: timedelta
    ) -> timedelta | None:
        if anchor is None:
            return timedelta(0)
        try:
            start = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        except ValueError:
            return None
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        gap = anchor - start
        if gap < timedelta(0):
            return None  # game starts after the market is expected to resolve
        return gap if gap <= window else None
