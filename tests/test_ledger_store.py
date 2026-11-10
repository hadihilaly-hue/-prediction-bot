"""ledger-restore / ledger-save against a local bare `origin` (real git) and a fake `gh`.

No network: git talks to a bare repository on disk and every `gh release` call is served
from an in-memory release by `FakeRunner`.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from prediction_bot import ledger_store as ls
from prediction_bot.paper.ledger import Ledger

UTC = timezone.utc
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).parents[1]
IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.com"]


def _no_sleep(_seconds: float) -> None:
    return None


class FakeRunner:
    """Real git in `cwd`; `gh release` served from `self.assets`. `fail` injects errors."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.release_exists = False
        self.assets: dict[str, bytes] = {}
        self.fail: dict[tuple[str, ...], list[int]] = {}
        self.calls: list[list[str]] = []

    def inject(self, prefix: Sequence[str], *codes: int) -> None:
        self.fail[tuple(prefix)] = list(codes)

    def run(self, argv: Sequence[str], cwd: Path | None = None) -> ls.CmdResult:
        argv = list(argv)
        self.calls.append(argv)
        for prefix, codes in self.fail.items():
            if tuple(argv[: len(prefix)]) == prefix and codes:
                return ls.CmdResult(codes.pop(0), b"", b"injected failure")
        if argv[0] == "git":
            proc = subprocess.run(argv, cwd=cwd or self.cwd, capture_output=True, check=False)
            return ls.CmdResult(proc.returncode, proc.stdout, proc.stderr)
        assert argv[:2] == ["gh", "release"], argv
        return self._gh(argv[2:])

    def _gh(self, args: list[str]) -> ls.CmdResult:
        verb, tag = args[0], args[1]
        assert tag == ls.RELEASE_TAG
        if verb == "create":
            self.release_exists = True
            return ls.CmdResult(0)
        if not self.release_exists:
            return ls.CmdResult(1, b"", b"release not found")
        if verb == "view":
            assets = [
                {"name": n, "size": len(b), "state": "uploaded"} for n, b in self.assets.items()
            ]
            return ls.CmdResult(0, json.dumps({"assets": assets, "tagName": tag}).encode())
        if verb == "upload":
            path = Path(args[2])
            if path.name in self.assets:
                return ls.CmdResult(1, b"", b"asset already exists")
            self.assets[path.name] = path.read_bytes()
            return ls.CmdResult(0)
        if verb == "download":
            name = args[args.index("--pattern") + 1]
            into = Path(args[args.index("--dir") + 1])
            if name not in self.assets:
                return ls.CmdResult(1, b"", b"no assets match")
            (into / name).write_bytes(self.assets[name])
            return ls.CmdResult(0)
        if verb == "delete-asset":
            self.assets.pop(args[2], None)
            return ls.CmdResult(0)
        raise AssertionError(args)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *IDENTITY, *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _make_db(path: Path, runs: int, now: datetime = NOW) -> Path:
    ledger = Ledger(path, Decimal("1000"))
    for i in range(runs):
        ledger.record_run(["book_scanner"], 0, 0, 0, 0, at=now - timedelta(minutes=30 * i))
    ledger.conn.close()
    return path


def _gz(path: Path) -> bytes:
    """Gzip exactly as `ledger-save` does. gzip.compress() writes a different OS byte on
    some Python versions (3 on 3.12, 255 on 3.13), which would make equal databases
    look changed."""
    out = path.with_suffix(".fixture.gz")
    ls.gzip_file(path, out)
    data = out.read_bytes()
    out.unlink()
    return data


def _manifest_for(name: str, blob: bytes, runs: int) -> ls.Manifest:
    return ls.Manifest(
        asset=name,
        sha256=hashlib.sha256(blob).hexdigest(),
        gz_bytes=len(blob),
        runs=runs,
        last_run_at=NOW.isoformat(),
        slots_last_24h=runs,
        written_at=NOW.isoformat(),
    )


