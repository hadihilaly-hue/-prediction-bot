"""Change-only injury writes give the injury gate the same answers.

The same random ESPN-like fetch stream is recorded twice: once one row per fetched
entry (the writer before the change, reproduced as raw INSERTs) and once through
Ledger.record_injury_snapshots. flag_for_fill must return the same flag and source and
name the same players for every sampled entry/kickoff window.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from prediction_bot.injuries import InjurySnapshotRow, flag_for_fill
from prediction_bot.paper.ledger import Ledger

UTC = timezone.utc
NFL = "americanfootball_nfl"
START = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
TEAMS = ["KC", "BUF", "PHI", "DAL"]
PLAYERS = {team: [(f"{team} P{i}", pos) for i, pos in enumerate(["QB", "WR", "K", "LB"])]
           for team in TEAMS}  # fmt: skip


def _stream(rng: random.Random, fetches: int) -> list[tuple[datetime, list[InjurySnapshotRow]]]:
    """Every fetch returns each team's whole current list; entries rarely change."""
    state: dict[tuple[str, str], tuple[str, str, datetime]] = {}
    out = []
    for n in range(fetches):
        at = START + timedelta(minutes=20 * n)
        for team in TEAMS:
            for athlete, _pos in PLAYERS[team]:
                key = (team, athlete)
                if key not in state and rng.random() < 0.03:
                    state[key] = (rng.choice(["Out", "Doubtful", "Questionable"]), "x", at)
                elif key in state and rng.random() < 0.02:
                    if rng.random() < 0.5:
                        del state[key]  # off the list
                    else:
                        status, _d, _r = state[key]
                        state[key] = (rng.choice(["Out", "Doubtful", "Questionable"]), "y", at)
        rows = [
            InjurySnapshotRow(NFL, team, athlete, pos, status, "INJURY_STATUS", reported, detail)
            for team in TEAMS
            for athlete, pos in PLAYERS[team]
            if (team, athlete) in state
            for status, detail, reported in [state[(team, athlete)]]
        ]
        out.append((at, rows))
    return out


def _old_insert(ledger: Ledger, at: datetime, rows: list[InjurySnapshotRow]) -> None:
    ledger.conn.executemany(
        "INSERT INTO injury_snapshots (fetched_at, sport, team_id, athlete, position,"
        " status, status_type, reported_at, detail, run_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                at.isoformat(),
                r.sport,
                r.team_id,
                r.athlete,
                r.position,
                r.status,
                r.status_type,
                r.reported_at.isoformat() if r.reported_at else None,
                r.detail,
                1,
            )
            for r in rows
        ],  # fmt: skip
    )
    ledger.conn.commit()


def _all_hits(
    conn: sqlite3.Connection, own: str, opp: str, created: datetime, kickoff: datetime
) -> set[tuple[str, str, str, str]]:
    """Every distinct (team, athlete, position, status) the gate's query matches, before
    the detail text is cut off."""
    rows = conn.execute(
        "SELECT DISTINCT team_id, athlete, position, status FROM injury_snapshots"
        " WHERE sport=? AND team_id IN (?,?) AND status IN ('Out','Doubtful')"
        " AND reported_at > ? AND reported_at < ?",
        (NFL, own, opp, created.isoformat(), kickoff.isoformat()),
    )
    return {tuple(str(v) for v in r) for r in rows}  # type: ignore[misc]


