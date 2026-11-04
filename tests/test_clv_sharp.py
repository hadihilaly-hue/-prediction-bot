from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from prediction_bot.clv import leg_outcome, record_sharp_clv, sharp_close_fair
from prediction_bot.devig import devig_all
from prediction_bot.odds import ConsensusOdds
from prediction_bot.paper.ledger import Ledger

KICKOFF = datetime(2026, 11, 1, 20, 25, tzinfo=timezone.utc)
NOW = KICKOFF + timedelta(hours=4)
NFL_EVENT = "KXNFLGAME-26NOV01ARIDAL"
MLS_EVENT = "KXMLSGAME-26OCT28CHIORL"


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def _game(
    event_id: str,
    home: str,
    away: str,
    books: dict[str, tuple[dict[str, str], datetime | None]],
) -> ConsensusOdds:
    book_odds = {
        book: {k: Decimal(v) for k, v in odds.items()} for book, (odds, _) in books.items()
    }
    updates = {book: _iso(at) for book, (_, at) in books.items() if at is not None}
    names = next(iter(book_odds.values()))
    return ConsensusOdds(
        event_id=event_id,
        home_team=home,
        away_team=away,
        commence_time=_iso(KICKOFF),
        probs={name: Decimal("0.5") for name in names},
        books_used=len(books),
        book_updates=updates,
        book_odds=book_odds,
    )


def _ledger(tmp_path) -> Ledger:  # type: ignore[no-untyped-def]
    return Ledger(tmp_path / "paper.sqlite", Decimal("1000"))


def _fill(ledger: Ledger, ticker: str, side: str, price: str, event: str) -> int:
    cursor = ledger.conn.execute(
        "INSERT INTO fills (created_at,strategy,ticker,side,price,count,fee,fair_prob,"
        "event_ticker,commence_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            (KICKOFF - timedelta(days=4)).isoformat(),
            "sportsbook_v3",
            ticker,
            side,
            price,
            "10",
            "0.10",
            "0.70",
            event,
            KICKOFF.isoformat(),
        ),
    )
    ledger.conn.commit()
    return int(cursor.lastrowid or 0)


def _near_miss(ledger: Ledger, ticker: str, side: str, price: str) -> int:
    cursor = ledger.conn.execute(
        "INSERT INTO shadow_decisions (created_at,rules_version,window,strategy,ticker,side,"
        "would_trade,reason,fair,price,size,commence_time,net_edge) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            (KICKOFF - timedelta(days=4)).isoformat(),
            "v3",
            "72h+",
            "sportsbook_v3",
            ticker,
            side,
            0,
            "net_edge",
            "0.30",
            price,
            "0",
            KICKOFF.isoformat(),
            "-0.02",
        ),
    )
    ledger.conn.commit()
    return int(cursor.lastrowid or 0)


def _sharp(ledger: Ledger, table: str, row_id: int) -> tuple[str | None, str | None]:
    row = ledger.conn.execute(
        f"SELECT clv_sharp_close,clv_sharp_source FROM {table} WHERE id=?", (row_id,)
    ).fetchone()
    return row[0], row[1]


NFL_NAMES = ("Arizona Cardinals", "Dallas Cowboys")


def _nfl(
    books: dict[str, tuple[tuple[str, str], datetime | None]],
) -> dict[str, tuple[dict[str, str], datetime | None]]:
    return {
        book: (dict(zip(NFL_NAMES, odds, strict=True)), at) for book, (odds, at) in books.items()
    }


def test_leg_outcome_maps_team_and_tie_legs() -> None:
    assert leg_outcome(f"{NFL_EVENT}-ARI") == "arizona-cardinals"
    assert leg_outcome(f"{MLS_EVENT}-TIE") == "draw"
    assert leg_outcome(f"{MLS_EVENT}-CHI") == "chicago-fire"
    assert leg_outcome("KXNOPE-26NOV01AB-AB") is None