class Repo:
    """A bare origin with `main` (and optionally `paper-ledger`) plus a clone of main."""

    def __init__(self, tmp: Path) -> None:
        self.origin = tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        seed = tmp / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        (seed / "README").write_text("main\n")
        _git(seed, "add", "README")
        _git(seed, "commit", "-qm", "main")
        _git(seed, "push", "-q", str(self.origin), "main")
        self.seed = seed
        self.clone = tmp / "clone"
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--single-branch",
                "-b",
                "main",
                str(self.origin),
                str(self.clone),
            ],
            check=True,
        )
        self.runner = FakeRunner(self.clone)

    def set_branch(self, files: dict[str, bytes]) -> str:
        """Commit `files` as the whole tree of paper-ledger on origin; return the sha."""
        work = self.seed
        _git(work, "checkout", "-q", "--orphan", f"tmp-{len(files)}-{id(files)}")
        _git(work, "rm", "-rqf", "--ignore-unmatch", ".")
        for name, data in files.items():
            (work / name).write_bytes(data)
        _git(work, "add", *files)
        _git(work, "commit", "-qm", "ledger")
        _git(work, "push", "-qf", str(self.origin), "HEAD:refs/heads/paper-ledger")
        sha = _git(work, "rev-parse", "HEAD").strip()
        _git(work, "checkout", "-q", "main")
        return sha

    def remote_tip_files(self) -> dict[str, bytes]:
        names = _git(self.origin, "ls-tree", "--name-only", "paper-ledger").split()
        return {
            n: subprocess.run(
                ["git", "show", f"paper-ledger:{n}"],
                cwd=self.origin,
                check=True,
                capture_output=True,
            ).stdout
            for n in names
        }

    def remote_manifest(self) -> ls.Manifest:
        return ls.Manifest.from_json(self.remote_tip_files()[ls.MANIFEST_NAME])


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def _publish(repo: Repo, tmp: Path, runs: int, name: str | None = None) -> ls.Manifest:
    db = _make_db(tmp / f"src-{runs}.sqlite", runs)
    blob = _gz(db)
    name = name or ls.asset_name(NOW - timedelta(hours=1), runs)
    repo.runner.release_exists = True
    repo.runner.assets[name] = blob
    manifest = _manifest_for(name, blob, runs)
    repo.set_branch({ls.MANIFEST_NAME: manifest.to_json().encode(), "REPORT.txt": b"r\n"})
    return manifest


def _restore(repo: Repo, out: Path, **kw: object) -> ls.RestoreResult:
    return ls.restore(repo.runner, out, now=NOW, sleep=_no_sleep, **kw)  # type: ignore[arg-type]


# ----------------------------------------------------------------------- restore


def test_restore_manifest_happy_path(repo: Repo, tmp_path: Path) -> None:
    manifest = _publish(repo, tmp_path, runs=5)
    out = tmp_path / "ledger" / "paper.sqlite"
    result = _restore(repo, out)
    assert (result.source, result.runs) == ("manifest", 5)
    assert sqlite3.connect(out).execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 5
    state = ls.RestoreState.from_json((out.parent / ls.RESTORE_STATE_NAME).read_text())
    assert state.manifest == manifest and state.runs == 5
    assert not list(out.parent.glob(".restore-*"))


def _assert_fails_closed(repo: Repo, out: Path, match: str, **kw: object) -> None:
    with pytest.raises(ls.LedgerStoreError, match=match):
        _restore(repo, out, **kw)
    assert not out.exists(), "a failed restore must never leave a ledger file behind"
    assert not (out.parent / ls.RESTORE_STATE_NAME).exists()


def test_restore_sha_mismatch_fails(repo: Repo, tmp_path: Path) -> None:
    manifest = _publish(repo, tmp_path, runs=3)
    blob = bytearray(repo.runner.assets[manifest.asset])
    blob[-9] ^= 0xFF  # same size, different bytes
    repo.runner.assets[manifest.asset] = bytes(blob)
    _assert_fails_closed(repo, tmp_path / "ledger" / "paper.sqlite", "sha256")


def test_restore_size_mismatch_fails(repo: Repo, tmp_path: Path) -> None:
    manifest = _publish(repo, tmp_path, runs=3)
    repo.runner.assets[manifest.asset] += b"x"
    _assert_fails_closed(repo, tmp_path / "ledger" / "paper.sqlite", "bytes")