def _hits(detail: str | None) -> set[str]:
    return set() if not detail else {part.strip() for part in detail.split(";")}


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_injury_gate_answers_are_unchanged(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    stream = _stream(rng, fetches=300)  # ~4 days of 20-minute fetches
    old = Ledger(tmp_path / "old.sqlite", Decimal(100))
    new = Ledger(tmp_path / "new.sqlite", Decimal(100))
    for at, rows in stream:
        _old_insert(old, at, rows)
        new.record_injury_snapshots(rows, run_id=1, at=at)
    n_old = old.conn.execute("SELECT COUNT(*) FROM injury_snapshots").fetchone()[0]
    n_new = new.conn.execute("SELECT COUNT(*) FROM injury_snapshots").fetchone()[0]
    assert n_new < 0.1 * n_old, (n_new, n_old)

    end = stream[-1][0]
    flagged = 0
    for _ in range(400):
        created = START + (end - START) * rng.random() - timedelta(hours=2)
        commence = created + timedelta(hours=rng.choice([1, 6, 24, 72]))
        own, opp = rng.sample(TEAMS, 2)
        args = dict(sport=NFL, own_team=own, opponent=opp, created_at=created)
        for kickoff in (commence, None):
            a = flag_for_fill(old.conn, commence_time=kickoff, **args)  # type: ignore[arg-type]
            b = flag_for_fill(new.conn, commence_time=kickoff, **args)  # type: ignore[arg-type]
            assert (a[0], a[2]) == (b[0], b[2]), (args, kickoff)
            # The old text was cut off at four entries, often one player repeated per
            # fetch; the new one lists each player once, so it names at least as many.
            assert _hits(a[1]) <= _hits(b[1]) or len(_hits(b[1])) == 4, (a[1], b[1])
            if kickoff is not None:
                assert _all_hits(old.conn, own, opp, created, kickoff) == _all_hits(
                    new.conn, own, opp, created, kickoff
                )
            flagged += a[0] == 1
        # Coverage for a team nobody has reported on stays "unknown" in both.
        args["own_team"], args["opponent"] = "NOBODY", None
        assert flag_for_fill(old.conn, commence_time=commence, **args) == flag_for_fill(  # type: ignore[arg-type]
            new.conn,
            commence_time=commence,
            **args,  # type: ignore[arg-type]
        )
    assert flagged > 10, "the sample exercises flagged windows"
    # Dashboard freshness: newest fetch.
    fresh = "SELECT MAX(COALESCE(last_seen_at,fetched_at)) FROM injury_snapshots"
    assert old.conn.execute(fresh).fetchone() == new.conn.execute(fresh).fetchone()


def test_distinct_players_in_the_detail_are_the_same(tmp_path: Path) -> None:
    """The old writer repeated a player once per fetch; the set of players is equal."""
    old = Ledger(tmp_path / "old.sqlite", Decimal(100))
    new = Ledger(tmp_path / "new.sqlite", Decimal(100))
    reported = START + timedelta(hours=1)
    row = InjurySnapshotRow(NFL, "KC", "KC QB", "QB", "Out", "INJURY_STATUS", reported, "knee")
    for n in range(6):
        at = START + timedelta(hours=2 + n)
        _old_insert(old, at, [row])
        new.record_injury_snapshots([row], at=at)
    args = dict(sport=NFL, own_team="KC", opponent="BUF", created_at=START,
                commence_time=START + timedelta(days=1))  # fmt: skip
    a = flag_for_fill(old.conn, **args)  # type: ignore[arg-type]
    b = flag_for_fill(new.conn, **args)  # type: ignore[arg-type]
    assert a[0] == b[0] == 1 and a[2] == b[2] == "espn"
    assert a[1] is not None and a[1].count("KC QB") == 4  # cut off at four, all the same
    assert b[1] == "own: KC QB (QB) Out"


def test_a_changed_entry_starts_a_new_row(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "i.sqlite", Decimal(100))
    base = InjurySnapshotRow(NFL, "KC", "A", "QB", "Questionable", "S", START, "ankle")
    worse = InjurySnapshotRow(NFL, "KC", "A", "QB", "Out", "S", START, "ankle")
    for n, row in enumerate([base, base, worse, worse, base]):
        ledger.record_injury_snapshots([row], at=START + timedelta(hours=n))
    rows = ledger.conn.execute(
        "SELECT status, fetched_at, last_seen_at FROM injury_snapshots ORDER BY id"
    ).fetchall()
    assert [r[0] for r in rows] == ["Questionable", "Out", "Questionable"]
    assert rows[0][2] == (START + timedelta(hours=1)).isoformat()
    assert rows[2][2] is None


def test_existing_ledgers_get_last_seen_at(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE injury_snapshots (id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL,"
        " sport TEXT NOT NULL, team_id TEXT NOT NULL, athlete TEXT NOT NULL, position TEXT,"
        " status TEXT, status_type TEXT, reported_at TEXT, detail TEXT, run_id INTEGER)"
    )
    conn.commit()
    conn.close()
    ledger = Ledger(path, Decimal(100))
    columns = {r[1] for r in ledger.conn.execute("PRAGMA table_info(injury_snapshots)")}
    assert "last_seen_at" in columns
