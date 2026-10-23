"""Reproduce the 2026-10-23 ledger audit from the paper ledger (read-only).

Usage: python scripts/audit_ledger.py --db ledger/paper.sqlite [--runs 200000] [--seed 7]

Every fill is assigned a canonical position key (event, winning outcome): on a two-way
game "A yes" and "B no" are the same bet, and fills from different strategies on the
same key are one position. Start times come only from verifiable sources: the ET time
encoded in MLB tickers, and published kickoffs listed in KNOWN_STARTS. Anything else is
"unknown" (never guessed).
"""

from __future__ import annotations

import argparse
import random
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
THREE_WAY = {
    "KXMLSGAME", "KXEPLGAME", "KXLALIGAGAME", "KXSERIEAGAME", "KXBUNDESLIGAGAME",
    "KXLIGUE1GAME", "KXUCLGAME", "KXUELGAME", "KXUECLGAME", "KXLIGAMXGAME",
}  # fmt: skip
# Published kickoffs (UTC) for games without a time in the ticker; sources in the brief.
KNOWN_STARTS = {
    "KXNFLGAME-26OCT04KCLV": datetime(2026, 10, 4, 20, 25, tzinfo=timezone.utc),
    "KXNFLGAME-26OCT04DENSF": datetime(2026, 10, 4, 20, 25, tzinfo=timezone.utc),
    "KXNFLGAME-26OCT04MIAMIN": datetime(2026, 10, 4, 20, 5, tzinfo=timezone.utc),
}
# No start time, but the quoted prices only make sense once the game has started.
INFERRED_IN_PLAY = {
    "KXNHLGAME-26OCT10EDMSJ": "Edmonton NO at 1c vs consensus 0.956",
    "KXNCAAFGAME-26OCT03AUBTENN": "Auburn at 7c vs consensus 0.094",
}
# The two pre-game outcomes the brief singles out (Oklahoma over Texas, South Carolina).
BIG_WINS = {("KXNCAAFGAME-26OCT10TEXOKLA", "OKLA"), ("KXNCAAFGAME-26OCT10SCARFLA", "SCAR")}
MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    )
}
EVENT_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{4})?([A-Z0-9]+)$")


@dataclass
class Fill:
    id: int
    created_at: datetime
    strategy: str
    ticker: str
    side: str
    price: float
    count: float
    fee: float
    fair: float
    result: str | None

    @property
    def event(self) -> str:
        return self.ticker.rsplit("-", 1)[0]

    @property
    def series(self) -> str:
        return self.ticker.split("-")[0]

    @property
    def cost(self) -> float:
        return self.price * self.count + self.fee

    @property
    def won(self) -> bool | None:
        return None if self.result is None else self.result == self.side

    @property
    def pnl(self) -> float | None:
        return None if self.won is None else (self.count if self.won else 0.0) - self.cost


def parse_event(event: str) -> tuple[datetime | None, datetime | None, str]:
    """(game date as ET midnight, MLB ET start or None, team-code string)."""
    code = event.split("-", 1)[1] if "-" in event else ""
    m = EVENT_RE.match(code)
    if not m:
        return None, None, code
    yy, mon, dd, hhmm, teams = m.groups()
    day = datetime(2000 + int(yy), MONTHS[mon], int(dd), tzinfo=ET)
    start = None
    if hhmm and event.startswith("KXMLBGAME"):
        start = day.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:])).astimezone(timezone.utc)
    return day, start, teams


def outcome_key(f: Fill) -> tuple[str, str]:
    """Canonical (event, outcome). Two-way: 'B no' == 'A yes'. Three-way: 'A no' is its own."""
    team = f.ticker.rsplit("-", 1)[1]
    if f.side == "yes":
        return f.event, team
    if f.series in THREE_WAY:
        return f.event, f"not {team}"
    _, _, teams = parse_event(f.event)
    if teams.startswith(team):
        return f.event, teams[len(team) :]
    if teams.endswith(team):
        return f.event, teams[: -len(team)]
    return f.event, f"not {team}"


def start_time(event: str) -> tuple[datetime | None, str]:
    if event in KNOWN_STARTS:
        return KNOWN_STARTS[event], "published kickoff"
    _, start, _ = parse_event(event)
    if start is not None:
        return start, "MLB ticker (ET)"
    return None, "unknown"


def load(db: str) -> list[Fill]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT f.id, f.created_at, f.strategy, f.ticker, f.side, f.price, f.count, f.fee,"
        " f.fair_prob, s.result FROM fills f LEFT JOIN settlements s ON s.ticker = f.ticker"
        " ORDER BY f.id"
    ).fetchall()
    out = []
    for r in rows:
        ts = datetime.fromisoformat(r[1])
        ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)  # ledger stores naive UTC
        out.append(Fill(r[0], ts, r[2], r[3], r[4], *map(float, r[5:9]), r[9]))
    return out