def test_restore_truncated_gz_fails(repo: Repo, tmp_path: Path) -> None:
    db = _make_db(tmp_path / "src.sqlite", 3)
    blob = _gz(db)[:-200]  # truncated, with a manifest that matches the truncated bytes
    name = ls.asset_name(NOW, 3)
    repo.runner.release_exists = True
    repo.runner.assets[name] = blob
    repo.set_branch({ls.MANIFEST_NAME: _manifest_for(name, blob, 3).to_json().encode()})
    _assert_fails_closed(repo, tmp_path / "ledger" / "paper.sqlite", "gunzip")


def test_restore_quick_check_failure_fails(repo: Repo, tmp_path: Path) -> None:
    db = _make_db(tmp_path / "src.sqlite", 40)
    raw = bytearray(db.read_bytes())
    page = 4096
    for offset in range(2 * page, len(raw) - page, page):  # scribble over b-tree pages
        raw[offset : offset + 64] = b"\xff" * 64
    blob = gzip.compress(bytes(raw), mtime=0)
    name = ls.asset_name(NOW, 40)
    repo.runner.release_exists = True
    repo.runner.assets[name] = blob
    repo.set_branch({ls.MANIFEST_NAME: _manifest_for(name, blob, 40).to_json().encode()})
    _assert_fails_closed(
        repo, tmp_path / "ledger" / "paper.sqlite", "quick_check|not a readable|malformed"
    )


def test_restore_runs_below_manifest_fails(repo: Repo, tmp_path: Path) -> None:
    db = _make_db(tmp_path / "src.sqlite", 4)
    blob = _gz(db)
    name = ls.asset_name(NOW, 9)
    repo.runner.release_exists = True
    repo.runner.assets[name] = blob
    repo.set_branch({ls.MANIFEST_NAME: _manifest_for(name, blob, 9).to_json().encode()})
    _assert_fails_closed(repo, tmp_path / "ledger" / "paper.sqlite", "fewer than the 9")


def test_restore_branch_missing_fails_without_bootstrap(repo: Repo, tmp_path: Path) -> None:
    _assert_fails_closed(repo, tmp_path / "ledger" / "paper.sqlite", "PBOT_LEDGER_BOOTSTRAP")


def test_restore_branch_missing_starts_empty_with_bootstrap(repo: Repo, tmp_path: Path) -> None:
    out = tmp_path / "ledger" / "paper.sqlite"
    result = _restore(repo, out, bootstrap=True)
    assert (result.source, result.runs) == ("bootstrap", 0)
    assert not out.exists()  # the Ledger creates it on the first cycle
    state = ls.RestoreState.from_json((out.parent / ls.RESTORE_STATE_NAME).read_text())
    assert state.source == "bootstrap" and state.manifest is None


def test_restore_network_error_on_ls_remote_fails(repo: Repo, tmp_path: Path) -> None:
    _publish(repo, tmp_path, runs=2)
    repo.runner.inject(["git", "ls-remote"], 128, 128, 128)
    _assert_fails_closed(repo, tmp_path / "ledger" / "paper.sqlite", "cannot reach origin")
    assert sum(c[:2] == ["git", "ls-remote"] for c in repo.runner.calls) == 3


def test_restore_network_error_does_not_bootstrap(repo: Repo, tmp_path: Path) -> None:
    repo.runner.inject(["git", "ls-remote"], 128, 128, 128)
    _assert_fails_closed(
        repo, tmp_path / "ledger" / "paper.sqlite", "cannot reach origin", bootstrap=True
    )


def test_restore_retries_a_flaky_download(repo: Repo, tmp_path: Path) -> None:
    _publish(repo, tmp_path, runs=2)
    repo.runner.inject(["gh", "release", "download"], 1, 1)
    assert _restore(repo, tmp_path / "ledger" / "paper.sqlite").runs == 2


def test_restore_download_failure_fails(repo: Repo, tmp_path: Path) -> None:
    _publish(repo, tmp_path, runs=2)
    repo.runner.inject(["gh", "release", "download"], 1, 1, 1, 1)
    _assert_fails_closed(repo, tmp_path / "ledger" / "paper.sqlite", "download")


