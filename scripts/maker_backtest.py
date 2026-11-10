"""Maker backtest from Kalshi's public trade tape (read-only, paper only).

Rests a hypothetical bid on the sharp-favoured side at every v3 near-miss decision
(`shadow_decisions`, reason net_edge or entry_window) and scores fills against the
public trade tape. Writes summary.md, summary.json and orders.csv to --out-dir.

    python scripts/maker_backtest.py --db ledger/paper.sqlite --cache trade_cache.sqlite

The ledger is opened with mode=ro. Only Kalshi's public endpoints are called (no
odds credits, no API key). --max-calls 0 runs from the cache alone.
"""

from __future__ import annotations

import argparse
import logging
import sys
from decimal import Decimal
from pathlib import Path

from prediction_bot import maker_backtest as mb
from prediction_bot import maker_backtest_io as bt_io
from prediction_bot.config import KALSHI_PROD_REST
from prediction_bot.venues.kalshi import PoliteFetcher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, required=True, help="paper ledger (read-only)")
    parser.add_argument("--cache", type=Path, default=Path("trade_cache.sqlite"))
    parser.add_argument("--since", default=bt_io.DEFAULT_SINCE, help="first decision day (UTC)")
    parser.add_argument("--until", default=None, help="last decision day, inclusive (UTC)")
    parser.add_argument(
        "--max-calls", type=int, default=6000, help="request budget; 0 = cache only"
    )
    parser.add_argument("--out-dir", type=Path, default=Path("reports/maker_backtest/local"))
    parser.add_argument("--base-url", default=KALSHI_PROD_REST)
    parser.add_argument("--ttl-minutes", type=int, default=mb.DEFAULT_TTL_MINUTES)
    parser.add_argument("--max-notional", type=Decimal, default=mb.DEFAULT_MAX_NOTIONAL)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    since = bt_io.parse_day(args.since)
    if since is None:
        parser.error("--since is required")
    until = bt_io.parse_day(args.until)
    if until is not None and "T" not in args.until:
        until = bt_io.day_after(until)
    if not args.db.exists():
        parser.error(f"{args.db} does not exist")
    fetcher = PoliteFetcher(args.base_url, max_calls=args.max_calls) if args.max_calls > 0 else None
    output = bt_io.run(
        args.db,
        args.cache,
        since=since,
        until=until,
        fetcher=fetcher,
        ttl_minutes=args.ttl_minutes,
        max_notional=args.max_notional,
    )
    paths = bt_io.write_outputs(output, args.out_dir)
    summary = output.summary
    print(("INVALID: " if summary.invalid else "") + summary.verdict)
    for name, path in paths.items():
        print(f"wrote {name}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