@dataclass
class Position:
    key: tuple[str, str]
    fills: list[Fill]

    @property
    def count(self) -> float:
        return sum(f.count for f in self.fills)

    @property
    def avg_price(self) -> float:
        return sum(f.price * f.count for f in self.fills) / self.count

    @property
    def avg_fair(self) -> float:
        return sum(f.fair * f.count for f in self.fills) / self.count

    @property
    def pnl(self) -> float:
        return sum(f.pnl or 0.0 for f in self.fills)

    @property
    def won(self) -> bool | None:
        return self.fills[0].won


def positions(fills: list[Fill]) -> list[Position]:
    by: dict[tuple[str, str], list[Fill]] = defaultdict(list)
    for f in fills:
        by[outcome_key(f)].append(f)
    return [Position(k, v) for k, v in by.items()]


def band(p: float) -> str:
    c = round(p * 100, 6)
    edges = ((0, 10, "<10c"), (10, 20, "10-19c"), (20, 35, "20-34c"), (35, 60, "35-59c"))
    for lo, hi, name in edges:
        if lo <= c < hi:
            return name
    return "60c+"


def brier(fills: list[Fill], forecast: str) -> float:
    return sum((getattr(f, forecast) - (1.0 if f.won else 0.0)) ** 2 for f in fills) / len(fills)


def bootstrap_brier_diff(fills: list[Fill], runs: int, rng: random.Random) -> tuple[float, float]:
    """Game-level bootstrap of mean(bot sq err) - mean(market sq err)."""
    games: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for f in fills:
        y = 1.0 if f.won else 0.0
        games[f.event].append(((f.fair - y) ** 2, (f.price - y) ** 2))
    keys = list(games)
    diffs = []
    for _ in range(runs):
        b = m = 0.0
        n = 0
        for _ in keys:
            for sb, sm in games[rng.choice(keys)]:
                b, m, n = b + sb, m + sm, n + 1
        diffs.append((b - m) / n)
    diffs.sort()
    return diffs[int(0.025 * runs)], diffs[int(0.975 * runs) - 1]


def monte_carlo(fills: list[Fill], prob: str, realized: float, runs: int, rng: random.Random):
    """One uniform per game decides every fill in it: fills backing the game's reference
    outcome win iff u < p, fills backing anything else win iff u > 1 - p, so both-sides
    and two-strategy fills stay perfectly correlated."""
    games: dict[str, list[tuple[bool, float, float, float]]] = defaultdict(list)
    ref: dict[str, tuple[str, str]] = {}
    for f in fills:
        k = outcome_key(f)
        ref.setdefault(f.event, k)
        games[f.event].append((k == ref[f.event], getattr(f, prob), f.count, f.cost))
    glist = list(games.values())
    hits, total = 0, 0.0
    for _ in range(runs):
        pnl = 0.0
        for legs in glist:
            u = rng.random()
            for is_ref, p, cnt, cost in legs:
                win = u < p if is_ref else u > 1 - p
                pnl += (cnt if win else 0.0) - cost
        total += pnl
        hits += pnl >= realized - 1e-9
    return hits / runs, total / runs


