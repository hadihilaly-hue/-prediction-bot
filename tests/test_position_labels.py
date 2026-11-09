"""Plain-English labels for open positions (REPORT.txt, PREDICTIONS.md, dashboard)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from prediction_bot.cli import _open_positions_markdown
from prediction_bot.models import OutcomeSide
from prediction_bot.paper.ledger import Ledger


def _ledger(tmp_path: Path) -> Ledger:
    ledger = Ledger(tmp_path / "labels.sqlite", Decimal("1000"))
    ledger.conn.executemany(
        "INSERT INTO markets (ticker,title,subtitle) VALUES (?,?,?)",
        [
            ("KXNCAAFGAME-26NOV14USCIND-IND", "Indiana wins", "Indiana"),
            ("KXNCAAFGAME-26NOV14FRESTXST-FRES", "Fresno St. wins", "Fresno St."),
            ("KXEPLGAME-26NOV22ARSTOT-TIE", "Tie", "Tie"),
        ],
    )
    ledger.remember_event_titles(
        [
            ("KXNCAAFGAME-26NOV14USCIND", "USC vs Indiana", "USC vs IND (Nov 14)"),
            ("KXNCAAFGAME-26NOV14FRESTXST", "Fresno St. vs Texas St.", "FRES vs TXST (Nov 14)"),
        ]
    )
    ledger.conn.commit()
    return ledger


def test_position_label_reads_like_the_bet(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    assert ledger.position_label("KXNCAAFGAME-26NOV14USCIND-IND", OutcomeSide.yes) == (
        "Indiana wins",
        "USC vs Indiana (Nov 14)",
    )
    assert ledger.position_label("KXNCAAFGAME-26NOV14FRESTXST-FRES", OutcomeSide.no) == (
        "Fresno St. does NOT win",
        "Fresno St. vs Texas St. (Nov 14)",
    )
    # No "X wins" title: say what is being bet against; no event title: the event ticker.
    assert ledger.position_label("KXEPLGAME-26NOV22ARSTOT-TIE", OutcomeSide.no) == (
        "NOT: Tie",
        "KXEPLGAME-26NOV22ARSTOT",
    )
    # Unknown market: fall back to the side and ticker rather than guess.
    assert ledger.position_label("KXNFLGAME-26NOV15ABCDEF-ABC", OutcomeSide.yes)[0] == (
        "YES on KXNFLGAME-26NOV15ABCDEF-ABC"
    )


def test_predictions_markdown_lists_open_positions_in_plain_english(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.conn.execute(
        "INSERT INTO fills (created_at,strategy,ticker,side,price,count,fee,fair_prob) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            datetime(2026, 11, 9, 2, 21, tzinfo=timezone.utc).isoformat(),
            "sportsbook_v3",
            "KXNCAAFGAME-26NOV14USCIND-IND",
            "yes",
            "0.69",
            "35",
            "0.53",
            "0.732",
        ),
    )
    ledger.conn.commit()
    lines = _open_positions_markdown(ledger)
    assert lines[0] == "## Open positions (1)"
    row = next(line for line in lines if "Indiana wins" in line)
    assert "USC vs Indiana \\(Nov 14\\)" in row and "| 35 |" in row and "73%" in row
    assert "`KXNCAAFGAME-26NOV14USCIND-IND`" in row