def test_two_way_uses_pinnacle_and_ignores_soft_books(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = _ledger(tmp_path)
    fresh = KICKOFF - timedelta(minutes=8)
    game = _game(
        "nfl1",
        "Dallas Cowboys",
        "Arizona Cardinals",
        _nfl(
            {
                # Equal prices devig to exactly 0.5 under every method.
                "pinnacle": (("1.90", "1.90"), fresh),
                "draftkings": (("3.00", "1.40"), fresh),
            }
        ),
    )
    ledger.record_consensus(
        {"americanfootball_nfl": {"nfl1": game}},
        at=KICKOFF - timedelta(minutes=5),
        event_links={"nfl1": NFL_EVENT},
    )
    no_fill = _fill(ledger, f"{NFL_EVENT}-ARI", "no", "0.68", NFL_EVENT)
    yes_fill = _fill(ledger, f"{NFL_EVENT}-DAL", "yes", "0.45", NFL_EVENT)

    assert record_sharp_clv(ledger, now=NOW) == 2

    clv, source = _sharp(ledger, "fills", no_fill)
    assert source == "pinnacle"
    assert Decimal(str(clv)) == Decimal("-18.00")  # (1 - 0.50 - 0.68) * 100
    clv, source = _sharp(ledger, "fills", yes_fill)
    assert (source, Decimal(str(clv))) == ("pinnacle", Decimal("5.00"))


def test_fallback_order_circa_then_consensus_of_circa_and_betonline() -> None:
    fresh = KICKOFF - timedelta(minutes=3)
    circa_only = json.dumps(
        {
            "circa": {
                "odds": dict(zip(NFL_NAMES, ("2.50", "1.60"), strict=True)),
                "last_update": _iso(fresh),
            }
        }
    )
    fair, source = sharp_close_fair(
        [(KICKOFF - timedelta(minutes=2), circa_only)],
        sport="americanfootball_nfl",
        outcome="arizona-cardinals",
        side="yes",
        commence=KICKOFF,
    ) or (None, None)
    expected = devig_all({"a": Decimal("2.50"), "b": Decimal("1.60")})["power"]["a"]
    assert source == "circa"
    assert fair == expected

    both = json.dumps(
        {
            "circa": {
                "odds": dict(zip(NFL_NAMES, ("2.00", "2.00"), strict=True)),
                "last_update": _iso(fresh),
            },
            "betonlineag": {
                "odds": dict(zip(NFL_NAMES, ("1.50", "3.00"), strict=True)),
                "last_update": _iso(fresh),
            },
        }
    )
    fair, source = sharp_close_fair(
        [(KICKOFF - timedelta(minutes=2), both)],
        sport="americanfootball_nfl",
        outcome="arizona-cardinals",
        side="yes",
        commence=KICKOFF,
    ) or (None, None)
    bol = devig_all({"a": Decimal("1.50"), "b": Decimal("3.00")})["power"]["a"]
    assert source == "consensus"
    assert fair == (Decimal("0.5") * 3 + bol * 2) / 5  # BOOK_WEIGHTS circa 3, betonline 2


def test_three_way_soccer_tie_and_no_legs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = _ledger(tmp_path)
    odds = {"Chicago Fire": "2.40", "Orlando City SC": "3.10", "Draw": "3.40"}
    game = _game(
        "mls1",
        "Chicago Fire",
        "Orlando City SC",
        {"pinnacle": (odds, KICKOFF - timedelta(minutes=15))},
    )
    ledger.record_consensus(
        {"soccer_usa_mls": {"mls1": game}},
        at=KICKOFF - timedelta(minutes=10),
        event_links={"mls1": MLS_EVENT},
    )
    tie = _near_miss(ledger, f"{MLS_EVENT}-TIE", "yes", "0.21")
    chi_no = _near_miss(ledger, f"{MLS_EVENT}-CHI", "no", "0.55")

    assert record_sharp_clv(ledger, now=NOW) == 2

    power = devig_all({k: Decimal(v) for k, v in odds.items()})["power"]
    clv, source = _sharp(ledger, "shadow_decisions", tie)
    assert source == "pinnacle"
    assert Decimal(str(clv)) == (power["Draw"] - Decimal("0.21")) * 100
    # NO on Chicago wins on an Orlando win *or* a draw: 1 - P(Chicago), not P(Orlando).
    clv, _ = _sharp(ledger, "shadow_decisions", chi_no)
    assert Decimal(str(clv)) == (Decimal(1) - power["Chicago Fire"] - Decimal("0.55")) * 100
    assert Decimal(str(clv)) != (power["Orlando City SC"] - Decimal("0.55")) * 100


def test_no_fresh_snapshot_leaves_null(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = _ledger(tmp_path)
    stale = _game(
        "nfl1",
        "Dallas Cowboys",
        "Arizona Cardinals",
        _nfl(
            {
                "pinnacle": (("1.90", "1.90"), KICKOFF - timedelta(minutes=30)),  # too old
                "circa": (("1.90", "1.90"), None),  # no last_update: fail closed
                "fanduel": (("1.90", "1.90"), KICKOFF - timedelta(minutes=1)),  # never sharp
            }
        ),
    )
    ledger.record_consensus(
        {"americanfootball_nfl": {"nfl1": stale}},
        at=KICKOFF - timedelta(minutes=25),
        event_links={"nfl1": NFL_EVENT},
    )
    in_play = _game(
        "nfl1",
        "Dallas Cowboys",
        "Arizona Cardinals",
        _nfl({"pinnacle": (("1.90", "1.90"), KICKOFF - timedelta(minutes=1))}),
    )
    # Taken after kickoff: not a pre-game close even though the update time qualifies.
    ledger.record_consensus(
        {"americanfootball_nfl": {"nfl1": in_play}},
        at=KICKOFF + timedelta(minutes=5),
        event_links={"nfl1": NFL_EVENT},
    )
    fill = _fill(ledger, f"{NFL_EVENT}-ARI", "no", "0.68", NFL_EVENT)

    assert record_sharp_clv(ledger, now=NOW) == 0
    assert _sharp(ledger, "fills", fill) == (None, None)


def test_unlinked_snapshot_and_future_game_are_not_scored(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = _ledger(tmp_path)
    game = _game(
        "nfl1",
        "Dallas Cowboys",
        "Arizona Cardinals",
        _nfl({"pinnacle": (("1.90", "1.90"), KICKOFF - timedelta(minutes=5))}),
    )
    ledger.record_consensus({"americanfootball_nfl": {"nfl1": game}}, at=KICKOFF)
    fill = _fill(ledger, f"{NFL_EVENT}-ARI", "no", "0.68", NFL_EVENT)
    assert record_sharp_clv(ledger, now=NOW) == 0
    assert _sharp(ledger, "fills", fill) == (None, None)

    ledger.record_consensus(
        {"americanfootball_nfl": {"nfl1": game}},
        at=KICKOFF - timedelta(minutes=1),
        event_links={"nfl1": NFL_EVENT},
    )
    assert record_sharp_clv(ledger, now=KICKOFF - timedelta(minutes=1)) == 0  # not started
    assert record_sharp_clv(ledger, now=NOW) == 1


def test_record_consensus_stores_book_odds_and_event_ticker(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = _ledger(tmp_path)
    fresh = KICKOFF - timedelta(minutes=5)
    game = _game(
        "nfl1",
        "Dallas Cowboys",
        "Arizona Cardinals",
        _nfl({"pinnacle": (("1.90", "1.95"), fresh)}),
    )
    assert (
        ledger.record_consensus(
            {"americanfootball_nfl": {"nfl1": game}}, event_links={"nfl1": NFL_EVENT}
        )
        == 2
    )
    rows = ledger.conn.execute("SELECT event_ticker,book_odds_json FROM odds_snapshots").fetchall()
    assert {row[0] for row in rows} == {NFL_EVENT}
    stored = json.loads(rows[0][1])
    assert stored == {
        "pinnacle": {
            "odds": {"Arizona Cardinals": "1.90", "Dallas Cowboys": "1.95"},
            "last_update": _iso(fresh),
        }
    }
