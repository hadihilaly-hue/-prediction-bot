"""Sportsbook odds via The Odds API (https://the-odds-api.com). Free tier: 500 req/month."""

from __future__ import annotations

import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timezone
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any, cast

import httpx

log = logging.getLogger(__name__)

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
    "KXLALIGAGAME": "soccer_spain_la_liga",
    "KXSERIEAGAME": "soccer_italy_serie_a",
    "KXBUNDESLIGAGAME": "soccer_germany_bundesliga",
    "KXLIGUE1GAME": "soccer_france_ligue_one",
    "KXUELGAME": "soccer_uefa_europa_league",
    "KXUECLGAME": "soccer_uefa_europa_conference_league",
    "KXLIGAMXGAME": "soccer_mexico_ligamx",
    "KXUFCFIGHT": "mma_mixed_martial_arts",
}

# Kalshi tournament-winner series (one market per player) -> The Odds API "outrights" sport key.
# The free tier only carries golf's four majors; Kalshi lists these a few weeks before each.
KALSHI_SERIES_TO_OUTRIGHT: dict[str, str] = {
    "KXMASTERS": "golf_masters_tournament_winner",
    "KXPGA": "golf_pga_championship_winner",
    "KXUSOPEN": "golf_us_open_winner",
    "KXTHEOPEN": "golf_the_open_championship_winner",
}


# Relative trust in each book's line when forming the consensus. Sharp, early-posting
# books (Pinnacle, Circa, BetOnline) carry the most signal far out from game time —
# exactly the window the min-lead gate restricts entries to — while soft recreational
# books track them with a lag, so equal weighting would average real edge away.
BOOK_WEIGHTS: dict[str, Decimal] = {
    "pinnacle": Decimal(3),
    "circa": Decimal(3),
    "betonlineag": Decimal(2),
}


@dataclass(frozen=True)
class ConsensusOdds:
    """De-vigged win probabilities for one game, weighted-averaged across bookmakers."""

    event_id: str
    home_team: str
    away_team: str
    commence_time: str
    probs: dict[str, Decimal]  # team/player name -> probability (plus "Draw" for soccer)
    books_used: int
    book_updates: dict[str, str] = dataclass_field(default_factory=dict)
    book_odds: dict[str, dict[str, Decimal]] = dataclass_field(default_factory=dict)

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


MIN_FIELD_COVERAGE = Decimal("0.9")


def consensus(
    event: dict[str, Any], bookmakers: list[str] | None = None, market: str = "h2h"
) -> ConsensusOdds | None:
    per_team: dict[str, list[tuple[Decimal, Decimal]]] = {}
    used = 0
    quotes: list[dict[str, Decimal]] = []
    weights: list[Decimal] = []
    book_odds: dict[str, dict[str, Decimal]] = {}
    book_updates: dict[str, str] = {}
    for book in event.get("bookmakers", []):
        if bookmakers and book.get("key") not in bookmakers:
            continue
        mkt = next((m for m in book.get("markets", []) if m.get("key") == market), None)
        if not mkt:
            continue
        if isinstance(book.get("last_update"), str):
            book_updates[str(book.get("key", ""))] = str(book["last_update"])
        odds = {o["name"]: Decimal(str(o["price"])) for o in mkt.get("outcomes", [])}
        quotes.append(odds)
        book_odds[str(book.get("key", ""))] = odds
        weights.append(BOOK_WEIGHTS.get(str(book.get("key")), Decimal(1)))
    # The feed never says how big the real field is; the widest book is the best proxy,
    # and a book quoting far fewer names than that is pricing a subset, not the field.
    field = max((len(q) for q in quotes), default=0)
    for odds, weight in zip(quotes, weights, strict=True):
        if market == "outrights" and len(odds) < field * MIN_FIELD_COVERAGE:
            continue
        fair = devig(odds, require_full=market == "outrights")
        if not fair:
            continue
        used += 1
        for team, p in fair.items():
            per_team.setdefault(team, []).append((p, weight))
    if not used:
        return None
    probs = {
        t: sum((p * w for p, w in pws), Decimal(0)) / sum((w for _, w in pws), Decimal(0))
        for t, pws in per_team.items()
    }
    return ConsensusOdds(
        event_id=str(event["id"]),
        home_team=str(event.get("home_team") or ""),
        away_team=str(event.get("away_team") or ""),
        commence_time=str(event.get("commence_time", "")),
        probs=probs,
        books_used=used,
        book_updates=book_updates,
        book_odds=book_odds,
    )


