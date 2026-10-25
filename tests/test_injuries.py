"""Phase 1 injury measurement: ESPN parser, per-run snapshot, settlement tag."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from prediction_bot.injuries import (
    InjuryClient,
    InjurySnapshotRow,
    parse_injuries,
    snapshot_injuries,
)
from prediction_bot.models import OutcomeSide
from prediction_bot.paper.ledger import Ledger

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "espn_injuries_nfl.json").read_text())

ENTRY = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
KICKOFF = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
NFL_EVENT = "KXNFLGAME-25OCT26DETKC"
NFL_TICKER = NFL_EVENT + "-KC"
CFB_EVENT = "KXNCAAFGAME-25OCT25ALAARK"
CFB_TICKER = CFB_EVENT + "-ALA"


def _ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "paper.sqlite", Decimal("1000"))


def _nfl_fill(ledger: Ledger, side: OutcomeSide = OutcomeSide.yes) -> None:
    ledger.record_fill(
        "sportsbook_v3",
        NFL_TICKER,
        side,
        Decimal("0.5"),
        Decimal("1"),
        Decimal("0"),
        Decimal("0.55"),
        at=ENTRY,
        position_key=f"{NFL_EVENT}:kansas-city-chiefs",
        event_ticker=NFL_EVENT,
        commence_time=KICKOFF,
    )


def _cfb_fill(ledger: Ledger) -> None:
    ledger.record_fill(
        "sportsbook_v3",
        CFB_TICKER,
        OutcomeSide.yes,
        Decimal("0.5"),
        Decimal("1"),
        Decimal("0"),
        Decimal("0.55"),
        at=ENTRY,
        position_key=f"{CFB_EVENT}:alabama-crimson-tide",
        event_ticker=CFB_EVENT,
        commence_time=KICKOFF,
    )


def _snap(
    team_id: str,
    athlete: str,
    position: str,
    status: str,
    reported: datetime,
    sport: str = "americanfootball_nfl",
) -> InjurySnapshotRow:
    return InjurySnapshotRow(
        sport=sport,
        team_id=team_id,
        athlete=athlete,
        position=position,
        status=status,
        status_type="INJURY_STATUS_" + status.upper(),
        reported_at=reported,
        detail="test",
    )


def _flag(ledger: Ledger, ticker: str = NFL_TICKER) -> tuple[object, object, object]:
    row = ledger.conn.execute(
        "SELECT injury_flag, injury_detail, injury_source FROM fills WHERE ticker=?",
        (ticker,),
    ).fetchone()
    return row[0], row[1], row[2]


def test_parse_fixture() -> None:
    entries = parse_injuries(FIXTURE)
    assert len(entries) == 5
    mahomes = entries[0]
    assert mahomes.team == "Kansas City Chiefs"
    assert mahomes.athlete == "Patrick Mahomes"
    assert mahomes.position == "QB"
    assert mahomes.status == "Out"
    assert mahomes.status_type == "INJURY_STATUS_OUT"
    assert mahomes.reported_at == datetime(2026, 10, 25, 18, 48, tzinfo=timezone.utc)
    assert "hamstring" in mahomes.detail


def test_parse_schema_surprise() -> None:
    assert parse_injuries({"nope": []}) == []
    assert parse_injuries("garbage") == []
    assert parse_injuries({"injuries": "not-a-list"}) == []


def _client(handler) -> InjuryClient:  # type: ignore[no-untyped-def]
    return InjuryClient(httpx.Client(transport=httpx.MockTransport(handler)))


def test_client_fetches_and_memoizes() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=FIXTURE)

    client = _client(handler)
    entries = client.fetch_injuries("americanfootball_nfl")
    assert len(entries) == 5
    client.fetch_injuries("americanfootball_nfl")
    assert len(calls) == 1  # one request per sport per run
    assert calls[0].endswith("/football/nfl/injuries")


def test_client_non_200_returns_empty() -> None:
    client = _client(lambda request: httpx.Response(500))
    assert client.fetch_injuries("americanfootball_nfl") == []


def test_client_unknown_sport_returns_empty() -> None:
    client = _client(lambda request: httpx.Response(200, json=FIXTURE))
    assert client.fetch_injuries("soccer_mexico_ligamx") == []


def test_tag_own_team_hit(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _nfl_fill(led)
    led.record_injury_snapshots(
        [_snap("kansas-city-chiefs", "Patrick Mahomes", "QB", "Out", ENTRY.replace(hour=18))]
    )
    led.tag_injuries([NFL_TICKER])
    flag, detail, source = _flag(led)
    assert flag == 1
    assert source == "espn"
    assert detail.startswith("own:") and "Mahomes" in detail


def test_tag_opponent_hit(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _nfl_fill(led)
    led.record_injury_snapshots(
        [_snap("detroit-lions", "Penei Sewell", "OT", "Doubtful", ENTRY.replace(hour=18))]
    )
    led.tag_injuries([NFL_TICKER])
    flag, detail, _ = _flag(led)
    assert flag == 1
    assert detail.startswith("opp:")


def test_tag_no_side_flip(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _nfl_fill(led, side=OutcomeSide.no)  # holding KC-NO == Detroit wins
    led.record_injury_snapshots(
        [_snap("detroit-lions", "Penei Sewell", "OT", "Out", ENTRY.replace(hour=18))]
    )
    led.tag_injuries([NFL_TICKER])
    flag, detail, _ = _flag(led)
    assert flag == 1
    assert detail.startswith("own:")  # Detroit is our side now


def test_reported_before_entry_no_flag(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _nfl_fill(led)
    led.record_injury_snapshots(
        [_snap("detroit-lions", "Jahmyr Gibbs", "RB", "Out", ENTRY.replace(hour=6))]
    )
    led.tag_injuries([NFL_TICKER])
    flag, _, source = _flag(led)
    assert flag == 0 and source == "espn"


def test_reported_after_kickoff_no_flag(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _nfl_fill(led)
    led.record_injury_snapshots(
        [_snap("kansas-city-chiefs", "Patrick Mahomes", "QB", "Out", KICKOFF.replace(hour=14))]
    )
    led.tag_injuries([NFL_TICKER])
    flag, _, _ = _flag(led)
    assert flag == 0


def test_non_key_position_no_flag(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _nfl_fill(led)
    led.record_injury_snapshots(
        [_snap("kansas-city-chiefs", "Juan Thornhill", "S", "Out", ENTRY.replace(hour=18))]
    )
    led.tag_injuries([NFL_TICKER])
    flag, _, _ = _flag(led)
    assert flag == 0  # S is not a key position


def test_questionable_no_flag(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _nfl_fill(led)
    led.record_injury_snapshots(
        [_snap("kansas-city-chiefs", "Travis Kelce", "TE", "Questionable", ENTRY.replace(hour=18))]
    )
    led.tag_injuries([NFL_TICKER])
    flag, _, _ = _flag(led)
    assert flag == 0  # Questionable is for the Phase 2 gate, not the tag


def test_no_coverage_is_unknown(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _nfl_fill(led)
    led.tag_injuries([NFL_TICKER])
    flag, detail, source = _flag(led)
    assert flag is None and detail is None and source is None


def test_settle_triggers_tag(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _nfl_fill(led)
    led.record_injury_snapshots(
        [_snap("kansas-city-chiefs", "Patrick Mahomes", "QB", "Out", ENTRY.replace(hour=18))]
    )
    led.settle(NFL_TICKER, OutcomeSide.yes)
    flag, detail, _ = _flag(led)
    assert flag == 1


def _odds_row(
    ledger: Ledger,
    created: datetime,
    prob: str,
    team: str = "Alabama Crimson Tide",
) -> None:
    ledger.conn.execute(
        "INSERT INTO odds_snapshots (created_at, feed, event_id, home_team, away_team,"
        " commence_time, team, prob, books_used)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            created.isoformat(),
            "americanfootball_ncaaf",
            "ev-ala-ark",
            "Alabama Crimson Tide",
            "Arkansas Razorbacks",
            KICKOFF.isoformat(),
            team,
            prob,
            4,
        ),
    )
    ledger.conn.commit()


def test_cfb_proxy_flags_adverse_move(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _cfb_fill(led)
    _odds_row(led, ENTRY, "0.65")
    _odds_row(led, ENTRY.replace(hour=18), "0.60")  # -5c before kickoff
    led.tag_injuries([CFB_TICKER])
    flag, detail, source = _flag(led, CFB_TICKER)
    assert flag == 1
    assert source == "line_move_proxy"
    assert "proxy" in detail


def test_cfb_proxy_small_move_no_flag(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _cfb_fill(led)
    _odds_row(led, ENTRY, "0.65")
    _odds_row(led, ENTRY.replace(hour=18), "0.63")  # -2c: below the 4c bar
    led.tag_injuries([CFB_TICKER])
    flag, _, source = _flag(led, CFB_TICKER)
    assert flag == 0
    assert source == "line_move_proxy"


def test_cfb_no_line_data_is_unknown(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _cfb_fill(led)
    led.tag_injuries([CFB_TICKER])
    flag, _, _ = _flag(led, CFB_TICKER)
    assert flag is None


def test_snapshot_injuries_only_relevant_teams(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _nfl_fill(led)
    client = _client(lambda request: httpx.Response(200, json=FIXTURE))
    n = snapshot_injuries(led, client, run_id=7)
    rows = led.conn.execute("SELECT DISTINCT team_id, run_id FROM injury_snapshots").fetchall()
    assert {r[0] for r in rows} == {"kansas-city-chiefs", "detroit-lions"}
    assert {r[1] for r in rows} == {7}
    assert n == 5  # 3 KC + 2 DET entries; Eagles aren't in our games


def test_injury_snapshot_events_window(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _nfl_fill(led)
    events = led.injury_snapshot_events(datetime(2026, 10, 25, tzinfo=timezone.utc))
    assert events == [NFL_EVENT]
