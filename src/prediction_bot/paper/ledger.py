from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from prediction_bot.models import (
    Block,
    EdgeReview,
    MakerShadowCandidate,
    MakerShadowUpdate,
    Market,
    OutcomeSide,
    QuoteSnapshot,
    Signal,
)

if TYPE_CHECKING:
    from prediction_bot.odds import ConsensusOdds


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
    acted INTEGER NOT NULL DEFAULT 0,
    rules_version TEXT,
    position_key TEXT,
    event_ticker TEXT,
    tags TEXT,
    commence_time TEXT,
    devig_json TEXT,
    reference_source TEXT,
    book_count INTEGER,
    book_spread TEXT,
    book_probs_json TEXT,
    fair_pre_haircut TEXT,
    fee_type TEXT,
    fee_multiplier TEXT
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
    fair_prob TEXT NOT NULL,
    rules_version TEXT,
    position_key TEXT,
    event_ticker TEXT,
    tags TEXT,
    commence_time TEXT,
    fee_model_version TEXT,
    fee_paid TEXT
);
CREATE TABLE IF NOT EXISTS series_fees (
    series_ticker TEXT PRIMARY KEY,
    fee_type TEXT NOT NULL,
    fee_multiplier TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS maker_shadow (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    position_key TEXT NOT NULL,
    price TEXT NOT NULL,
    size TEXT NOT NULL,
    fair TEXT NOT NULL,
    maker_fee TEXT NOT NULL,
    status TEXT NOT NULL,
    status_at TEXT NOT NULL,
    trade_through_at TEXT,
    event_ticker TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    commence_time TEXT,
    clv_kalshi TEXT,
    clv_gameday TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS maker_shadow_position_key
    ON maker_shadow(position_key);
CREATE TABLE IF NOT EXISTS settlements (
    ticker TEXT PRIMARY KEY,
    settled_at TEXT NOT NULL,
    result TEXT NOT NULL,
    pnl TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS halts (
    day TEXT PRIMARY KEY,
    reason TEXT NOT NULL
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
    open_cost TEXT NOT NULL,
    odds_used INTEGER,
    odds_remaining INTEGER
);
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    feed TEXT NOT NULL,
    event_id TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    commence_time TEXT NOT NULL,
    team TEXT NOT NULL,
    prob TEXT NOT NULL,
    books_used INTEGER NOT NULL,
    max_book_update TEXT
);
CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    rules_version TEXT,
    strategy TEXT,
    ticker TEXT,
    event_ticker TEXT,
    reason TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS edge_reviews (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    fair_prob TEXT NOT NULL,
    price TEXT NOT NULL,
    gross_edge TEXT NOT NULL,
    refetched_price TEXT,
    refetched_fair TEXT,
    confirmed INTEGER NOT NULL,
    whitelisted INTEGER NOT NULL,
    traded INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS quote_snapshots (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    yes_bid TEXT,
    yes_ask TEXT,
    last_price TEXT,
    volume TEXT NOT NULL,
    open_interest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS closing_quotes (
    ticker TEXT NOT NULL,
    commence_time TEXT NOT NULL,
    t_minus_min INTEGER NOT NULL,
    yes_bid TEXT NOT NULL,
    yes_ask TEXT NOT NULL,
    mid TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (ticker, commence_time, t_minus_min)
);
CREATE TABLE IF NOT EXISTS shadow_decisions (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    rules_version TEXT,
    window TEXT NOT NULL,
    strategy TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    position_key TEXT,
    would_trade INTEGER NOT NULL,
    reason TEXT NOT NULL,
    fair TEXT,
    price TEXT,
    size TEXT,
    commence_time TEXT,
    clv_kalshi TEXT,
    clv_gameday TEXT
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
        for table, columns in {
            "signals": {
                "rules_version": "TEXT",
                "position_key": "TEXT",
                "event_ticker": "TEXT",
                "tags": "TEXT",
                "commence_time": "TEXT",
                "devig_json": "TEXT",
                "reference_source": "TEXT",
                "book_count": "INTEGER",
                "book_spread": "TEXT",
                "book_probs_json": "TEXT",
                "fair_pre_haircut": "TEXT",
                "fee_type": "TEXT",
                "fee_multiplier": "TEXT",
            },
            "fills": {
                "rules_version": "TEXT",
                "position_key": "TEXT",
                "event_ticker": "TEXT",
                "tags": "TEXT",
                "commence_time": "TEXT",
                "fee_model_version": "TEXT",
                "fee_paid": "TEXT",
                "clv_kalshi": "TEXT",
                "clv_gameday": "TEXT",
                "line_move_since_entry": "TEXT",
            },
            "odds_snapshots": {"max_book_update": "TEXT"},
            "runs": {"odds_used": "INTEGER", "odds_remaining": "INTEGER"},
            "shadow_decisions": {"clv_kalshi": "TEXT", "clv_gameday": "TEXT"},
            "maker_shadow": {
                "commence_time": "TEXT",
                "clv_kalshi": "TEXT",
                "clv_gameday": "TEXT",
            },
        }.items():
            self._ensure_columns(self.conn, table, columns)
        self.starting_cash = starting_cash
        row = self.conn.execute("SELECT balance FROM cash WHERE id = 1").fetchone()
        if row is None:
            self.conn.execute("INSERT INTO cash (id, balance) VALUES (1, ?)", (str(starting_cash),))
            self.conn.commit()

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, column_type in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")
        conn.commit()

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )

    @classmethod
    def _timestamp_param(cls, value: datetime) -> str:
        return cls._utc(value).replace(tzinfo=None).isoformat()

    def get_series_fee(self, series_ticker: str) -> tuple[str, Decimal, datetime] | None:
        row = self.conn.execute(
            "SELECT fee_type, fee_multiplier, fetched_at FROM series_fees WHERE series_ticker=?",
            (series_ticker,),
        ).fetchone()
        if row is None:
            return None
        fetched_at = datetime.fromisoformat(str(row[2]))
        return str(row[0]), Decimal(str(row[1])), self._utc(fetched_at)

    def save_series_fee(
        self, series_ticker: str, fee_type: str, multiplier: Decimal, fetched_at: datetime
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO series_fees"
            " (series_ticker, fee_type, fee_multiplier, fetched_at) VALUES (?,?,?,?)",
            (series_ticker, fee_type, str(multiplier), self._utc(fetched_at).isoformat()),
        )
        self.conn.commit()

    def v2_total_open_cost(self) -> Decimal:
        rows = self.conn.execute(
            "SELECT f.price, f.count, f.fee FROM fills f LEFT JOIN settlements s"
            " ON s.ticker=f.ticker WHERE s.ticker IS NULL AND f.rules_version='v2'"
        )
        return sum(
            (Decimal(price) * Decimal(count) + Decimal(fee) for price, count, fee in rows),
            Decimal(0),
        )

    def v2_new_risk_since(self, since: datetime) -> Decimal:
        rows = self.conn.execute(
            "SELECT price, count, fee FROM fills WHERE rules_version='v2' AND created_at >= ?",
            (self._utc(since).isoformat(),),
        )
        return sum(
            (Decimal(price) * Decimal(count) + Decimal(fee) for price, count, fee in rows),
            Decimal(0),
        )

    def record_maker_shadows(self, candidates: Sequence[MakerShadowCandidate]) -> int:
        inserted = 0
        for shadow in candidates:
            try:
                self.conn.execute(
                    "INSERT INTO maker_shadow (created_at, ticker, side, position_key, price,"
                    " size, fair, maker_fee, status, status_at, trade_through_at, event_ticker,"
                    " expires_at, commence_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        self._utc(shadow.created_at).isoformat(),
                        shadow.ticker,
                        shadow.side.value,
                        shadow.position_key,
                        str(shadow.price),
                        str(shadow.size),
                        str(shadow.fair),
                        str(shadow.maker_fee),
                        "open",
                        self._utc(shadow.created_at).isoformat(),
                        None,
                        shadow.event_ticker,
                        self._utc(shadow.expires_at).isoformat(),
                        self._utc(shadow.commence_time).isoformat()
                        if shadow.commence_time
                        else None,
                    ),
                )
            except sqlite3.IntegrityError:
                continue
            inserted += 1
        self.conn.commit()
        return inserted

    def update_maker_shadows(self, updates: Sequence[MakerShadowUpdate]) -> int:
        by_contract = {(update.ticker, update.side): update for update in updates}
        rows = self.conn.execute(
            "SELECT id, ticker, side, price, size, maker_fee, status_at, expires_at,"
            " trade_through_at"
            " FROM maker_shadow WHERE status='open'"
        ).fetchall()
        changed = 0
        for row in rows:
            (
                shadow_id,
                ticker,
                side_text,
                price_text,
                size_text,
                fee_text,
                previous_status_at,
                expires_text,
                through,
            ) = row
            side = OutcomeSide(side_text)
            update = by_contract.get((str(ticker), side))
            if update is None:
                continue
            price = Decimal(price_text)
            status = "open"
            if update.at >= self._utc(datetime.fromisoformat(str(expires_text))):
                status = "expired"
            elif update.ask is not None and update.ask <= price:
                status = "filled"
            elif update.fair is not None:
                size = Decimal(size_text)
                per_contract_fee = Decimal(fee_text) / size if size else Decimal(0)
                if update.fair - price - per_contract_fee < 0:
                    status = "cancelled"
            side_last = (
                update.last_price
                if side is OutcomeSide.yes or update.last_price is None
                else Decimal(1) - update.last_price
            )
            trade_through_at = through
            if side_last is not None and side_last <= price and trade_through_at is None:
                trade_through_at = self._utc(update.at).isoformat()
            status_at = self._utc(update.at).isoformat()
            self.conn.execute(
                "UPDATE maker_shadow SET status=?, status_at=?, trade_through_at=? WHERE id=?",
                (
                    status,
                    status_at if status != "open" else previous_status_at,
                    trade_through_at,
                    shadow_id,
                ),
            )
            changed += 1
        self.conn.commit()
        return changed

    def maker_shadow_metrics(self) -> dict[str, Decimal | int | None]:
        filled = int(
            self.conn.execute("SELECT COUNT(*) FROM maker_shadow WHERE status='filled'").fetchone()[
                0
            ]
        )
        rows = self.conn.execute(
            "SELECT m.ticker, m.side, m.price, m.size, m.maker_fee, m.expires_at, s.result"
            " FROM maker_shadow m LEFT JOIN settlements s ON s.ticker=m.ticker"
            " WHERE m.status='filled'"
        )
        pnl = Decimal(0)
        settled = 0
        clvs: list[Decimal] = []
        for ticker, side, price, size, fee, expires_at, result in rows:
            if result is not None:
                settled += 1
                cost = Decimal(price) * Decimal(size) + Decimal(fee)
                cost = Decimal(size) - cost if result == side else -cost
                pnl += cost
            quote = self.conn.execute(
                "SELECT yes_bid, yes_ask FROM quote_snapshots WHERE ticker=? AND created_at<=?"
                " AND yes_bid IS NOT NULL AND yes_ask IS NOT NULL ORDER BY created_at DESC, id DESC"
                " LIMIT 1",
                (ticker, expires_at),
            ).fetchone()
            if quote is not None:
                mid = (Decimal(quote[0]) + Decimal(quote[1])) / 2
                side_mid = mid if side == OutcomeSide.yes.value else Decimal(1) - mid
                clvs.append(side_mid - Decimal(price))
        return {
            "filled": filled,
            "settled": settled,
            "pnl": pnl,
            "clv_count": len(clvs),
            "mean_clv": sum(clvs, Decimal(0)) / len(clvs) if clvs else None,
        }

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
            " edge, size, rationale, acted, rules_version, position_key, event_ticker, tags,"
            " commence_time, devig_json, reference_source, book_count, book_spread,"
            " book_probs_json,"
            " fair_pre_haircut, fee_type, fee_multiplier)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._utc(sig.created_at).isoformat(),
                sig.strategy,
                sig.ticker,
                sig.side.value,
                str(sig.fair_prob),
                str(sig.limit_price),
                str(sig.edge),
                str(sig.size),
                sig.rationale,
                int(acted),
                sig.rules_version,
                sig.position_key,
                sig.event_ticker,
                ",".join(sorted(sig.tags)) if sig.tags is not None else None,
                self._utc(sig.commence_time).isoformat() if sig.commence_time else None,
                sig.devig_json,
                sig.reference_source,
                sig.book_count,
                str(sig.book_spread) if sig.book_spread is not None else None,
                sig.book_probs_json,
                str(sig.fair_pre_haircut) if sig.fair_pre_haircut is not None else None,
                sig.fee_type,
                str(sig.fee_multiplier) if sig.fee_multiplier is not None else None,
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
        *,
        rules_version: str | None = None,
        position_key: str | None = None,
        event_ticker: str | None = None,
        tags: tuple[str, ...] | None = None,
        commence_time: datetime | None = None,
        fee_model_version: str | None = None,
    ) -> None:
        cost = price * count + fee
        if cost > self.cash:
            raise ValueError(f"insufficient paper cash: need {cost}, have {self.cash}")
        self.conn.execute(
            "INSERT INTO fills (created_at, strategy, ticker, side, price, count, fee, fair_prob,"
            " rules_version, position_key, event_ticker, tags, commence_time, fee_model_version,"
            " fee_paid) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._utc(at or datetime.now(timezone.utc)).isoformat(),
                strategy,
                ticker,
                side.value,
                str(price),
                str(count),
                str(fee),
                str(fair_prob),
                rules_version,
                position_key,
                event_ticker,
                ",".join(sorted(tags)) if tags is not None else None,
                self._utc(commence_time).isoformat() if commence_time else None,
                fee_model_version,
                str(fee),
            ),
        )
        self._set_cash(self.cash - cost)
        self.conn.commit()

    @staticmethod
    def _fill_filter(strategy: str | None, league: str | None) -> tuple[str, tuple[str, ...]]:
        """SQL suffix restricting fills `f` to one strategy and/or one Kalshi series."""
        where = ""
        args: tuple[str, ...] = ()
        if strategy is not None:
            where, args = where + " AND f.strategy = ?", (*args, strategy)
        if league is not None:
            esc = league.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where, args = where + " AND f.ticker LIKE ? ESCAPE '\\'", (*args, esc + "-%")
        return where, args

    def positions(self, strategy: str | None = None, league: str | None = None) -> list[Position]:
        where, args = self._fill_filter(strategy, league)
        rows = self.conn.execute(
            "SELECT f.ticker, f.side, f.price, f.count, f.fee, f.fair_prob FROM fills f"
            " LEFT JOIN settlements s ON s.ticker = f.ticker WHERE s.ticker IS NULL" + where,
            args,
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
            (
                ticker,
                self._utc(at or datetime.now(timezone.utc)).isoformat(),
                result.value,
                str(pnl),
            ),
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

    def record_consensus(
        self,
        seen: dict[str, dict[str, ConsensusOdds]],
        at: datetime | None = None,
    ) -> int:
        """Persist one row per (feed, event, team) of sportsbook consensus seen this cycle.

        `seen` maps feed name -> event_id -> ConsensusOdds, already deduplicated by the
        caller across co-running strategies sharing one odds client. These snapshots are
        the raw material for per-league calibration and line-movement signals.
        """
        stamp = self._utc(at or datetime.now(timezone.utc)).isoformat()
        rows = [
            (
                stamp,
                feed,
                game.event_id,
                game.home_team,
                game.away_team,
                game.commence_time,
                team,
                str(prob),
                game.books_used,
                self._max_book_update(game.book_updates),
            )
            for feed, events in seen.items()
            for game in events.values()
            for team, prob in game.probs.items()
        ]
        self.conn.executemany(
            "INSERT INTO odds_snapshots (created_at, feed, event_id, home_team, away_team,"
            " commence_time, team, prob, books_used, max_book_update)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    @staticmethod
    def _max_book_update(updates: dict[str, str]) -> str | None:
        parsed: list[datetime] = []
        for value in updates.values():
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            parsed.append(
                dt.replace(tzinfo=timezone.utc)
                if dt.tzinfo is None
                else dt.astimezone(timezone.utc)
            )
        return max(parsed).isoformat() if parsed else None

    def first_consensus(self, event_id: str) -> dict[str, Decimal]:
        row = self.conn.execute(
            "SELECT created_at FROM odds_snapshots WHERE event_id=? "
            "ORDER BY created_at, id LIMIT 1",
            (event_id,),
        ).fetchone()
        if row is None:
            return {}
        rows = self.conn.execute(
            "SELECT team, prob FROM odds_snapshots WHERE event_id=? AND created_at=? ORDER BY id",
            (event_id, row[0]),
        )
        return {str(team): Decimal(prob) for team, prob in rows}

    def previous_consensus(self, event_id: str, before: datetime) -> dict[str, Decimal]:
        row = self.conn.execute(
            "SELECT created_at FROM odds_snapshots WHERE event_id=? AND created_at < ?"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (event_id, self._timestamp_param(before)),
        ).fetchone()
        if row is None:
            return {}
        rows = self.conn.execute(
            "SELECT team, prob FROM odds_snapshots WHERE event_id=? AND created_at=? ORDER BY id",
            (event_id, row[0]),
        )
        return {str(team): Decimal(prob) for team, prob in rows}

    def previous_quote_snapshot(
        self, ticker: str, before: datetime
    ) -> tuple[Decimal | None, Decimal | None] | None:
        row = self.conn.execute(
            "SELECT yes_bid,yes_ask FROM quote_snapshots WHERE ticker=? AND created_at<? "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
            (ticker, self._timestamp_param(before)),
        ).fetchone()
        if row is None:
            return None
        return (
            Decimal(row[0]) if row[0] is not None else None,
            Decimal(row[1]) if row[1] is not None else None,
        )

    def first_quote(self, ticker: str) -> tuple[Decimal | None, Decimal | None] | None:
        row = self.conn.execute(
            "SELECT yes_bid, yes_ask FROM quote_snapshots WHERE ticker=?"
            " ORDER BY created_at, id LIMIT 1",
            (ticker,),
        ).fetchone()
        if row is None:
            return None
        return (
            Decimal(row[0]) if row[0] is not None else None,
            Decimal(row[1]) if row[1] is not None else None,
        )

    def record_blocks(self, blocks: Sequence[Block]) -> int:
        self.conn.executemany(
            "INSERT INTO blocks (created_at, rules_version, strategy, ticker, event_ticker,"
            " reason, detail) VALUES (?,?,?,?,?,?,?)",
            [
                (
                    self._utc(block.created_at).isoformat(),
                    block.rules_version,
                    block.strategy,
                    block.ticker,
                    block.event_ticker,
                    block.reason,
                    block.detail,
                )
                for block in blocks
            ],
        )
        self.conn.commit()
        return len(blocks)

    def record_quote_snapshots(self, snapshots: Sequence[QuoteSnapshot]) -> int:
        self.conn.executemany(
            "INSERT INTO quote_snapshots (created_at, ticker, event_ticker, yes_bid, yes_ask,"
            " last_price, volume, open_interest) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    self._utc(snapshot.created_at).isoformat(),
                    snapshot.ticker,
                    snapshot.event_ticker,
                    str(snapshot.yes_bid) if snapshot.yes_bid is not None else None,
                    str(snapshot.yes_ask) if snapshot.yes_ask is not None else None,
                    str(snapshot.last_price) if snapshot.last_price is not None else None,
                    str(snapshot.volume),
                    str(snapshot.open_interest),
                )
                for snapshot in snapshots
            ],
        )
        self.conn.commit()
        return len(snapshots)

    def record_edge_reviews(self, reviews: Sequence[EdgeReview]) -> int:
        self.conn.executemany(
            "INSERT INTO edge_reviews (created_at, ticker, side, fair_prob, price, gross_edge,"
            " refetched_price, refetched_fair, confirmed, whitelisted, traded)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    self._utc(review.created_at).isoformat(),
                    review.ticker,
                    review.side.value,
                    str(review.fair_prob),
                    str(review.price),
                    str(review.gross_edge),
                    str(review.refetched_price) if review.refetched_price is not None else None,
                    str(review.refetched_fair) if review.refetched_fair is not None else None,
                    int(review.confirmed),
                    int(review.whitelisted),
                    int(review.traded),
                )
                for review in reviews
            ],
        )
        self.conn.commit()
        return len(reviews)

    def mark_edge_review_traded(self, sig: Signal) -> None:
        self.conn.execute(
            "UPDATE edge_reviews SET traded=1 WHERE ticker=? AND side=? AND created_at=?",
            (sig.ticker, sig.side.value, self._utc(sig.created_at).isoformat()),
        )
        self.conn.commit()

    def _v2_open_cost(
        self, *, position_key: str | None = None, event_ticker: str | None = None
    ) -> Decimal:
        if position_key is None and event_ticker is None:
            raise ValueError("position_key or event_ticker is required")
        field = "position_key" if position_key is not None else "event_ticker"
        value = position_key if position_key is not None else event_ticker
        rows = self.conn.execute(
            f"SELECT f.price, f.count, f.fee FROM fills f LEFT JOIN settlements s"
            f" ON s.ticker=f.ticker WHERE s.ticker IS NULL AND f.rules_version='v2'"
            f" AND f.{field}=?",
            (value,),
        )
        return sum(
            (Decimal(price) * Decimal(count) + Decimal(fee) for price, count, fee in rows),
            Decimal(0),
        )

    def position_cost(self, position_key: str) -> Decimal:
        return self._v2_open_cost(position_key=position_key)

    def event_cost(self, event_ticker: str) -> Decimal:
        return self._v2_open_cost(event_ticker=event_ticker)

    def held_keys(self, event_ticker: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT f.position_key FROM fills f LEFT JOIN settlements s"
            " ON s.ticker=f.ticker WHERE s.ticker IS NULL AND f.rules_version='v2'"
            " AND f.event_ticker=? AND f.position_key IS NOT NULL",
            (event_ticker,),
        )
        return {str(row[0]) for row in rows}

    def record_run(
        self,
        strategies: Sequence[str],
        scanned: int,
        signals: int,
        filled: int,
        settled: int,
        at: datetime | None = None,
        odds_used: int | None = None,
        odds_remaining: int | None = None,
    ) -> None:
        """Snapshot one engine cycle so the dashboard can chart activity and equity over time."""
        open_cost = sum((p.cost for p in self.positions()), Decimal(0))
        self.conn.execute(
            "INSERT INTO runs (ran_at, strategies, scanned, signals, filled, settled, cash,"
            " open_cost, odds_used, odds_remaining) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                self._utc(at or datetime.now(timezone.utc)).isoformat(),
                ",".join(strategies),
                scanned,
                signals,
                filled,
                settled,
                str(self.cash),
                str(open_cost),
                odds_used,
                odds_remaining,
            ),
        )
        self.conn.commit()

    def record_shadow_decision(self, sig: Signal, rules_version: str, reason: str = "") -> None:
        commence = sig.commence_time
        if commence is None:
            window = "unknown"
        else:
            hours = (commence - sig.created_at).total_seconds() / 3600
            window = (
                "72h+"
                if hours >= 72
                else "24-72h"
                if hours >= 24
                else "2-24h"
                if hours >= 2
                else "<2h"
            )
        self.conn.execute(
            "INSERT INTO shadow_decisions (created_at,rules_version,window,strategy,ticker,side,"
            "position_key,would_trade,reason,fair,price,size,commence_time) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._utc(sig.created_at).isoformat(),
                rules_version,
                window,
                sig.strategy,
                sig.ticker,
                sig.side.value,
                sig.position_key,
                1,
                reason,
                str(sig.fair_prob),
                str(sig.limit_price),
                str(sig.size),
                self._utc(commence).isoformat() if commence else None,
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
        """Sum of settlement PnL booked at or after `since`."""
        rows = self.conn.execute(
            "SELECT pnl FROM settlements WHERE settled_at >= ?", (self._timestamp_param(since),)
        )
        return sum((Decimal(r[0]) for r in rows), Decimal(0))

    def worst_drawdown_since(self, since: datetime) -> Decimal:
        """Lowest running total of settlement PnL since `since`, in settlement order.

        The daily-loss limit uses this rather than the net, so a loss that crossed the limit
        is not masked by a win booked later in the same cycle.
        """
        rows = self.conn.execute(
            "SELECT pnl FROM settlements WHERE settled_at >= ? ORDER BY settled_at, rowid",
            (self._timestamp_param(since),),
        )
        total = low = Decimal(0)
        for (pnl,) in rows:
            total += Decimal(pnl)
            low = min(low, total)
        return low

    def halt_for_day(self, day: str) -> str | None:
        """Reason trading was halted on `day` (YYYY-MM-DD), if a hard stop tripped that day."""
        row = self.conn.execute("SELECT reason FROM halts WHERE day = ?", (day,)).fetchone()
        return None if row is None else str(row[0])

    def record_halt(self, day: str, reason: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO halts (day, reason) VALUES (?, ?)", (day, reason))
        self.conn.commit()

    def strategies(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT strategy FROM (SELECT strategy FROM fills UNION"
            " SELECT strategy FROM signals) ORDER BY strategy"
        )
        return [r[0] for r in rows]

    def performance_by_strategy(self) -> dict[str, Performance]:
        return {name: self.performance(name) for name in self.strategies()}

    def leagues(self) -> list[str]:
        """Kalshi series (ticker prefix, e.g. `KXEPLGAME`) that have fills."""
        rows = self.conn.execute("SELECT DISTINCT ticker FROM fills").fetchall()
        return sorted({r[0].split("-")[0] for r in rows})

    def performance_by_league(self) -> dict[str, Performance]:
        return {name: self.performance(league=name) for name in self.leagues()}

    def performance(self, strategy: str | None = None, league: str | None = None) -> Performance:
        """Whole-book performance, or a slice of it for one `strategy` and/or `league`
        (Kalshi series ticker, e.g. `KXEPLGAME`).

        Settlement PnL is stored per ticker, so per-strategy realized PnL is recomputed
        from that strategy's fills (payout minus cost) rather than read from `settlements`.
        """
        where, args = self._fill_filter(strategy, league)
        pos = self.positions(strategy, league)
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
