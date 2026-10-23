from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from prediction_bot.analysis import parse_event, start_time
from prediction_bot.candles import candle_time, candle_value, fetch_candles
from prediction_bot.odds import KALSHI_SERIES_TO_SPORT
from prediction_bot.paper.ledger import Ledger
from prediction_bot.teams import team_for_code, team_for_odds_name
from prediction_bot.venues.kalshi import KalshiClient

ET = ZoneInfo("America/New_York")
SOURCE = "kalshi_candles_1m"
T_MINUS = (60, 15, 5)
log = logging.getLogger(__name__)


def _quote_at(
    candles: list[dict[str, Any]], target: datetime
) -> tuple[datetime, Decimal, Decimal, Decimal] | None:
    eligible = []
    for candle in candles:
        at = candle_time(candle)
        if at is not None and at <= target:
            eligible.append((at, candle))
    if not eligible:
        return None
    at, candle = max(eligible, key=lambda item: item[0])
    bid_d = candle_value(candle, "yes_bid", "close")
    ask_d = candle_value(candle, "yes_ask", "close")
    if bid_d is None or ask_d is None:
        return None
    if bid_d <= 0 or ask_d >= 1 or bid_d > ask_d:
        return None
    return (
        at,
        bid_d,
        ask_d,
        (bid_d + ask_d) / 2,
    )


def game_day_target(event_ticker: str, commence: datetime | None = None) -> datetime | None:
    if commence is not None:
        return datetime.combine(commence.astimezone(ET).date(), time(10), ET).astimezone(
            timezone.utc
        )
    day, _, _ = parse_event(event_ticker)
    if day is None:
        return None
    return datetime.combine(day.date(), time(10), ET).astimezone(timezone.utc)


