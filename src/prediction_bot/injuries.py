"""Injury reports from ESPN's unofficial injuries endpoint (free, no API key).

Phase 1 measures whether injury news between entry and kickoff is costing CLV:
each paper run snapshots the current injury list for teams in games we hold (or
signalled in the last week), and settled fills get tagged when a key-position
Out/Doubtful report landed in the entry->kickoff window.

Feed status (probed 2026-10-25, see docs/injury_feeds.md): NFL returns live data
for all 32 teams; college football is stale (3 teams, entries from 2020-2022);
soccer/eng.1 is empty. Only NFL is treated as verified this phase — the
`espn_names` aliases in teams.json exist for NFL teams only.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

from prediction_bot.teams import odds_name_for, team_for_espn_name

if TYPE_CHECKING:
    from prediction_bot.paper.ledger import Ledger

log = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# Odds API sport key -> ESPN sport_path for the /injuries endpoint.
ESPN_SPORT_PATHS: dict[str, str] = {
    "americanfootball_nfl": "football/nfl",
    "americanfootball_ncaaf": "football/college-football",
    "basketball_nba": "basketball/nba",
    "basketball_ncaab": "basketball/mens-college-basketball",
    "basketball_wnba": "basketball/wnba",
    "baseball_mlb": "baseball/mlb",
    "icehockey_nhl": "hockey/nhl",
    "soccer_epl": "soccer/eng.1",
}

# Sports whose feed is verified live AND whose teams carry `espn_names` aliases.
# The CFB feed is stale and every other path is unverified, so the gate and the
# snapshot tagging skip them (recorded as injury_gate_skipped / unknown).
ESPN_VERIFIED_SPORTS = frozenset({"americanfootball_nfl"})

# A 4c+ adverse move of the devigged consensus between entry and the last
# pre-kickoff snapshot marks a CFB fill (there is no league-wide CFB injury
# report, so line movement is the proxy — always labelled as such).
CFB_PROXY_SPORT = "americanfootball_ncaaf"
CFB_PROXY_SOURCE = "line_move_proxy"
CFB_PROXY_MOVE = 0.04

# Statuses that mean "confirmed out": Questionable/Doubtful are unresolved and
# only matter for the Phase 2 gate; Active/IR/Suspension are either non-news or
# already priced.
FLAGGED_STATUSES = frozenset({"Out", "Doubtful"})
UNCERTAIN_STATUSES = frozenset({"Questionable", "Doubtful"})

# Positions where an injury plausibly moves the market. Per the spec, NFL key
# positions are QB, RB, WR, TE plus any offensive/defensive line slot ("line").
LINE_POSITIONS = frozenset({"OT", "G", "C", "OL", "DE", "DT", "NT", "DL", "EDGE"})
KEY_POSITIONS: dict[str, frozenset[str]] = {
    "americanfootball_nfl": frozenset({"QB", "RB", "WR", "TE"}) | LINE_POSITIONS,
}


@dataclass(frozen=True)
class InjuryEntry:
    """One player on one team's current injury list."""

    team: str  # ESPN displayName, e.g. "Kansas City Chiefs"
    athlete: str
    position: str  # abbreviation, e.g. "QB"
    status: str  # "Out" / "Doubtful" / "Questionable" / "Active" / ...
    status_type: str  # type.name, e.g. "INJURY_STATUS_OUT"
    reported_at: datetime | None
    detail: str  # shortComment