def test_restore_legacy_gz_when_there_is_no_manifest(repo: Repo, tmp_path: Path) -> None:
    db = _make_db(tmp_path / "src.sqlite", 6)
    repo.set_branch({ls.LEGACY_GZ: _gz(db), "REPORT.txt": b"r\n"})
    out = tmp_path / "ledger" / "paper.sqlite"
    result = _restore(repo, out)
    assert (result.source, result.runs) == ("legacy_gz", 6)
    assert sqlite3.connect(out).execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 6


def test_restore_legacy_raw_sqlite(repo: Repo, tmp_path: Path) -> None:
    db = _make_db(tmp_path / "src.sqlite", 2)
    repo.set_branch({ls.LEGACY_RAW: db.read_bytes()})
    assert _restore(repo, tmp_path / "ledger" / "paper.sqlite").source == "legacy_raw"


def test_restore_require_manifest_rejects_the_legacy_layout(repo: Repo, tmp_path: Path) -> None:
    db = _make_db(tmp_path / "src.sqlite", 2)
    repo.set_branch({ls.LEGACY_GZ: _gz(db)})
    _assert_fails_closed(
        repo, tmp_path / "ledger" / "paper.sqlite", "no ledger_manifest", require_manifest=True
    )


def test_restore_branch_without_any_ledger_fails(repo: Repo, tmp_path: Path) -> None:
    repo.set_branch({"REPORT.txt": b"r\n"})
    _assert_fails_closed(repo, tmp_path / "ledger" / "paper.sqlite", "has no")


def test_failed_restore_keeps_an_existing_ledger(repo: Repo, tmp_path: Path) -> None:
    manifest = _publish(repo, tmp_path, runs=3)
    out = tmp_path / "ledger" / "paper.sqlite"
    out.parent.mkdir()
    out.write_bytes(b"previous")
    repo.runner.assets[manifest.asset] = b"\x1f\x8b" + b"0" * (manifest.gz_bytes - 2)
    with pytest.raises(ls.LedgerStoreError):
        _restore(repo, out)
    assert out.read_bytes() == b"previous"


# -------------------------------------------------------------------------- save


def _restored(repo: Repo, tmp_path: Path, runs: int) -> Path:
    _publish(repo, tmp_path, runs=runs)
    out = tmp_path / "ledger" / "paper.sqlite"
    _restore(repo, out)
    for name in ls.SMALL_FILES:
        (out.parent / name).write_text(f"{name}\n")
    return out


def _save(repo: Repo, db: Path, now: datetime = NOW) -> ls.SaveResult:
    return ls.save(repo.runner, db, db.parent, worktree=repo.clone / "wt", now=now, sleep=_no_sleep)


def _add_runs(db: Path, n: int) -> None:
    ledger = Ledger(db, Decimal("1000"))
    for i in range(n):
        ledger.record_run(["book_scanner"], 0, 0, 0, 0, at=NOW + timedelta(minutes=i))
    ledger.conn.close()


def test_save_round_trip_moves_the_database_off_the_branch(repo: Repo, tmp_path: Path) -> None:
    db = tmp_path / "ledger" / "paper.sqlite"
    db.parent.mkdir()
    repo.set_branch({ls.LEGACY_GZ: _gz(_make_db(tmp_path / "src.sqlite", 4)), "x.txt": b"1"})
    _restore(repo, db)
    for name in ls.SMALL_FILES:
        (db.parent / name).write_text(f"{name}\n")
    _add_runs(db, 1)

    result = _save(repo, db)
    assert result.uploaded and result.manifest.runs == 5
    tip = repo.remote_tip_files()
    assert not any(n.startswith("paper.sqlite") for n in tip), sorted(tip)
    assert set(ls.SMALL_FILES) <= set(tip)
    assert repo.remote_manifest() == result.manifest
    assert result.manifest.asset in repo.runner.assets
    assert result.manifest.asset == "paper-20261109T120000Z-r5.sqlite.gz"
    assert not list(db.parent.glob("*.sqlite.gz")), "the local upload copy is removed"

    # A fresh checkout restores the same database from the release.
    fresh = tmp_path / "fresh" / "paper.sqlite"
    again = ls.restore(FakeRunnerFor(repo), fresh, now=NOW, sleep=_no_sleep)
    assert (again.source, again.runs) == ("manifest", 5)


