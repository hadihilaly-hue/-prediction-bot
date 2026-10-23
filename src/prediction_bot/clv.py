from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from prediction_bot.analysis import event_datetime, parse_event, start_time
from prediction_bot.candles import candle_time, candle_value, fetch_candles
from prediction_bot.config import Settings
from prediction_bot.odds import KALSHI_SERIES_TO_SPORT
from prediction_bot.paper.ledger import Ledger
from prediction_bot.teams import team_for_code, team_for_odds_name
from prediction_bot.venues.kalshi import KalshiClient

ET = ZoneInfo("America/New_York")
SOURCE = "kalshi_candles_1m"
T_MINUS = (60, 15, 5)
log = logging.getLogger(__name__)


def _utc_datetime(value: str | datetime) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def clv_status(
    created_at: str | datetime,
    commence: str | datetime | None,
    sport_buffer_minutes: int,
    has_clv: bool,
) -> str:
    if commence is None:
        return "no_commence"
    if _utc_datetime(created_at) >= _utc_datetime(commence) - timedelta(
        minutes=sport_buffer_minutes
    ):
        return "in_game"
    return "ok" if has_clv else "pending"


def refresh_clv_statuses(ledger: Ledger, settings: Settings) -> int:
    changed = 0
    for table in ("fills", "shadow_decisions"):
        rows = ledger.conn.execute(
            f"SELECT id,created_at,ticker,COALESCE(commence_time,commence_time_backfill),"
            f"clv_kalshi FROM {table} WHERE clv_status IS NULL OR clv_status='pending' "
            "OR commence_time_backfill IS NOT NULL"
        ).fetchall()
        for row_id, created_at, ticker, commence, clv in rows:
            series = str(ticker).split("-", 1)[0]
            sport = KALSHI_SERIES_TO_SPORT.get(series)
            buffer = settings.kickoff_buffer_default_minutes
            if sport is not None:
                buffer = settings.kickoff_buffer_minutes.get(sport, buffer)
            try:
                status = clv_status(str(created_at), commence, buffer, clv is not None)
            except (TypeError, ValueError):
                log.exception("Invalid CLV timestamps for %s row %s", table, row_id)
                continue
            ledger.conn.execute(
                f"UPDATE {table} SET clv_status=? WHERE id=?",
                (status, row_id),
            )
            changed += 1
    ledger.conn.commit()
    return changed