@dataclass(frozen=True)
class InjurySnapshotRow:
    """One injury_snapshots ledger row: an ESPN entry resolved to our team id."""

    sport: str
    team_id: str
    athlete: str
    position: str
    status: str
    status_type: str
    reported_at: datetime | None
    detail: str


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def parse_injuries(data: object) -> list[InjuryEntry]:
    """Parse the ESPN /injuries response. Schema surprises return []."""
    if not isinstance(data, dict):
        log.warning("espn injuries: unexpected payload type %s", type(data).__name__)
        return []
    teams = data.get("injuries")
    if not isinstance(teams, list):
        log.warning("espn injuries: missing injuries[] list")
        return []
    out: list[InjuryEntry] = []
    for team in teams:
        if not isinstance(team, dict):
            continue
        team_name = team.get("displayName")
        entries = team.get("injuries")
        if not isinstance(team_name, str) or not isinstance(entries, list):
            log.warning("espn injuries: malformed team block %r", list(team)[:5])
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            athlete = entry.get("athlete") or {}
            position = athlete.get("position") or {}
            status_type = entry.get("type") or {}
            details = entry.get("details") or {}
            out.append(
                InjuryEntry(
                    team=team_name,
                    athlete=str(athlete.get("displayName") or ""),
                    position=str(position.get("abbreviation") or ""),
                    status=str(entry.get("status") or ""),
                    status_type=str(status_type.get("name") or ""),
                    reported_at=_parse_time(entry.get("date")),
                    detail=str(entry.get("shortComment") or details.get("type") or ""),
                )
            )
    return out


