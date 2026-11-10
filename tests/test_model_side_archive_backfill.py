"""One-time model_side back-fill from archived market snapshots."""

from __future__ import annotations

import gzip
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from prediction_bot import cli
from prediction_bot import ledger_archive as la
from prediction_bot.config import Settings
from prediction_bot.paper.ledger import Ledger
from tests.test_ledger_archive import FakeGh, _no_sleep

AT = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
OLD_TICKER = "KXNFLGAME-26NOV01ARIDAL-ARI"  # snapshots only in the archive
HOT_TICKER = "KXNFLGAME-26NOV01ARIDAL-DAL"  # no snapshot anywhere


def _shadow(ledger: Ledger, ticker: str, side: str, fair: str) -> None:
    ledger.conn.execute(
        "INSERT INTO shadow_decisions (created_at,rules_version,window,strategy,ticker,side,"
        "would_trade,reason,fair,price) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (AT.isoformat(), "v3", "72h+", "sportsbook_v3", ticker, side, 0, "net_edge", fair, "0.52"),
    )
    ledger.conn.commit()


def _archive_asset(tmp: Path, rows: list[tuple[datetime, str, str, str]]) -> bytes:
    """An archive database with a market_snapshots table, gzipped as on the release."""
    path = tmp / "archive.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE market_snapshots (id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, "
        "ticker TEXT NOT NULL, event_ticker TEXT NOT NULL, series TEXT NOT NULL, yes_bid TEXT, "
        "yes_ask TEXT, yes_bid_size TEXT NOT NULL, yes_ask_size TEXT NOT NULL, last_price TEXT, "
        "open_interest TEXT NOT NULL, volume TEXT NOT NULL, expected_expiration TEXT NOT NULL)"
    )
    for at, ticker, bid, ask in rows:
        conn.execute(
            "INSERT INTO market_snapshots (fetched_at,ticker,event_ticker,series,yes_bid,"
            "yes_ask,yes_bid_size,yes_ask_size,open_interest,volume,expected_expiration) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (at.isoformat(), ticker, ticker.rsplit("-", 1)[0], "KXNFLGAME", bid, ask, "1", "1",
             "1", "1", (AT + timedelta(days=5)).isoformat()),
        )  # fmt: skip
    conn.commit()
    conn.close()
    data = gzip.compress(path.read_bytes())
    path.unlink()
    return data


def _gh_with(tmp: Path, *assets: list[tuple[datetime, str, str, str]]) -> FakeGh:
    gh = FakeGh()
    gh.releases[la.ARCHIVE_TAG] = {
        f"archive-2026-11-{10 + i:02d}.sqlite.gz": _archive_asset(tmp, rows)
        for i, rows in enumerate(assets)
    }
    return gh


def test_rows_whose_snapshots_were_archived_get_their_mid_once(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "hot.sqlite", Decimal("100"))
    _shadow(ledger, OLD_TICKER, "yes", "0.52")
    _shadow(ledger, OLD_TICKER, "no", "0.52")
    _shadow(ledger, HOT_TICKER, "yes", "0.52")
    gh = _gh_with(
        tmp_path,
        [(AT - timedelta(minutes=4), OLD_TICKER, "0.44", "0.46")],  # mid .45, farther
        [(AT + timedelta(minutes=1), OLD_TICKER, "0.48", "0.50")],  # mid .49, nearest
    )
    result = la.backfill_model_side_from_archives(gh, ledger, tmp_path, sleep=_no_sleep)
    assert result == (2, ["archive-2026-11-10.sqlite.gz", "archive-2026-11-11.sqlite.gz"])
    rows = ledger.conn.execute(
        "SELECT ticker,side,mid,model_side FROM shadow_decisions ORDER BY id"
    ).fetchall()
    # The nearest snapshot across both archive files wins; NO's mid is 1 - YES mid.
    assert rows == [
        (OLD_TICKER, "yes", "0.49", 1),
        (OLD_TICKER, "no", "0.51", 1),
        (HOT_TICKER, "yes", None, None),
    ]
    # Nothing was merged into the hot ledger.
    assert ledger.conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone() == (0,)
    assert not list(tmp_path.glob(".model-side-backfill*"))
    # Marked done: the next run downloads nothing.
    calls = len(gh.calls)
    assert la.backfill_model_side_from_archives(gh, ledger, tmp_path, sleep=_no_sleep) is None
    assert len(gh.calls) == calls
    assert ledger.conn.execute(
        "SELECT detail FROM ledger_migrations WHERE key=?", (Ledger.ARCHIVE_BACKFILL_KEY,)
    ).fetchone() == ("rows=2 assets=2",)


def test_an_unreadable_release_leaves_it_to_retry(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "hot.sqlite", Decimal("100"))
    _shadow(ledger, OLD_TICKER, "yes", "0.52")
    with pytest.raises(la.ArchiveError):
        la.backfill_model_side_from_archives(FakeGh(), ledger, tmp_path, sleep=_no_sleep)
    assert not ledger.archive_backfill_done()
    gh = _gh_with(tmp_path, [(AT, OLD_TICKER, "0.48", "0.50")])
    gh.fail["download"] = 99
    with pytest.raises(Exception):  # noqa: B017 - the store's own download error
        la.backfill_model_side_from_archives(gh, ledger, tmp_path, sleep=_no_sleep)
    assert not ledger.archive_backfill_done()
    assert ledger.conn.execute("SELECT mid FROM shadow_decisions").fetchone() == (None,)


def test_the_cli_only_warns_and_then_reports_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "paper.sqlite"
    ledger = Ledger(db, Decimal("100"))
    _shadow(ledger, OLD_TICKER, "yes", "0.52")
    ledger.close()
    monkeypatch.setattr(cli, "_settings", lambda: Settings(_env_file=None, paper_db_path=db))  # type: ignore[call-arg]
    monkeypatch.setattr(cli.ledger_store, "SubprocessRunner", lambda: FakeGh())
    cli.ledger_backfill_model_side(db=db)
    assert "will retry next run" in capsys.readouterr().err
    gh = _gh_with(tmp_path, [(AT, OLD_TICKER, "0.48", "0.50")])
    monkeypatch.setattr(cli.ledger_store, "SubprocessRunner", lambda: gh)
    monkeypatch.setattr(la.time, "sleep", _no_sleep)
    cli.ledger_backfill_model_side(db=db)
    assert "1 rows set from 1 archive files" in capsys.readouterr().out
    cli.ledger_backfill_model_side(db=db)
    assert "already done" in capsys.readouterr().out
