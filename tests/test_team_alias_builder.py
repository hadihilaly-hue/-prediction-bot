from __future__ import annotations

from pathlib import Path

import pytest

from scripts.build_team_aliases import (
    build,
    build_alias_proposal,
    canonical_id,
    event_date,
    event_market_code,
    fetch_odds_events,
    render_report,
)


def test_ticker_parsers_and_canonical_id() -> None:
    assert event_date("KXNFLGAME-26OCT04KCLV") is not None
    assert event_date("KXNFLGAME-invalid") is None
    assert event_market_code("KXNFLGAME-26OCT04KCLV", "KXNFLGAME-26OCT04KCLV-KC") == "KC"
    assert event_market_code("event", "other-market") is None
    assert canonical_id("New York Jets") == "new-york-jets"


def test_alias_proposal_requires_both_legs_and_unique_date_pairing() -> None:
    kalshi = {
        "KXNFLGAME": [
            {
                "event_ticker": "KXNFLGAME-26OCT25KCLV",
                "markets": [
                    {
                        "ticker": "KXNFLGAME-26OCT25KCLV-KC",
                        "yes_sub_title": "Kansas City",
                    },
                    {
                        "ticker": "KXNFLGAME-26OCT25KCLV-LV",
                        "yes_sub_title": "Las Vegas",
                    },
                ],
            }
        ]
    }
    odds = {
        "americanfootball_nfl": [
            {
                "home_team": "Kansas City Chiefs",
                "away_team": "Las Vegas Raiders",
                "commence_time": "2026-10-25T20:25:00Z",
            }
        ]
    }
    proposal, report = build_alias_proposal(kalshi, odds)
    assert proposal["americanfootball_nfl"]["kansas-city-chiefs"]["kalshi_codes"] == ["KC"]
    assert proposal["americanfootball_nfl"]["las-vegas-raiders"]["kalshi_codes"] == ["LV"]
    assert report["americanfootball_nfl"]["codes_unmatched"] == []

    odds["americanfootball_nfl"].append(
        {
            "home_team": "Kansas City Chiefs",
            "away_team": "Las Vegas Raiders",
            "commence_time": "2026-10-25T20:30:00Z",
        }
    )
    _, ambiguous = build_alias_proposal(kalshi, odds)
    assert "KXNFLGAME-26OCT25KCLV" in ambiguous["americanfootball_nfl"]["unmatched_events"]


def test_rules_primary_disambiguates_shared_city_short_names() -> None:
    kalshi = {
        "KXNBAGAME": [
            {
                "event_ticker": "KXNBAGAME-26OCT25LALCLP",
                "markets": [
                    {
                        "ticker": "KXNBAGAME-26OCT25LALCLP-LAL",
                        "yes_sub_title": "Los Angeles L",
                        "rules_primary": (
                            "If Los Angeles Lakers wins the Los Angeles Lakers vs Los Angeles "
                            "Clippers NBA game, the market resolves yes."
                        ),
                    },
                    {
                        "ticker": "KXNBAGAME-26OCT25LALCLP-LAC",
                        "yes_sub_title": "Los Angeles C",
                        "rules_primary": (
                            "If Los Angeles Clippers win the Los Angeles Lakers vs Los Angeles "
                            "Clippers NBA game, the market resolves yes."
                        ),
                    },
                ],
            }
        ]
    }
    odds = {
        "basketball_nba": [
            {
                "home_team": "Los Angeles Lakers",
                "away_team": "Los Angeles Clippers",
                "commence_time": "2026-10-25T20:00:00Z",
            }
        ]
    }

    proposal, report = build_alias_proposal(kalshi, odds)

    assert proposal["basketball_nba"]["los-angeles-lakers"]["kalshi_codes"] == ["LAL"]
    assert proposal["basketball_nba"]["los-angeles-clippers"]["kalshi_codes"] == ["LAC"]
    assert report["basketball_nba"]["codes_unmatched"] == []


def test_fetch_events_checks_response_and_headers() -> None:
    class Response:
        status_code = 200
        headers = {"x-requests-last": "0", "x-requests-used": "341", "x-requests-remaining": "159"}

        @staticmethod
        def json() -> list[dict[str, str]]:
            return [{"id": "one"}]

    class Client:
        def get(self, url: str, params: dict[str, str]) -> Response:
            assert url.endswith("/sports/americanfootball_nfl/events")
            assert params == {"apiKey": "not-logged"}
            return Response()

    events, headers = fetch_odds_events(Client(), "americanfootball_nfl", "not-logged")
    assert events == [{"id": "one"}]
    assert headers["x-requests-last"] == "0"


def test_build_stops_when_events_endpoint_has_a_cost(tmp_path: Path) -> None:
    class Response:
        status_code = 200
        headers = {"x-requests-last": "1"}

        @staticmethod
        def json() -> list[dict[str, str]]:
            return []

    class Client:
        urls: list[str] = []

        def get(self, url: str, params: dict[str, str]) -> Response:
            self.urls.append(url)
            return Response()

    class Kalshi:
        def get_events(self, **kwargs: object) -> list[dict[str, object]]:
            return []

    client = Client()
    with pytest.raises(RuntimeError, match="non-zero x-requests-last"):
        build("key", tmp_path / "teams.json", tmp_path / "report.md", Kalshi(), client)
    assert len(client.urls) == 1
    assert client.urls[0].endswith("/events")


def test_build_uses_only_events_and_writes_proposal_and_report(tmp_path: Path) -> None:
    class Kalshi:
        def get_events(self, **kwargs: object) -> list[dict[str, object]]:
            return []

    class Response:
        status_code = 200
        headers = {
            "x-requests-last": "0",
            "x-requests-used": "341",
            "x-requests-remaining": "159",
        }

        @staticmethod
        def json() -> list[object]:
            return []

    class Http:
        urls: list[str] = []

        def get(self, url: str, params: dict[str, str]) -> Response:
            self.urls.append(url)
            return Response()

    http = Http()
    proposal_path = tmp_path / "teams.json"
    report_path = tmp_path / "report.md"
    proposal, report = build("key", proposal_path, report_path, Kalshi(), http)
    assert all(not teams for teams in proposal.values())
    assert report
    assert len(http.urls) == len({sport for sport in report})
    assert all(url.endswith("/events") and "/odds" not in url for url in http.urls)
    assert proposal_path.exists()
    assert "Odds API request headers" in report_path.read_text()


def test_report_includes_unmatched_mappings_and_credits() -> None:
    text = render_report(
        {
            "americanfootball_nfl": {
                "series": "KXNFLGAME",
                "teams_mapped": 1,
                "codes_seen": ["KC", "LAC"],
                "codes_unmatched": ["LAC"],
                "odds_names_seen": ["Los Angeles Chargers"],
                "odds_names_unmatched": ["Los Angeles Chargers"],
                "unmatched_events": ["KXNFLGAME-26OCT04KCLAC"],
            }
        },
        [{"x-requests-last": "0", "x-requests-used": "341", "x-requests-remaining": "159"}],
    )
    assert "LAC" in text
    assert "Los Angeles Chargers" in text
    assert "x-requests-last=0" in text