class InjuryClient:
    """Fetches ESPN injury lists. One request per sport, memoized so a run only
    ever hits each feed once. Any failure logs a warning and returns [] — an
    injury feed must never block a run."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=15.0)
        self._memo: dict[str, list[InjuryEntry]] = {}
        self._ok: dict[str, bool] = {}

    def fetched_ok(self, sport: str) -> bool:
        """True once fetch_injuries got a 200 back for the sport. A healthy
        league and a dead feed both yield [] entries; only the former counts
        as coverage."""
        return self._ok.get(sport, False)

    def fetch_injuries(self, sport: str) -> list[InjuryEntry]:
        """Injury list for an Odds API sport key; [] when the sport has no path
        or the fetch/parse fails."""
        if sport not in self._memo:
            path = ESPN_SPORT_PATHS.get(sport)
            if path is None:
                self._memo[sport] = []
                self._ok[sport] = False
                return []
            url = f"{ESPN_BASE}/{path}/injuries"
            data: object = None
            self._ok[sport] = False
            for attempt in range(2):
                try:
                    resp = self._client.get(url)
                    if resp.status_code != 200:
                        log.warning("espn injuries: %s returned HTTP %d", sport, resp.status_code)
                        break
                    data = resp.json()
                    self._ok[sport] = True
                    break
                except httpx.HTTPError as e:
                    log.warning("espn injuries: %s fetch failed (%s)", sport, e)
                    if attempt == 0:
                        time.sleep(0.5)
            self._memo[sport] = parse_injuries(data) if data is not None else []
            log.info("espn injuries: %s -> %d entries", sport, len(self._memo[sport]))
        return self._memo[sport]

    def entries_for_team(self, sport: str, team_id: str) -> list[InjuryEntry]:
        return [
            entry
            for entry in self.fetch_injuries(sport)
            if team_for_espn_name(sport, entry.team) == team_id
        ]


def snapshot_injuries(
    ledger: Ledger,
    client: InjuryClient,
    run_id: int | None = None,
    at: datetime | None = None,
) -> int:
    """Snapshot ESPN injury rows for every team in a game we hold an open
    position on, recently settled, or signalled in the last 7 days — never the
    whole league. ESPN display names that don't map to a team id are logged and
    skipped (they belong on the alias-gaps list)."""
    now = at or datetime.now(timezone.utc)
    sports: set[str] = set()
    wanted: set[tuple[str, str]] = set()  # (sport, team_id)
    for event_ticker in ledger.injury_snapshot_events(now):
        sport, team_ids = ledger.event_teams(event_ticker)
        if sport is None or sport not in ESPN_SPORT_PATHS:
            continue
        sports.add(sport)
        for team_id in team_ids:
            wanted.add((sport, team_id))
    if not wanted:
        return 0
    entries = {sport: client.fetch_injuries(sport) for sport in sports}
    mapped_names = {
        (sport, entry.team)
        for sport, sport_entries in entries.items()
        for entry in sport_entries
        if team_for_espn_name(sport, entry.team) is not None
    }
    for sport, sport_entries in entries.items():
        for name in sorted({entry.team for entry in sport_entries}):
            if (sport, name) not in mapped_names:
                log.warning("espn injuries: unmapped team %r (%s)", name, sport)
    rows = [
        InjurySnapshotRow(
            sport=sport,
            team_id=team_id,
            athlete=entry.athlete,
            position=entry.position,
            status=entry.status,
            status_type=entry.status_type,
            reported_at=entry.reported_at,
            detail=entry.detail,
        )
        for sport, team_id in sorted(wanted)
        for entry in entries[sport]
        if team_for_espn_name(sport, entry.team) == team_id
    ]
    # Teams with no listed injuries still count as covered (we checked and they
    # were clean): write one empty row per covered team so flag_for_fill can
    # tell "checked, no hit" apart from "no coverage" (unknown). Only when the
    # feed actually answered — a failed fetch leaves fills unknown.
    covered = {(sport, team_id) for sport, team_id in wanted if client.fetched_ok(sport)}
    hit_teams = {(row.sport, row.team_id) for row in rows}
    rows += [
        InjurySnapshotRow(
            sport=sport,
            team_id=team_id,
            athlete="",
            position="",
            status="",
            status_type="",
            reported_at=None,
            detail="",
        )
        for sport, team_id in sorted(covered - hit_teams)
    ]
    return ledger.record_injury_snapshots(rows, run_id=run_id, at=now)


def injury_gate_reasons(
    sport: str,
    own_entries: Sequence[InjuryEntry],
    opp_entries: Sequence[InjuryEntry],
) -> list[tuple[str, str]]:
    """(reason, detail) pairs for the pre-entry gate.

    Only unresolved key-position statuses (Questionable/Doubtful) count — Out
    is already in the price and must not block. injury_uncertain_own is the
    team we are buying; injury_uncertain_opp the opponent.
    """
    key_positions = KEY_POSITIONS.get(sport, frozenset())
    reasons: list[tuple[str, str]] = []
    for reason, entries in (
        ("injury_uncertain_own", own_entries),
        ("injury_uncertain_opp", opp_entries),
    ):
        hits = [
            entry
            for entry in entries
            if entry.position in key_positions and entry.status in UNCERTAIN_STATUSES
        ]
        if hits:
            detail = ", ".join(
                f"{entry.athlete} ({entry.position}) {entry.status}" for entry in hits[:3]
            )
            reasons.append((reason, detail))
    return reasons


def flag_for_fill(
    conn: sqlite3.Connection,
    *,
    sport: str | None,
    own_team: str | None,
    opponent: str | None,
    created_at: datetime,
    commence_time: datetime | None,
) -> tuple[int | None, str | None, str | None]:
    """The Phase 1 settlement tag: (injury_flag, injury_detail, injury_source).

    `own_team`/`opponent` are teams.json ids. flag 1 = a key-position
    Out/Doubtful report landed between entry and kickoff (detail records which
    side was hit), 0 = coverage existed but no qualifying report, None = no
    coverage (unknown). CFB uses the line-move proxy instead of an injury feed;
    every other sport stays unknown.
    """
    if sport == CFB_PROXY_SPORT:
        return _cfb_proxy_flag(
            conn,
            own_team=own_team,
            opponent=opponent,
            created_at=created_at,
            commence_time=commence_time,
        )
    if sport not in ESPN_VERIFIED_SPORTS:
        return None, None, None
    teams = [team for team in (own_team, opponent) if team is not None]
    if not teams:
        return None, None, None
    coverage = conn.execute(
        f"SELECT 1 FROM injury_snapshots WHERE sport=? AND team_id IN"
        f" ({','.join('?' for _ in teams)}) LIMIT 1",
        (sport, *teams),
    ).fetchone()
    if coverage is None:
        return None, None, None
    if commence_time is None:
        return 0, None, "espn"
    created_iso = created_at.astimezone(timezone.utc).isoformat()
    commence_iso = commence_time.astimezone(timezone.utc).isoformat()
    hits: list[tuple[str, str, str, str]] = []  # side, athlete, position, status
    key_positions = KEY_POSITIONS.get(sport, frozenset())
    for side, team_id in (("own", own_team), ("opp", opponent)):
        if team_id is None:
            continue
        rows = conn.execute(
            "SELECT athlete, position, status, reported_at FROM injury_snapshots"
            " WHERE sport=? AND team_id=? AND status IN ('Out','Doubtful')"
            " AND reported_at > ? AND reported_at < ? ORDER BY reported_at",
            (sport, team_id, created_iso, commence_iso),
        ).fetchall()
        for athlete, position, status, _reported in rows:
            if str(position) in key_positions:
                hits.append((side, str(athlete), str(position), str(status)))
    if not hits:
        return 0, None, "espn"
    detail = "; ".join(
        f"{side}: {athlete} ({position}) {status}" for side, athlete, position, status in hits[:4]
    )
    return 1, detail, "espn"


def _cfb_proxy_flag(
    conn: sqlite3.Connection,
    *,
    own_team: str | None,
    opponent: str | None,
    created_at: datetime,
    commence_time: datetime | None,
) -> tuple[int | None, str | None, str | None]:
    """CFB has no league-wide injury report; the proxy is a >=4c adverse move of
    the devigged book consensus for the held side between entry and the last
    pre-kickoff odds snapshot. Labelled line_move_proxy everywhere.

    own_team/opponent are teams.json ids; odds_snapshots stores Odds display
    names, so the ids are resolved before querying.
    """
    if own_team is None or opponent is None or commence_time is None:
        return None, None, None
    own_name = odds_name_for(CFB_PROXY_SPORT, own_team)
    opp_name = odds_name_for(CFB_PROXY_SPORT, opponent)
    if own_name is None or opp_name is None:
        return None, None, None
    created_iso = created_at.astimezone(timezone.utc).isoformat()
    commence_iso = commence_time.astimezone(timezone.utc).isoformat()
    # Our held side's consensus prob for this event: the odds_snapshots rows
    # whose home/away pair is exactly these two teams.
    event_ids = [
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT event_id FROM odds_snapshots WHERE feed=? AND"
            " ((home_team=? AND away_team=?) OR (home_team=? AND away_team=?))",
            (CFB_PROXY_SPORT, own_name, opp_name, opp_name, own_name),
        )
    ]
    if not event_ids:
        return None, None, None
    placeholders = ",".join("?" for _ in event_ids)
    rows = conn.execute(
        f"SELECT created_at, prob FROM odds_snapshots WHERE feed=? AND team=?"
        f" AND event_id IN ({placeholders}) AND created_at >= ?"
        " ORDER BY created_at, id",
        (CFB_PROXY_SPORT, own_name, *event_ids, created_iso),
    ).fetchall()
    if not rows:
        return None, None, None
    # Both endpoints must be strictly pre-kickoff: odds that first appear after
    # kickoff are not an entry baseline and say nothing about the hold window.
    pre_kickoff = [row for row in rows if str(row[0]) < commence_iso]
    if not pre_kickoff:
        return None, None, None
    entry_prob = float(pre_kickoff[0][1])
    last_prob = float(pre_kickoff[-1][1])
    move = last_prob - entry_prob
    if move <= -CFB_PROXY_MOVE:
        detail = (
            f"proxy: consensus for {own_name} moved {move:+.3f}"
            f" ({entry_prob:.3f} -> {last_prob:.3f}) between entry and the last"
            " pre-kickoff snapshot"
        )
        return 1, detail, CFB_PROXY_SOURCE
    return 0, None, CFB_PROXY_SOURCE