def FakeRunnerFor(repo: Repo) -> FakeRunner:  # noqa: N802 - reads like a constructor
    """A second checkout of the same origin sharing the same fake release."""
    clone = repo.clone.parent / f"clone-{len(repo.runner.calls)}"
    subprocess.run(
        ["git", "clone", "-q", "--single-branch", "-b", "main", str(repo.origin), str(clone)],
        check=True,
    )
    runner = FakeRunner(clone)
    runner.release_exists = repo.runner.release_exists
    runner.assets = repo.runner.assets
    return runner


def test_save_creates_the_release_on_first_save(repo: Repo, tmp_path: Path) -> None:
    db = tmp_path / "ledger" / "paper.sqlite"
    _restore(repo, db, bootstrap=True)
    _make_db(db, 1)
    result = _save(repo, db)
    assert repo.runner.release_exists and result.uploaded
    create = next(c for c in repo.runner.calls if c[:3] == ["gh", "release", "create"])
    assert "--prerelease" in create and "--latest=false" in create
    assert create[create.index("--target") + 1] == "main"
    assert "--clobber" not in next(c for c in repo.runner.calls if c[2:3] == ["upload"])
    assert repo.remote_manifest().runs == 1


def test_save_refuses_fewer_runs_than_restored(repo: Repo, tmp_path: Path) -> None:
    db = _restored(repo, tmp_path, runs=5)
    before = repo.remote_manifest()
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM runs WHERE id IN (SELECT id FROM runs LIMIT 2)")
    conn.commit()
    conn.close()
    with pytest.raises(ls.LedgerStoreError, match="fewer than the 5"):
        _save(repo, db)
    assert repo.remote_manifest() == before
    assert not any(c[2:3] == ["upload"] for c in repo.runner.calls)


def test_save_refuses_an_empty_database(repo: Repo, tmp_path: Path) -> None:
    db = _restored(repo, tmp_path, runs=2)
    db.write_bytes(b"")
    with pytest.raises(ls.LedgerStoreError, match="missing or empty"):
        _save(repo, db)


def test_save_without_a_restore_fails(repo: Repo, tmp_path: Path) -> None:
    db = _make_db(tmp_path / "ledger" / "paper.sqlite", 2)
    with pytest.raises(ls.LedgerStoreError, match="ledger-restore"):
        _save(repo, db)


def test_save_push_failure_leaves_the_previous_manifest(repo: Repo, tmp_path: Path) -> None:
    db = _restored(repo, tmp_path, runs=3)
    before = repo.remote_manifest()
    state_before = (db.parent / ls.RESTORE_STATE_NAME).read_text()
    _add_runs(db, 1)
    repo.runner.inject(["git", "push"], 1, 1, 1)
    with pytest.raises(ls.LedgerStoreError, match="git push"):
        _save(repo, db)
    assert repo.remote_manifest() == before
    assert (db.parent / ls.RESTORE_STATE_NAME).read_text() == state_before
    assert not any(c[2:3] == ["delete-asset"] for c in repo.runner.calls), "no prune"
    assert before.asset in repo.runner.assets


