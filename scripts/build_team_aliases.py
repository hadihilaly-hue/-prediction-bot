from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from prediction_bot.config import KALSHI_PROD_REST
from prediction_bot.odds import KALSHI_SERIES_TO_SPORT, best_team
from prediction_bot.venues.kalshi import KalshiClient

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ET = ZoneInfo("America/New_York")
DRAW_NAMES = {"draw", "tie"}
SUPPORTED_SERIES = {
    series: sport
    for series, sport in KALSHI_SERIES_TO_SPORT.items()
    if sport != "mma_mixed_martial_arts"
}


def event_date(ticker: str) -> date | None:
    match = re.match(r"^[^-]+-(\d{2}[A-Z]{3}\d{2})", ticker)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%y%b%d").date()
    except ValueError:
        return None


def event_market_code(event_ticker: str, market_ticker: str) -> str | None:
    prefix = f"{event_ticker}-"
    if not market_ticker.startswith(prefix):
        return None
    return market_ticker[len(prefix) :]


def canonical_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def build_alias_proposal(
    kalshi_events: dict[str, list[dict[str, Any]]],
    odds_events: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, dict[str, dict[str, list[str]]]], dict[str, dict[str, Any]]]:
    proposal: dict[str, dict[str, dict[str, set[str]]]] = defaultdict(
        lambda: defaultdict(lambda: {"kalshi_codes": set(), "kalshi_names": set(), "odds_names": set()})
    )
    report: dict[str, dict[str, Any]] = {}
    for series, sport in SUPPORTED_SERIES.items():
        seen_codes: set[str] = set()
        paired_codes: set[str] = set()
        seen_names = {
            name
            for event in odds_events.get(sport, [])
            for name in (event.get("home_team"), event.get("away_team"))
            if isinstance(name, str) and name
        }
        paired_names: set[str] = set()
        unmatched_events: list[str] = []
        series_events = kalshi_events.get(series, [])
        games = odds_events.get(sport, [])
        for event in series_events:
            event_ticker = str(event.get("event_ticker") or event.get("ticker") or "")
            ticker_day = event_date(event_ticker)
            if ticker_day is None:
                unmatched_events.append(event_ticker or "(missing event ticker)")
                continue
            team_markets: list[tuple[str, str]] = []
            for market in event.get("markets", []):
                if not isinstance(market, dict):
                    continue
                market_ticker = str(market.get("ticker") or "")
                code = event_market_code(event_ticker, market_ticker)
                name = str(market.get("yes_sub_title") or market.get("subtitle") or "").strip()
                if (
                    not code
                    or not name
                    or code.casefold() in DRAW_NAMES
                    or name.casefold() in DRAW_NAMES
                ):
                    continue
                seen_codes.add(code)
                team_markets.append((code, name))
            if len(team_markets) < 2:
                unmatched_events.append(event_ticker)
                continue
            candidates: list[tuple[dict[str, Any], dict[str, str]]] = []
            for game in games:
                home, away = game.get("home_team"), game.get("away_team")
                commence = game.get("commence_time")
                if not isinstance(home, str) or not isinstance(away, str):
                    continue
                if not isinstance(commence, str):
                    continue
                try:
                    start = datetime.fromisoformat(commence.replace("Z", "+00:00"))
                    start_day = start.astimezone(ET).date()
                except ValueError:
                    continue
                if start_day not in (ticker_day, ticker_day + timedelta(days=1)):
                    continue
                assignment: dict[str, str] = {}
                for code, name in team_markets:
                    matched = best_team(name, [home, away])
                    if matched is None or matched in assignment.values():
                        assignment = {}
                        break
                    assignment[code] = matched
                if len(assignment) == len(team_markets) and set(assignment.values()) == {
                    home,
                    away,
                }:
                    candidates.append((game, assignment))
            if len(candidates) != 1:
                unmatched_events.append(event_ticker)
                continue
            game, assignment = candidates[0]
            for code, name in team_markets:
                odds_name = assignment[code]
                team = proposal[sport][canonical_id(odds_name)]
                team["kalshi_codes"].add(code)
                team["kalshi_names"].add(name)
                team["odds_names"].add(odds_name)
                paired_codes.add(code)
                paired_names.add(odds_name)
        report[sport] = {
            "series": series,
            "teams_mapped": len(proposal[sport]),
            "codes_seen": sorted(seen_codes),
            "codes_unmatched": sorted(seen_codes - paired_codes),
            "odds_names_seen": sorted(seen_names),
            "odds_names_unmatched": sorted(seen_names - paired_names),
            "unmatched_events": sorted(set(unmatched_events)),
        }
    normalized = {
        sport: {
            team_id: {
                key: sorted(values)
                for key, values in team.items()
            }
            for team_id, team in teams.items()
        }
        for sport, teams in proposal.items()
    }
    return normalized, report


