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
from prediction_bot.odds import (
    _ALIASES,
    KALSHI_SERIES_TO_SPORT,
    _normalise,
    _tokens,
    best_team,
    team_match_score,
)
from prediction_bot.teams import kalshi_name_ok, team_for_code, team_for_odds_name
from prediction_bot.venues.kalshi import KalshiClient

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ET = ZoneInfo("America/New_York")
DRAW_NAMES = {"draw", "tie"}
RULES_WINNER = re.compile(r"\bif\s+(?:the\s+)?(.+?)\s+wins?\b", re.IGNORECASE)
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


def _market_team(name: str, rules_primary: str, candidates: list[str]) -> str | None:
    title_match = best_team(name, candidates)
    rules_match = None
    winner = RULES_WINNER.search(rules_primary)
    if winner is not None:
        winner_name = winner.group(1).strip()
        rules_match = best_team(winner_name, candidates)
        if rules_match is None:
            parts = winner_name.split()
            for index in range(1, len(parts)):
                rules_match = best_team(" ".join(parts[index:]), candidates)
                if rules_match is not None:
                    break
    if title_match is not None and rules_match is not None and title_match != rules_match:
        return None
    return rules_match or title_match


def _participant_team(
    name: str, rules_primary: str, candidates: list[str]
) -> tuple[str | None, list[str]]:
    winner = RULES_WINNER.search(rules_primary)
    queries = [name]
    if winner is not None:
        queries.append(winner.group(1).strip())
    matches: list[set[str]] = []
    for query in queries:
        tokens = _tokens(query)
        if not tokens:
            continue
        normalized_matches = {candidate for candidate in candidates if tokens == _tokens(candidate)}
        if normalized_matches:
            matches.append(normalized_matches)
    if not matches:
        return None, []
    common = set.intersection(*matches)
    if len(common) == 1 and all(len(match) == 1 for match in matches):
        return next(iter(common)), sorted(common)
    return None, sorted(set.union(*matches))


def _prefix_tokens(name: str) -> list[str]:
    expanded = re.sub(r"\bst\.(?=\s|$)", "State", name, flags=re.IGNORECASE)
    normalized = _normalise(expanded)
    normalized = _ALIASES.get(normalized, normalized)
    return re.findall(r"[a-z0-9]+", normalized)


def _participant_prefix_candidates(
    name: str, rules_primary: str, candidates: list[str]
) -> set[str]:
    winner = RULES_WINNER.search(rules_primary)
    queries = [name]
    if winner is not None:
        queries.append(winner.group(1).strip())
    query_matches: list[set[str]] = []
    normalized_candidates = {candidate: _prefix_tokens(candidate) for candidate in candidates}
    for query in queries:
        tokens = _prefix_tokens(query)
        if not tokens:
            continue
        query_matches.append(
            {
                candidate
                for candidate, candidate_tokens in normalized_candidates.items()
                if candidate_tokens[: len(tokens)] == tokens
            }
        )
    if not query_matches or any(not matches for matches in query_matches):
        return set()
    return set.intersection(*query_matches)


def _participant_suggestions(
    name: str, rules_primary: str, candidates: list[str]
) -> list[tuple[str, float]]:
    winner = RULES_WINNER.search(rules_primary)
    queries = [name]
    if winner is not None:
        queries.append(winner.group(1).strip())
    scores = {
        candidate: max(team_match_score(query, candidate) for query in queries)
        for candidate in candidates
    }
    return sorted(
        ((candidate, score) for candidate, score in scores.items() if score > 0),
        key=lambda item: (-item[1], item[0]),
    )[:5]


