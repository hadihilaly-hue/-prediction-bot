"""One-shot injury backfill.

ESPN's endpoint only shows the *current* injury list, so there is no history to
replay for past fills: this fetches today's lists once per sport, snapshots them
into injury_snapshots, and tags open positions with it. Settled fills whose
games already played stay tagged 'unknown'. Run it once after deploying the
injury code:

    python scripts/backfill_injuries.py [--db data/paper.sqlite]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal

from prediction_bot.injuries import InjuryClient, snapshot_injuries
from prediction_bot.paper.ledger import Ledger


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/paper.sqlite", help="paper ledger path")
    args = parser.parse_args()

    ledger = Ledger(args.db, Decimal("0"))
    client = InjuryClient()
    n = snapshot_injuries(ledger, client, run_id=None, at=datetime.now(timezone.utc))
    open_tickers = ledger.unsettled_tickers()
    tagged = ledger.tag_injuries(open_tickers)
    print(
        f"snapshotted {n} injury rows; tagged {tagged} fills across {len(open_tickers)} open tickers"
    )
    print("Note: settled fills keep injury_flag=NULL — ESPN has no history to backfill.")


if __name__ == "__main__":
    main()
