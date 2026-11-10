"""Where the paper ledger lives between workflow runs.

The SQLite ledger is a GitHub Release asset (tag ``ledger-store``), one uniquely named
``paper-<UTC stamp>-r<runs>.sqlite.gz`` per save. The ``paper-ledger`` branch holds the
small report files plus ``ledger_manifest.json``, the single source of truth for which
asset is current. Committing the gzipped database to the branch on every cycle (the
layout before this module) added ~43 MB of history per commit, because gzip files do not
delta-compress in git.

Every restore fails closed: the target file is written only after the downloaded bytes
match the manifest's size and sha256, ``PRAGMA quick_check`` passes, and the ``runs``
count is at least the manifest's. A failure never leaves an empty or older database at
``--out``.

Decisions are pure functions; every git/gh call goes through a ``Runner`` so the flow is
unit-tested with a fake runner.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import time
import zlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

BRANCH = "paper-ledger"
RELEASE_TAG = "ledger-store"
MANIFEST_NAME = "ledger_manifest.json"
RESTORE_STATE_NAME = "restore_state.json"
LEGACY_GZ = "paper.sqlite.gz"
LEGACY_RAW = "paper.sqlite"
SMALL_FILES = (
    "last_run.txt",
    "REPORT.txt",
    "PREDICTIONS.md",
    "index.html",
    ".nojekyll",
    "polymarket_alias_gaps.md",
)
ASSET_PREFIX = "paper-"
ASSET_SUFFIX = ".sqlite.gz"
ASSET_STAMP = "%Y%m%dT%H%M%SZ"
KEEP_NEWEST = 10
KEEP_DAYS = 7
BOT_NAME = "paper-bot"
BOT_EMAIL = "paper-bot@users.noreply.github.com"


class LedgerStoreError(RuntimeError):
    """A restore or save that must fail the job."""


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Manifest:
    asset: str
    sha256: str
    gz_bytes: int
    runs: int
    last_run_at: str | None
    slots_last_24h: int
    written_at: str
    release_tag: str = RELEASE_TAG
    version: int = 1

    def to_json(self) -> str:
        data = {
            "version": self.version,
            "release_tag": self.release_tag,
            "asset": self.asset,
            "sha256": self.sha256,
            "gz_bytes": self.gz_bytes,
            "runs": self.runs,
            "last_run_at": self.last_run_at,
            "slots_last_24h": self.slots_last_24h,
            "written_at": self.written_at,
        }
        return json.dumps(data, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str | bytes) -> Manifest:
        try:
            data = json.loads(text)
        except (ValueError, UnicodeDecodeError) as exc:
            raise LedgerStoreError(f"{MANIFEST_NAME} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != 1:
            raise LedgerStoreError(f"{MANIFEST_NAME}: unsupported version {data!r:.80}")
        try:
            manifest = cls(
                version=1,
                release_tag=str(data["release_tag"]),
                asset=str(data["asset"]),
                sha256=str(data["sha256"]),
                gz_bytes=int(data["gz_bytes"]),
                runs=int(data["runs"]),
                last_run_at=None if data.get("last_run_at") is None else str(data["last_run_at"]),
                slots_last_24h=int(data["slots_last_24h"]),
                written_at=str(data["written_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerStoreError(f"{MANIFEST_NAME} is missing or has a bad field: {exc}") from exc
        if parse_asset_time(manifest.asset) is None or len(manifest.sha256) != 64:
            raise LedgerStoreError(f"{MANIFEST_NAME} names a malformed asset or sha256")
        return manifest


@dataclass(frozen=True)
class RestoreState:
    """What `ledger-restore` found, written next to the database for `ledger-save`."""

    source: str  # manifest | legacy_gz | legacy_raw | bootstrap
    runs: int
    manifest: Manifest | None

    def to_json(self) -> str:
        data: dict[str, Any] = {"source": self.source, "runs": self.runs, "manifest": None}
        if self.manifest is not None:
            data["manifest"] = asdict(self.manifest)
        return json.dumps(data, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> RestoreState:
        data = json.loads(text)
        manifest = None
        if data.get("manifest") is not None:
            manifest = Manifest.from_json(json.dumps(data["manifest"]))
        return cls(source=str(data["source"]), runs=int(data["runs"]), manifest=manifest)


@dataclass(frozen=True)
class DbStats:
    runs: int
    last_run_at: str | None
    slots_last_24h: int


# --------------------------------------------------------------------- pure logic


def decide_restore_source(tip_files: Iterable[str]) -> str | None:
    """Which file on the branch tip holds the ledger, in order of preference."""
    names = set(tip_files)
    if MANIFEST_NAME in names:
        return "manifest"
    if LEGACY_GZ in names:
        return "legacy_gz"
    if LEGACY_RAW in names:
        return "legacy_raw"
    return None


def asset_name(now: datetime, runs: int) -> str:
    stamp = now.astimezone(timezone.utc).strftime(ASSET_STAMP)
    return f"{ASSET_PREFIX}{stamp}-r{runs}{ASSET_SUFFIX}"


def parse_asset_time(name: str) -> datetime | None:
    """The UTC save time encoded in an asset name, or None for any other name."""
    if not (name.startswith(ASSET_PREFIX) and name.endswith(ASSET_SUFFIX)):
        return None
    core = name[len(ASSET_PREFIX) : -len(ASSET_SUFFIX)]
    stamp, sep, runs = core.partition("-r")
    if not sep or not runs.isdigit():
        return None
    try:
        return datetime.strptime(stamp, ASSET_STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def select_prunable(
    names: Iterable[str],
    keep: str,
    now: datetime,
    keep_newest: int = KEEP_NEWEST,
    keep_days: int = KEEP_DAYS,
) -> list[str]:
    """Assets safe to delete: never `keep`, the newest `keep_newest`, the newest of each
    of the last `keep_days` UTC days, or any name this module did not write."""
    dated = sorted(
        ((t, n) for n in set(names) if (t := parse_asset_time(n)) is not None),
        reverse=True,
    )
    kept = {keep} | {n for _t, n in dated[:keep_newest]}
    first_day = now.astimezone(timezone.utc).date() - timedelta(days=keep_days - 1)
    seen_days: set[object] = set()
    for t, n in dated:
        day = t.date()
        if day >= first_day and day not in seen_days:
            seen_days.add(day)
            kept.add(n)
    return sorted(n for _t, n in dated if n not in kept)


def slots_last_24h(conn: sqlite3.Connection, now: datetime) -> int:
    """Half-hour slots of the last 24h with a scheduled (untriggered) run."""
    since = now - timedelta(days=1)
    slots: set[tuple[int, int]] = set()
    for ran_at, reason in conn.execute(
        "SELECT ran_at,trigger_reason FROM runs WHERE ran_at>=?", (since.isoformat(),)
    ):
        if reason is not None:
            continue
        try:
            at = datetime.fromisoformat(str(ran_at))
        except ValueError:
            continue
        at = at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)
        slots.add((at.hour, at.minute // 30))
    return len(slots)


def db_stats(path: Path, now: datetime) -> DbStats:
    """quick_check the database and read the counts the manifest records."""
    if not path.is_file() or path.stat().st_size == 0:
        raise LedgerStoreError(f"{path} is missing or empty")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise LedgerStoreError(f"{path} does not open as SQLite: {exc}") from exc
    try:
        result = [str(r[0]) for r in conn.execute("PRAGMA quick_check").fetchall()]
        if result != ["ok"]:
            raise LedgerStoreError(f"{path} failed quick_check: {'; '.join(result[:5])}")
        has_runs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone()
        if not has_runs:
            raise LedgerStoreError(f"{path} has no runs table")
        runs, last = conn.execute("SELECT COUNT(*), MAX(ran_at) FROM runs").fetchone()
        return DbStats(int(runs), None if last is None else str(last), slots_last_24h(conn, now))
    except sqlite3.DatabaseError as exc:
        raise LedgerStoreError(f"{path} is not a readable SQLite database: {exc}") from exc
    finally:
        conn.close()


def check_min_runs(stats: DbStats, min_runs: int, what: str) -> None:
    if stats.runs < min_runs:
        raise LedgerStoreError(
            f"{what} has {stats.runs} runs, fewer than the {min_runs} the manifest records"
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def gunzip_file(src: Path, dest: Path) -> None:
    try:
        with gzip.open(src, "rb") as fin, dest.open("wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
    except (OSError, EOFError, zlib.error) as exc:
        raise LedgerStoreError(f"{src.name} does not gunzip: {exc}") from exc


def gzip_file(src: Path, dest: Path) -> None:
    """Like `gzip -9 -n`: no file name and mtime 0, so equal input gives equal bytes."""
    with (
        src.open("rb") as fin,
        dest.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", compresslevel=9, fileobj=raw, mtime=0) as fout,
    ):
        shutil.copyfileobj(fin, fout, 1 << 20)


def asset_on_release(view_json: str, name: str, size: int) -> bool:
    """True when `gh release view --json assets` lists `name` uploaded at `size` bytes."""
    try:
        assets = json.loads(view_json).get("assets") or []
    except (ValueError, AttributeError):
        return False
    return any(
        a.get("name") == name and int(a.get("size", -1)) == size and a.get("state") == "uploaded"
        for a in assets
    )


def release_asset_names(view_json: str) -> list[str]:
    try:
        return [str(a["name"]) for a in json.loads(view_json).get("assets") or []]
    except (ValueError, AttributeError, KeyError, TypeError):
        return []


# ------------------------------------------------------------------------- runner


@dataclass(frozen=True)
class CmdResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace").strip()


class Runner(Protocol):
    def run(self, argv: Sequence[str], cwd: Path | None = None) -> CmdResult: ...


class SubprocessRunner:
    def __init__(self, timeout: float = 240.0) -> None:
        self.timeout = timeout

    def run(self, argv: Sequence[str], cwd: Path | None = None) -> CmdResult:
        try:
            proc = subprocess.run(
                list(argv), cwd=cwd, capture_output=True, timeout=self.timeout, check=False
            )
        except subprocess.TimeoutExpired:
            return CmdResult(124, b"", f"timed out after {self.timeout:.0f}s".encode())
        except OSError as exc:
            return CmdResult(127, b"", str(exc).encode())
        return CmdResult(proc.returncode, proc.stdout, proc.stderr)


Sleep = Callable[[float], None]


def run_retry(
    runner: Runner,
    argv: Sequence[str],
    attempts: int,
    sleep: Sleep,
    what: str,
    cwd: Path | None = None,
) -> CmdResult:
    """Run `argv` up to `attempts` times with 2s, 4s, ... backoff; raise on the last failure."""
    result = CmdResult(1)
    for attempt in range(attempts):
        result = runner.run(argv, cwd=cwd)
        if result.returncode == 0:
            return result
        log.warning("%s failed (attempt %d/%d): %s", what, attempt + 1, attempts, result.err)
        if attempt + 1 < attempts:
            sleep(2.0 * 2**attempt)
    raise LedgerStoreError(f"{what} failed after {attempts} attempts: {result.err}")


# ------------------------------------------------------------------------ restore


@dataclass(frozen=True)
class RestoreResult:
    source: str
    runs: int
    detail: str


def _branch_exists(runner: Runner, sleep: Sleep) -> bool:
    argv = ["git", "ls-remote", "--exit-code", "--heads", "origin", BRANCH]
    result = CmdResult(1)
    for attempt in range(3):
        result = runner.run(argv)
        if result.returncode == 0:
            return True
        if result.returncode == 2:
            return False
        log.warning("git ls-remote failed (attempt %d/3): %s", attempt + 1, result.err)
        if attempt < 2:
            sleep(2.0 * 2**attempt)
    raise LedgerStoreError(f"cannot reach origin to look up {BRANCH}: {result.err}")


def _tip_files(runner: Runner) -> list[str]:
    result = runner.run(["git", "ls-tree", "--name-only", BRANCH])
    if result.returncode != 0:
        raise LedgerStoreError(f"git ls-tree {BRANCH} failed: {result.err}")
    return [line for line in result.text.splitlines() if line]


def _git_show(runner: Runner, name: str) -> bytes:
    result = runner.run(["git", "show", f"{BRANCH}:{name}"])
    if result.returncode != 0:
        raise LedgerStoreError(f"git show {BRANCH}:{name} failed: {result.err}")
    return result.stdout


def _download_asset(runner: Runner, manifest: Manifest, into: Path, sleep: Sleep) -> Path:
    argv = [
        "gh",
        "release",
        "download",
        manifest.release_tag,
        "--pattern",
        manifest.asset,
        "--dir",
        str(into),
        "--clobber",
    ]
    run_retry(runner, argv, 4, sleep, f"download of {manifest.asset}")
    path = into / manifest.asset
    if not path.is_file():
        raise LedgerStoreError(f"gh release download left no {manifest.asset}")
    size = path.stat().st_size
    if size != manifest.gz_bytes:
        raise LedgerStoreError(f"{manifest.asset}: {size} bytes, manifest says {manifest.gz_bytes}")
    digest = sha256_file(path)
    if digest != manifest.sha256:
        raise LedgerStoreError(f"{manifest.asset}: sha256 {digest} != manifest {manifest.sha256}")
    return path


def restore(
    runner: Runner,
    out: Path,
    *,
    bootstrap: bool = False,
    require_manifest: bool = False,
    now: datetime | None = None,
    sleep: Sleep = time.sleep,
) -> RestoreResult:
    """Put the current ledger at `out`, verified, or raise without touching `out`."""
    now = now or datetime.now(timezone.utc)
    out.parent.mkdir(parents=True, exist_ok=True)
    state_path = out.parent / RESTORE_STATE_NAME

    def _bootstrap(why: str) -> RestoreResult:
        if not bootstrap:
            raise LedgerStoreError(f"{why}; set PBOT_LEDGER_BOOTSTRAP=true to start empty")
        if out.exists():
            raise LedgerStoreError(f"{why}, and {out} already exists; refusing to bootstrap")
        state_path.write_text(RestoreState("bootstrap", 0, None).to_json())
        return RestoreResult("bootstrap", 0, f"{why}; starting an empty ledger")

    if not _branch_exists(runner, sleep):
        return _bootstrap(f"origin has no {BRANCH} branch")
    run_retry(
        runner,
        ["git", "fetch", "--depth=1", "origin", f"{BRANCH}:{BRANCH}"],
        3,
        sleep,
        f"git fetch {BRANCH}",
    )
    source = decide_restore_source(_tip_files(runner))
    if source is None:
        return _bootstrap(f"the {BRANCH} tip has no {MANIFEST_NAME} or {LEGACY_GZ}")
    if require_manifest and source != "manifest":
        raise LedgerStoreError(f"the {BRANCH} tip has no {MANIFEST_NAME} (found {source})")

    work = out.parent / f".restore-{os.getpid()}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    tmp_db = work / "paper.sqlite"
    try:
        manifest: Manifest | None = None
        if source == "manifest":
            manifest = Manifest.from_json(_git_show(runner, MANIFEST_NAME))
            gz = _download_asset(runner, manifest, work, sleep)
            gunzip_file(gz, tmp_db)
            detail = f"asset {manifest.asset}"
        elif source == "legacy_gz":
            gz = work / LEGACY_GZ
            gz.write_bytes(_git_show(runner, LEGACY_GZ))
            gunzip_file(gz, tmp_db)
            detail = f"{BRANCH}:{LEGACY_GZ} (migration)"
        else:
            tmp_db.write_bytes(_git_show(runner, LEGACY_RAW))
            detail = f"{BRANCH}:{LEGACY_RAW} (migration)"
        stats = db_stats(tmp_db, now)
        if manifest is not None:
            check_min_runs(stats, manifest.runs, "the restored ledger")
        os.replace(tmp_db, out)
        state_path.write_text(RestoreState(source, stats.runs, manifest).to_json())
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return RestoreResult(source, stats.runs, f"{detail}: {stats.runs} runs")


# --------------------------------------------------------------------------- save


@dataclass(frozen=True)
class SaveResult:
    manifest: Manifest
    uploaded: bool
    pruned: list[str]
    prune_errors: list[str]


def _release_view(runner: Runner, sleep: Sleep, tag: str = RELEASE_TAG) -> str:
    argv = ["gh", "release", "view", tag, "--json", "assets"]
    return run_retry(runner, argv, 3, sleep, f"gh release view {tag}").text


STORE_NOTES = (
    "Paper ledger snapshots. The current one is named in ledger_manifest.json "
    "on the paper-ledger branch. Written by `pbot ledger-save`."
)


def _ensure_release(
    runner: Runner,
    sleep: Sleep,
    tag: str = RELEASE_TAG,
    title: str = "Paper ledger store",
    notes: str = STORE_NOTES,
) -> None:
    if runner.run(["gh", "release", "view", tag, "--json", "tagName"]).returncode == 0:
        return
    create = [
        "gh",
        "release",
        "create",
        tag,
        "--prerelease",
        "--latest=false",
        "--target",
        "main",
        "--title",
        title,
        "--notes",
        notes,
    ]
    if runner.run(create).returncode == 0:
        return
    # Created concurrently, or a network blip: either way it must exist now.
    run_retry(
        runner,
        ["gh", "release", "view", tag, "--json", "tagName"],
        3,
        sleep,
        f"create release {tag}",
    )


def _upload(runner: Runner, path: Path, sleep: Sleep, tag: str = RELEASE_TAG) -> None:
    size = path.stat().st_size
    last = ""
    for attempt in range(4):
        result = runner.run(["gh", "release", "upload", tag, str(path)])
        last = result.err
        # Check even after a failed upload: an earlier attempt may have landed.
        view = runner.run(["gh", "release", "view", tag, "--json", "assets"])
        if view.returncode == 0 and asset_on_release(view.text, path.name, size):
            return
        log.warning("upload of %s not confirmed (attempt %d/4): %s", path.name, attempt + 1, last)
        if attempt < 3:
            sleep(2.0 * 2**attempt)
    raise LedgerStoreError(f"upload of {path.name} not confirmed after 4 attempts: {last}")


def _commit_branch(
    runner: Runner, worktree: Path, manifest: Manifest, src_dir: Path, sleep: Sleep
) -> None:
    def git(*args: str, cwd: Path | None = None) -> CmdResult:
        result = runner.run(["git", *args], cwd=cwd)
        if result.returncode != 0:
            raise LedgerStoreError(f"git {' '.join(args)} failed: {result.err}")
        return result

    if worktree.exists():
        runner.run(["git", "worktree", "remove", "--force", str(worktree)])
        shutil.rmtree(worktree, ignore_errors=True)
    has_branch = runner.run(["git", "rev-parse", "-q", "--verify", f"refs/heads/{BRANCH}"])
    if has_branch.returncode == 0:
        git("worktree", "add", str(worktree), BRANCH)
    else:
        git("worktree", "add", "--detach", str(worktree), "HEAD")
        git("checkout", "-q", "--orphan", BRANCH, cwd=worktree)
        git("rm", "-rfq", "--ignore-unmatch", ".", cwd=worktree)
    (worktree / MANIFEST_NAME).write_text(manifest.to_json())
    added = [MANIFEST_NAME]
    for name in SMALL_FILES:
        src = src_dir / name
        if src.is_file():
            shutil.copyfile(src, worktree / name)
            added.append(name)
        else:
            log.warning("ledger-save: %s is missing; the branch keeps its old copy", src)
    git("rm", "-q", "--cached", "--ignore-unmatch", LEGACY_GZ, LEGACY_RAW, cwd=worktree)
    for name in (LEGACY_GZ, LEGACY_RAW):
        (worktree / name).unlink(missing_ok=True)
    git("add", *added, cwd=worktree)
    git(
        "-c",
        f"user.name={BOT_NAME}",
        "-c",
        f"user.email={BOT_EMAIL}",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        f"paper cycle {manifest.written_at}",
        cwd=worktree,
    )
    run_retry(
        runner, ["git", "push", "origin", BRANCH], 3, sleep, f"git push {BRANCH}", cwd=worktree
    )


def _prune(runner: Runner, keep: str, now: datetime, sleep: Sleep) -> tuple[list[str], list[str]]:
    deleted: list[str] = []
    errors: list[str] = []
    try:
        names = release_asset_names(_release_view(runner, sleep))
    except LedgerStoreError as exc:
        return deleted, [str(exc)]
    for name in select_prunable(names, keep, now):
        if name == keep:  # select_prunable never returns it; belt and braces
            continue
        result = runner.run(["gh", "release", "delete-asset", RELEASE_TAG, name, "--yes"])
        if result.returncode == 0:
            deleted.append(name)
        else:
            errors.append(f"{name}: {result.err}")
    return deleted, errors


def save(
    runner: Runner,
    db: Path,
    src_dir: Path,
    *,
    worktree: Path = Path("wt"),
    now: datetime | None = None,
    sleep: Sleep = time.sleep,
) -> SaveResult:
    """Upload `db` as a new asset, point the branch manifest at it, then prune."""
    now = now or datetime.now(timezone.utc)
    state_path = src_dir / RESTORE_STATE_NAME
    if not state_path.is_file():
        raise LedgerStoreError(f"{state_path} is missing: run `pbot ledger-restore` first")
    state = RestoreState.from_json(state_path.read_text())
    min_runs = max(state.runs, state.manifest.runs if state.manifest else 0)

    stats = db_stats(db, now)
    check_min_runs(stats, min_runs, str(db))

    name = asset_name(now, stats.runs)
    gz = src_dir / name
    gzip_file(db, gz)
    digest = sha256_file(gz)
    previous = state.manifest
    uploaded = False
    if previous is not None and previous.sha256 == digest:
        # Unchanged database: point at the asset that already holds these bytes.
        name, size = previous.asset, previous.gz_bytes
        gz.unlink()
    else:
        _ensure_release(runner, sleep)
        _upload(runner, gz, sleep)
        uploaded = True
        size = gz.stat().st_size
        gz.unlink()
    manifest = Manifest(
        asset=name,
        sha256=digest,
        gz_bytes=size,
        runs=stats.runs,
        last_run_at=stats.last_run_at,
        slots_last_24h=stats.slots_last_24h,
        written_at=now.astimezone(timezone.utc).isoformat(timespec="seconds"),
    )
    _commit_branch(runner, worktree, manifest, src_dir, sleep)
    # The push landed: this manifest is now the restore baseline for a later save.
    state_path.write_text(RestoreState("manifest", stats.runs, manifest).to_json())
    pruned, prune_errors = _prune(runner, manifest.asset, now, sleep)
    for error in prune_errors:
        log.warning("ledger-save: prune: %s", error)
    return SaveResult(manifest, uploaded, pruned, prune_errors)
