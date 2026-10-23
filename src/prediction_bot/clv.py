from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from prediction_bot.analysis import parse_event, start_time
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
    eligible = [
        candle
        for candle in candles
        if int(candle.get("end_period_ts", 0)) <= int(target.timestamp())
    ]
    if not eligible:
        return None
    candle = max(eligible, key=lambda item: int(item["end_period_ts"]))
    bid = (candle.get("yes_bid") or {}).get("close_dollars")
    ask = (candle.get("yes_ask") or {}).get("close_dollars")
    if bid in (None, "") or ask in (None, ""):
        return None
    bid_d, ask_d = Decimal(str(bid)), Decimal(str(ask))
    if bid_d <= 0 or ask_d >= 1 or bid_d > ask_d:
        return None
    return (
        datetime.fromtimestamp(int(candle["end_period_ts"]), timezone.utc),
        bid_d,
        ask_d,
        (bid_d + ask_d) / 2,
    )


def game_day_target(event_ticker: str) -> datetime | None:
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
        "SELECT m.id,m.ticker,m.side,m.price,m.size,m.commence_time,m.event_ticker,s.ticker,m.fair "
        "FROM maker_shadow m LEFT JOIN settlements s ON s.ticker=m.ticker "
        "WHERE m.status='filled' AND m.clv_kalshi IS NULL ORDER BY m.id"
    ).fetchall()
    line_move_rows = ledger.conn.execute(
        "SELECT d.id,d.ticker,d.side,d.price,d.size,d.commence_time,"
        "'',s.ticker,d.fair "
        "FROM shadow_decisions d LEFT JOIN settlements s ON s.ticker=d.ticker "
        "WHERE d.strategy='line_move_follower' AND s.ticker IS NOT NULL "
        "AND d.clv_kalshi IS NULL ORDER BY d.id"
    ).fetchall()
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
            fill_id, ticker, side, price, count, raw_start, event_ticker, settled, fair = row
            version = "v2"
        if not event_ticker:
            event_ticker = str(ticker).rsplit("-", 1)[0]
        commence: datetime | None = None
        if version == "v2" and raw_start:
            parsed = datetime.fromisoformat(str(raw_start).replace("Z", "+00:00"))
            commence = (
                parsed.replace(tzinfo=timezone.utc)
                if parsed.tzinfo is None
                else parsed.astimezone(timezone.utc)
            )
        elif version != "v2":
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

    calls = 0
    for ticker, fills in candidates.items():
        try:
            commence = next((fill[5] for fill in fills if fill[5] is not None), None)
            event = fills[0][6]
            gameday = game_day_target(event)
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
                series = event.split("-", 1)[0]
                calls += 1
                candles = kalshi.get_candlesticks(
                    series,
                    ticker,
                    int(start.timestamp()),
                    int(end.timestamp()),
                    period_interval=1,
                )
                fetched = now.astimezone(timezone.utc).isoformat()
                quote_targets = (
                    [(n, commence - timedelta(minutes=n)) for n in T_MINUS] if commence else []
                )
                if gameday is not None:
                    quote_targets.append((-1, gameday))
                for t_minus, target in quote_targets:
                    quote = _quote_at(candles, target)
                    if quote is None:
                        continue
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
                ledger.conn.commit()

            for table, fill_id, side, price, _, fill_start, _, version, fair in fills:
                line_move = None
                if table == "fills" and version == "v2" and fill_start is not None:
                    series = ticker.split("-", 1)[0]
                    team = team_for_code(series, ticker.rsplit("-", 1)[-1])
                    sport = KALSHI_SERIES_TO_SPORT.get(series)
                    if team and sport:
                        snapshots = ledger.conn.execute(
                            "SELECT prob,team FROM odds_snapshots WHERE commence_time=? "
                            "AND created_at<? ORDER BY created_at DESC,id DESC",
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
                clv = None if side_t5 is None else (side_t5 - price) * 100
                day_clv = None if side_day is None else (side_day - price) * 100
                if table == "fills":
                    ledger.conn.execute(
                        "UPDATE fills SET clv_kalshi=?, clv_gameday=?,"
                        " line_move_since_entry=? WHERE id=?",
                        (
                            None if clv is None else str(clv),
                            None if day_clv is None else str(day_clv),
                            None if line_move is None else str(line_move),
                            fill_id,
                        ),
                    )
                elif table == "maker_shadow":
                    ledger.conn.execute(
                        "UPDATE maker_shadow SET clv_kalshi=?,clv_gameday=? WHERE id=?",
                        (
                            None if clv is None else str(clv),
                            None if day_clv is None else str(day_clv),
                            fill_id,
                        ),
                    )
                else:
                    ledger.conn.execute(
                        "UPDATE shadow_decisions SET clv_kalshi=?,clv_gameday=? WHERE id=?",
                        (
                            None if clv is None else str(clv),
                            None if day_clv is None else str(day_clv),
                            fill_id,
                        ),
                    )
            ledger.conn.commit()
        except Exception:
            ledger.conn.rollback()
            log.exception("Failed to record CLV for ticker %s; it will be retried next run", ticker)
    return calls
