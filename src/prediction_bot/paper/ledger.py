from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from prediction_bot.models import Market, OutcomeSide, Signal


def describe(
    title: str,
    ticker: str,
    strategy: str,
    side: OutcomeSide,
    price: Decimal,
    fair: Decimal,
    verb: str,
) -> str:
    """Plain English, e.g. 'Fresno St. wins — NO: Kalshi says 9%, we say 12%; bought at 9¢'.

    `book_scanner` legs are one half of a YES+NO pair whose `fair` is 1 - other leg's ask,
    not an outcome probability, so they are described as an arbitrage instead.
    """
    what = title.rstrip(".?") or ticker
    s, cents = side.value.upper(), f"{price * 100:.0f}¢"
    if strategy == "book_scanner":
        pair = (price + Decimal(1) - fair) * 100
        return (
            f"{what} — {s} leg of a YES+NO arbitrage: {verb} at {cents};"
            f" pair costs {pair:.0f}¢ for a $1 payout"
        )
    return (
        f"{what} — {s}: Kalshi says {price * 100:.0f}%, we say {fair * 100:.0f}%; {verb} at {cents}"
    )


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
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    subtitle TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    ran_at TEXT NOT NULL,
    strategies TEXT NOT NULL,
    scanned INTEGER NOT NULL,
    signals INTEGER NOT NULL,
    filled INTEGER NOT NULL,
    settled INTEGER NOT NULL,
    cash TEXT NOT NULL,
    open_cost TEXT NOT NULL
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
class Prediction:
    created_at: str
    strategy: str
    ticker: str
    side: OutcomeSide
    limit_price: Decimal
    fair_prob: Decimal
    edge: Decimal
    size: Decimal
    rationale: str
    acted: bool
    result: OutcomeSide | None  # settled market outcome, None while open
    title: str = ""
    filled: bool = False  # a paper fill was recorded for this signal

    @property
    def won(self) -> bool | None:
        return None if self.result is None else self.result == self.side

    @property
    def summary(self) -> str:
        verb = "bought" if self.filled else "placed order" if self.acted else "would buy"
        return describe(
            self.title,
            self.ticker,
            self.strategy,
            self.side,
            self.limit_price,
            self.fair_prob,
            verb,
        )


@dataclass(frozen=True)
class Trade:
    """A paper fill, joined to the market's settlement once it resolves."""

    created_at: str
    strategy: str
    ticker: str
    side: OutcomeSide
    price: Decimal
    count: Decimal
    fee: Decimal
    fair_prob: Decimal
    result: OutcomeSide | None
    settled_at: str | None
    title: str = ""

    @property
    def summary(self) -> str:
        return describe(
            self.title, self.ticker, self.strategy, self.side, self.price, self.fair_prob, "bought"
        )

    @property
    def cost(self) -> Decimal:
        return self.price * self.count + self.fee

    @property
    def pnl(self) -> Decimal | None:
        if self.result is None:
            return None
        payout = self.count if self.result == self.side else Decimal(0)
        return payout - self.cost


