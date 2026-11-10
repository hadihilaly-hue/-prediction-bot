"""ledger-archive: select, upload, then delete; resume after a lost save; full history."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from prediction_bot import ledger_archive as la
from prediction_bot import ledger_store as ls
from prediction_bot.models import Block, QuoteSnapshot
from prediction_bot.paper.ledger import Ledger

UTC = timezone.utc
NOW = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=10)
TABLES = list(la.TABLES)


def _no_sleep(_s: float) -> None:
    return None


class FakeGh:
    """`gh release` for any tag, served from memory; `fail` injects errors by verb."""

    def __init__(self) -> None:
        self.releases: dict[str, dict[str, bytes]] = {}
        self.fail: dict[str, int] = {}
        self.calls: list[list[str]] = []

    def run(self, argv: Sequence[str], cwd: Path | None = None) -> ls.CmdResult:
        argv = list(argv)
        self.calls.append(argv)
        assert argv[:2] == ["gh", "release"], argv
        verb, tag = argv[2], argv[3]
        if self.fail.get(verb, 0) > 0:
            self.fail[verb] -= 1
            return ls.CmdResult(1, b"", b"injected")
        if verb == "create":
            self.releases.setdefault(tag, {})
            return ls.CmdResult(0)
        assets = self.releases.get(tag)
        if assets is None:
            return ls.CmdResult(1, b"", b"release not found")
        if verb == "view":
            listed = [{"name": n, "size": len(b), "state": "uploaded"} for n, b in assets.items()]
            return ls.CmdResult(0, json.dumps({"assets": listed, "tagName": tag}).encode())
        if verb == "upload":
            path = Path(argv[4])
            if path.name in assets:
                return ls.CmdResult(1, b"", b"already exists")
            assets[path.name] = path.read_bytes()
            return ls.CmdResult(0)
        if verb == "download":
            name = argv[argv.index("--pattern") + 1]
            into = Path(argv[argv.index("--dir") + 1])
            (into / name).write_bytes(assets[name])
            return ls.CmdResult(0)
        raise AssertionError(argv)


def _market(ledger: Ledger, ticker: str, at: datetime, expiration: datetime) -> None:
    ledger.conn.execute(
        "INSERT INTO market_snapshots (fetched_at,ticker,event_ticker,series,yes_bid,yes_ask,"
        "yes_bid_size,yes_ask_size,open_interest,volume,expected_expiration) "
        "VALUES (?,?,?,?,'0.4','0.5','1','1','1','1',?)",
        (at.isoformat(), ticker, ticker.rsplit("-", 1)[0], "KXNFLGAME", expiration.isoformat()),
    )


def _quote(ledger: Ledger, ticker: str, at: datetime, bid: str) -> None:
    ledger.record_quote_snapshots(
        [
            QuoteSnapshot(
                ticker,
                ticker.rsplit("-", 1)[0],
                Decimal(bid),
                Decimal("0.6"),
                None,
                Decimal(1),
                Decimal(1),
                created_at=at,
            )
        ]
    )


def _block(ledger: Ledger, ticker: str, at: datetime, reason: str = "net_edge") -> None:
    ledger.record_blocks([Block(reason, "sportsbook_v3", ticker, "E", created_at=at)])


def _orderbook(ledger: Ledger, ticker: str, at: datetime) -> None:
    ledger.record_orderbook_snapshot(ticker, {"yes": [(Decimal("0.4"), Decimal(5))]}, at)


CLOSED = "KXNFLGAME-26NOV09-CLOSED"
OPEN = "KXNFLGAME-26NOV30-OPEN"
HELD = "KXNFLGAME-26NOV09-HELD"  # closed but an unsettled fill holds it
SCANNER = "KXSCAN-26NOV01-IDLE"  # no market_snapshots, idle for 10 days


def _build(path: Path) -> Ledger:
    ledger = Ledger(path, Decimal(1000))
    for i in range(6):  # 10 to 9.5 days ago, every 2h
        at = OLD + timedelta(hours=2 * i)
        _market(ledger, CLOSED, at, NOW - timedelta(days=9))
        _market(ledger, HELD, at, NOW - timedelta(days=9))
        _market(ledger, OPEN, at, NOW + timedelta(days=5))
        for ticker in (CLOSED, OPEN, HELD, SCANNER):
            _quote(ledger, ticker, at, f"0.4{i % 3}")
            _block(ledger, ticker, at, "net_edge" if i % 2 else "mapping_failed")
            _orderbook(ledger, ticker, at)
    _block(ledger, "", OLD, "dq_stop")  # tickerless, by age
    # Recent activity: kept everywhere.
    recent = NOW - timedelta(hours=3)
    _market(ledger, OPEN, recent, NOW + timedelta(days=5))
    _quote(ledger, OPEN, recent, "0.45")
    _block(ledger, OPEN, recent)
    _orderbook(ledger, OPEN, recent)
    _orderbook(ledger, OPEN, NOW - timedelta(days=2))  # within 3 days: kept
    ledger.conn.execute(
        "INSERT INTO fills (created_at,strategy,ticker,side,price,count,fee,fair_prob) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (OLD.isoformat(), "s", HELD, "yes", "0.4", "1", "0", "0.5"),
    )
    # A filled maker shadow on the closed ticker; its CLV reads the quote at expiry.
    ledger.conn.execute(
        "INSERT INTO maker_shadow (created_at,ticker,side,position_key,price,size,fair,"
        "maker_fee,status,status_at,event_ticker,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            OLD.isoformat(),
            CLOSED,
            "yes",
            "k",
            "0.40",
            "1",
            "0.5",
            "0",
            "filled",
            OLD.isoformat(),
            "E",
            (OLD + timedelta(hours=5)).isoformat(),
        ),
    )
    ledger.conn.commit()
    return ledger


def _rows(conn: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def _block_totals(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT reason,SUM(n) FROM (SELECT reason,seen_count n FROM blocks "
        "UNION ALL SELECT reason,n FROM blocks_rollup) GROUP BY reason"
    )
    return {str(r): int(n) for r, n in rows}


# --------------------------------------------------------------------- selection


def test_selection_keeps_open_held_recent_and_newest_rows(tmp_path: Path) -> None:
    ledger = _build(tmp_path / "hot.sqlite")
    ids = la.select_archivable(ledger.conn, NOW)

    def tickers(table: str) -> set[str]:
        if not ids[table]:
            return set()
        marks = ",".join("?" * len(ids[table]))
        return {
            str(r[0])
            for r in ledger.conn.execute(
                f"SELECT DISTINCT ticker FROM {table} WHERE id IN ({marks})", ids[table]
            )
        }

    assert tickers("quote_snapshots") == {CLOSED, SCANNER}
    assert tickers("market_snapshots") == {CLOSED}
    assert tickers("blocks") == {CLOSED, SCANNER, ""}
    # Order books go by age alone: every ticker's 10-day-old books, none newer.
    assert tickers("orderbook_snapshots") == {CLOSED, OPEN, HELD, SCANNER}
    two_days = (NOW - timedelta(days=2)).isoformat()
    assert not ledger.conn.execute(
        "SELECT 1 FROM orderbook_snapshots WHERE fetched_at>=? AND id IN "
        f"({','.join(map(str, ids['orderbook_snapshots']))})",
        (two_days,),
    ).fetchone()
    for table in TABLES:
        newest = ledger.conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]
        assert newest not in ids[table]


def test_a_ticker_only_closes_a_day_after_expiration(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "hot.sqlite", Decimal(1000))
    _market(ledger, CLOSED, OLD, NOW - timedelta(hours=12))
    _market(ledger, OPEN, NOW, NOW)
    assert CLOSED not in la.closed_tickers(ledger.conn, NOW, 7)
    assert CLOSED in la.closed_tickers(ledger.conn, NOW + timedelta(hours=13), 7)


# ----------------------------------------------------------------------- the run


def _run(ledger: Ledger, gh: FakeGh, tmp_path: Path, now: datetime = NOW) -> la.ArchiveResult:
    return la.run_archive(gh, ledger.conn, tmp_path, now=now, sleep=_no_sleep)


def test_archive_moves_rows_out_and_keeps_every_reader_whole(tmp_path: Path) -> None:
    ledger = _build(tmp_path / "hot.sqlite")
    before = {t: _rows(ledger.conn, t) for t in TABLES}
    totals = _block_totals(ledger.conn)
    maker_before = ledger.maker_shadow_metrics()
    selected = la.select_archivable(ledger.conn, NOW)
    gh = FakeGh()

    result = _run(ledger, gh, tmp_path)
    assert result.status == "archived"
    assert result.asset == "archive-2026-11-20.sqlite.gz"
    assert result.rows == {t: len(selected[t]) for t in TABLES}
    assert result.asset in gh.releases[la.ARCHIVE_TAG]
    create = next(c for c in gh.calls if c[2] == "create")
    assert create[3] == la.ARCHIVE_TAG and "--prerelease" in create and "--latest=false" in create
    # Deleted exactly the archived rows.
    for table in TABLES:
        assert len(_rows(ledger.conn, table)) == len(before[table]) - len(selected[table])
    # All-time block counts and the maker shadow's CLV survive the move.
    assert _block_totals(ledger.conn) == totals
    assert ledger.maker_shadow_metrics() == maker_before
    frozen = ledger.conn.execute(
        "SELECT expiry_yes_bid,expiry_yes_ask,expiry_quote_at FROM maker_shadow"
    ).fetchone()
    assert frozen[:2] == ("0.42", "0.6") and frozen[2] is not None
    log = ledger.conn.execute("SELECT asset,rows_json FROM archive_log").fetchone()
    assert log[0] == result.asset and json.loads(log[1]) == result.rows
    # No temp files left behind.
    assert not list(tmp_path.glob(".archive-*"))

    # The archive plus the hot ledger is the original history, row for row.
    full = tmp_path / "full.sqlite"
    shutil.copyfile(tmp_path / "hot.sqlite", full)
    conn = sqlite3.connect(full)
    merged = la.fetch_full_history(gh, conn, tmp_path, sleep=_no_sleep)
    assert merged == {result.asset: result.rows}
    for table in TABLES:
        cols = [c for c in la._columns(conn, table)]
        restored = conn.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY id").fetchall()
        assert restored == [row[: len(cols)] for row in before[table]], table


def test_running_again_the_same_day_does_nothing(tmp_path: Path) -> None:
    ledger = _build(tmp_path / "hot.sqlite")
    gh = FakeGh()
    _run(ledger, gh, tmp_path)
    calls = len(gh.calls)
    assert _run(ledger, gh, tmp_path, NOW + timedelta(hours=3)).status == "already_done"
    assert len(gh.calls) == calls
    # The next UTC day archives again (nothing new is old enough here).
    assert _run(ledger, gh, tmp_path, NOW + timedelta(days=1)).status == "nothing"


def test_an_upload_failure_deletes_nothing(tmp_path: Path) -> None:
    ledger = _build(tmp_path / "hot.sqlite")
    before = {t: _rows(ledger.conn, t) for t in TABLES}
    gh = FakeGh()
    gh.fail["upload"] = 99
    with pytest.raises(ls.LedgerStoreError, match="not confirmed"):
        _run(ledger, gh, tmp_path)
    assert {t: _rows(ledger.conn, t) for t in TABLES} == before
    assert not ledger.conn.execute("SELECT 1 FROM archive_log").fetchone()
    assert not ledger.conn.execute("SELECT 1 FROM blocks_rollup").fetchone()


def test_the_cli_only_warns_when_the_archive_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import prediction_bot.cli as cli

    db = tmp_path / "hot.sqlite"
    _build(db).conn.close()
    gh = FakeGh()
    gh.fail["upload"] = 99
    monkeypatch.setattr(ls, "SubprocessRunner", lambda: gh)
    monkeypatch.setattr(la.time, "sleep", _no_sleep)
    monkeypatch.setattr(ls.time, "sleep", _no_sleep)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> _Clock:  # type: ignore[override]
            return cls.fromtimestamp(NOW.timestamp(), UTC)

    monkeypatch.setattr(la, "datetime", _Clock)
    cli.ledger_archive_command(db=db, keep_days=7, orderbook_days=3)  # no exception
    assert "ledger-archive skipped" in capsys.readouterr().err


def test_a_lost_save_resumes_from_the_uploaded_asset(tmp_path: Path) -> None:
    """Archive ran and uploaded, but the save never landed: the next run restores the
    pre-archive ledger, finds today's asset, and deletes exactly the rows inside it."""
    _build(tmp_path / "hot.sqlite").conn.close()
    shutil.copyfile(tmp_path / "hot.sqlite", tmp_path / "restored.sqlite")
    gh = FakeGh()
    first = Ledger(tmp_path / "hot.sqlite", Decimal(1000))
    done = _run(first, gh, tmp_path)
    uploads = sum(c[2] == "upload" for c in gh.calls)

    again = Ledger(tmp_path / "restored.sqlite", Decimal(1000))
    _quote(again, CLOSED, OLD + timedelta(hours=1), "0.99")  # a row the asset lacks
    resumed = _run(again, gh, tmp_path, NOW + timedelta(hours=1))
    assert resumed.status == "resumed" and resumed.rows == done.rows
    assert sum(c[2] == "upload" for c in gh.calls) == uploads
    for table in TABLES:
        if table != "quote_snapshots":
            assert _rows(again.conn, table) == _rows(first.conn, table)
    left = again.conn.execute(
        "SELECT yes_bid FROM quote_snapshots WHERE ticker=?", (CLOSED,)
    ).fetchall()
    assert left == [("0.99",)]  # only the row the archive does not hold stays


