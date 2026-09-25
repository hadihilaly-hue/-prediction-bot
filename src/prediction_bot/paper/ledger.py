from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from prediction_bot.models import OutcomeSide, Signal

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    strategy TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    fair_prob TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    edge TEXT NOT NULL,
    size TEXT NOT NULL,
    rationale TEXT NOT NULL,
    acted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    strategy TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    count TEXT NOT NULL,
    fee TEXT NOT NULL,
    fair_prob TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlements (
    ticker TEXT PRIMARY KEY,
    settled_at TEXT NOT NULL,
    result TEXT NOT NULL,
    pnl TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cash (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Position:
    ticker: str
    side: OutcomeSide
    count: Decimal
    cost: Decimal  # dollars paid incl. fees
    fair_prob_avg: Decimal


@dataclass(frozen=True)
class Performance:
    cash: Decimal
    starting_cash: Decimal
    open_positions: int
    open_cost: Decimal
    settled_count: int
    realized_pnl: Decimal
    fees_paid: Decimal
    wins: int
    brier_score: Decimal | None  # mean (fair_prob - outcome)^2 over settled fills
    market_brier_score: Decimal | None  # same using the price paid as the forecast


class Ledger:
    """SQLite-backed paper-trading book. Prices/cash are stored as Decimal strings."""

    def __init__(self, path: Path | str, starting_cash: Decimal) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.executescript(SCHEMA)
        self.starting_cash = starting_cash
        row = self.conn.execute("SELECT balance FROM cash WHERE id = 1").fetchone()
        if row is None:
            self.conn.execute("INSERT INTO cash (id, balance) VALUES (1, ?)", (str(starting_cash),))
            self.conn.commit()

    # ---- cash ---------------------------------------------------------------

    @property
    def cash(self) -> Decimal:
        row = self.conn.execute("SELECT balance FROM cash WHERE id = 1").fetchone()
        return Decimal(row[0])

    def _set_cash(self, value: Decimal) -> None:
        self.conn.execute("UPDATE cash SET balance = ? WHERE id = 1", (str(value),))

    # ---- signals / fills ----------------------------------------------------

    def record_signal(self, sig: Signal, acted: bool) -> int:
        cur = self.conn.execute(
            "INSERT INTO signals (created_at, strategy, ticker, side, fair_prob, limit_price,"
            " edge, size, rationale, acted) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                sig.created_at.isoformat(),
                sig.strategy,
                sig.ticker,
                sig.side.value,
                str(sig.fair_prob),
                str(sig.limit_price),
                str(sig.edge),
                str(sig.size),
                sig.rationale,
                int(acted),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def record_fill(
        self,
        strategy: str,
        ticker: str,
        side: OutcomeSide,
        price: Decimal,
        count: Decimal,
        fee: Decimal,
        fair_prob: Decimal,
        at: datetime | None = None,
    ) -> None:
        cost = price * count + fee
        if cost > self.cash:
            raise ValueError(f"insufficient paper cash: need {cost}, have {self.cash}")
        self.conn.execute(
            "INSERT INTO fills (created_at, strategy, ticker, side, price, count, fee, fair_prob)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                (at or datetime.utcnow()).isoformat(),
                strategy,
                ticker,
                side.value,
                str(price),
                str(count),
                str(fee),
                str(fair_prob),
            ),
        )
        self._set_cash(self.cash - cost)
        self.conn.commit()

    def positions(self) -> list[Position]:
        rows = self.conn.execute(
            "SELECT f.ticker, f.side, f.price, f.count, f.fee, f.fair_prob FROM fills f"
            " LEFT JOIN settlements s ON s.ticker = f.ticker WHERE s.ticker IS NULL"
        ).fetchall()
        agg: dict[tuple[str, OutcomeSide], list[Decimal]] = {}
        for ticker, side, price, count, fee, fair in rows:
            key = (ticker, OutcomeSide(side))
            cnt, cost, fair_w = agg.setdefault(key, [Decimal(0), Decimal(0), Decimal(0)])
            c, p, f, fp = Decimal(count), Decimal(price), Decimal(fee), Decimal(fair)
            agg[key] = [cnt + c, cost + p * c + f, fair_w + fp * c]
        return [
            Position(t, s, cnt, cost, (fair_w / cnt) if cnt else Decimal(0))
            for (t, s), (cnt, cost, fair_w) in sorted(agg.items())
        ]

    def position_count(self, ticker: str, side: OutcomeSide) -> Decimal:
        return sum(
            (p.count for p in self.positions() if p.ticker == ticker and p.side == side), Decimal(0)
        )

    def open_tickers(self) -> list[str]:
        return sorted({p.ticker for p in self.positions()})

    # ---- settlement ---------------------------------------------------------

    def settle(self, ticker: str, result: OutcomeSide, at: datetime | None = None) -> Decimal:
        """Settle every open fill in `ticker`: winning contracts pay $1 each."""
        if self.conn.execute("SELECT 1 FROM settlements WHERE ticker = ?", (ticker,)).fetchone():
            return Decimal(0)
        rows = self.conn.execute(
            "SELECT side, price, count, fee FROM fills WHERE ticker = ?", (ticker,)
        ).fetchall()
        payout = Decimal(0)
        cost = Decimal(0)
        for side, price, count, fee in rows:
            c = Decimal(count)
            cost += Decimal(price) * c + Decimal(fee)
            if OutcomeSide(side) == result:
                payout += c
        pnl = payout - cost
        self.conn.execute(
            "INSERT INTO settlements (ticker, settled_at, result, pnl) VALUES (?,?,?,?)",
            (ticker, (at or datetime.utcnow()).isoformat(), result.value, str(pnl)),
        )
        self._set_cash(self.cash + payout)
        self.conn.commit()
        return pnl

    # ---- reporting ----------------------------------------------------------

    def performance(self) -> Performance:
        pos = self.positions()
        settled = self.conn.execute(
            "SELECT f.side, f.price, f.count, f.fair_prob, s.result FROM fills f"
            " JOIN settlements s ON s.ticker = f.ticker"
        ).fetchall()
        realized = sum(
            (Decimal(r[0]) for r in self.conn.execute("SELECT pnl FROM settlements")), Decimal(0)
        )
        fees = sum((Decimal(r[0]) for r in self.conn.execute("SELECT fee FROM fills")), Decimal(0))
        wins = 0
        brier_num = market_num = Decimal(0)
        weight = Decimal(0)
        for side, price, count, fair, result in settled:
            outcome = Decimal(1) if side == result else Decimal(0)
            c = Decimal(count)
            wins += int(outcome == 1)
            brier_num += (Decimal(fair) - outcome) ** 2 * c
            market_num += (Decimal(price) - outcome) ** 2 * c
            weight += c
        return Performance(
            cash=self.cash,
            starting_cash=self.starting_cash,
            open_positions=len(pos),
            open_cost=sum((p.cost for p in pos), Decimal(0)),
            settled_count=len(settled),
            realized_pnl=realized,
            fees_paid=fees,
            wins=wins,
            brier_score=(brier_num / weight) if weight else None,
            market_brier_score=(market_num / weight) if weight else None,
        )

    def close(self) -> None:
        self.conn.close()
