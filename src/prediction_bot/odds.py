"""Sportsbook odds via The Odds API (https://the-odds-api.com). Free tier: 500 req/month."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

import httpx

# Kalshi game-winner series -> The Odds API sport key
KALSHI_SERIES_TO_SPORT: dict[str, str] = {
    "KXNFLGAME": "americanfootball_nfl",
    "KXNCAAFGAME": "americanfootball_ncaaf",
    "KXNBAGAME": "basketball_nba",
    "KXNCAAMBGAME": "basketball_ncaab",
    "KXWNBAGAME": "basketball_wnba",
    "KXMLBGAME": "baseball_mlb",
    "KXNHLGAME": "icehockey_nhl",
    "KXEPLGAME": "soccer_epl",
    "KXUCLGAME": "soccer_uefa_champs_league",
    "KXMLSGAME": "soccer_usa_mls",
}

# Kalshi tournament-winner series (one market per player) -> The Odds API "outrights" sport key.
# The free tier only carries golf's four majors; Kalshi lists these a few weeks before each.
KALSHI_SERIES_TO_OUTRIGHT: dict[str, str] = {
    "KXMASTERS": "golf_masters_tournament_winner",
    "KXPGA": "golf_pga_championship_winner",
    "KXUSOPEN": "golf_us_open_winner",
    "KXTHEOPEN": "golf_the_open_championship_winner",
}


@dataclass(frozen=True)
class ConsensusOdds:
    """De-vigged win probabilities for one game, averaged across bookmakers."""

    event_id: str
    home_team: str
    away_team: str
    commence_time: str
    probs: dict[str, Decimal]  # team/player name -> probability (plus "Draw" for soccer)
    books_used: int

    @property
    def names(self) -> list[str]:
        return list(self.probs)


def devig(decimal_odds: dict[str, Decimal], require_full: bool = False) -> dict[str, Decimal]:
    """Multiplicative de-vig: implied = 1/odds, normalised to sum to 1.

    A complete book always has implied probabilities summing to at least 1 (the margin).
    With `require_full` (outright fields, where a book may quote only some players) a
    list summing to less than 1 is an incomplete field: normalising it would inflate every
    listed player and the raw quotes still carry margin, so the book is dropped instead.
    """
    implied = {k: Decimal(1) / v for k, v in decimal_odds.items() if v > 0}
    total = sum(implied.values(), Decimal(0))
    if total <= 0 or (require_full and total < 1):
        return {}
    return {k: v / total for k, v in implied.items()}


def consensus(
    event: dict[str, Any], bookmakers: list[str] | None = None, market: str = "h2h"
) -> ConsensusOdds | None:
    per_team: dict[str, list[Decimal]] = {}
    used = 0
    for book in event.get("bookmakers", []):
        if bookmakers and book.get("key") not in bookmakers:
            continue
        mkt = next((m for m in book.get("markets", []) if m.get("key") == market), None)
        if not mkt:
            continue
        odds = {o["name"]: Decimal(str(o["price"])) for o in mkt.get("outcomes", [])}
        fair = devig(odds, require_full=market == "outrights")
        if not fair:
            continue
        used += 1
        for team, p in fair.items():
            per_team.setdefault(team, []).append(p)
    if not used:
        return None
    probs = {t: sum(ps, Decimal(0)) / len(ps) for t, ps in per_team.items()}
    return ConsensusOdds(
        event_id=str(event["id"]),
        home_team=str(event.get("home_team") or ""),
        away_team=str(event.get("away_team") or ""),
        commence_time=str(event.get("commence_time", "")),
        probs=probs,
        books_used=used,
    )


_STOP = {"the", "at", "vs", "v", "winner", "state", "st", "university", "of", "fc"}


def _tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", name.lower()) if t not in _STOP}


def team_match_score(kalshi_name: str, book_name: str) -> float:
    """Jaccard-ish token overlap; 1.0 == identical token sets."""
    a, b = _tokens(kalshi_name), _tokens(book_name)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def best_team(kalshi_name: str, candidates: list[str], min_score: float = 0.34) -> str | None:
    scored = sorted(((team_match_score(kalshi_name, c), c) for c in candidates), reverse=True)
    if not scored or scored[0][0] < min_score:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None  # ambiguous
    return scored[0][1]


class OddsClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.the-odds-api.com/v4",
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=15.0)
        self.requests_remaining: str | None = None

    def h2h(self, sport: str, bookmakers: list[str] | None = None) -> list[dict[str, Any]]:
        return self.odds(sport, "h2h", bookmakers)

    def outrights(self, sport: str, bookmakers: list[str] | None = None) -> list[dict[str, Any]]:
        return self.odds(sport, "outrights", bookmakers)

    def odds(
        self, sport: str, market: str, bookmakers: list[str] | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {
            "apiKey": self.api_key,
            "regions": "us,eu",
            "markets": market,
            "oddsFormat": "decimal",
        }
        if bookmakers:
            params["bookmakers"] = ",".join(bookmakers)
        resp = self._client.get(f"{self.base_url}/sports/{sport}/odds", params=params)
        resp.raise_for_status()
        self.requests_remaining = resp.headers.get("x-requests-remaining")
        return cast(list[dict[str, Any]], resp.json())
