from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_audit_cli_reproduces_documented_ledger_report() -> None:
    db = ROOT / "tests" / "fixtures" / "audit_paper.sqlite"
    doc = ROOT / "docs" / "AUDIT_2026-10-23.md"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/audit_ledger.py",
            "--db",
            str(db),
            "--runs",
            "200000",
            "--seed",
            "7",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    report = doc.read_text()
    expected = report[report.index("## Totals") :]
    assert completed.stdout == expected
    assert completed.stderr == ""
