from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import typer

from prediction_bot.config import Settings
from prediction_bot.paper.ledger import Ledger

ROOT = Path(__file__).parents[1]
UTC = timezone.utc


def _ledger_with_runs(tmp_path: Path, runs: list[tuple[timedelta, str, str | None]]) -> Path:
    path = tmp_path / "runs.sqlite"
    ledger = Ledger(path, Decimal("1000"))
    now = datetime.now(UTC)
    for ago, strategies, reason in runs:
        ledger.record_run(strategies.split(","), 0, 0, 0, 0, at=now - ago, trigger_reason=reason)
    return path


def _use(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    import prediction_bot.cli as cli

    monkeypatch.setattr(cli, "_settings", lambda: Settings(_env_file=None, paper_db_path=path))  # type: ignore[call-arg]


def test_slot_guard_skips_a_slot_that_already_ran(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import prediction_bot.cli as cli

    path = _ledger_with_runs(
        tmp_path,
        [
            (timedelta(minutes=5), "book_scanner", None),
            (timedelta(minutes=4), "sportsbook_v3", "plan:americanfootball_nfl"),  # not a slot
            (timedelta(minutes=50), "sportsbook_v3", None),  # previous sportsbook slot
        ],
    )
    _use(monkeypatch, path)
    cli.slot_guard(slot="scanner", within_minutes=20)
    cli.slot_guard(slot="sportsbook", within_minutes=20)
    assert capsys.readouterr().out.split() == ["skip", "run"]
    with pytest.raises(typer.BadParameter):
        cli.slot_guard(slot="nightly", within_minutes=20)


def test_health_fails_when_the_last_run_is_stale(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import prediction_bot.cli as cli

    fresh = _ledger_with_runs(tmp_path, [(timedelta(minutes=20), "book_scanner", None)])
    _use(monkeypatch, fresh)
    cli.health(max_age_minutes=90)
    assert "1 of 48 half-hour slots ran in the last 24h" in capsys.readouterr().out

    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    stale = _ledger_with_runs(stale_dir, [(timedelta(hours=3), "book_scanner", None)])
    _use(monkeypatch, stale)
    with pytest.raises(typer.Exit) as exc:
        cli.health(max_age_minutes=90)
    assert exc.value.exit_code == 1

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    _use(monkeypatch, empty_dir / "none.sqlite")
    with pytest.raises(typer.Exit):
        cli.health(max_age_minutes=90)


def test_paper_workflow_takes_the_slot_from_the_dispatch_input() -> None:
    workflow = (ROOT / ".github/workflows/paper.yml").read_text()
    assert "    inputs:\n      slot:" in workflow
    assert (
        "SPORTSBOOK_SLOT: ${{ inputs.slot == 'sportsbook' || (github.event_name == "
        "'workflow_dispatch' && inputs.slot == '') || github.event.schedule == '17 */2 * * *' }}"
    ) in workflow
    assert (
        'DECISION="$(pbot slot-guard --slot "$SLOT" --within-minutes 25 --plan-due)"'
    ) in workflow
    assert "      target:" in workflow and 'pbot pull-due --target "$TARGET"' in workflow
    # Run cycle, archive old rows, save: none of them on a light tick.
    assert workflow.count("if: steps.guard.outputs.skip != 'true'") == 3
    # The GitHub schedule stays as the fallback, off :00.
    for cron in ('"17 */2 * * *"', '"47 * * * *"', '"17 1-23/2 * * *"'):
        assert f"- cron: {cron}" in workflow


def test_health_workflow_opens_one_missed_runs_issue() -> None:
    workflow = (ROOT / ".github/workflows/health.yml").read_text()
    assert '- cron: "33 * * * *"' in workflow
    assert 'pbot health --max-age-minutes "$MAX_AGE_MINUTES"' in workflow
    assert "vars.PBOT_HEALTH_MAX_AGE_MINUTES || '90'" in workflow and "shell: bash" in workflow
    assert 'TITLE="paper bot missed runs"' in workflow
    assert "gh issue comment" in workflow and "gh issue create" in workflow
    assert "issues: write" in workflow


def test_cloudflare_worker_config_and_unit_tests() -> None:
    config = (ROOT / "infra/cloudflare-trigger/wrangler.toml").read_text()
    assert 'crons = ["7-57/10 * * * *"]' in config  # Phase 3-E: every 10 min, off :00/:30
    assert "GITHUB_TOKEN" in config and "secret" in config
    assert "ghp_" not in config and "github_pat_" not in config
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; run `node --test` in infra/cloudflare-trigger")
    result = subprocess.run(
        [node, "--test", "worker.test.mjs"],
        cwd=ROOT / "infra/cloudflare-trigger",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ledger_lives_on_the_release_not_in_branch_history() -> None:
    """Every committed ~43 MB paper.sqlite.gz stayed in git history for good (PR #73)."""
    paper = (ROOT / ".github/workflows/paper.yml").read_text()
    health = (ROOT / ".github/workflows/health.yml").read_text()
    steps = paper.index("    steps:")
    restore = paper.index("pbot ledger-restore --out ledger/paper.sqlite")
    guard = paper.index("pbot slot-guard", steps)
    assert steps < restore < guard < paper.index("pbot paper -s book_scanner")
    assert paper.index("pbot paper -s book_scanner") < paper.index("pbot ledger-save", steps)
    assert "timeout-minutes: 5" in paper[paper.index("- name: Restore ledger") : restore]
    assert paper.count("GH_TOKEN: ${{ github.token }}") == 3  # restore, archive, save
    assert "gzip -9 -n -c ledger/paper.sqlite > wt/paper.sqlite.gz" not in paper
    assert "git add paper.sqlite.gz" not in paper
    assert "--force" not in paper
    # health reads the manifest, falls back to the old database, and opens an issue on
    # any failed or hung step.
    assert "git show paper-ledger:ledger_manifest.json > ledger/ledger_manifest.json" in health
    assert '--min-slots "$MIN_SLOTS"' in health
    assert "vars.PBOT_HEALTH_MIN_SLOTS || '36'" in health
    assert "if: failure()\n" in health and "steps.check.outcome == 'failure'" not in health
    assert health.count("timeout-minutes: 2") == 2
    assert "Failed step: %s" in health


def test_ledger_reset_workflow_is_manual_and_guarded() -> None:
    reset = (ROOT / ".github/workflows/ledger-reset-history.yml").read_text()
    on = reset[reset.index("\non:") : reset.index("concurrency:")]
    assert "workflow_dispatch:" in on and "schedule" not in on and "push" not in on
    assert 'if [ "$CONFIRM" != "RESET" ]' in reset
    assert "group: paper-ledger" in reset and "cancel-in-progress: false" in reset
    assert "pbot ledger-restore --out verify/paper.sqlite --require-manifest" in reset
    assert "grep -E '^paper\\.sqlite'" in reset
    assert 'git commit-tree "${TIP}^{tree}"' in reset
    assert '--force-with-lease="paper-ledger:${TIP}"' in reset
    assert "--force " not in reset and "push -f" not in reset
