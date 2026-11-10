"""One source of truth for kickoff times (credits/kickoff fix, Part 1)."""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from prediction_bot import kickoffs
from prediction_bot.clv import reanchor_kickoffs, record_clv, refresh_clv_statuses
from prediction_bot.config import Settings
from prediction_bot.models import MakerShadowCandidate, OutcomeSide
from prediction_bot.paper.ledger import Ledger
from prediction_bot.pull_plan import (
    eligible_feeds,
    mark_pulled,
    plan_kickoffs,
    plan_pulls,
    select_plan_pull,
)

UTC = timezone.utc
SETTINGS = Settings(_env_file=None)  # type: ignore[call-arg]
SAT = datetime(2026, 11, 14, tzinfo=UTC)
EVENT = "KXNCAAFGAME-26NOV14CINISU"


def _ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "k.sqlite", Decimal("1000"))


def _snapshot(
    ledger: Ledger, event: str, commence: datetime, created: datetime, event_id: str = "oid"
) -> None:
    ledger.conn.execute(
        "INSERT INTO odds_snapshots (created_at,feed,event_id,home_team,away_team,"
        "commence_time,team,prob,books_used,event_ticker) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (created.isoformat(), "americanfootball_ncaaf", event_id, "A", "B",
         commence.strftime("%Y-%m-%dT%H:%M:%SZ"), "A", "0.5", 2, event),
    )  # fmt: skip
    ledger.conn.commit()


def _game(ledger: Ledger, event: str, expiration: datetime, fetched: datetime) -> None:
    ledger.conn.execute(
        "INSERT INTO market_snapshots (fetched_at,ticker,event_ticker,series,yes_bid,yes_ask,"
        "yes_bid_size,yes_ask_size,open_interest,volume,expected_expiration) "
        "VALUES (?,?,?,?,'0.48','0.50','100','100','10','10',?)",
        (fetched.isoformat(), f"{event}-A", event, event.split("-", 1)[0],
         expiration.isoformat()),
    )  # fmt: skip
    ledger.conn.commit()


def _eligible(ledger: Ledger, feeds: list[str], at: datetime) -> None:
    ledger.conn.executemany(
        "INSERT INTO feed_stats (computed_at,feed,lead_p50_h,lead_p90_h,pulls_14d,"
        "useful_pulls_14d,provisional,eligible) VALUES (?,?,100,200,0,0,0,1)",
        [(at.isoformat(), feed) for feed in feeds],
    )
    ledger.conn.commit()


# --- observe / resolve ----------------------------------------------------------------


def test_resolve_priority_and_live_observations_never_win(tmp_path: Path) -> None:
    conn = _ledger(tmp_path).conn
    at = SAT - timedelta(days=2)
    assert kickoffs.resolve(conn, EVENT) is None
    kickoffs.observe(conn, EVENT, SAT.replace(hour=20, minute=30), "kalshi_expiry", at)
    assert kickoffs.resolve(conn, EVENT) == (SAT.replace(hour=20, minute=30), "kalshi_expiry")
    assert kickoffs.resolve_odds(conn, EVENT) is None
    # Odds beat the Kalshi estimate; a later estimate never overwrites the books.
    kickoffs.observe(conn, EVENT, SAT.replace(hour=17), "odds", at + timedelta(hours=1))
    kickoffs.observe(conn, EVENT, SAT.replace(hour=21), "kalshi_expiry", at + timedelta(hours=2))
    assert kickoffs.resolve(conn, EVENT) == (SAT.replace(hour=17), "odds")
    # The free listing ranks equal: the most recent observation wins...
    kickoffs.observe(conn, EVENT, SAT.replace(hour=18), "odds_events", at + timedelta(hours=3))
    assert kickoffs.resolve(conn, EVENT) == (SAT.replace(hour=18), "odds_events")
    # ...and an older one of either source does not.
    kickoffs.observe(conn, EVENT, SAT.replace(hour=19), "odds", at + timedelta(hours=2))
    assert kickoffs.resolve(conn, EVENT) == (SAT.replace(hour=18), "odds_events")
    # Once live, the Odds API rewrites commence_time to the real start: ignored.
    assert not kickoffs.observe(
        conn, EVENT, SAT.replace(hour=18, minute=7), "odds", SAT.replace(hour=18, minute=30)
    )
    assert kickoffs.resolve(conn, EVENT) == (SAT.replace(hour=18), "odds_events")