def table(rows: list[list[object]], head: list[str]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/paper.sqlite")
    ap.add_argument("--runs", type=int, default=200_000)
    ap.add_argument("--bootstrap", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    fills = load(args.db)
    settled = [f for f in fills if f.won is not None]
    realized = sum(f.pnl or 0.0 for f in settled)
    fees = sum(f.fee for f in fills)
    open_cost = sum(f.cost for f in fills if f.won is None)
    pos = [p for p in positions(settled)]
    pos.sort(key=lambda p: -p.pnl)
    p_out: list[str] = []
    say = p_out.append

    say("## Totals (reconcile to the dashboard)")
    say(table([[len(fills), len(settled), len(fills) - len(settled),
                sum(1 for f in settled if f.won), f"{realized:.2f}", f"{fees:.2f}",
                f"{open_cost:.2f}"]],
              ["fills", "settled", "open", "winning fills", "realized P&L $", "fees $",
               "open cost $"]))  # fmt: skip

    say("\n## Positions (canonical key = event + winning outcome)")
    by_strat: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for f in settled:
        by_strat[f.strategy].add(outcome_key(f))
    shared = by_strat["underdog_value"] & by_strat["sportsbook_arb"]
    wins = sum(1 for p in pos if p.won)
    say(f"- {len(settled)} settled fills collapse to {len(pos)} positions; {wins} won.")
    say(f"- underdog_value positions: {len(by_strat['underdog_value'])}, of which "
        f"{len(shared)} also held by sportsbook_arb.")  # fmt: skip
    say(
        f"- Expected wins: market {sum(p.avg_price for p in pos):.1f},"
        f" bot fair {sum(p.avg_fair for p in pos):.1f}."
    )
    top = pos[:3]
    say(
        f"- Top 3 positions: ${sum(p.pnl for p in top):.2f}; other {len(pos) - 3}:"
        f" ${sum(p.pnl for p in pos[3:]):.2f}."
    )
    say(table([[p.key[0], p.key[1], f"{p.count:.0f}", f"{p.avg_price * 100:.1f}",
                f"{p.avg_fair:.3f}", "W" if p.won else "L", f"{p.pnl:.2f}",
                ",".join(sorted({f.strategy for f in p.fills}))] for p in pos],
              ["event", "outcome", "contracts", "avg price c", "avg fair", "result", "P&L $",
               "strategies"]))  # fmt: skip

    say("\n## Price bands (by position, count-weighted average entry)")
    bands: dict[str, list[Position]] = defaultdict(list)
    for p in pos:
        bands[band(p.avg_price)].append(p)
    order = ["<10c", "10-19c", "20-34c", "35-59c", "60c+"]
    say(table([[b, len(bands[b]), sum(1 for p in bands[b] if p.won),
                f"{sum(p.avg_price for p in bands[b]):.1f}",
                f"{sum(p.pnl for p in bands[b]):.2f}"] for b in order if bands[b]],
              ["band", "positions", "won", "market exp. wins", "P&L $"]))  # fmt: skip

    say("\n## Days to game (ticker game date in ET minus fill date in ET, by fill)")
    dd: dict[str, list[Fill]] = defaultdict(list)
    for f in settled:
        day, _, _ = parse_event(f.event)
        if day is None:
            dd["unknown"].append(f)
            continue
        d = (day.date() - f.created_at.astimezone(ET).date()).days
        dd["0 (same day)" if d <= 0 else "1-2" if d <= 2 else "3-6" if d <= 6 else "7+"].append(f)
    say(table([[k, len(v), sum(1 for f in v if f.won), f"{sum(f.price for f in v):.1f}",
                f"{sum(f.pnl or 0 for f in v):.2f}"]
               for k, v in sorted(dd.items())],
              ["days", "fills", "won", "market exp. wins", "P&L $"]))  # fmt: skip

    say("\n## Gross edge (fair - price) by fill")
    big = [f for f in settled if f.fair - f.price >= 0.05 - 1e-9]
    small = [f for f in settled if f.fair - f.price < 0.05 - 1e-9]
    say(table([[name, len(v), sum(1 for f in v if f.won), f"{sum(f.fair for f in v):.1f}",
                f"{sum(f.price for f in v):.1f}", f"{sum(f.pnl or 0 for f in v):.2f}"]
               for name, v in (("edge >= 5c", big), ("edge < 5c", small))],
              ["bucket", "fills", "won", "bot exp. wins", "market exp. wins",
               "P&L $"]))  # fmt: skip

    say("\n## Calibration (Brier on settled fills)")
    lo, hi = bootstrap_brier_diff(settled, args.bootstrap, rng)
    say(
        f"- Bot {brier(settled, 'fair'):.4f}, market (price paid) {brier(settled, 'price'):.4f};"
        f" game-level bootstrap 95% CI on bot - market: [{lo:+.4f}, {hi:+.4f}]"
        f" ({args.bootstrap} resamples)."
    )

    say("\n## Monte Carlo luck test (one draw per game, net of fees)")
    pm, em = monte_carlo(settled, "price", realized, args.runs, rng)
    pb, eb = monte_carlo(settled, "fair", realized, args.runs, rng)
    say(
        f"- P(P&L >= ${realized:.2f}) if market prices were right: {pm:.1%}"
        f" (mean ${em:.2f}); if bot fair values were right: {pb:.1%} (mean ${eb:.2f})."
        f" {args.runs} runs, seed {args.seed}."
    )
    say(
        f"- Bot's own expected profit on these fills: "
        f"${sum(f.count * f.fair - f.cost for f in settled):.2f}."
    )

    say("\n## In-game fills")
    rows, conf = [], []
    for f in fills:
        st, src = start_time(f.event)
        if st is not None and f.created_at >= st:
            conf.append(f)
            rows.append(
                [
                    f.ticker,
                    f.side,
                    f.strategy,
                    f.created_at.strftime("%Y-%m-%d %H:%M"),
                    st.strftime("%H:%M"),
                    src,
                    f"{(f.created_at - st) / timedelta(minutes=1):.0f}",
                    f"{f.pnl:.2f}",
                ]
            )
    say(table(rows, ["ticker", "side", "strategy", "filled UTC", "start UTC", "source",
                     "min after start", "P&L $"]))  # fmt: skip
    say(f"- Confirmed in-game fills: {len(conf)}, net ${sum(f.pnl or 0 for f in conf):.2f}.")
    inf = [f for f in fills if f.event in INFERRED_IN_PLAY]
    for ev, why in INFERRED_IN_PLAY.items():
        say(f"- In-play by inference: {ev} ({why}).")
    pre = [f for f in settled if f not in conf and f not in inf]
    say(
        f"- Pre-game-only (excl. confirmed and inferred): {len(pre)} fills, "
        f"${sum(f.pnl or 0 for f in pre):.2f}; without the Oklahoma and South Carolina outcomes:"
        f" ${sum(f.pnl or 0 for f in pre if outcome_key(f) not in BIG_WINS):.2f}."
    )
    unknown = sorted({f.event for f in fills if start_time(f.event)[0] is None})
    say(
        f"- Start time unknown (not guessed) for {len(unknown)} of"
        f" {len({f.event for f in fills})} events."
    )
    print("\n".join(p_out))


if __name__ == "__main__":
    main()
