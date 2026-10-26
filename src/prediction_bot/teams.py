from __future__ import annotations

import json
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