def record_clv(
    ledger: Ledger,
    kalshi: KalshiClient,
    *,
    now: datetime | None = None,
    max_calls: int = 50,
) -> int:
    """Fetch one candle window per eligible ticker and persist fill CLV in cents."""
    now = now or datetime.now(timezone.utc)
    fill_rows = ledger.conn.execute(
        "SELECT f.id, f.ticker, f.side, f.price, f.count, f.commence_time, "
        "COALESCE(f.event_ticker,''), s.ticker, f.rules_version, f.fair_prob "
        "FROM fills f LEFT JOIN settlements s ON s.ticker=f.ticker "
        "WHERE f.clv_kalshi IS NULL ORDER BY f.id"
    ).fetchall()
    shadow_rows = ledger.conn.execute(
        "SELECT m.id,m.ticker,m.side,m.price,m.size,m.commence_time,m.event_ticker,s.ticker,"
        "m.fair,COALESCE(m.rules_version,'v2') "
        "FROM maker_shadow m LEFT JOIN settlements s ON s.ticker=m.ticker "
        "WHERE m.clv_kalshi IS NULL AND "
        "(m.status='filled' OR m.fill_trade_through_at IS NOT NULL "
        "OR m.fill_optimistic_at IS NOT NULL) ORDER BY m.id"
    ).fetchall()
    line_move_rows = ledger.conn.execute(
        "SELECT d.id,d.ticker,d.side,d.price,d.size,d.commence_time,"
        "'',s.ticker,d.fair,COALESCE(d.rules_version,'v2') "
        "FROM shadow_decisions d LEFT JOIN settlements s ON s.ticker=d.ticker "
        "WHERE d.strategy='line_move_follower' AND d.commence_time IS NOT NULL "
        "AND d.clv_kalshi IS NULL ORDER BY d.id"
    ).fetchall()
    entry_window_rows = ledger.conn.execute(
        "SELECT d.id,d.ticker,d.side,d.price,d.size,d.commence_time,"
        "'',s.ticker,d.fair,COALESCE(d.rules_version,'v2') "
        "FROM shadow_decisions d LEFT JOIN settlements s ON s.ticker=d.ticker "
        "WHERE d.reason='entry_window' AND d.rules_version IN ('v2','v3') "
        "AND d.window IN ('72h+','24-72h','2-24h') AND d.commence_time IS NOT NULL "
        "AND d.clv_kalshi IS NULL ORDER BY d.id"
    ).fetchall()
    commenced_shadow_decisions = []
    for row in [*line_move_rows, *entry_window_rows]:
        try:
            commence_at = datetime.fromisoformat(str(row[5]).replace("Z", "+00:00"))
        except ValueError:
            continue
        commence_at = (
            commence_at.replace(tzinfo=timezone.utc)
            if commence_at.tzinfo is None
            else commence_at.astimezone(timezone.utc)
        )
        if commence_at.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
            commenced_shadow_decisions.append(row)
    line_move_rows = commenced_shadow_decisions
    candidates: dict[str, list[tuple[Any, ...]]] = {}
    entries = (
        [("fills", row) for row in fill_rows]
        + [("maker_shadow", row) for row in shadow_rows]
        + [("shadow_decisions", row) for row in line_move_rows]
    )
    for table, row in entries:
        if table == "fills":
            (
                fill_id,
                ticker,
                side,
                price,
                count,
                raw_start,
                event_ticker,
                settled,
                version,
                fair,
            ) = row
        else:
            (
                fill_id,
                ticker,
                side,
                price,
                count,
                raw_start,
                event_ticker,
                settled,
                fair,
                version,
            ) = row
        if not event_ticker:
            event_ticker = str(ticker).rsplit("-", 1)[0]
        commence: datetime | None = None
        if raw_start:
            parsed = datetime.fromisoformat(str(raw_start).replace("Z", "+00:00"))
            commence = (
                parsed.replace(tzinfo=timezone.utc)
                if parsed.tzinfo is None
                else parsed.astimezone(timezone.utc)
            )
        else:
            commence = start_time(str(event_ticker))[0]
        if commence is None and not settled:
            continue
        if commence is not None and commence > now and not settled:
            continue
        candidates.setdefault(str(ticker), []).append(
            (
                table,
                fill_id,
                str(side),
                Decimal(str(price)),
                Decimal(str(count)),
                commence,
                str(event_ticker),
                version,
                fair,
            )
        )

    attempts_by_ticker = {
        str(ticker): (int(attempts), str(status))
        for ticker, attempts, status in ledger.conn.execute(
            "SELECT ticker,attempts,status FROM clv_fetch_attempts"
        )
    }
    ordered_candidates = sorted(
        candidates.items(),
        key=lambda item: (
            attempts_by_ticker.get(item[0], (0, "pending"))[0] > 0,
            -max(
                (fill[5].timestamp() for fill in item[1] if fill[5] is not None),
                default=0,
            ),
        ),
    )
    calls = 0
    for ticker, fills in ordered_candidates:
        attempts = attempts_by_ticker.get(ticker, (0, "pending"))[0]
        if attempts_by_ticker.get(ticker, (0, "pending"))[1] == "unavailable":
            continue
        attempt_number: int | None = None
        try:
            commence = next((fill[5] for fill in fills if fill[5] is not None), None)
            event = fills[0][6]
            gameday = game_day_target(event, commence)
            cached = {
                int(row[0])
                for row in ledger.conn.execute(
                    "SELECT t_minus_min FROM closing_quotes WHERE ticker=? AND commence_time=?",
                    (ticker, commence.isoformat() if commence else ""),
                )
            }
            needed = set(T_MINUS) if commence is not None else set()
            if gameday is not None:
                needed.add(-1)
            if needed and needed <= cached:
                candles: list[dict[str, Any]] = []
            else:
                if calls >= max_calls:
                    break
                targets = [commence - timedelta(minutes=n) for n in T_MINUS] if commence else []
                if gameday is not None:
                    targets.append(gameday)
                if not targets:
                    continue
                if commence is not None:
                    start = commence - timedelta(minutes=65)
                    if gameday is not None:
                        start = min(start, gameday - timedelta(minutes=2))
                    end = commence
                else:
                    start = min(targets) - timedelta(minutes=2)
                    end = max(targets) + timedelta(minutes=1)
                calls += 1
                attempt_number = attempts + 1
                ledger.conn.execute(
                    "INSERT INTO clv_fetch_attempts(ticker,attempts,last_attempt_at,status) "
                    "VALUES (?,?,?,'pending') ON CONFLICT(ticker) DO UPDATE SET "
                    "attempts=excluded.attempts,last_attempt_at=excluded.last_attempt_at,"
                    "status='pending'",
                    (ticker, attempt_number, now.astimezone(timezone.utc).isoformat()),
                )
                ledger.conn.commit()
                candles = fetch_candles(kalshi, ticker, start, end)
                fetched = now.astimezone(timezone.utc).isoformat()
                quote_targets = (
                    [(n, commence - timedelta(minutes=n)) for n in T_MINUS] if commence else []
                )
                if gameday is not None:
                    quote_targets.append((-1, gameday))
                usable_quotes = 0
                for t_minus, target in quote_targets:
                    quote = _quote_at(candles, target)
                    if quote is None:
                        continue
                    usable_quotes += 1
                    _, bid, ask, mid = quote
                    ledger.conn.execute(
                        "INSERT OR REPLACE INTO closing_quotes "
                        "(ticker, commence_time, t_minus_min, yes_bid, yes_ask, mid, fetched_at, "
                        "source) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (
                            ticker,
                            commence.isoformat() if commence else "",
                            t_minus,
                            str(bid),
                            str(ask),
                            str(mid),
                            fetched,
                            SOURCE,
                        ),
                    )
                if usable_quotes == 0 and attempt_number >= 3:
                    ledger.conn.execute(
                        "UPDATE clv_fetch_attempts SET status='unavailable' WHERE ticker=?",
                        (ticker,),
                    )
                ledger.conn.commit()

            for table, fill_id, side, price, _, fill_start, _, version, fair in fills:
                line_move = None
                if table == "fills" and version == "v2" and fill_start is not None:
                    series = ticker.split("-", 1)[0]
                    team = team_for_code(series, ticker.rsplit("-", 1)[-1])
                    sport = KALSHI_SERIES_TO_SPORT.get(series)
                    if team and sport:
                        snapshots = ledger.conn.execute(
                            "SELECT prob,team FROM odds_snapshots "
                            "WHERE julianday(commence_time)=julianday(?) "
                            "AND julianday(created_at)<julianday(?) "
                            "ORDER BY created_at DESC,id DESC",
                            (fill_start.isoformat(), fill_start.isoformat()),
                        ).fetchall()
                        snapshot = next(
                            (
                                row
                                for row in snapshots
                                if team_for_odds_name(sport, str(row[1])) == team
                            ),
                            None,
                        )
                        if snapshot is not None:
                            probability = Decimal(str(snapshot[0]))
                            if side == "no":
                                probability = Decimal(1) - probability
                            line_move = probability - Decimal(str(fair))
                quote_row = (
                    ledger.conn.execute(
                        "SELECT mid FROM closing_quotes WHERE ticker=? AND commence_time=? "
                        "AND t_minus_min=5",
                        (ticker, fill_start.isoformat() if fill_start else ""),
                    ).fetchone()
                    if fill_start
                    else None
                )
                exit_quote = (
                    ledger.conn.execute(
                        "SELECT yes_bid,yes_ask FROM closing_quotes WHERE ticker=? "
                        "AND commence_time=? AND t_minus_min=5",
                        (ticker, fill_start.isoformat()),
                    ).fetchone()
                    if fill_start
                    else None
                )
                game_row = ledger.conn.execute(
                    "SELECT mid FROM closing_quotes WHERE ticker=? AND commence_time=? "
                    "AND t_minus_min=-1",
                    (ticker, fill_start.isoformat() if fill_start else ""),
                ).fetchone()
                side_t5 = None if quote_row is None else Decimal(str(quote_row[0]))
                side_day = None if game_row is None else Decimal(str(game_row[0]))
                if side == "no":
                    side_t5 = None if side_t5 is None else Decimal(1) - side_t5
                    side_day = None if side_day is None else Decimal(1) - side_day
                side_bid_t5 = (
                    None
                    if exit_quote is None
                    else Decimal(str(exit_quote[0]))
                    if side == "yes"
                    else Decimal(1) - Decimal(str(exit_quote[1]))
                )
                clv = None if side_t5 is None else (side_t5 - price) * 100
                day_clv = None if side_day is None else (side_day - price) * 100
                exit_clv = None if side_bid_t5 is None else (side_bid_t5 - price) * 100
                if table == "fills":
                    ledger.conn.execute(
                        "UPDATE fills SET clv_kalshi=?, clv_gameday=?,clv_exit=?,"
                        " line_move_since_entry=? WHERE id=?",
                        (
                            None if clv is None else str(clv),
                            None if day_clv is None else str(day_clv),
                            None if exit_clv is None else str(exit_clv),
                            None if line_move is None else str(line_move),
                            fill_id,
                        ),
                    )
                elif table == "maker_shadow":
                    ledger.conn.execute(
                        "UPDATE maker_shadow SET clv_kalshi=?,clv_gameday=?,clv_exit=? WHERE id=?",
                        (
                            None if clv is None else str(clv),
                            None if day_clv is None else str(day_clv),
                            None if exit_clv is None else str(exit_clv),
                            fill_id,
                        ),
                    )
                else:
                    ledger.conn.execute(
                        "UPDATE shadow_decisions SET clv_kalshi=?,clv_gameday=?,clv_exit=? "
                        "WHERE id=?",
                        (
                            None if clv is None else str(clv),
                            None if day_clv is None else str(day_clv),
                            None if exit_clv is None else str(exit_clv),
                            fill_id,
                        ),
                    )
            ledger.conn.commit()
        except Exception:
            ledger.conn.rollback()
            if attempt_number is not None:
                ledger.conn.execute(
                    "UPDATE clv_fetch_attempts SET status=? WHERE ticker=?",
                    ("unavailable" if attempt_number >= 3 else "pending", ticker),
                )
                ledger.conn.commit()
            log.exception("Failed to record CLV for ticker %s; it will be retried next run", ticker)
    return calls
