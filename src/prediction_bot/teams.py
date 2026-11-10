from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from prediction_bot.odds import KALSHI_SERIES_TO_SPORT


def _validate_aliases(
    data: dict[str, dict[str, dict[str, list[str]]]],
) -> dict[str, dict[str, dict[str, list[str]]]]:
    for sport, teams in data.items():
        codes: dict[str, str] = {}
        names: dict[str, str] = {}
        for team_id, aliases in teams.items():
            for code in aliases["kalshi_codes"]:
                owner = codes.setdefault(code, team_id)
                if owner != team_id:
                    raise ValueError(f"Kalshi code {code!r} maps to multiple teams in {sport}")
            for name in aliases["odds_names"]:
                owner = names.setdefault(name, team_id)
                if owner != team_id:
                    raise ValueError(f"Odds name {name!r} maps to multiple teams in {sport}")
            for name in aliases.get("espn_names", []):
                owner = names.setdefault(name, team_id)
                if owner != team_id:
                    raise ValueError(f"ESPN name {name!r} maps to multiple teams in {sport}")
    return data


def _load_aliases() -> dict[str, dict[str, dict[str, list[str]]]]:
    path = Path(__file__).with_name("data") / "teams.json"
    data: dict[str, dict[str, dict[str, list[str]]]] = json.loads(path.read_text())
    return _validate_aliases(data)


_TEAMS = _load_aliases()


def _sport_for_series(series: str) -> str | None:
    return KALSHI_SERIES_TO_SPORT.get(series.split("-", 1)[0])


def team_for_code(series: str, code: str) -> str | None:
    sport = _sport_for_series(series)
    if sport is None:
        return None
    code = code.strip()
    return next(
        (
            team_id
            for team_id, aliases in _TEAMS.get(sport, {}).items()
            if code in aliases["kalshi_codes"]
        ),
        None,
    )


def team_name_for_code(series: str, code: str) -> str | None:
    sport = _sport_for_series(series)
    team_id = team_for_code(series, code)
    if sport is None or team_id is None:
        return None
    names = _TEAMS[sport][team_id]["odds_names"]
    return names[0] if names else team_id.replace("-", " ").title()


def mapped_odds_names(sport: str) -> set[str] | None:
    """All Odds API display names we can map for a sport, or None if the sport
    has no entries (unmapped sports can't use the events-count dyn layer)."""
    teams = _TEAMS.get(sport)
    if not teams:
        return None
    return {name for aliases in teams.values() for name in aliases["odds_names"]}


# v3 never evaluates these even when teams map (sportsbook_v2 blocks them as
# 'unsupported_series'), so no credit may be spent on them.
UNEVALUABLE_FEEDS = frozenset({"mma_mixed_martial_arts"})
_DRAW_CODES = frozenset({"TIE", "DRAW"})


def evaluable_feed(feed: str) -> bool:
    """Whether v3 can use an odds pull of `feed`: its teams map and it is supported."""
    return feed not in UNEVALUABLE_FEEDS and mapped_odds_names(feed) is not None


def event_teams_mapped(event_ticker: str, tickers: Iterable[str]) -> bool:
    """True when every team leg of the event (ticker suffix after the event) maps.

    Draw legs are ignored; an event with no team leg at all does not count as mapped.
    """
    series = event_ticker.split("-", 1)[0]
    prefix = f"{event_ticker}-"
    codes = {
        ticker[len(prefix) :]
        for ticker in tickers
        if ticker.startswith(prefix) and ticker[len(prefix) :].upper() not in _DRAW_CODES
    }
    return bool(codes) and all(team_for_code(series, code) is not None for code in codes)


def team_for_odds_name(sport: str, name: str) -> str | None:
    name = name.strip()
    return next(
        (
            team_id
            for team_id, aliases in _TEAMS.get(sport, {}).items()
            if name in aliases["odds_names"]
        ),
        None,
    )


def team_for_espn_name(sport: str, name: str) -> str | None:
    name = name.strip()
    return next(
        (
            team_id
            for team_id, aliases in _TEAMS.get(sport, {}).items()
            if name in aliases.get("espn_names", [])
        ),
        None,
    )


def odds_name_for(sport: str, team_id: str) -> str | None:
    """The primary Odds API display name for a team id, or None."""
    names = _TEAMS.get(sport, {}).get(team_id, {}).get("odds_names", [])
    return names[0] if names else None


def kalshi_name_ok(series: str, team_id: str, yes_sub_title: str) -> bool:
    sport = _sport_for_series(series)
    if sport is None:
        return False
    aliases: dict[str, Any] | None = _TEAMS.get(sport, {}).get(team_id)
    return aliases is not None and yes_sub_title.strip() in aliases["kalshi_names"]


def _norm(name: str) -> str:
    return " ".join(name.replace(".", "").casefold().split())


def team_for_reference_name(sport: str, name: str) -> str | None:
    """Team id for a name from a second reference venue (e.g. Polymarket), or None.

    An exact match on any known alias wins: optional `polymarket_names`, then Odds API,
    Kalshi and ESPN names. Otherwise a nickname ("Chiefs") is accepted only when exactly
    one team's Odds API name ends with it. Anything ambiguous fails closed.
    """
    wanted = _norm(name)
    if not wanted:
        return None
    teams = _TEAMS.get(sport, {})
    for key in ("polymarket_names", "odds_names", "kalshi_names", "espn_names"):
        exact = [
            team_id
            for team_id, aliases in teams.items()
            if wanted in {_norm(alias) for alias in aliases.get(key, [])}
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return None
    suffix = [
        team_id
        for team_id, aliases in teams.items()
        if any(_norm(alias).endswith(" " + wanted) for alias in aliases.get("odds_names", []))
    ]
    return suffix[0] if len(suffix) == 1 else None