def test_merge_refuses_a_reused_id(tmp_path: Path) -> None:
    ledger = _build(tmp_path / "hot.sqlite")
    gh = FakeGh()
    result = _run(ledger, gh, tmp_path)
    archived_id = ledger.conn.execute("SELECT MIN(id) FROM blocks").fetchone()[0] - 1
    ledger.conn.execute(
        "INSERT INTO blocks (id,created_at,reason) VALUES (?,?,?)",
        (archived_id, NOW.isoformat(), "different"),
    )
    ledger.conn.commit()
    raw = tmp_path / "a.sqlite.gz"
    raw.write_bytes(gh.releases[la.ARCHIVE_TAG][result.asset])
    ls.gunzip_file(raw, tmp_path / "a.sqlite")
    with pytest.raises(la.ArchiveError, match="ids already hold different rows"):
        la.merge_archive(ledger.conn, tmp_path / "a.sqlite")


def test_nothing_old_enough_uploads_nothing(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "hot.sqlite", Decimal(1000))
    _quote(ledger, OPEN, NOW, "0.4")
    gh = FakeGh()
    assert _run(ledger, gh, tmp_path).status == "nothing"
    assert not any(c[2] in {"create", "upload"} for c in gh.calls)
    assert ledger.conn.execute("SELECT asset FROM archive_log").fetchone() == ("",)


def test_archive_shrinks_the_file(tmp_path: Path) -> None:
    ledger = _build(tmp_path / "hot.sqlite")
    for i in range(400):  # bulk so VACUUM has pages to give back
        _orderbook(ledger, CLOSED, OLD + timedelta(minutes=i))
    size = (tmp_path / "hot.sqlite").stat().st_size
    _run(ledger, FakeGh(), tmp_path)
    assert (tmp_path / "hot.sqlite").stat().st_size < size