@dataclass(frozen=True)
class Run:
    ran_at: str
    strategies: str
    scanned: int
    signals: int
    filled: int
    settled: int
    cash: Decimal
    open_cost: Decimal


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

    def remember_markets(self, markets: Sequence[Market]) -> None:
        """Keep the human-readable title of every market we signalled on."""
        self.conn.executemany(
            "INSERT OR REPLACE INTO markets (ticker, title, subtitle) VALUES (?,?,?)",
            [(m.ticker, m.title, m.subtitle) for m in markets if m.title],
        )
        self.conn.commit()

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

    def positions(self, strategy: str | None = None) -> list[Position]:
        rows = self.conn.execute(
            "SELECT f.ticker, f.side, f.price, f.count, f.fee, f.fair_prob FROM fills f"
            " LEFT JOIN settlements s ON s.ticker = f.ticker WHERE s.ticker IS NULL"
            + (" AND f.strategy = ?" if strategy is not None else ""),
            (strategy,) if strategy is not None else (),
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

    def position_count(
        self, ticker: str, side: OutcomeSide, strategy: str | None = None
    ) -> Decimal:
        """Open contracts on a side; per strategy when given so paper books stay independent."""
        return sum(
            (p.count for p in self.positions(strategy) if p.ticker == ticker and p.side == side),
            Decimal(0),
        )

    def open_tickers(self) -> list[str]:
        return sorted({p.ticker for p in self.positions()})

    def unsettled_tickers(self) -> list[str]:
        """Tickers with any fill *or* signal that Kalshi has not yet been seen to resolve."""
        rows = self.conn.execute(
            "SELECT DISTINCT t FROM (SELECT ticker AS t FROM fills UNION"
            " SELECT ticker FROM signals) WHERE t NOT IN (SELECT ticker FROM settlements)"
            " ORDER BY t"
        ).fetchall()
        return [r[0] for r in rows]

    def untitled_tickers(self) -> list[str]:
        """Tickers with a fill or signal but no remembered market title yet."""
        rows = self.conn.execute(
            "SELECT DISTINCT t FROM (SELECT ticker AS t FROM fills UNION"
            " SELECT ticker FROM signals) WHERE t NOT IN"
            " (SELECT ticker FROM markets WHERE title != '') ORDER BY t"
        ).fetchall()
        return [r[0] for r in rows]

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

    def predictions(self, limit: int = 0) -> list[Prediction]:
        """Every recorded signal (newest first) with the market's result once settled."""
        sql = (
            "SELECT g.created_at, g.strategy, g.ticker, g.side, g.limit_price, g.fair_prob,"
            " g.edge, g.size, g.rationale, g.acted, s.result, m.title,"
            " EXISTS (SELECT 1 FROM fills f WHERE f.ticker = g.ticker AND f.side = g.side"
            " AND f.strategy = g.strategy AND f.created_at >= g.created_at) FROM signals g"
            " LEFT JOIN settlements s ON s.ticker = g.ticker"
            " LEFT JOIN markets m ON m.ticker = g.ticker ORDER BY g.id DESC"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [
            Prediction(
                created_at=r[0],
                strategy=r[1],
                ticker=r[2],
                side=OutcomeSide(r[3]),
                limit_price=Decimal(r[4]),
                fair_prob=Decimal(r[5]),
                edge=Decimal(r[6]),
                size=Decimal(r[7]),
                rationale=r[8],
                acted=bool(r[9]),
                result=OutcomeSide(r[10]) if r[10] else None,
                title=r[11] or "",
                filled=bool(r[12]),
            )
            for r in self.conn.execute(sql)
        ]

    def trades(self) -> list[Trade]:
        """Every paper fill (newest first) with the settlement result once known."""
        rows = self.conn.execute(
            "SELECT f.created_at, f.strategy, f.ticker, f.side, f.price, f.count, f.fee,"
            " f.fair_prob, s.result, s.settled_at, m.title FROM fills f"
            " LEFT JOIN settlements s ON s.ticker = f.ticker"
            " LEFT JOIN markets m ON m.ticker = f.ticker ORDER BY f.id DESC"
        )
        return [
            Trade(
                created_at=r[0],
                strategy=r[1],
                ticker=r[2],
                side=OutcomeSide(r[3]),
                price=Decimal(r[4]),
                count=Decimal(r[5]),
                fee=Decimal(r[6]),
                fair_prob=Decimal(r[7]),
                result=OutcomeSide(r[8]) if r[8] else None,
                settled_at=r[9],
                title=r[10] or "",
            )
            for r in rows
        ]

    def record_run(
        self,
        strategies: Sequence[str],
        scanned: int,
        signals: int,
        filled: int,
        settled: int,
        at: datetime | None = None,
    ) -> None:
        """Snapshot one engine cycle so the dashboard can chart activity and equity over time."""
        open_cost = sum((p.cost for p in self.positions()), Decimal(0))
        self.conn.execute(
            "INSERT INTO runs (ran_at, strategies, scanned, signals, filled, settled, cash,"
            " open_cost) VALUES (?,?,?,?,?,?,?,?)",
            (
                (at or datetime.utcnow()).isoformat(),
                ",".join(strategies),
                scanned,
                signals,
                filled,
                settled,
                str(self.cash),
                str(open_cost),
            ),
        )
        self.conn.commit()

    def runs(self) -> list[Run]:
        rows = self.conn.execute(
            "SELECT ran_at, strategies, scanned, signals, filled, settled, cash, open_cost"
            " FROM runs ORDER BY id"
        )
        return [Run(r[0], r[1], r[2], r[3], r[4], r[5], Decimal(r[6]), Decimal(r[7])) for r in rows]

    def realized_pnl_since(self, since: datetime) -> Decimal:
        """Sum of settlement PnL booked at or after `since` (used for the daily-loss limit)."""
        rows = self.conn.execute(
            "SELECT pnl FROM settlements WHERE settled_at >= ?", (since.isoformat(),)
        )
        return sum((Decimal(r[0]) for r in rows), Decimal(0))

    def strategies(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT strategy FROM (SELECT strategy FROM fills UNION"
            " SELECT strategy FROM signals) ORDER BY strategy"
        )
        return [r[0] for r in rows]

    def performance_by_strategy(self) -> dict[str, Performance]:
        return {name: self.performance(name) for name in self.strategies()}

    def performance(self, strategy: str | None = None) -> Performance:
        """Whole-book performance, or one strategy's slice of it when `strategy` is given.

        Settlement PnL is stored per ticker, so per-strategy realized PnL is recomputed
        from that strategy's fills (payout minus cost) rather than read from `settlements`.
        """
        where = " AND f.strategy = ?" if strategy is not None else ""
        args: tuple[str, ...] = (strategy,) if strategy is not None else ()
        pos = self.positions(strategy)
        settled = self.conn.execute(
            "SELECT f.side, f.price, f.count, f.fair_prob, s.result, f.fee FROM fills f"
            " JOIN settlements s ON s.ticker = f.ticker WHERE 1=1" + where,
            args,
        ).fetchall()
        fees = sum(
            (
                Decimal(r[0])
                for r in self.conn.execute("SELECT f.fee FROM fills f WHERE 1=1" + where, args)
            ),
            Decimal(0),
        )
        wins = 0
        realized = Decimal(0)
        brier_num = market_num = Decimal(0)
        weight = Decimal(0)
        for side, price, count, fair, result, fee in settled:
            outcome = Decimal(1) if side == result else Decimal(0)
            c = Decimal(count)
            wins += int(outcome == 1)
            realized += outcome * c - Decimal(price) * c - Decimal(fee)
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