def test_save_push_failure_exits_non_zero_from_the_cli(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import typer

    import prediction_bot.cli as cli

    db = _restored(repo, tmp_path, runs=3)
    _add_runs(db, 1)
    repo.runner.inject(["git", "push"], 1, 1, 1)
    monkeypatch.setattr(ls, "SubprocessRunner", lambda: repo.runner)
    monkeypatch.setattr(ls.time, "sleep", _no_sleep)
    monkeypatch.chdir(repo.clone)
    with pytest.raises(typer.Exit) as exc:
        cli.ledger_save(db=db, dir=db.parent, worktree=Path("wt"))
    assert exc.value.exit_code == 1


def test_upload_is_confirmed_even_if_the_first_upload_call_errors(
    repo: Repo, tmp_path: Path
) -> None:
    db = _restored(repo, tmp_path, runs=2)
    _add_runs(db, 1)
    repo.runner.inject(["gh", "release", "upload"], 1)
    result = _save(repo, db)
    assert result.manifest.asset in repo.runner.assets


def test_unconfirmed_upload_fails_before_the_branch_moves(repo: Repo, tmp_path: Path) -> None:
    db = _restored(repo, tmp_path, runs=2)
    before = repo.remote_manifest()
    _add_runs(db, 1)
    repo.runner.inject(["gh", "release", "upload"], 1, 1, 1, 1)
    with pytest.raises(ls.LedgerStoreError, match="not confirmed"):
        _save(repo, db)
    assert repo.remote_manifest() == before


def test_unchanged_database_is_not_uploaded_again(repo: Repo, tmp_path: Path) -> None:
    db = _restored(repo, tmp_path, runs=2)
    restored = repo.remote_manifest()
    same = _save(repo, db)
    assert not same.uploaded and same.manifest.asset == restored.asset
    _add_runs(db, 1)
    changed = _save(repo, db, now=NOW + timedelta(minutes=10))
    again = _save(repo, db, now=NOW + timedelta(minutes=20))
    assert changed.uploaded and not again.uploaded
    assert again.manifest.asset == changed.manifest.asset == repo.remote_manifest().asset


def test_prune_never_deletes_the_manifest_asset(repo: Repo, tmp_path: Path) -> None:
    db = _restored(repo, tmp_path, runs=2)
    old = NOW - timedelta(days=30)
    for i in range(25):
        repo.runner.assets[ls.asset_name(old - timedelta(hours=i), 1)] = b"old"
    repo.runner.assets["notes.txt"] = b"not ours"
    _add_runs(db, 1)
    result = _save(repo, db)
    assert result.manifest.asset in repo.runner.assets
    assert "notes.txt" in repo.runner.assets
    # 27 assets: the new one, the restored one, 25 old. The 10 newest are the first two
    # plus 8 old ones; no old one is within 7 days.
    assert len(result.pruned) == 25 - (ls.KEEP_NEWEST - 2)
    assert not result.prune_errors


def test_prune_failure_does_not_fail_the_save(repo: Repo, tmp_path: Path) -> None:
    db = _restored(repo, tmp_path, runs=2)
    for i in range(15):
        repo.runner.assets[ls.asset_name(NOW - timedelta(days=20, hours=i), 1)] = b"old"
    repo.runner.inject(["gh", "release", "delete-asset"], *([1] * 50))
    _add_runs(db, 1)
    result = _save(repo, db)
    assert result.prune_errors and not result.pruned
    assert repo.remote_manifest() == result.manifest


def test_prune_view_failure_does_not_fail_the_save(repo: Repo, tmp_path: Path) -> None:
    db = _restored(repo, tmp_path, runs=2)
    _add_runs(db, 1)
    # Upload confirmation needs view to work; break it only after the push.
    original = repo.runner.run

    def run(argv: Sequence[str], cwd: Path | None = None) -> ls.CmdResult:
        pushed = any(c[:2] == ["git", "push"] for c in repo.runner.calls)
        if pushed and list(argv[:3]) == ["gh", "release", "view"]:
            repo.runner.calls.append(list(argv))
            return ls.CmdResult(1, b"", b"api down")
        return original(argv, cwd)

    repo.runner.run = run  # type: ignore[method-assign]
    result = _save(repo, db)
    assert result.prune_errors and repo.remote_manifest() == result.manifest


# -------------------------------------------------------------------- pure logic


def test_select_prunable_keeps_newest_daily_and_current() -> None:
    names = [ls.asset_name(NOW - timedelta(hours=6 * i), i) for i in range(60)]  # 15 days
    keep = names[-1]  # oldest is current, e.g. after failed pushes
    prunable = set(ls.select_prunable(names, keep, NOW))
    kept = set(names) - prunable
    assert keep in kept
    assert set(names[:10]) <= kept
    days = {ls.parse_asset_time(n).date() for n in kept if n != keep}  # type: ignore[union-attr]
    assert {(NOW - timedelta(days=d)).date() for d in range(7)} <= days
    # The 10 newest span Nov 7-9; Nov 3-6 add one each; plus the manifest's asset.
    assert len(kept) == 10 + 4 + 1
    assert ls.select_prunable(["other.zip", keep], keep, NOW) == []


def test_asset_name_round_trips() -> None:
    name = ls.asset_name(NOW, 1234)
    assert name == "paper-20261109T120000Z-r1234.sqlite.gz"
    assert ls.parse_asset_time(name) == NOW
    assert ls.parse_asset_time("paper.sqlite.gz") is None


def test_decide_restore_source_prefers_the_manifest() -> None:
    assert ls.decide_restore_source([ls.LEGACY_GZ, ls.MANIFEST_NAME]) == "manifest"
    assert ls.decide_restore_source([ls.LEGACY_RAW, ls.LEGACY_GZ]) == "legacy_gz"
    assert ls.decide_restore_source([ls.LEGACY_RAW]) == "legacy_raw"
    assert ls.decide_restore_source(["REPORT.txt"]) is None


def test_gzip_file_is_deterministic_like_gzip_minus_n(tmp_path: Path) -> None:
    src = _make_db(tmp_path / "a.sqlite", 3)
    first, second = tmp_path / "1.gz", tmp_path / "2.gz"
    ls.gzip_file(src, first)
    ls.gzip_file(src, second)
    blob = first.read_bytes()
    assert blob == second.read_bytes()
    assert blob[3] == 0  # FLG: no stored file name
    assert blob[4:8] == b"\x00\x00\x00\x00"  # MTIME 0
    plain = subprocess.run(["gzip", "-dc", str(first)], capture_output=True, check=True)
    assert plain.stdout == src.read_bytes()


def test_manifest_rejects_bad_json() -> None:
    with pytest.raises(ls.LedgerStoreError):
        ls.Manifest.from_json("{}")
    with pytest.raises(ls.LedgerStoreError):
        ls.Manifest.from_json("not json")


# ------------------------------------------------------------------ health / cli


def test_health_reads_the_manifest_and_checks_slots(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    import typer

    import prediction_bot.cli as cli

    now = datetime.now(UTC)
    m = ls.Manifest(
        asset=ls.asset_name(now, 9),
        sha256="0" * 64,
        gz_bytes=1,
        runs=9,
        last_run_at=(now - timedelta(minutes=12)).isoformat(),
        slots_last_24h=40,
        written_at=now.isoformat(),
    )
    path = tmp_path / ls.MANIFEST_NAME
    path.write_text(m.to_json())
    cli.health(max_age_minutes=40, min_slots=36, manifest=path)
    assert "40 of 48 half-hour slots" in capsys.readouterr().out
    with pytest.raises(typer.Exit):
        cli.health(max_age_minutes=40, min_slots=41, manifest=path)
    assert "below 41 of 48" in capsys.readouterr().out
    with pytest.raises(typer.Exit):
        cli.health(max_age_minutes=5, min_slots=0, manifest=path)


def test_slots_last_24h_counts_untriggered_half_hours(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "s.sqlite", Decimal("1000"))
    for minutes in (5, 20, 40, 70):  # buckets: 11:30, 11:30, 11:00, 10:30 -> 3
        ledger.record_run(["book_scanner"], 0, 0, 0, 0, at=NOW - timedelta(minutes=minutes))
    ledger.record_run(["x"], 0, 0, 0, 0, at=NOW - timedelta(hours=5), trigger_reason="plan:x")
    ledger.record_run(["book_scanner"], 0, 0, 0, 0, at=NOW - timedelta(hours=30))
    assert ls.slots_last_24h(ledger.conn, NOW) == 3


# --------------------------------------------------------------------- workflows


def _workflows() -> dict[str, str]:
    return {p.name: p.read_text() for p in (ROOT / ".github/workflows").glob("*.yml")}


def test_no_workflow_fetches_paper_ledger_history() -> None:
    for name, text in _workflows().items():
        for line in text.splitlines():
            if "git fetch" in line and "paper-ledger" in line:
                assert "--depth=1" in line, f"{name}: {line.strip()}"


def test_no_step_writes_the_ledger_with_or_true() -> None:
    for name, text in _workflows().items():
        for line in text.splitlines():
            if "ledger/paper.sqlite" in line:
                assert "|| true" not in line, f"{name}: {line.strip()}"
