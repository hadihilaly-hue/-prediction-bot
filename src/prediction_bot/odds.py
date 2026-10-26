"""Sportsbook odds via The Odds API (https://the-odds-api.com). Free tier: 500 req/month."""

from __future__ import annotations

import json
import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any, cast

import httpx

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from prediction_bot.cadence import CadenceCalendar
    from prediction_bot.paper.ledger import Ledger


def _format_odds_time(value: datetime) -> str:
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def per_run_budget(
    remaining: int,
    runs_per_day: float,
    now: datetime | None = None,
    days_left_in_month: int | None = None,
) -> int:
    """Whole requests a single run may spend; never more than its share, so the quota
    can't hit zero before the reset."""
    if days_left_in_month is not None:
        runs_left = max(1, math.ceil(max(1, days_left_in_month) * runs_per_day))
        return math.floor(max(0, remaining) / runs_left)
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
        self.requests_used: str | None = None
        # Max quota-costing requests this client may make; None = unlimited.
        self.budget = budget
        self.requests_made = 0
        self.odds_requests_made = 0
        self.requests_last_by_sport: dict[str, int | None] = {}
        self.events_free = True
        self._events_requests_used_baseline: int | None = None
        self._memo: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}
        self.allowed_sports: set[str] | None = None

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
        if self.allowed_sports is not None and sport not in self.allowed_sports:
            return []
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
        self.odds_requests_made += 1
        resp = self._client.get(f"{self.base_url}/sports/{sport}/odds", params=params)
        resp.raise_for_status()
        self.requests_remaining = resp.headers.get("x-requests-remaining")
        self.requests_used = resp.headers.get("x-requests-used")
        requests_last = resp.headers.get("x-requests-last")
        try:
            requests_last_value = int(requests_last) if requests_last is not None else None
        except ValueError:
            requests_last_value = None
        if sport in self.requests_last_by_sport:
            previous_requests_last = self.requests_last_by_sport[sport]
            if requests_last_value is not None:
                self.requests_last_by_sport[sport] = (
                    previous_requests_last or 0
                ) + requests_last_value
        else:
            self.requests_last_by_sport[sport] = requests_last_value
        log.info("odds api: %s x-requests-last=%s", sport, requests_last)
        if requests_last != "1":
            log.error(
                "odds api: expected x-requests-last=1 for %s, got %r; halting odds pulls",
                sport,
                requests_last,
            )
            self.budget = self.requests_made
        data = cast(list[dict[str, Any]], resp.json())
        self._memo[key] = (time.monotonic(), data)
        return data

    def quota_remaining(self) -> int | None:
        """Requests left this month. `/sports` doesn't count against the quota."""
        resp = self._client.get(f"{self.base_url}/sports", params={"apiKey": self.api_key})
        resp.raise_for_status()
        left = resp.headers.get("x-requests-remaining")
        self.requests_remaining = left
        self.requests_used = resp.headers.get("x-requests-used")
        try:
            self._events_requests_used_baseline = (
                int(self.requests_used) if self.requests_used is not None else None
            )
        except ValueError:
            self._events_requests_used_baseline = None
        try:
            return int(float(left)) if left is not None else None
        except ValueError:
            return None

    def events(
        self,
        sport: str,
        commence_from: datetime,
        commence_to: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch free event metadata for a sport and commencement window."""
        if not self.events_free:
            return []
        params = {
            "apiKey": self.api_key,
            "commenceTimeFrom": _format_odds_time(commence_from),
            "commenceTimeTo": _format_odds_time(commence_to),
        }
        resp = self._client.get(f"{self.base_url}/sports/{sport}/events", params=params)
        resp.raise_for_status()
        self.requests_remaining = resp.headers.get("x-requests-remaining")
        self.requests_used = resp.headers.get("x-requests-used")
        try:
            used = int(self.requests_used) if self.requests_used is not None else None
        except ValueError:
            used = None
        baseline = self._events_requests_used_baseline
        if baseline is not None and used is not None and used > baseline:
            log.error(
                "odds api events endpoint consumed quota for %s (%d -> %d); "
                "disabling further free event checks",
                sport,
                baseline,
                used,
            )
            self.requests_made += used - baseline
            self.events_free = False
        if used is not None:
            self._events_requests_used_baseline = used
        return cast(list[dict[str, Any]], resp.json())


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class OddsPacer:
    """Sets `client.budget` at the start of each run to that run's share of the quota
    left this month. Unspent fractions carry over, so a frequent loop (share < 1 request
    per cycle) still fetches every few cycles instead of never. If the quota can't be
    read the run gets no odds requests (fail closed) but everything else still runs."""

    def __init__(
        self,
        client: OddsClient,
        runs_per_day: float,
        ledger: Ledger | None = None,
        trigger_mode: bool = False,
        cadence_mode: str = "legacy",
        calendar: CadenceCalendar | None = None,
        day_overrides: dict[date, float] | None = None,
    ) -> None:
        self.client = client
        self.runs_per_day = runs_per_day
        self.ledger = ledger
        self.trigger_mode = trigger_mode
        self.cadence_mode = cadence_mode if calendar is not None else "legacy"
        self.calendar = calendar
        self.day_overrides = day_overrides or {}
        self.credit = 0.0

    def _cadence_start(self, left: int, now: datetime) -> int:
        from prediction_bot.cadence import build_plan

        assert self.calendar is not None
        today = _utc(now).date()
        dyn: dict[date, int] | None = None
        if self.cadence_mode == "calendar":
            dyn = self._dyn_counts(today)
        plan = build_plan(
            now,
            left,
            self.runs_per_day,
            self.calendar,
            dyn=dyn,
            overrides=self.day_overrides,
        )
        self.credit = min(self.credit + plan.share, float(max(0, left)))
        # Cadence rounds the carried share: <0.5 nothing, then up to 3 feeds.
        self.client.budget = max(0, min(3, int(math.floor(self.credit + 0.5))))
        if self.ledger is not None:
            self.ledger.record_cadence_day(
                plan.day,
                static_w=plan.static_w,
                dyn_count=plan.dyn_count,
                dyn_json=json.dumps({d.isoformat(): n for d, n in (dyn or {}).items()})
                if dyn is not None
                else None,
                blended_w=plan.blended_w,
                budget=plan.budget,
                mode=self.cadence_mode,
                at=now,
            )
        log.info(
            "odds api: %d requests left; cadence %s budget %.2f share %.2f -> %d feeds",
            left,
            self.cadence_mode,
            plan.budget,
            plan.share,
            self.client.budget,
        )
        return self.client.budget

    def _dyn_counts(self, today: date) -> dict[date, int] | None:
        """One free /events call per active sport per day, cached in cadence_days."""
        assert self.calendar is not None
        from prediction_bot.cadence import dynamic_counts
        from prediction_bot.teams import mapped_odds_names

        if self.ledger is not None:
            cached = self.ledger.cadence_day(today)
            if cached is not None and cached.get("dyn_json"):
                try:
                    return {
                        date.fromisoformat(d): int(n)
                        for d, n in json.loads(str(cached["dyn_json"])).items()
                    }
                except (ValueError, TypeError):
                    pass
        if self.client.requests_used is not None:
            self.client._events_requests_used_baseline = int(self.client.requests_used)
        commence_from = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        commence_from += timedelta(days=self.calendar.shift_lo)
        commence_to = commence_from + timedelta(days=10)
        sports: set[str] = set()
        for i in range(7):
            sports |= self.calendar.active_sports(today + timedelta(days=i))
        events: list[dict[str, Any]] = []
        for sport in sorted(sports):
            if mapped_odds_names(sport) is None:
                continue
            try:
                events += [
                    {"sport_key": sport, **e}
                    for e in self.client.events(sport, commence_from, commence_to)
                ]
            except httpx.HTTPError:
                log.warning("odds api events fetch failed for %s; skipping", sport)
        if not self.client.events_free:
            log.warning("odds api: /events is not free; cadence dyn layer disabled")
            return None
        dyn = dynamic_counts(events, today, self.calendar, mapped_odds_names)
        if self.ledger is not None:
            self.ledger.record_cadence_day(
                today,
                static_w=self.calendar.static_weight(today),
                dyn_count=dyn.get(today),
                dyn_json=json.dumps({d.isoformat(): n for d, n in dyn.items()}),
                blended_w=self.calendar.static_weight(today),
                budget=0.0,
                mode=self.cadence_mode,
                at=datetime.now(timezone.utc),
            )
        return dyn

    def start_run(self, now: datetime | None = None) -> int:
        if self.ledger is None:
            self.credit -= self.client.requests_made
        else:
            self.credit = self.ledger.odds_pacer_carry()
        self.client.requests_made = 0
        self.client.odds_requests_made = 0
        self.client.requests_last_by_sport.clear()
        self.client.events_free = True
        if self.trigger_mode and self.credit < 0:
            self.client.budget = 0
            self.client._events_requests_used_baseline = None
            return 0
        try:
            left = self.client.quota_remaining()
        except httpx.HTTPError as e:
            log.warning("odds api quota check failed (%s); no odds requests this run", e)
            left = None
        if left is None:
            self.client.budget = 0
            return 0
        if self.trigger_mode:
            self.client.budget = min(1, max(0, left))
            self.client._events_requests_used_baseline = (
                int(self.client.requests_used) if self.client.requests_used else None
            )
            log.info("odds api: trigger run budget %d", self.client.budget)
            return self.client.budget
        if self.cadence_mode != "legacy":
            return self._cadence_start(left, now or datetime.now(timezone.utc))
        share = run_share(left, self.runs_per_day, now)
        self.credit = min(self.credit + share, float(max(0, left)))
        self.client.budget = max(0, math.floor(self.credit + 1e-9))
        self.client._events_requests_used_baseline = (
            int(self.client.requests_used) if self.client.requests_used else None
        )
        log.info("odds api: %d requests left this month, run budget %d", left, self.client.budget)
        return self.client.budget

    def finish_run(self, now: datetime | None = None, feeds: list[str] | None = None) -> None:
        if self.ledger is None:
            return
        carry = max(-1.0, min(self.credit - self.client.requests_made, 1.0))
        self.ledger.record_odds_pacer_carry(carry, now)
        self.ledger.record_cadence_pulls(
            _utc(now or datetime.now(timezone.utc)).date(),
            self.client.odds_requests_made,
            json.dumps(sorted(feeds or [])),
            now,
            mode=self.cadence_mode,
        )
