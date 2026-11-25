"""T-10m closes are funded outside the plan allowance, down to the plan floor."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from prediction_bot.cli import _refresh_pull_plan, _release_calendar
from prediction_bot.config import Settings
from prediction_bot.paper.ledger import Ledger
from prediction_bot.pull_plan import (
    must_demand,
    plan_allowance_left,
    plan_hold,
    select_plan_pull,
)

UTC = timezone.utc
SETTINGS = Settings(_env_file=None, rules_version="v3", odds_entry_share=0.7)  # type: ignore[call-arg]
FEED = "americanfootball_nfl"
SUNDAY = datetime(2026, 11, 22, tzinfo=UTC)


def _ledger(tmp_path: Path, balance: int, plan_spent: float) -> Ledger:
    ledger = Ledger(tmp_path / "close.sqlite", Decimal("1000"))
    ledger.conn.execute(
        "INSERT INTO feed_stats (computed_at,feed,lead_p50_h,lead_p90_h,pulls_14d,"
        "useful_pulls_14d,provisional,eligible) VALUES (?,?,100,200,0,0,0,1)",
        ((SUNDAY - timedelta(days=2)).isoformat(), FEED),
    )
    for at, left in ((datetime(2026, 11, 1, 1, tzinfo=UTC), 500), (SUNDAY, balance)):
        ledger.conn.execute(
            "INSERT INTO runs (ran_at,strategies,scanned,signals,filled,settled,cash,"
            "open_cost,odds_remaining) VALUES (?,'sportsbook_v3',0,0,0,0,'1000','0',?)",
            (at.isoformat(), left),
        )
    # Plan credits already spent this month on a non-close target.
    ledger.conn.execute(
        "INSERT INTO pull_plan (event_ticker,feed,target,kickoff,target_at,window_start,"
        "window_end,priority,status,credits,planned_at,pulled_at,source) VALUES "
        "('E0',?,'K-90m',?,?,?,?,1,'pulled',?,?,?,'kickoff')",
        (
            FEED,
            *[datetime(2026, 11, 2, tzinfo=UTC).isoformat()] * 4,
            plan_spent,
            *[datetime(2026, 11, 2, tzinfo=UTC).isoformat()] * 2,
        ),
    )
    ledger.conn.commit()
    return ledger


def _game(ledger: Ledger, event: str, kickoff: datetime) -> None:
    for code in ("KC", "BUF"):
        ledger.conn.execute(
            "INSERT INTO market_snapshots (fetched_at,ticker,event_ticker,series,yes_bid,"
            "yes_ask,yes_bid_size,yes_ask_size,open_interest,volume,expected_expiration) "
            "VALUES (?,?,?,'KXNFLGAME','0.48','0.50','100','100','10','10',?)",
            ((SUNDAY - timedelta(hours=6)).isoformat(), f"{event}-{code}", event,
             (kickoff + timedelta(hours=3)).isoformat()),
        )  # fmt: skip
    ledger.conn.commit()


def _plan(ledger: Ledger, now: datetime) -> None:
    _refresh_pull_plan(ledger, now, SETTINGS, _release_calendar(SETTINGS))


def test_a_due_close_is_funded_when_the_allowance_is_spent(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, balance=200, plan_spent=150)
    kickoff = SUNDAY.replace(hour=18)
    _game(ledger, "KXNFLGAME-26NOV22KCBUF", kickoff)
    now = kickoff - timedelta(minutes=10)
    _plan(ledger, now)
    left = plan_allowance_left(ledger, SETTINGS, now)
    assert left is not None and left < 1
    pick = select_plan_pull(ledger, SETTINGS, now, calendar=_release_calendar(SETTINGS))
    assert pick is not None and pick.feed == FEED
    close = ledger.conn.execute(
        "SELECT budget_blocked_at FROM pull_plan WHERE target='T-10m'"
    ).fetchone()
    assert close == (None,)


def test_other_targets_still_need_allowance(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, balance=200, plan_spent=150)
    kickoff = SUNDAY.replace(hour=18)
    _game(ledger, "KXNFLGAME-26NOV22KCBUF", kickoff)
    cal = _release_calendar(SETTINGS)
    _plan(ledger, SUNDAY.replace(hour=6))
    # A due non-close target with no allowance left is blocked, not pulled.
    due = ledger.conn.execute(
        "SELECT id,window_start FROM pull_plan WHERE status='planned' AND target<>'T-10m' "
        "AND merged_into IS NULL ORDER BY window_start LIMIT 1"
    ).fetchone()
    assert due is not None
    at = datetime.fromisoformat(str(due[1])) + timedelta(minutes=1)
    assert select_plan_pull(ledger, SETTINGS, at, calendar=cal) is None
    stamped = ledger.conn.execute(
        "SELECT budget_blocked_at FROM pull_plan WHERE id=?", (due[0],)
    ).fetchone()[0]
    assert stamped is not None


def test_a_close_is_never_funded_below_the_plan_floor(tmp_path: Path) -> None:
    # Floor = 25 hard reserve + 50 unused move reserve.
    ledger = _ledger(tmp_path, balance=75, plan_spent=100)
    kickoff = SUNDAY.replace(hour=18)
    _game(ledger, "KXNFLGAME-26NOV22KCBUF", kickoff)
    now = kickoff - timedelta(minutes=10)
    _plan(ledger, now)
    assert select_plan_pull(ledger, SETTINGS, now, calendar=_release_calendar(SETTINGS)) is None


def test_close_credits_do_not_use_up_the_allowance(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, balance=200, plan_spent=0)
    now = SUNDAY.replace(hour=12)
    before = plan_allowance_left(ledger, SETTINGS, now)
    for target, credits in (("T-10m", 3.0), ("K-90m", 1.0)):
        ledger.conn.execute(
            "INSERT INTO pull_plan (event_ticker,feed,target,kickoff,target_at,window_start,"
            "window_end,priority,status,credits,planned_at,pulled_at) VALUES "
            "(?,?,?,?,?,?,?,1,'pulled',?,?,?)",
            (
                f"E-{target}",
                FEED,
                target,
                *[SUNDAY.replace(hour=9).isoformat()] * 4,
                credits,
                *[SUNDAY.replace(hour=9).isoformat()] * 2,
            ),
        )
    ledger.conn.commit()
    assert plan_allowance_left(ledger, SETTINGS, now) == before - 1  # type: ignore[operator]


def test_month_end_hold_covers_closes_even_with_no_allowance_left(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, balance=120, plan_spent=200)
    now = datetime(2026, 11, 27, 12, tzinfo=UTC)  # inside the month's last 7 days
    for i, hour in enumerate((18, 21)):
        _game(ledger, f"KXNFLGAME-26NOV29G{i}", datetime(2026, 11, 29, hour, tzinfo=UTC))
    ledger.conn.execute(
        "UPDATE market_snapshots SET fetched_at=?", ((now - timedelta(hours=1)).isoformat(),)
    )
    ledger.conn.commit()
    cal = _release_calendar(SETTINGS)
    assert cal is not None
    closes = must_demand(ledger, cal, now, datetime(2026, 12, 1, tzinfo=UTC), closes_only=True)
    assert closes == 2
    assert plan_hold(ledger, SETTINGS, cal, now) >= closes
