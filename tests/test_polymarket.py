from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pytest_httpx import HTTPXMock

from prediction_bot.clv import record_polymarket_clv
from prediction_bot.clv_report import clv_report
from prediction_bot.config import Settings
from prediction_bot.paper.ledger import Ledger
from prediction_bot.reference_quotes import (
    TrackedEvent,
    match_tokens,
    poll_reference_quotes,
    tracked_events,
)
from prediction_bot.teams import team_for_reference_name
from prediction_bot.venues.polymarket import (
    PolyEvent,
    PolyMarket,
    PolymarketClient,
    parse_book,
    parse_event,
)

UTC = timezone.utc
KICK = datetime(2026, 11, 8, 18, tzinfo=UTC)
NFL = "KXNFLGAME-26NOV08ARIDAL"
MLS = "KXMLSGAME-26NOV08CHIORL"


def _market(outcomes: list[str], tokens: list[str], subject: str = "", start=KICK) -> PolyMarket:  # type: ignore[no-untyped-def]
    return PolyMarket("q", tuple(outcomes), tuple(tokens), subject, start)


# --- parsing -----------------------------------------------------------------------


def test_parse_event_reads_json_string_lists_and_skips_bad_markets() -> None:
    event = parse_event(
        {
            "title": "Cardinals vs. Cowboys",
            "startDate": "2026-11-08T18:00:00Z",
            "markets": [
                {
                    "question": "Cardinals vs. Cowboys",
                    "outcomes": '["Cardinals", "Cowboys"]',
                    "clobTokenIds": '["t1", "t2"]',
                    "gameStartTime": "2026-11-08 18:00:00+00",
                },
                {"outcomes": '["Yes", "No"]', "clobTokenIds": '["only-one"]'},
                {"outcomes": ["A", "B"], "clobTokenIds": ["x", "y"], "closed": True},
            ],
        }
    )
    assert event.start == KICK
    (market,) = event.markets
    assert (market.outcomes, market.token_ids, market.start) == (
        ("Cardinals", "Cowboys"),
        ("t1", "t2"),
        KICK,
    )
    assert parse_book(
        {"bids": [{"price": "0.40"}, {"price": "0.42"}], "asks": [{"price": "0.45"}]}
    ) == (
        Decimal("0.42"),
        Decimal("0.45"),
    )
    assert parse_book({"bids": [], "asks": [{"price": "bad"}]}) == (None, None)


def test_client_pages_gamma_events_and_reads_the_book(httpx_mock: HTTPXMock) -> None:
    gamma = "https://gamma.example"
    page = "&closed=false&active=true&limit=1"
    httpx_mock.add_response(
        url=f"{gamma}/events?tag_slug=nfl{page}&offset=0", json=[{"title": "A vs. B"}]
    )
    httpx_mock.add_response(url=f"{gamma}/events?tag_slug=nfl{page}&offset=1", json=[])
    httpx_mock.add_response(
        url="https://clob.example/book?token_id=t1",
        json={"bids": [{"price": "0.50", "size": "9"}], "asks": [{"price": "0.52", "size": "4"}]},
    )
    client = PolymarketClient(gamma, "https://clob.example")
    assert [e.title for e in client.events("nfl", limit=1)] == ["A vs. B"]
    assert client.book("t1") == (Decimal("0.50"), Decimal("0.52"))
    assert client.requests == 3


def test_reference_names_use_aliases_or_a_unique_nickname() -> None:
    assert team_for_reference_name("americanfootball_nfl", "Cardinals") == "arizona-cardinals"
    assert team_for_reference_name("americanfootball_nfl", "Dallas Cowboys") == "dallas-cowboys"
    assert team_for_reference_name("americanfootball_nfl", "New York") is None  # Giants or Jets
    assert team_for_reference_name("soccer_epl", "United") is None


# --- matching ----------------------------------------------------------------------

NFL_EVENT = TrackedEvent(
    NFL, "americanfootball_nfl", KICK, frozenset({"arizona-cardinals", "dallas-cowboys"})
)
MLS_EVENT = TrackedEvent(
    MLS, "soccer_usa_mls", KICK, frozenset({"chicago-fire", "orlando-city-sc", "draw"})
)


def test_two_way_match_needs_both_teams_and_a_close_kickoff() -> None:
    game = PolyEvent(
        "Cardinals vs. Cowboys", KICK, (_market(["Cardinals", "Cowboys"], ["t1", "t2"]),)
    )
    tokens, unknown = match_tokens(NFL_EVENT, [game])
    assert tokens == {"arizona-cardinals": "t1", "dallas-cowboys": "t2"} and unknown == []
    late = PolyEvent("Cardinals vs. Cowboys", KICK + timedelta(hours=5), game.markets)
    assert match_tokens(NFL_EVENT, [late])[0] == {}
    other = PolyEvent("Cardinals vs. Rams", KICK, (_market(["Cardinals", "Rams"], ["a", "b"]),))
    assert match_tokens(NFL_EVENT, [other])[0] == {}
    odd = PolyEvent("x", KICK, (_market(["Cardinals", "Big Red"], ["a", "b"]),))
    assert match_tokens(NFL_EVENT, [odd]) == ({}, ["Big Red"])


def test_three_way_soccer_uses_yes_tokens_of_team_and_draw_markets() -> None:
    game = PolyEvent(
        "Chicago Fire vs. Orlando City SC",
        KICK,
        (
            _market(["Yes", "No"], ["chi-y", "chi-n"], "Chicago Fire"),
            _market(["Yes", "No"], ["draw-y", "draw-n"], "Draw (Chicago Fire vs. Orlando City SC)"),
            _market(["Yes", "No"], ["orl-y", "orl-n"], "Orlando City SC"),
        ),
    )
    tokens, _ = match_tokens(MLS_EVENT, [game])
    assert tokens == {"chicago-fire": "chi-y", "draw": "draw-y", "orlando-city-sc": "orl-y"}