def test_live_snapshot_never_wins_through_sync(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _snapshot(ledger, EVENT, SAT.replace(hour=17), SAT - timedelta(days=1))
    _snapshot(ledger, EVENT, SAT.replace(hour=17, minute=4), SAT.replace(hour=17, minute=30))
    kickoffs.sync_snapshots(ledger.conn)
    assert kickoffs.resolve(ledger.conn, EVENT) == (SAT.replace(hour=17), "odds")


def test_kickoff_moves_earlier_later_and_back(tmp_path: Path) -> None:
    conn = _ledger(tmp_path).conn
    t0 = SAT - timedelta(days=3)
    k = SAT.replace(hour=20, minute=30)
    kickoffs.observe(conn, EVENT, k, "odds", t0)

    def row() -> tuple[Any, ...]:
        return tuple(
            conn.execute(
                "SELECT kickoff,prev_kickoff,changed_at FROM event_kickoffs WHERE event_ticker=?",
                (EVENT,),
            ).fetchone()
        )

    assert row() == (k.isoformat(), None, None)
    # Within 60 seconds is not a move: the stored kickoff stays.
    kickoffs.observe(conn, EVENT, k + timedelta(seconds=30), "odds", t0 + timedelta(hours=1))
    assert row() == (k.isoformat(), None, None)
    earlier = SAT.replace(hour=17)
    t1 = t0 + timedelta(hours=2)
    kickoffs.observe(conn, EVENT, earlier, "odds", t1)
    assert row() == (earlier.isoformat(), k.isoformat(), t1.isoformat())
    later = SAT.replace(hour=22, minute=30)
    t2 = t1 + timedelta(hours=1)
    kickoffs.observe(conn, EVENT, later, "odds_events", t2)
    assert row() == (later.isoformat(), earlier.isoformat(), t2.isoformat())
    t3 = t2 + timedelta(hours=1)
    kickoffs.observe(conn, EVENT, earlier, "odds", t3)
    assert row() == (earlier.isoformat(), later.isoformat(), t3.isoformat())


def test_record_consensus_observes_linked_events(tmp_path: Path) -> None:
    from prediction_bot.odds import ConsensusOdds

    ledger = _ledger(tmp_path)
    game = ConsensusOdds(
        event_id="oid",
        home_team="Iowa State Cyclones",
        away_team="Cincinnati Bearcats",
        commence_time="2026-11-14T17:00:00Z",
        probs={"Iowa State Cyclones": Decimal("0.6"), "Cincinnati Bearcats": Decimal("0.4")},
        books_used=2,
    )
    other = ConsensusOdds(
        event_id="unlinked",
        home_team="X",
        away_team="Y",
        commence_time="2026-11-14T19:00:00Z",
        probs={"X": Decimal("0.5"), "Y": Decimal("0.5")},
        books_used=2,
    )
    ledger.record_consensus(
        {"americanfootball_ncaaf": {"oid": game, "unlinked": other}},
        at=SAT - timedelta(days=1),
        event_links={"oid": EVENT},
    )
    assert kickoffs.resolve(ledger.conn, EVENT) == (SAT.replace(hour=17), "odds")
    assert ledger.conn.execute("SELECT COUNT(*) FROM event_kickoffs").fetchone()[0] == 1


# --- expiry offsets --------------------------------------------------------------------


def test_expiry_offset_static_table_then_series_median(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    now = SAT
    conn = ledger.conn
    assert kickoffs.expiry_offset(conn, "KXEPLGAME", now) == timedelta(hours=3)
    assert kickoffs.expiry_offset(conn, "KXMLSGAME", now) == timedelta(hours=2, minutes=50)
    assert kickoffs.expiry_offset(conn, "KXNBAGAME", now) == timedelta(hours=2, minutes=50)
    assert kickoffs.expiry_offset(conn, "KXNHLGAME", now) == timedelta(hours=2, minutes=50)
    assert kickoffs.expiry_offset(conn, "KXNFLGAME", now) == timedelta(hours=3)
    # Four linked NHL games: still the static table. A fifth: their median.
    for i, minutes in enumerate([160, 165, 170, 175, 200]):
        event = f"KXNHLGAME-26NOV0{i + 1}AB"
        kickoff = now - timedelta(days=i + 1)
        _game(ledger, event, kickoff + timedelta(minutes=minutes), kickoff - timedelta(hours=2))
        kickoffs.observe(conn, event, kickoff, "odds", kickoff - timedelta(hours=1))
        expected = timedelta(hours=2, minutes=50) if i < 4 else timedelta(minutes=170)
        assert kickoffs.expiry_offset(conn, "KXNHLGAME", now) == expected
    # Estimates never feed the median.
    conn.execute("UPDATE event_kickoffs SET source='kalshi_expiry'")
    assert kickoffs.expiry_offset(conn, "KXNHLGAME", now) == timedelta(hours=2, minutes=50)


def test_no_ncaaf_close_from_a_kalshi_estimate(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    now = SAT.replace(hour=6)
    _eligible(ledger, ["americanfootball_ncaaf"], now - timedelta(days=1))
    _game(ledger, EVENT, SAT.replace(hour=23, minute=30), now - timedelta(hours=1))
    kickoffs_ = plan_kickoffs(ledger.conn, eligible_feeds(ledger.conn), now)
    assert kickoffs_[EVENT] == ("americanfootball_ncaaf", SAT.replace(hour=20, minute=30))
    assert kickoffs.resolve(ledger.conn, EVENT)[1] == "kalshi_expiry"  # type: ignore[index]
    plan_pulls(ledger, kickoffs_, now)
    targets = {str(t) for (t,) in ledger.conn.execute("SELECT target FROM pull_plan")}
    assert "T-10m" not in targets and targets
    # Once the books list the game, the close is planned at their kickoff.
    _snapshot(ledger, EVENT, SAT.replace(hour=17), now + timedelta(minutes=5))
    later = now + timedelta(minutes=10)
    plan_pulls(ledger, plan_kickoffs(ledger.conn, eligible_feeds(ledger.conn), later), later)
    close = ledger.conn.execute(
        "SELECT target_at,status FROM pull_plan WHERE target='T-10m'"
    ).fetchone()
    assert close == (SAT.replace(hour=16, minute=50).isoformat(), "planned")


# --- free /events refresh --------------------------------------------------------------


class FreeEvents:
    """Fake OddsClient: /events never moves x-requests-used."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events_free = True
        self.requests_used: str | None = None
        self.requests_remaining = "266"
        self.requests_made = 0
        self._events_requests_used_baseline: int | None = None
        self._events = events
        self.calls: list[str] = []
        self.quota_calls = 0

    def quota_remaining(self) -> int:
        self.quota_calls += 1
        self.requests_used = "234"
        self._events_requests_used_baseline = 234
        return 266

    def events(self, sport: str, commence_from: datetime, commence_to: datetime) -> list[Any]:
        assert self._events_requests_used_baseline is not None  # the guard is armed
        self.calls.append(sport)
        self.requests_used = "234"
        return self._events


def test_free_refresh_spends_no_credits_and_updates_only_linked_events(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    now = SAT - timedelta(days=1)
    _snapshot(ledger, EVENT, SAT.replace(hour=20, minute=30), now - timedelta(days=2), "oid")
    ledger.record_fill(
        "sportsbook_v3", f"{EVENT}-ISU", OutcomeSide.yes, Decimal("0.5"), Decimal(1),
        Decimal(0), Decimal("0.6"), rules_version="v3", event_ticker=EVENT,
        commence_time=SAT.replace(hour=20, minute=30),
    )  # fmt: skip
    odds = FreeEvents(
        [
            {"id": "oid", "commence_time": "2026-11-14T17:00:00Z"},
            {"id": "never-linked", "commence_time": "2026-11-14T19:00:00Z"},
        ]
    )
    seen = kickoffs.refresh_free(ledger.conn, odds, now)  # type: ignore[arg-type]
    assert seen == {"americanfootball_ncaaf": 1}
    assert odds.requests_made == 0 and odds.requests_used == "234" and odds.events_free
    assert kickoffs.resolve(ledger.conn, EVENT) == (SAT.replace(hour=17), "odds_events")
    assert ledger.conn.execute("SELECT COUNT(*) FROM event_kickoffs").fetchone()[0] == 1
    # At most once every 3 hours per sport.
    assert kickoffs.refresh_free(ledger.conn, odds, now + timedelta(hours=2)) == {}  # type: ignore[arg-type]
    assert kickoffs.refresh_free(ledger.conn, odds, now + timedelta(hours=3)) != {}  # type: ignore[arg-type]
    assert odds.calls == ["americanfootball_ncaaf"] * 2


def test_free_refresh_stops_when_events_consumes_quota(tmp_path: Path) -> None:
    from prediction_bot.odds import OddsClient

    ledger = _ledger(tmp_path)
    now = SAT - timedelta(days=1)
    _snapshot(ledger, EVENT, SAT.replace(hour=20, minute=30), now - timedelta(days=2), "oid")
    ledger.record_fill(
        "sportsbook_v3", f"{EVENT}-ISU", OutcomeSide.yes, Decimal("0.5"), Decimal(1),
        Decimal(0), Decimal("0.6"), rules_version="v3", event_ticker=EVENT,
        commence_time=SAT.replace(hour=20, minute=30),
    )  # fmt: skip

    class Resp:
        def __init__(self, used: str) -> None:
            self.headers = {"x-requests-used": used, "x-requests-remaining": "200"}

        def raise_for_status(self) -> None:
            pass

        def json(self) -> list[Any]:
            return [{"id": "oid", "commence_time": "2026-11-14T17:00:00Z"}]

    class Http:
        def get(self, url: str, params: Any = None) -> Resp:
            return Resp("100" if url.endswith("/sports") else "101")

    client = OddsClient("key")
    client._client = Http()  # type: ignore[assignment]
    kickoffs.refresh_free(ledger.conn, client, now)
    assert client.events_free is False  # the existing guard tripped and stays off


# --- plan re-timing ---------------------------------------------------------------------


def test_planned_rows_follow_a_kickoff_moved_3_5_hours_earlier(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    feed = "americanfootball_nfl"
    now = SAT.replace(hour=1)
    _eligible(ledger, [feed], now - timedelta(days=1))
    moved, passed = "KXNFLGAME-26NOV14MOVED", "KXNFLGAME-26NOV14PASSED"
    first = SAT.replace(hour=20, minute=30)
    for event in (moved, passed):
        _game(ledger, event, first + timedelta(hours=3), now - timedelta(hours=1))
        _snapshot(ledger, event, first, now - timedelta(hours=2), event_id=event)
    plan_pulls(ledger, plan_kickoffs(ledger.conn, eligible_feeds(ledger.conn), now), now)
    close = ledger.conn.execute(
        "SELECT target_at FROM pull_plan WHERE event_ticker=? AND target='T-10m'", (moved,)
    ).fetchone()[0]
    assert close == (first - timedelta(minutes=10)).isoformat()

    # Hours later the books move MOVED 3.5h earlier and PASSED to a time already gone.
    later = SAT.replace(hour=14)
    earlier = first - timedelta(hours=3, minutes=30)  # 17:00
    _snapshot(ledger, moved, earlier, later - timedelta(minutes=5), event_id=moved)
    _snapshot(ledger, passed, SAT.replace(hour=13), SAT.replace(hour=12), event_id=passed)
    plan_pulls(ledger, plan_kickoffs(ledger.conn, eligible_feeds(ledger.conn), later), later)
    rows = {
        (str(e), str(t)): (str(k), str(at), str(s), n)
        for e, t, k, at, s, n in ledger.conn.execute(
            "SELECT event_ticker,target,kickoff,target_at,status,note FROM pull_plan"
        )
    }
    assert rows[(moved, "T-10m")][:3] == (
        earlier.isoformat(),
        (earlier - timedelta(minutes=10)).isoformat(),
        "planned",
    )
    assert rows[(moved, "T-90m")][1] == (earlier - timedelta(minutes=90)).isoformat()
    for target in ("T-10m", "T-90m"):
        assert rows[(passed, target)][2:] == ("superseded", "kickoff_passed")
    # The superseded rows can never fund a pull, at any tick.
    for minutes in range(0, 200, 10):
        pick = select_plan_pull(ledger, SETTINGS, later + timedelta(minutes=minutes))
        if pick is not None:
            ids = set(pick.row_ids)
            passed_ids = {
                int(i)
                for (i,) in ledger.conn.execute(
                    "SELECT id FROM pull_plan WHERE event_ticker=?", (passed,)
                )
            }
            assert not ids & passed_ids
            mark_pulled(ledger, pick.feed, later + timedelta(minutes=minutes), 1.0)
    assert {
        str(s)
        for (s,) in ledger.conn.execute(
            "SELECT status FROM pull_plan WHERE event_ticker=? AND target IN ('T-10m','T-90m')",
            (passed,),
        )
    } == {"superseded"}


# --- re-anchoring CLV rows --------------------------------------------------------------


def _candles(at_t5: datetime, bid: str = "0.60", ask: str = "0.62") -> list[dict[str, Any]]:
    def candle(at: datetime, b: str, a: str) -> dict[str, Any]:
        return {
            "end_period_ts": int(at.timestamp()),
            "yes_bid": {"close_dollars": b},
            "yes_ask": {"close_dollars": a},
        }

    return [candle(at_t5 - timedelta(minutes=55), "0.40", "0.42"), candle(at_t5, bid, ask)]


def _shadow(ledger: Ledger, ticker: str, commence: datetime, created: datetime) -> int:
    cursor = ledger.conn.execute(
        "INSERT INTO shadow_decisions (created_at,rules_version,window,strategy,ticker,side,"
        "would_trade,reason,fair,price,size,commence_time) "
        "VALUES (?,'v3','24-72h','sportsbook_v3',?,'yes',0,'net_edge','0.55','0.50','1',?)",
        (created.isoformat(), ticker, commence.isoformat()),
    )
    ledger.conn.commit()
    return int(cursor.lastrowid or 0)


def test_pending_row_is_reanchored_then_scored_at_the_new_t5(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ticker = f"{EVENT}-ISU"
    stale, real = SAT.replace(hour=20, minute=30), SAT.replace(hour=17)
    decided = SAT - timedelta(days=2)
    row = _shadow(ledger, ticker, stale, decided)
    _snapshot(ledger, EVENT, real, decided + timedelta(hours=1))
    refresh_clv_statuses(ledger, SETTINGS)

    counts = reanchor_kickoffs(ledger, SETTINGS)
    assert counts["reanchored"] == 1
    commence, decision, status = ledger.conn.execute(
        "SELECT commence_time,commence_time_decision,clv_status FROM shadow_decisions WHERE id=?",
        (row,),
    ).fetchone()
    assert (commence, decision, status) == (real.isoformat(), stale.isoformat(), None)
    assert reanchor_kickoffs(ledger, SETTINGS)["reanchored"] == 0  # idempotent

    class Candles:
        def __init__(self) -> None:
            self.windows: list[tuple[int, int]] = []

        def get_candlesticks(self, series, market, start, end, period_interval=1):  # type: ignore[no-untyped-def]
            self.windows.append((start, end))
            return _candles(real - timedelta(minutes=5))

    client = Candles()
    record_clv(ledger, client, now=real + timedelta(minutes=30))  # type: ignore[arg-type]
    assert client.windows[0][1] == int(real.timestamp())  # fetched up to the real kickoff
    clv = ledger.conn.execute(
        "SELECT clv_kalshi FROM shadow_decisions WHERE id=?", (row,)
    ).fetchone()[0]
    assert Decimal(clv) == Decimal("11.00")  # mid 0.61 at 16:55 vs 0.50
    refresh_clv_statuses(ledger, SETTINGS)
    assert (
        ledger.conn.execute(
            "SELECT clv_status FROM shadow_decisions WHERE id=?", (row,)
        ).fetchone()[0]
        == "ok"
    )


def test_scored_rows_31_minutes_off_are_flagged_and_4_minutes_left_alone(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    real = SAT.replace(hour=17)
    decided = SAT - timedelta(days=2)
    far = _shadow(ledger, f"{EVENT}-ISU", real + timedelta(minutes=31), decided)
    near = _shadow(ledger, f"{EVENT}-CIN", real + timedelta(minutes=4), decided)
    unscored_near = _shadow(ledger, f"{EVENT}-TIE", real - timedelta(minutes=4), decided)
    ledger.conn.execute(
        "UPDATE shadow_decisions SET clv_kalshi='3.0',clv_status='ok' WHERE id IN (?,?)",
        (far, near),
    )
    ledger.conn.commit()
    _snapshot(ledger, EVENT, real, decided + timedelta(hours=1))
    counts = reanchor_kickoffs(ledger, SETTINGS)
    assert counts == {"reanchored": 0, "mismatch": 1, "expiry_moved": 0}
    rows = {
        int(i): (str(c), s, k)
        for i, c, s, k in ledger.conn.execute(
            "SELECT id,commence_time,clv_status,clv_kalshi FROM shadow_decisions"
        )
    }
    assert rows[far] == ((real + timedelta(minutes=31)).isoformat(), "kickoff_mismatch", "3.0")
    assert rows[near][1] == "ok"
    assert rows[unscored_near][0] == (real - timedelta(minutes=4)).isoformat()
    # The flag survives the status refresh, including rows with a backfilled kickoff.
    ledger.conn.execute(
        "UPDATE shadow_decisions SET commence_time_backfill=commence_time WHERE id=?", (far,)
    )
    refresh_clv_statuses(ledger, SETTINGS)
    assert (
        ledger.conn.execute(
            "SELECT clv_status FROM shadow_decisions WHERE id=?", (far,)
        ).fetchone()[0]
        == "kickoff_mismatch"
    )


def test_open_maker_shadow_cut_at_kickoff_moves_its_expiry(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    stale, real = SAT.replace(hour=20, minute=30), SAT.replace(hour=17)
    created = SAT.replace(hour=15)
    buffer = timedelta(minutes=SETTINGS.kickoff_buffer_minutes.get(
        "americanfootball_ncaaf", SETTINGS.kickoff_buffer_default_minutes
    ))  # fmt: skip
    ledger.record_maker_shadows(
        [
            MakerShadowCandidate(
                created_at=created,
                ticker=f"{EVENT}-ISU",
                side=OutcomeSide.yes,
                event_ticker=EVENT,
                position_key=f"{EVENT}:isu:shadow",
                price=Decimal("0.50"),
                size=Decimal(1),
                fair=Decimal("0.60"),
                maker_fee=Decimal("0.01"),
                expires_at=stale - buffer,
                commence_time=stale,
            )
        ]
    )
    _snapshot(ledger, EVENT, real, created - timedelta(hours=1))
    counts = reanchor_kickoffs(ledger, SETTINGS)
    assert counts["expiry_moved"] == 1
    commence, expires = ledger.conn.execute(
        "SELECT commence_time,expires_at FROM maker_shadow"
    ).fetchone()
    assert kickoffs._parse(commence) == real
    assert kickoffs._parse(expires) == real - buffer


def test_missing_t5_after_three_attempts_stops_the_retry(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ticker = "KXMLSGAME-26NOV01VANSEA-SEA"
    commence = datetime(2026, 11, 1, 21, 30, tzinfo=UTC)
    ledger.record_fill(
        "sportsbook_v3", ticker, OutcomeSide.yes, Decimal("0.5"), Decimal(1), Decimal(0),
        Decimal("0.6"), rules_version="v3", event_ticker="KXMLSGAME-26NOV01VANSEA",
        commence_time=commence,
    )  # fmt: skip

    class InPlay:
        calls = 0

        def get_candlesticks(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            # T-60 and T-15 usable; the T-5 candle is crossed (the game is live).
            return _candles(commence - timedelta(minutes=5), bid="0.70", ask="0.60")

    client = InPlay()
    now = commence + timedelta(days=1)
    for _ in range(3):
        record_clv(ledger, client, now=now)  # type: ignore[arg-type]
    assert client.calls == 3
    assert (
        ledger.conn.execute(
            "SELECT status FROM clv_fetch_attempts WHERE ticker=?", (ticker,)
        ).fetchone()[0]
        == "unavailable"
    )
    record_clv(ledger, client, now=now)  # type: ignore[arg-type]
    assert client.calls == 3


# --- migration and audit -----------------------------------------------------------------


def test_migration_backfills_from_snapshots_and_flags_vansea_once(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    ledger = Ledger(path, Decimal("1000"))
    ledger.conn.execute("DELETE FROM kickoff_state")
    ledger.conn.execute("DELETE FROM event_kickoffs")
    _snapshot(ledger, EVENT, SAT.replace(hour=20, minute=30), SAT - timedelta(days=3))
    _snapshot(ledger, EVENT, SAT.replace(hour=17), SAT - timedelta(days=1))
    _snapshot(ledger, EVENT, SAT.replace(hour=17, minute=6), SAT.replace(hour=18))  # live
    vansea = "KXMLSGAME-26NOV01VANSEA-VAN"
    stale = datetime(2026, 11, 1, 21, 30, tzinfo=UTC)
    flagged = _shadow(ledger, vansea, stale, stale - timedelta(days=1))
    fixed = _shadow(ledger, vansea, stale - timedelta(hours=1), stale - timedelta(days=1))
    ledger.conn.execute("UPDATE shadow_decisions SET clv_kalshi='1',clv_status='ok'")
    ledger.conn.commit()
    ledger.conn.close()

    ledger = Ledger(path, Decimal("1000"))
    assert kickoffs.resolve(ledger.conn, EVENT) == (SAT.replace(hour=17), "odds")
    statuses = dict(ledger.conn.execute("SELECT id,clv_status FROM shadow_decisions").fetchall())
    assert statuses == {flagged: "kickoff_mismatch", fixed: "ok"}
    # Runs once: a later reset of the flag is not redone on the next open.
    ledger.conn.execute("UPDATE shadow_decisions SET clv_status='ok'")
    ledger.conn.commit()
    ledger.conn.close()
    ledger = Ledger(path, Decimal("1000"))
    assert (
        ledger.conn.execute(
            "SELECT COUNT(*) FROM shadow_decisions WHERE clv_status='kickoff_mismatch'"
        ).fetchone()[0]
        == 0
    )


def test_audit_kickoffs_lists_rows_off_by_more_than_5_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from prediction_bot.cli import app

    path = tmp_path / "audit.sqlite"
    ledger = Ledger(path, Decimal("1000"))
    real = SAT.replace(hour=17)
    _shadow(ledger, f"{EVENT}-ISU", SAT.replace(hour=20, minute=30), SAT - timedelta(days=2))
    _shadow(ledger, f"{EVENT}-CIN", real + timedelta(minutes=3), SAT - timedelta(days=2))
    _snapshot(ledger, EVENT, real, SAT - timedelta(days=1))
    kickoffs.sync_snapshots(ledger.conn)
    ledger.conn.close()
    monkeypatch.setenv("PBOT_PAPER_DB_PATH", str(path))
    result = CliRunner().invoke(app, ["audit-kickoffs", "--day", "2026-11-14"])
    assert result.exit_code == 0, result.output
    assert f"{EVENT}-ISU | 2026-11-14 20:30Z | 2026-11-14 17:00Z | -210 |" in result.output
    assert f"{EVENT}-CIN" not in result.output
    assert "1 rows more than 5 minutes off (1 not yet scored)." in result.output


# --- replay on a ledger copy --------------------------------------------------------------

SIX = ("CINISU", "KSUTCU", "KUWVU", "TEXLSU", "MISSOKLA", "BAYBYU")


@pytest.mark.skipif(not os.environ.get("PBOT_TEST_LEDGER"), reason="PBOT_TEST_LEDGER unset")
def test_replay_six_nov14_events_have_no_pending_rows_off(tmp_path: Path) -> None:
    copy = tmp_path / "replay.sqlite"
    shutil.copy(os.environ["PBOT_TEST_LEDGER"], copy)
    ledger = Ledger(copy, Decimal("1000"))  # runs the migration
    reanchor_kickoffs(ledger, SETTINGS)  # as the first paper cycle does
    rows = [
        r
        for r in kickoffs.kickoff_rows(ledger.conn)
        if not r["scored"]
        and r["table"] != "pull_plan"
        and any(r["event_ticker"] == f"KXNCAAFGAME-26NOV14{e}" for e in SIX)
    ]
    assert rows == []
    moved = ledger.conn.execute(
        "SELECT COUNT(*) FROM shadow_decisions WHERE commence_time_decision IS NOT NULL "
        "AND ticker LIKE 'KXNCAAFGAME-26NOV14%'"
    ).fetchone()[0]
    assert moved > 0