def backfill_clv_commence_times(
    ledger: Ledger,
    kalshi: KalshiClient,
    settings: Settings,
    *,
    now: datetime | None = None,
    max_calls: int = 50,
) -> int:
    """Backfill commencement into new columns; failed lookups are attempted only once."""
    now = _utc_datetime(now or datetime.now(timezone.utc))
    rows_by_series: dict[str, list[tuple[str, int, str]]] = {}
    for table in ("fills", "shadow_decisions"):
        event_expression = "COALESCE(event_ticker,'')" if table == "fills" else "''"
        rows = ledger.conn.execute(
            f"SELECT id,ticker,{event_expression} FROM {table} "
            "WHERE commence_time IS NULL AND commence_time_backfill IS NULL "
            "AND commence_source IS NULL AND NOT EXISTS ("
            "SELECT 1 FROM clv_commence_attempts a "
            "WHERE a.table_name=? AND a.row_id=id)",
            (table,),
        ).fetchall()
        for row_id, ticker, raw_event in rows:
            event_ticker = str(raw_event or "") or str(ticker).rsplit("-", 1)[0]
            series = event_ticker.split("-", 1)[0] if "-" in event_ticker else ""
            rows_by_series.setdefault(series, []).append((table, int(row_id), event_ticker))

    backfilled = 0
    calls = 0
    statuses = ("settled", "closed", "open")
    for series, rows in rows_by_series.items():
        target_events = {row[2] for row in rows}
        events_by_ticker: dict[str, dict[str, object]] = {}
        statuses_attempted = 0
        for status in statuses:
            if target_events <= events_by_ticker.keys():
                break
            if calls >= max_calls:
                break
            calls += 1
            statuses_attempted += 1
            try:
                events = kalshi.get_events(
                    series_ticker=series,
                    status=status,
                    with_nested_markets=False,
                )
            except Exception:
                log.exception("Kalshi commencement lookup failed for %s (%s)", series, status)
                continue
            for found_event in events:
                ticker = str(found_event.get("event_ticker", ""))
                if ticker in target_events:
                    events_by_ticker[ticker] = found_event

        complete = target_events <= events_by_ticker.keys() or statuses_attempted == len(statuses)
        for table, row_id, event_ticker in rows:
            commence: datetime | None = None
            source: str | None = None
            event_data = events_by_ticker.get(event_ticker)
            if event_data is not None:
                commence = event_datetime(event_data)
                if commence is not None:
                    source = "kalshi_event"
            if commence is None and series == "KXMLBGAME":
                commence, _ = start_time(event_ticker)
                if commence is not None:
                    source = "mlb_ticker"

            if source is None and not complete and series:
                continue
            with ledger.conn:
                if commence is not None and source is not None:
                    ledger.conn.execute(
                        f"UPDATE {table} SET commence_time_backfill=?,commence_source=? "
                        "WHERE id=? AND commence_time IS NULL "
                        "AND commence_time_backfill IS NULL AND commence_source IS NULL",
                        (_utc_datetime(commence).isoformat(), source, row_id),
                    )
                    if ledger.conn.execute("SELECT changes()").fetchone()[0]:
                        backfilled += 1
                ledger.conn.execute(
                    "INSERT OR IGNORE INTO clv_commence_attempts "
                    "(table_name,row_id,attempted_at,status) VALUES (?,?,?,?)",
                    (table, row_id, now.isoformat(), source or "unavailable"),
                )

    refresh_clv_statuses(ledger, settings)
    return backfilled


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
        "SELECT f.id, f.ticker, f.side, f.price, f.count,"
        " COALESCE(f.commence_time,f.commence_time_backfill), "
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
        "SELECT d.id,d.ticker,d.side,d.price,d.size,"
        "COALESCE(d.commence_time,d.commence_time_backfill),"
        "'',s.ticker,d.fair,COALESCE(d.rules_version,'v2') "
        "FROM shadow_decisions d LEFT JOIN settlements s ON s.ticker=d.ticker "
        "WHERE d.strategy='line_move_follower' AND d.would_trade=1 "
        "AND COALESCE(d.commence_time,d.commence_time_backfill) IS NOT NULL "
        "AND d.clv_kalshi IS NULL ORDER BY d.id"
    ).fetchall()
    entry_window_rows = ledger.conn.execute(
        "SELECT d.id,d.ticker,d.side,d.price,d.size,"
        "COALESCE(d.commence_time,d.commence_time_backfill),"
        "'',s.ticker,d.fair,COALESCE(d.rules_version,'v2') "
        "FROM shadow_decisions d LEFT JOIN settlements s ON s.ticker=d.ticker "
        "WHERE d.reason IN ('entry_window','net_edge','single_sharp') "
        "AND d.rules_version IN ('v2','v3') "
        "AND d.window IN ('72h+','24-72h','2-24h') "
        "AND COALESCE(d.commence_time,d.commence_time_backfill) IS NOT NULL "
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
                        "INSERT OR IGNORE INTO closing_quotes "
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
                        "UPDATE fills SET clv_kalshi=COALESCE(clv_kalshi,?), "
                        "clv_gameday=COALESCE(clv_gameday,?),"
                        "clv_exit=COALESCE(clv_exit,?),"
                        "clv_kalshi_bid=COALESCE(clv_kalshi_bid,?),"
                        "line_move_since_entry=COALESCE(line_move_since_entry,?) WHERE id=?",
                        (
                            None if clv is None else str(clv),
                            None if day_clv is None else str(day_clv),
                            None if exit_clv is None else str(exit_clv),
                            None if exit_clv is None else str(exit_clv),
                            None if line_move is None else str(line_move),
                            fill_id,
                        ),
                    )
                elif table == "maker_shadow":
                    ledger.conn.execute(
                        "UPDATE maker_shadow SET clv_kalshi=COALESCE(clv_kalshi,?),"
                        "clv_gameday=COALESCE(clv_gameday,?),clv_exit=COALESCE(clv_exit,?),"
                        "clv_kalshi_bid=COALESCE(clv_kalshi_bid,?) "
                        "WHERE id=?",
                        (
                            None if clv is None else str(clv),
                            None if day_clv is None else str(day_clv),
                            None if exit_clv is None else str(exit_clv),
                            None if exit_clv is None else str(exit_clv),
                            fill_id,
                        ),
                    )
                else:
                    ledger.conn.execute(
                        "UPDATE shadow_decisions SET clv_kalshi=COALESCE(clv_kalshi,?),"
                        "clv_gameday=COALESCE(clv_gameday,?),clv_exit=COALESCE(clv_exit,?),"
                        "clv_kalshi_bid=COALESCE(clv_kalshi_bid,?) "
                        "WHERE id=?",
                        (
                            None if clv is None else str(clv),
                            None if day_clv is None else str(day_clv),
                            None if exit_clv is None else str(exit_clv),
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