# --- polling and CLV -----------------------------------------------------------------


class FakePoly:
    def __init__(self, events: list[PolyEvent]) -> None:
        self.events_list = events
        self.requests = 0

    def events(self, tag_slug: str) -> list[PolyEvent]:
        self.requests += 1
        return self.events_list

    def book(self, token_id: str) -> tuple[Decimal, Decimal]:
        self.requests += 1
        return {"t1": (Decimal("0.40"), Decimal("0.42")), "t2": (Decimal("0.57"), Decimal("0.59"))}[
            token_id
        ]


def _tracked_ledger(tmp_path, now: datetime) -> Ledger:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "ref.sqlite", Decimal("1000"))
    for code in ("ARI", "DAL"):
        ledger.conn.execute(
            "INSERT INTO market_snapshots (fetched_at,ticker,event_ticker,series,yes_bid,yes_ask,"
            "yes_bid_size,yes_ask_size,open_interest,volume,expected_expiration) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                now.isoformat(),
                f"{NFL}-{code}",
                NFL,
                "KXNFLGAME",
                "0.4",
                "0.42",
                "1",
                "1",
                "1",
                "1",
                (KICK + timedelta(hours=3)).isoformat(),
            ),
        )
    ledger.conn.execute(
        "INSERT INTO shadow_decisions (created_at,rules_version,window,strategy,ticker,side,"
        "would_trade,reason,price,commence_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            now.isoformat(),
            "v3",
            "<2h",
            "sportsbook_v3",
            f"{NFL}-ARI",
            "no",
            0,
            "net_edge",
            "0.57",
            KICK.isoformat(),
        ),
    )
    ledger.conn.commit()
    return ledger


def test_poll_stores_mids_respects_the_cap_and_logs_unmatched(tmp_path) -> None:  # type: ignore[no-untyped-def]
    now = KICK - timedelta(minutes=13)
    ledger = _tracked_ledger(tmp_path, now)
    assert [e.event_ticker for e in tracked_events(ledger, now)] == [NFL]
    game = PolyEvent(
        "Cardinals vs. Cowboys", KICK, (_market(["Cardinals", "Cowboys"], ["t1", "t2"]),)
    )
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert poll_reference_quotes(ledger, FakePoly([game]), settings, now=now) == 2  # type: ignore[arg-type]
    rows = ledger.conn.execute(
        "SELECT side,mid,bid,ask,market_ref FROM reference_quotes ORDER BY side"
    ).fetchall()
    assert rows == [
        ("arizona-cardinals", "0.41", "0.40", "0.42", "t1"),
        ("dallas-cowboys", "0.58", "0.57", "0.59", "t2"),
    ]
    capped = Settings(_env_file=None, polymarket_max_requests=1)  # type: ignore[call-arg]
    assert poll_reference_quotes(ledger, FakePoly([game]), capped, now=now) == 0  # type: ignore[arg-type]

    assert poll_reference_quotes(ledger, FakePoly([]), settings, now=now) == 0  # type: ignore[arg-type]
    assert ledger.conn.execute(
        "SELECT event_ticker,reason FROM reference_unmatched"
    ).fetchall() == [(NFL, "no_market")]


def test_clv_polymarket_uses_the_close_window_and_the_side(tmp_path) -> None:  # type: ignore[no-untyped-def]
    now = KICK - timedelta(minutes=13)
    ledger = _tracked_ledger(tmp_path, now)
    ledger.record_reference_quote(
        NFL,
        "arizona-cardinals",
        "polymarket",
        Decimal("0.41"),
        None,
        None,
        "t1",
        KICK - timedelta(minutes=50),
    )
    assert record_polymarket_clv(ledger, now=KICK + timedelta(hours=1)) == 0  # too early
    ledger.record_reference_quote(
        NFL,
        "arizona-cardinals",
        "polymarket",
        Decimal("0.40"),
        None,
        None,
        "t1",
        KICK - timedelta(minutes=13),
    )
    assert record_polymarket_clv(ledger, now=KICK + timedelta(hours=1)) == 1
    clv = ledger.conn.execute("SELECT clv_polymarket FROM shadow_decisions").fetchone()[0]
    assert Decimal(clv) == Decimal("3.00")  # NO on Arizona: (1 - 0.40) - 0.57
    ledger.conn.execute("UPDATE shadow_decisions SET clv_status='ok'")
    ledger.conn.commit()
    report = clv_report(ledger.conn)
    poly = report["cohorts"]["v3_net_edge"]["metrics"]["clv_polymarket"]["all"]
    assert poly["n"] == 1 and poly["mean"] == 3.0


def test_reference_gaps_command_lists_unmatched_events(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import prediction_bot.cli as cli

    path = tmp_path / "gaps.sqlite"
    ledger = Ledger(path, Decimal("1000"))
    ledger.record_reference_unmatched(NFL, "polymarket", "no_market", "Big Red", datetime.now(UTC))
    monkeypatch.setattr(cli, "_settings", lambda: Settings(_env_file=None, paper_db_path=path))  # type: ignore[call-arg]
    cli.reference_gaps(days=14)
    out = capsys.readouterr().out
    assert "- `Big Red` (1 events)" in out and f"| `{NFL}` | no_market | 1 |" in out