_STOP = {
    "the", "at", "vs", "v", "winner", "state", "st", "university", "of", "fc",
    "ac", "afc", "cf", "sc", "fk", "calcio", "club", "and", "de", "stade",
}  # fmt: skip

# Kalshi short names the books spell differently enough that token overlap misses them.
_ALIASES = {
    "psg": "paris saint germain",
    "stade rennais": "rennes",
    "sporting cp": "sporting lisbon",
    "slavia prague": "slavia praha",
}

_TRANSLIT = str.maketrans({"ø": "oe", "æ": "ae", "ß": "ss", "ł": "l", "đ": "d"})


def _normalise(name: str) -> str:
    """Lower-case, strip accents ("Atlético" -> "atletico") and apostrophes ("Lone'er")."""
    name = unicodedata.normalize("NFKD", name.lower().translate(_TRANSLIT))
    name = "".join(ch for ch in name if not unicodedata.combining(ch))
    return re.sub(r"['’`]", "", name)


def _tokens(name: str) -> set[str]:
    norm = _normalise(name).strip()
    norm = _ALIASES.get(norm, norm)
    toks = set(re.findall(r"[a-z0-9]+", norm))
    # "Abdul-Rakhman" vs "Abdulrakhman": keep the hyphen-joined form as well.
    toks |= {w.replace("-", "") for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)+", norm)}
    return {t for t in toks if t not in _STOP}


def _same_token(a: str, b: str) -> bool:
    """Exact, or a one-letter transliteration slip in a long name ("Jasmin"/"Iasmin")."""
    if a == b:
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.8


# A Kalshi short name whose every token appears in the book's longer name ("Brighton" in
# "Brighton and Hove Albion") scores exactly the match threshold: enough to match, but an
# exact-length name ("Texas" vs "Texas Longhorns") still outranks it.
SUBSET_SCORE = 0.34


def team_match_score(kalshi_name: str, book_name: str) -> float:
    """Jaccard-ish token overlap; 1.0 == identical token sets."""
    a, b = _tokens(kalshi_name), _tokens(book_name)
    if not a or not b:
        return 0.0
    unmatched = set(b)
    common = 0
    for t in sorted(a):
        hit = (
            t if t in unmatched else next((u for u in sorted(unmatched) if _same_token(t, u)), None)
        )
        if hit is not None:
            unmatched.discard(hit)
            common += 1
    score = common / (len(a) + len(b) - common)
    if common == len(a) and score < SUBSET_SCORE:
        return SUBSET_SCORE
    return score


def best_team(kalshi_name: str, candidates: list[str], min_score: float = 0.34) -> str | None:
    scored = sorted(((team_match_score(kalshi_name, c), c) for c in candidates), reverse=True)
    if not scored or scored[0][0] < min_score:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None  # ambiguous
    return scored[0][1]


class OddsBudgetExhausted(RuntimeError):
    """This run already spent its share of the monthly Odds API quota."""


