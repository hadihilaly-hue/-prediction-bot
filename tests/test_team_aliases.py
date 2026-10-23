from __future__ import annotations

import pytest

from prediction_bot.teams import (
    _TEAMS,
    _sport_for_series,
    _validate_aliases,
    kalshi_name_ok,
    team_for_code,
    team_for_odds_name,
)


@pytest.mark.parametrize(
    ("sport", "teams"),
    [
        (
            "soccer_usa_mls",
            ("la-galaxy", "los-angeles-fc"),
        ),
        (
            "americanfootball_nfl",
            (
                "miami-dolphins",
                "los-angeles-rams",
                "new-york-jets",
                "new-york-giants",
            ),
        ),
        (
            "americanfootball_ncaaf",
            (
                "miami-oh-redhawks",
                "miami-hurricanes",
                "texas-longhorns",
                "texas-a-m-aggies",
                "texas-tech-red-raiders",
                "ohio-state-buckeyes",
                "ohio-bobcats",
                "south-carolina-gamecocks",
                "usc-trojans",
                "washington-huskies",
                "washington-state-cougars",
                "georgia-bulldogs",
                "georgia-tech-yellow-jackets",
                "georgia-state-panthers",
            ),
        ),
    ],
)
def test_ambiguous_team_aliases_are_exact(sport: str, teams: tuple[str, ...]) -> None:
    for team_id in teams:
        aliases = _TEAMS[sport][team_id]
        series = {
            "soccer_usa_mls": "KXMLSGAME",
            "americanfootball_nfl": "KXNFLGAME",
            "americanfootball_ncaaf": "KXNCAAFGAME",
        }[sport]
        assert len(aliases["kalshi_codes"]) == 1
        assert team_for_code(series, aliases["kalshi_codes"][0]) == team_id
        assert team_for_odds_name(sport, aliases["odds_names"][0]) == team_id
        assert kalshi_name_ok(series, team_id, aliases["kalshi_names"][0])
        assert team_for_code(series, aliases["kalshi_codes"][0].lower()) is None


def test_exact_manual_titles_disambiguate_miami_and_los_angeles() -> None:
    assert team_for_code("KXNFLGAME", "MIA") == "miami-dolphins"
    assert team_for_code("KXNCAAFGAME", "MIA") == "miami-hurricanes"
    assert team_for_code("KXNCAAFGAME", "MOH") == "miami-oh-redhawks"
    assert team_for_code("KXMLSGAME", "LAFC") == "los-angeles-fc"
    assert team_for_code("KXMLSGAME", "LAG") == "la-galaxy"
    assert kalshi_name_ok("KXNCAAFGAME", "miami-hurricanes", "Miami (FL)")
    assert kalshi_name_ok("KXMLSGAME", "los-angeles-fc", "Los Angeles F")
    assert kalshi_name_ok("KXMLSGAME", "la-galaxy", "Los Angeles G")
    assert not kalshi_name_ok("KXMLSGAME", "la-galaxy", "Los Angeles FC")


def test_unverified_mlb_codes_fail_closed() -> None:
    assert team_for_code("KXNFLGAME", "LAC") == "los-angeles-chargers"
    for code in ("SD", "LAD", "ATL"):
        assert team_for_code("KXMLBGAME", code) is None


def test_series_resolution_and_unknown_aliases() -> None:
    assert _sport_for_series("KXNFLGAME-26OCT04KCLV") == "americanfootball_nfl"
    assert _sport_for_series("UNKNOWN") is None
    assert team_for_code("UNKNOWN", "KC") is None
    assert team_for_odds_name("americanfootball_nfl", "los angeles chargers") is None
    assert (
        team_for_odds_name("americanfootball_nfl", "Los Angeles Chargers") == "los-angeles-chargers"
    )


def test_alias_validation_rejects_code_and_name_collisions() -> None:
    duplicate_code = {
        "sport": {
            "team-a": {"kalshi_codes": ["A"], "kalshi_names": [], "odds_names": []},
            "team-b": {"kalshi_codes": ["A"], "kalshi_names": [], "odds_names": []},
        }
    }
    duplicate_name = {
        "sport": {
            "team-a": {"kalshi_codes": [], "kalshi_names": [], "odds_names": ["Name"]},
            "team-b": {"kalshi_codes": [], "kalshi_names": [], "odds_names": ["Name"]},
        }
    }
    with pytest.raises(ValueError, match="Kalshi code"):
        _validate_aliases(duplicate_code)
    with pytest.raises(ValueError, match="Odds name"):
        _validate_aliases(duplicate_name)