def build_alias_proposal(
    kalshi_events: dict[str, list[dict[str, Any]]],
    odds_events: dict[str, list[dict[str, Any]]],
    participants: dict[str, list[dict[str, Any]]] | None = None,
    series_filter: set[str] | None = None,
) -> tuple[dict[str, dict[str, dict[str, list[str]]]], dict[str, dict[str, Any]]]:
    proposal: dict[str, dict[str, dict[str, set[str]]]] = defaultdict(
        lambda: defaultdict(
            lambda: {"kalshi_codes": set(), "kalshi_names": set(), "odds_names": set()}
        )
    )
    report: dict[str, dict[str, Any]] = {}
    for series, sport in SUPPORTED_SERIES.items():
        if series_filter is not None and series not in series_filter:
            continue
        seen_codes: set[str] = set()
        paired_codes: set[str] = set()
        owners_by_code: dict[str, set[str]] = defaultdict(set)
        market_names_by_code: dict[str, set[str]] = defaultdict(set)
        codes_by_event: dict[str, list[tuple[str, str, str]]] = {}
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
            team_markets: list[tuple[str, str, str]] = []
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
                team_markets.append((code, name, str(market.get("rules_primary") or "")))
            codes_by_event[event_ticker] = team_markets
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
                for code, name, rules_primary in team_markets:
                    known_team = team_for_code(series, code)
                    known_name = (
                        team_for_odds_name(sport, home) == known_team
                        or team_for_odds_name(sport, away) == known_team
                    )
                    matched = (
                        next(
                            candidate
                            for candidate in (home, away)
                            if team_for_odds_name(sport, candidate) == known_team
                        )
                        if known_team is not None
                        and kalshi_name_ok(series, known_team, name)
                        and known_name
                        else _market_team(name, rules_primary, [home, away])
                    )
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
            for code, name, _ in team_markets:
                odds_name = assignment[code]
                team = proposal[sport][canonical_id(odds_name)]
                team["kalshi_codes"].add(code)
                team["kalshi_names"].add(name)
                team["odds_names"].add(odds_name)
                owners_by_code[code].add(odds_name)
                market_names_by_code[code].add(name)
                paired_codes.add(code)
                paired_names.add(odds_name)
        participant_names = sorted(
            {
                str(item["full_name"])
                for item in (participants or {}).get(sport, [])
                if isinstance(item.get("full_name"), str) and item["full_name"]
            }
        )
        prefix_candidates_by_code: dict[str, set[str]] = defaultdict(set)
        prefix_paired_events: set[str] = set()
        for team_markets in codes_by_event.values():
            for code, name, rules_primary in team_markets:
                matched, _ = _participant_team(name, rules_primary, participant_names)
                if matched is not None:
                    team = proposal[sport][canonical_id(matched)]
                    team["kalshi_codes"].add(code)
                    team["kalshi_names"].add(name)
                    team["odds_names"].add(matched)
                    owners_by_code[code].add(matched)
                    market_names_by_code[code].add(name)
                    paired_codes.add(code)
                    paired_names.add(matched)
        for event_ticker, team_markets in codes_by_event.items():
            if len(team_markets) != 2 or len({code for code, _, _ in team_markets}) != 2:
                continue
            ticker_day = event_date(event_ticker)
            if ticker_day is None:
                continue
            code_a, name_a, rules_a = team_markets[0]
            code_b, name_b, rules_b = team_markets[1]
            candidates_a = _participant_prefix_candidates(name_a, rules_a, participant_names)
            candidates_b = _participant_prefix_candidates(name_b, rules_b, participant_names)
            prefix_candidates_by_code[code_a].update(candidates_a)
            prefix_candidates_by_code[code_b].update(candidates_b)
            if not candidates_a or not candidates_b:
                continue
            matching_pairs: list[tuple[str, str]] = []
            for game in games:
                home, away = game.get("home_team"), game.get("away_team")
                commence = game.get("commence_time")
                if not isinstance(home, str) or not isinstance(away, str):
                    continue
                if not isinstance(commence, str):
                    continue
                try:
                    start_day = (
                        datetime.fromisoformat(commence.replace("Z", "+00:00"))
                        .astimezone(ET)
                        .date()
                    )
                except ValueError:
                    continue
                if start_day != ticker_day:
                    continue
                if home in candidates_a and away in candidates_b:
                    matching_pairs.append((home, away))
                if away in candidates_a and home in candidates_b:
                    matching_pairs.append((away, home))
            if len(matching_pairs) != 1:
                unmatched_events.append(event_ticker)
                continue
            owner_a, owner_b = matching_pairs[0]
            for code, name, odds_name in (
                (code_a, name_a, owner_a),
                (code_b, name_b, owner_b),
            ):
                team = proposal[sport][canonical_id(odds_name)]
                team["kalshi_codes"].add(code)
                team["kalshi_names"].add(name)
                team["odds_names"].add(odds_name)
                owners_by_code[code].add(odds_name)
                market_names_by_code[code].add(name)
                paired_codes.add(code)
                paired_names.add(odds_name)
            prefix_paired_events.add(event_ticker)
        unmatched_events = [
            event_ticker
            for event_ticker in unmatched_events
            if event_ticker not in prefix_paired_events
        ]
        conflicting_codes = {
            code: sorted(owners) for code, owners in owners_by_code.items() if len(owners) > 1
        }
        for team_id, team in list(proposal[sport].items()):
            valid_codes = set(team["kalshi_codes"]) - set(conflicting_codes)
            if not valid_codes:
                del proposal[sport][team_id]
                continue
            team["kalshi_codes"] = valid_codes
            team["kalshi_names"] = set().union(
                *(
                    market_names_by_code[code]
                    for code in valid_codes
                    if canonical_id(next(iter(owners_by_code[code]))) == team_id
                )
            )
        paired_codes.difference_update(conflicting_codes)
        paired_names = {
            owner
            for code, owners in owners_by_code.items()
            if code not in conflicting_codes
            for owner in owners
        }
        unmatched_codes = seen_codes - paired_codes
        code_markets: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for team_markets in codes_by_event.values():
            for code, name, rules_primary in team_markets:
                code_markets[code].append((name, rules_primary))
        report[sport] = {
            "series": series,
            "teams_mapped": len(proposal[sport]),
            "codes_seen": sorted(seen_codes),
            "codes_unmatched": sorted(unmatched_codes),
            "odds_names_seen": sorted(seen_names),
            "odds_names_unmatched": sorted(seen_names - paired_names),
            "unmatched_events": sorted(set(unmatched_events)),
            "participants_seen": len(participant_names),
            "participants_without_confirmed_market": len(set(participant_names) - paired_names),
            "unmatched_candidates": {
                code: {
                    "market_names": sorted({name for name, _rules in code_markets[code]}),
                    "prefix_candidates": sorted(prefix_candidates_by_code.get(code, set())),
                    "candidates": [
                        {"name": candidate, "score": round(score, 3)}
                        for market_name, rules_primary in code_markets[code]
                        for candidate, score in _participant_suggestions(
                            market_name, rules_primary, participant_names
                        )
                    ][:10]
                    + [{"name": owner, "score": 1.0} for owner in conflicting_codes.get(code, [])],
                    "reason": (
                        "Kalshi code identifies multiple Odds API participants"
                        if code in conflicting_codes
                        else "No unique normalized participant match or unique kickoff pairing"
                    ),
                }
                for code in sorted(unmatched_codes)
            },
            "conflicting_codes": conflicting_codes,
        }
    normalized = {
        sport: {
            team_id: {key: sorted(values) for key, values in team.items()}
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


def render_report(report: dict[str, dict[str, Any]], headers: list[dict[str, str | None]]) -> str:
    lines = [
        "# Team alias build report",
        "",
        "Proposals are generated from public Kalshi events and The Odds API events. "
        "Only unique kickoff pairings or exact participant token-set matches under the builder's "
        "normalization are included. Codes shared by different teams remain unmapped.",
        "",
        "| Sport | Series | Teams mapped / codes seen | Unmatched codes | "
        "Participants without a confirmed market |",
        "|---|---|---:|---|---:|",
    ]
    for sport, data in sorted(report.items()):
        codes = data["codes_seen"]
        lines.append(
            f"| `{sport}` | `{data['series']}` | {data['teams_mapped']} / {len(codes)} "
            f"| {', '.join(data['codes_unmatched']) or '—'} "
            f"| {data['participants_without_confirmed_market']} |"
        )
    lines.extend(["", "## Unmatched Kalshi events and codes", ""])
    for sport, data in sorted(report.items()):
        if data["unmatched_events"] or data["codes_unmatched"]:
            lines.append(f"### `{sport}`")
            if data["unmatched_events"]:
                lines.append("- Kalshi events: " + ", ".join(data["unmatched_events"]))
            for code in data["codes_unmatched"]:
                details = data["unmatched_candidates"][code]
                candidates = ", ".join(
                    f"{item['name']} (score {item['score']:.3f})" for item in details["candidates"]
                )
                prefix_candidates = ", ".join(details.get("prefix_candidates", []))
                lines.append(
                    f"- `{code}` — market names: "
                    f"{', '.join(details['market_names']) or 'unknown'}; "
                    f"prefix candidates: {prefix_candidates or 'none'}; "
                    f"candidates: {candidates or 'none'}; "
                    f"{details['reason']}."
                )
            if data["participants_seen"]:
                lines.append(
                    f"- Participants without a confirmed current-market mapping: "
                    f"{data['participants_without_confirmed_market']} of "
                    f"{data['participants_seen']}."
                )
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