def run_share(remaining: int, runs_per_day: float, now: datetime | None = None) -> float:
    """This run's even share of the quota left this month (free tier resets on the 1st UTC)."""
    now = now or datetime.now(timezone.utc)
    nxt = datetime(now.year + now.month // 12, now.month % 12 + 1, 1, tzinfo=timezone.utc)
    runs_left = max(1, math.ceil((nxt - now).total_seconds() / 86400 * runs_per_day))
    return max(0, remaining) / runs_left


def per_run_budget(remaining: int, runs_per_day: float, now: datetime | None = None) -> int:
    """Whole requests a single run may spend; never more than its share, so the quota
    can't hit zero before the reset."""
    return math.floor(run_share(remaining, runs_per_day, now))


class OddsClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.the-odds-api.com/v4",
        client: httpx.Client | None = None,
        budget: int | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=15.0)
        self.requests_remaining: str | None = None
        # Max quota-costing requests this client may make; None = unlimited.
        self.budget = budget
        self.requests_made = 0
        self._memo: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}

    # Strategies sharing one client within a cycle reuse the response instead of
    # spending another quota request; later cycles (minutes apart) refetch.
    MEMO_TTL_S = 60.0

    def h2h(
        self,
        sport: str,
        bookmakers: list[str] | None = None,
        *,
        bypass_cache: bool = False,
    ) -> list[dict[str, Any]]:
        return self.odds(sport, "h2h", bookmakers, bypass_cache=bypass_cache)

    def outrights(self, sport: str, bookmakers: list[str] | None = None) -> list[dict[str, Any]]:
        return self.odds(sport, "outrights", bookmakers)

    def odds(
        self,
        sport: str,
        market: str,
        bookmakers: list[str] | None = None,
        *,
        bypass_cache: bool = False,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {
            "apiKey": self.api_key,
            "regions": "us,eu",
            "markets": market,
            "oddsFormat": "decimal",
        }
        if bookmakers:
            params["bookmakers"] = ",".join(bookmakers)
        key = (sport, market, params.get("bookmakers", ""))
        hit = None if bypass_cache else self._memo.get(key)
        if hit and time.monotonic() - hit[0] < self.MEMO_TTL_S:
            return hit[1]
        if self.budget is not None and self.requests_made >= self.budget:
            raise OddsBudgetExhausted(f"odds budget of {self.budget} requests spent")
        self.requests_made += 1
        resp = self._client.get(f"{self.base_url}/sports/{sport}/odds", params=params)
        resp.raise_for_status()
        self.requests_remaining = resp.headers.get("x-requests-remaining")
        data = cast(list[dict[str, Any]], resp.json())
        self._memo[key] = (time.monotonic(), data)
        return data

    def quota_remaining(self) -> int | None:
        """Requests left this month. `/sports` doesn't count against the quota."""
        resp = self._client.get(f"{self.base_url}/sports", params={"apiKey": self.api_key})
        resp.raise_for_status()
        left = resp.headers.get("x-requests-remaining")
        self.requests_remaining = left
        try:
            return int(float(left)) if left is not None else None
        except ValueError:
            return None


class OddsPacer:
    """Sets `client.budget` at the start of each run to that run's share of the quota
    left this month. Unspent fractions carry over, so a frequent loop (share < 1 request
    per cycle) still fetches every few cycles instead of never. If the quota can't be
    read the run gets no odds requests (fail closed) but everything else still runs."""

    def __init__(self, client: OddsClient, runs_per_day: float) -> None:
        self.client = client
        self.runs_per_day = runs_per_day
        self.credit = 0.0

    def start_run(self, now: datetime | None = None) -> int:
        self.credit -= self.client.requests_made
        self.client.requests_made = 0
        try:
            left = self.client.quota_remaining()
        except httpx.HTTPError as e:
            log.warning("odds api quota check failed (%s); no odds requests this run", e)
            left = None
        if left is None:
            self.client.budget = 0
            return 0
        share = run_share(left, self.runs_per_day, now)
        self.credit = min(self.credit + share, float(max(0, left)))
        self.client.budget = max(0, math.floor(self.credit))
        log.info("odds api: %d requests left this month, run budget %d", left, self.client.budget)
        return self.client.budget