def fetch_odds_events(
    client: httpx.Client, sport: str, api_key: str
) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    try:
        response = client.get(
            f"{ODDS_API_BASE}/sports/{sport}/events",
            params={"apiKey": api_key},
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"The Odds API events request failed: {type(exc).__name__}") from None
    headers = {
        name: response.headers.get(name)
        for name in ("x-requests-last", "x-requests-used", "x-requests-remaining")
    }
    if response.status_code >= 400:
        raise RuntimeError(f"The Odds API events request failed: HTTP {response.status_code}")
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError("The Odds API events response was not a list")
    return data, headers


def render_report(
    report: dict[str, dict[str, Any]], headers: list[dict[str, str | None]]
) -> str:
    lines = [
        "# Team alias build report",
        "",
        "Proposals are generated from public Kalshi events and The Odds API events. "
        "Only unique team/date pairings accepted by `best_team` are included.",
        "",
        "| Sport | Series | Teams mapped / codes seen | Unmatched codes | Unmatched Odds API names |",
        "|---|---|---:|---|---|",
    ]
    for sport, data in sorted(report.items()):
        codes = data["codes_seen"]
        lines.append(
            f"| `{sport}` | `{data['series']}` | {data['teams_mapped']} / {len(codes)} "
            f"| {', '.join(data['codes_unmatched']) or '—'} "
            f"| {', '.join(data['odds_names_unmatched']) or '—'} |"
        )
    lines.extend(["", "## Unmatched Kalshi events and Odds API names", ""])
    for sport, data in sorted(report.items()):
        if data["unmatched_events"] or data["codes_unmatched"] or data["odds_names_unmatched"]:
            lines.append(f"### `{sport}`")
            if data["unmatched_events"]:
                lines.append("- Kalshi events: " + ", ".join(data["unmatched_events"]))
            if data["codes_unmatched"]:
                lines.append("- Kalshi codes: " + ", ".join(data["codes_unmatched"]))
            if data["odds_names_unmatched"]:
                lines.append("- Odds API names: " + ", ".join(data["odds_names_unmatched"]))
            lines.append("")
    lines.extend(["## Odds API request headers", ""])
    for i, item in enumerate(headers, start=1):
        lines.append(
            f"- Request {i}: `x-requests-last={item.get('x-requests-last')}`, "
            f"`x-requests-used={item.get('x-requests-used')}`, "
            f"`x-requests-remaining={item.get('x-requests-remaining')}`"
        )
    return "\n".join(lines) + "\n"


def build(
    api_key: str,
    proposal_path: Path,
    report_path: Path,
    kalshi: KalshiClient | None = None,
    http: httpx.Client | None = None,
) -> tuple[dict[str, dict[str, dict[str, list[str]]]], dict[str, dict[str, Any]]]:
    kalshi_client = kalshi or KalshiClient(KALSHI_PROD_REST)
    http_client = http or httpx.Client(timeout=30.0)
    should_close_http = http is None
    kalshi_events: dict[str, list[dict[str, Any]]] = {}
    for series in SUPPORTED_SERIES:
        events: list[dict[str, Any]] = []
        for status in ("open", "settled"):
            events.extend(
                kalshi_client.get_events(
                    series_ticker=series,
                    status=status,
                    with_nested_markets=True,
                    limit=200,
                )
            )
        kalshi_events[series] = events
    odds_events: dict[str, list[dict[str, Any]]] = {}
    headers = []
    try:
        for sport in sorted(set(SUPPORTED_SERIES.values())):
            events, response_headers = fetch_odds_events(http_client, sport, api_key)
            if response_headers["x-requests-last"] not in ("0", "0.0"):
                raise RuntimeError(
                    "The Odds API events call had a non-zero x-requests-last; stopping."
                )
            odds_events[sport] = events
            headers.append(response_headers)
    finally:
        if should_close_http:
            http_client.close()
    proposal, report = build_alias_proposal(kalshi_events, odds_events)
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text(json.dumps(proposal, indent=2, sort_keys=True) + "\n")
    report_path.write_text(render_report(report, headers))
    return proposal, report


def main() -> None:
    parser = argparse.ArgumentParser(description="Propose exact Kalshi/Odds API team aliases.")
    parser.add_argument(
        "--proposal",
        type=Path,
        default=Path.home() / "work" / "alias_proposals.json",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path.home() / "work" / "alias_report.md",
    )
    args = parser.parse_args()
    api_key = os.environ.get("PBOT_ODDS_API_KEY")
    if not api_key:
        raise SystemExit("PBOT_ODDS_API_KEY is required")
    proposal, report = build(api_key, args.proposal, args.report)
    print(f"proposed teams: {sum(len(teams) for teams in proposal.values())}")
    print(f"coverage report: {args.report}")
    for sport, data in sorted(report.items()):
        codes = data["codes_seen"]
        print(
            f"{sport}: {data['teams_mapped']} mapped teams / "
            f"{len(codes) - len(data['codes_unmatched'])} of {len(codes)} codes"
        )


if __name__ == "__main__":
    main()
