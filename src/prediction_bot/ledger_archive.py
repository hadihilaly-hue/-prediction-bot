"""Keep the hot paper ledger small: archive old rows of the four big tables.

Once per UTC day (`pbot ledger-archive`, run by paper.yml before `pbot ledger-save`):

1. Select rows to move out:
   - quote_snapshots, market_snapshots, blocks: last seen more than `keep_days` (7) ago,
     on a ticker that is closed (latest market_snapshots.expected_expiration more than a
     day old; for a ticker with no market_snapshots, nothing of it seen for `keep_days`)
     and has no open fill, paper order or maker shadow. Blocks with no ticker go by age.
   - orderbook_snapshots: older than `orderbook_days` (3), any ticker. No live reader
     looks back more than 30 minutes.
   The newest row of each table is never selected, so SQLite never reuses an id.
2. Write them to `archive-YYYY-MM-DD.sqlite.gz` (YYYY-MM-DD = the UTC day of the cut,
   not of the data) and upload it to the `ledger-archive` release; confirm it is there.
3. Only then, in one transaction: copy each affected maker shadow's quote at expires_at
   onto the shadow (maker_shadow_metrics reads it), add the archived blocks' counts to
   blocks_rollup, delete the rows, and log the batch in archive_log. Then VACUUM.

If a day's asset already exists (an earlier run archived but its save never landed),
it is downloaded and exactly the rows it holds, unchanged, are deleted; nothing is
uploaded twice. Any failure before the delete commits leaves the ledger untouched; the
command only warns, so an archive problem never stops a paper cycle or its save.

`pbot ledger-archive-fetch` rebuilds full history for analysis: the hot ledger plus
every archive asset, merged by (table, id).
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from prediction_bot import ledger_store as ls

if TYPE_CHECKING:
    from prediction_bot.paper.ledger import Ledger

log = logging.getLogger(__name__)

ARCHIVE_TAG = "ledger-archive"
ARCHIVE_TITLE = "Paper ledger archive"
ARCHIVE_NOTES = (
    "Rows moved out of the hot paper ledger, one write-once file per UTC day of the cut "
    "(`pbot ledger-archive`). Rebuild full history with `pbot ledger-archive-fetch`."
)
ARCHIVE_PREFIX = "archive-"
ARCHIVE_SUFFIX = ".sqlite.gz"
KEEP_DAYS = 7
ORDERBOOK_DAYS = 3
CLOSED_GRACE = timedelta(days=1)

# table -> the time column that says how recent a row is
TABLES: dict[str, str] = {
    "quote_snapshots": "COALESCE(last_seen_at,created_at)",
    "market_snapshots": "fetched_at",
    "blocks": "COALESCE(last_seen_at,created_at)",
    "orderbook_snapshots": "fetched_at",
}


class ArchiveError(RuntimeError):
    pass


@dataclass
class ArchiveResult:
    day: str
    status: str  # archived | resumed | nothing | already_done
    asset: str = ""
    rows: dict[str, int] = field(default_factory=dict)


def archive_asset_name(day: date) -> str:
    return f"{ARCHIVE_PREFIX}{day.isoformat()}{ARCHIVE_SUFFIX}"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------- selection (pure SQL)


def protected_tickers(conn: sqlite3.Connection) -> set[str]:
    """Tickers with an open paper position, open paper order or open maker shadow."""
    rows = conn.execute(
        "SELECT ticker FROM fills WHERE ticker NOT IN (SELECT ticker FROM settlements) "
        "UNION SELECT ticker FROM paper_orders WHERE status='open' "
        "UNION SELECT ticker FROM maker_shadow WHERE status='open'"
    )
    return {str(r[0]) for r in rows}


def closed_tickers(conn: sqlite3.Connection, now: datetime, keep_days: int) -> set[str]:
    cutoff = _iso(now - timedelta(days=keep_days))
    expired = _iso(now - CLOSED_GRACE)
    closed = {
        str(r[0])
        for r in conn.execute(
            "SELECT ticker FROM market_snapshots GROUP BY ticker "
            "HAVING MAX(expected_expiration) < ?",
            (expired,),
        )
    }
    # Tickers Kalshi lists outside the sportsbook series (no market_snapshots rows):
    # closed once nothing of them has been seen for keep_days.
    closed |= {
        str(r[0])
        for r in conn.execute(
            "SELECT ticker FROM (SELECT ticker, COALESCE(last_seen_at,created_at) AS t "
            "FROM quote_snapshots UNION ALL SELECT ticker, COALESCE(last_seen_at,created_at) "
            "FROM blocks WHERE ticker IS NOT NULL AND ticker<>'') "
            "WHERE ticker NOT IN (SELECT ticker FROM market_snapshots) "
            "GROUP BY ticker HAVING MAX(t) < ?",
            (cutoff,),
        )
    }
    return closed


def select_archivable(
    conn: sqlite3.Connection,
    now: datetime,
    keep_days: int = KEEP_DAYS,
    orderbook_days: int = ORDERBOOK_DAYS,
) -> dict[str, list[int]]:
    """Row ids per table that may leave the hot ledger now."""
    cutoff = _iso(now - timedelta(days=keep_days))
    book_cutoff = _iso(now - timedelta(days=orderbook_days))
    tickers = closed_tickers(conn, now, keep_days) - protected_tickers(conn)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _archive_tickers (ticker TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _archive_tickers")
    conn.executemany("INSERT INTO _archive_tickers VALUES (?)", [(t,) for t in tickers])
    in_closed = "ticker IN (SELECT ticker FROM _archive_tickers)"
    where = {
        "quote_snapshots": f"{TABLES['quote_snapshots']} < :cut AND {in_closed}",
        "market_snapshots": f"fetched_at < :cut AND {in_closed}",
        "blocks": (f"{TABLES['blocks']} < :cut AND ({in_closed} OR ticker IS NULL OR ticker='')"),
        "orderbook_snapshots": "fetched_at < :book",
    }
    out: dict[str, list[int]] = {}
    for table, condition in where.items():
        out[table] = [
            int(r[0])
            for r in conn.execute(
                f"SELECT id FROM {table} WHERE {condition} "
                f"AND id < (SELECT MAX(id) FROM {table}) ORDER BY id",
                {"cut": cutoff, "book": book_cutoff},
            )
        ]
    return out


# ------------------------------------------------------------------- archive file


def _columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [str(r[1]) for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _load_ids(conn: sqlite3.Connection, ids: Mapping[str, Iterable[int]]) -> None:
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _archive_ids (tbl TEXT NOT NULL, id INTEGER NOT NULL, "
        "PRIMARY KEY (tbl, id))"
    )
    conn.execute("DELETE FROM _archive_ids")
    conn.executemany(
        "INSERT INTO _archive_ids VALUES (?,?)",
        [(table, i) for table, table_ids in ids.items() for i in table_ids],
    )


def _ids_sql(table: str) -> str:
    return f"id IN (SELECT id FROM temp._archive_ids WHERE tbl='{table}')"


def build_archive(
    conn: sqlite3.Connection, ids: Mapping[str, Iterable[int]], path: Path, day: date
) -> dict[str, int]:
    """Copy the selected rows into a new SQLite file at `path`."""
    path.unlink(missing_ok=True)
    _load_ids(conn, ids)
    conn.commit()
    conn.execute("ATTACH DATABASE ? AS arc", (str(path),))
    try:
        counts: dict[str, int] = {}
        for table in TABLES:
            conn.execute(f"CREATE TABLE arc.{table} AS SELECT * FROM main.{table} WHERE 0")
            cur = conn.execute(
                f"INSERT INTO arc.{table} SELECT * FROM main.{table} WHERE {_ids_sql(table)}"
            )
            counts[table] = cur.rowcount
        conn.execute("CREATE TABLE arc.archive_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO arc.archive_meta VALUES (?,?)",
            [("day", day.isoformat()), ("rows", json.dumps(counts, sort_keys=True))],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("DETACH DATABASE arc")
    return counts


def matching_ids(conn: sqlite3.Connection, archive: Path) -> dict[str, list[int]]:
    """Ids of hot rows identical (on shared columns) to a row in an existing archive."""
    conn.commit()
    conn.execute("ATTACH DATABASE ? AS arc", (str(archive),))
    try:
        out: dict[str, list[int]] = {}
        for table in TABLES:
            if not _columns(conn, table, "arc"):
                out[table] = []
                continue
            shared = [c for c in _columns(conn, table, "arc") if c in _columns(conn, table)]
            same = " AND ".join(f"h.{c} IS a.{c}" for c in shared)
            out[table] = [
                int(r[0])
                for r in conn.execute(
                    f"SELECT h.id FROM main.{table} h JOIN arc.{table} a ON a.id=h.id WHERE {same}"
                )
            ]
        return out
    finally:
        conn.execute("DETACH DATABASE arc")


# ---------------------------------------------------------------------- deletion


def apply_deletion(
    conn: sqlite3.Connection,
    ids: Mapping[str, Iterable[int]],
    *,
    day: date,
    asset: str,
    sha256: str,
    gz_bytes: int,
    now: datetime,
) -> dict[str, int]:
    """Freeze maker-shadow expiry quotes, roll up block counts, delete, log. Atomic."""
    _load_ids(conn, ids)
    stamp = _iso(now)
    try:
        # Shadows whose ticker loses quote rows keep the quote metrics need. The quote at
        # or before a past expires_at can no longer change, so freezing it is exact.
        tickers = [
            str(r[0])
            for r in conn.execute(
                f"SELECT DISTINCT ticker FROM quote_snapshots WHERE {_ids_sql('quote_snapshots')}"
            )
        ]
        for ticker in tickers:
            shadows = conn.execute(
                "SELECT id,expires_at FROM maker_shadow WHERE ticker=? "
                "AND expiry_quote_at IS NULL AND expires_at<=?",
                (ticker, stamp),
            ).fetchall()
            for shadow_id, expires_at in shadows:
                quote = conn.execute(
                    "SELECT yes_bid,yes_ask FROM quote_snapshots WHERE ticker=? AND created_at<=?"
                    " AND yes_bid IS NOT NULL AND yes_ask IS NOT NULL "
                    "ORDER BY created_at DESC,id DESC LIMIT 1",
                    (ticker, str(expires_at)),
                ).fetchone()
                conn.execute(
                    "UPDATE maker_shadow SET expiry_yes_bid=?,expiry_yes_ask=?,expiry_quote_at=? "
                    "WHERE id=?",
                    (
                        None if quote is None else quote[0],
                        None if quote is None else quote[1],
                        stamp,
                        shadow_id,
                    ),
                )
        conn.execute(
            "INSERT INTO blocks_rollup (day,reason,rules_version,strategy,n) "
            "SELECT substr(created_at,1,10),reason,COALESCE(rules_version,''),"
            "COALESCE(strategy,''),SUM(seen_count) FROM blocks "
            f"WHERE {_ids_sql('blocks')} GROUP BY 1,2,3,4 "
            "ON CONFLICT(day,reason,rules_version,strategy) DO UPDATE SET n=n+excluded.n"
        )
        deleted: dict[str, int] = {}
        for table in TABLES:
            deleted[table] = conn.execute(f"DELETE FROM {table} WHERE {_ids_sql(table)}").rowcount
        conn.execute(
            "INSERT OR REPLACE INTO archive_log (day,asset,sha256,gz_bytes,rows_json,archived_at)"
            " VALUES (?,?,?,?,?,?)",
            (day.isoformat(), asset, sha256, gz_bytes, json.dumps(deleted, sort_keys=True), stamp),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return deleted


# ---------------------------------------------------------------------- the step


def _asset_names(runner: ls.Runner) -> list[str] | None:
    """Assets on the archive release, or None when the release does not exist yet."""
    view = runner.run(["gh", "release", "view", ARCHIVE_TAG, "--json", "assets"])
    if view.returncode != 0:
        return None
    return ls.release_asset_names(view.text)


def _download(runner: ls.Runner, name: str, into: Path, sleep: ls.Sleep) -> Path:
    argv = [
        "gh",
        "release",
        "download",
        ARCHIVE_TAG,
        "--pattern",
        name,
        "--dir",
        str(into),
        "--clobber",
    ]
    ls.run_retry(runner, argv, 4, sleep, f"download of {name}")
    gz = into / name
    raw = into / name.removesuffix(".gz")
    ls.gunzip_file(gz, raw)
    return raw


def run_archive(
    runner: ls.Runner,
    conn: sqlite3.Connection,
    workdir: Path,
    *,
    now: datetime | None = None,
    keep_days: int = KEEP_DAYS,
    orderbook_days: int = ORDERBOOK_DAYS,
    sleep: ls.Sleep = time.sleep,
) -> ArchiveResult:
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(timezone.utc).date()
    if conn.execute("SELECT 1 FROM archive_log WHERE day=?", (day.isoformat(),)).fetchone():
        return ArchiveResult(day.isoformat(), "already_done")
    name = archive_asset_name(day)
    work = workdir / f".archive-{day.isoformat()}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        existing = _asset_names(runner)
        if existing is not None and name in existing:
            # An earlier run uploaded today's batch but its ledger save never landed.
            raw = _download(runner, name, work, sleep)
            ids = matching_ids(conn, raw)
            gz = work / name
            deleted = apply_deletion(
                conn,
                ids,
                day=day,
                asset=name,
                sha256=ls.sha256_file(gz),
                gz_bytes=gz.stat().st_size,
                now=now,
            )
            status = "resumed"
        else:
            ids = select_archivable(conn, now, keep_days, orderbook_days)
            if not any(ids.values()):
                apply_deletion(conn, ids, day=day, asset="", sha256="", gz_bytes=0, now=now)
                return ArchiveResult(day.isoformat(), "nothing")
            raw = work / name.removesuffix(".gz")
            built = build_archive(conn, ids, raw, day)
            gz = work / name
            ls.gzip_file(raw, gz)
            ls._ensure_release(runner, sleep, ARCHIVE_TAG, ARCHIVE_TITLE, ARCHIVE_NOTES)
            ls._upload(runner, gz, sleep, ARCHIVE_TAG)
            deleted = apply_deletion(
                conn,
                ids,
                day=day,
                asset=name,
                sha256=ls.sha256_file(gz),
                gz_bytes=gz.stat().st_size,
                now=now,
            )
            if deleted != built:  # same id list both times; never expected
                log.warning("ledger-archive: archived %s rows but deleted %s", built, deleted)
            status = "archived"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    conn.execute("VACUUM")
    return ArchiveResult(day.isoformat(), status, name, deleted)


# -------------------------------------------------------------------- full history


def merge_archive(conn: sqlite3.Connection, archive: Path) -> dict[str, int]:
    """Insert an archive's rows into `conn`. Identical rows already present are skipped;
    the same id with different content raises (an id was reused)."""
    conn.commit()
    conn.execute("ATTACH DATABASE ? AS arc", (str(archive),))
    try:
        added: dict[str, int] = {}
        for table in TABLES:
            arc_cols = _columns(conn, table, "arc")
            if not arc_cols:
                continue
            shared = [c for c in arc_cols if c in _columns(conn, table)]
            same = " AND ".join(f"m.{c} IS a.{c}" for c in shared)
            clash = conn.execute(
                f"SELECT COUNT(*) FROM arc.{table} a JOIN main.{table} m ON m.id=a.id "
                f"WHERE NOT ({same})"
            ).fetchone()[0]
            if clash:
                raise ArchiveError(
                    f"{archive.name}: {clash} {table} ids already hold different rows"
                )
            cols = ",".join(shared)
            added[table] = conn.execute(
                f"INSERT INTO main.{table} ({cols}) SELECT {cols} FROM arc.{table} "
                f"WHERE id NOT IN (SELECT id FROM main.{table})"
            ).rowcount
        conn.commit()
        return added
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("DETACH DATABASE arc")


def fetch_full_history(
    runner: ls.Runner,
    conn: sqlite3.Connection,
    workdir: Path,
    sleep: ls.Sleep = time.sleep,
) -> dict[str, dict[str, int]]:
    """Merge every archive asset into `conn` (a restored hot ledger)."""
    names = sorted(
        n
        for n in (_asset_names(runner) or [])
        if n.startswith(ARCHIVE_PREFIX) and n.endswith(ARCHIVE_SUFFIX)
    )
    out: dict[str, dict[str, int]] = {}
    work = workdir / ".archive-fetch"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        for name in names:
            out[name] = merge_archive(conn, _download(runner, name, work, sleep))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


# ------------------------------------------------------- model_side from the archive


def backfill_model_side_from_archives(
    runner: ls.Runner,
    ledger: Ledger,
    workdir: Path,
    sleep: ls.Sleep = time.sleep,
) -> tuple[int, list[str]] | None:
    """One-time: set mid/model_side on shadow rows whose market snapshots the daily
    archive moved out before the scoreboard backfill ran. Downloads every archive asset,
    searches its market_snapshots (nothing is merged into the hot ledger), writes the
    rows and records `ledger_migrations` so later runs skip it. Returns (rows set,
    assets read), or None when it already ran. Raises when the release cannot be read
    or an asset cannot be downloaded, leaving the marker unset so the next run retries.
    """
    if ledger.archive_backfill_done():
        return None
    names = _asset_names(runner)
    if names is None:
        raise ArchiveError(f"cannot list the {ARCHIVE_TAG} release")
    assets = sorted(n for n in names if n.startswith(ARCHIVE_PREFIX) and n.endswith(ARCHIVE_SUFFIX))
    work = workdir / ".model-side-backfill"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        paths = [_download(runner, name, work, sleep) for name in assets]
        updated = ledger.backfill_model_side(paths)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    ledger.mark_archive_backfill_done(f"rows={updated} assets={len(assets)}")
    return updated, assets
