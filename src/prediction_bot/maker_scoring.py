from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from prediction_bot.candles import fetch_candles
from prediction_bot.maker_fill import (
    candle_volume,
    optimistic_fill,
    queue_fill,
    run_over_fill,
    trade_through_fill,
)
from prediction_bot.models import OutcomeSide, Trade
from prediction_bot.paper.ledger import Ledger
from prediction_bot.venues.kalshi import KalshiClient

log = logging.getLogger(__name__)


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def score_maker_shadows(
    ledger: Ledger,
    kalshi: KalshiClient,
    now: datetime | None = None,
    max_calls: int = 120,
) -> int:
    """Score ended maker-shadow windows.

    Three fill rules are stored side by side: `trade_through` and `optimistic` from
    1-minute candles, and the v5 queue model (`fill_queue_at`) from the public trade
    tape against the queue recorded at placement. Each shadow costs two calls (candles,
    then the full trade tape). `fill_at`/`fill_rule` are the earlier of the queue fill
    and a run-over (price traded through, or the side's ask reached our price, in the
    tape, candles or stored quotes). A window whose candles show volume but whose tape
    parses to nothing is scored `tape_unavailable`, never `unfilled`.
    """
    now_utc = _utc(now or datetime.now(timezone.utc))
    rows = ledger.conn.execute(
        "SELECT id,ticker,side,price,created_at,status,status_at,expires_at,commence_time,"
        "score_attempts,score_status,queue_ahead,size FROM maker_shadow WHERE score_status NOT IN "
        "('scored','unavailable') ORDER BY score_attempts,created_at DESC,id"
    ).fetchall()
    candidates: list[tuple[tuple[int, float, int], tuple[object, ...], datetime]] = []
    for row in rows:
        (
            _shadow_id,
            _ticker,
            _side,
            _price,
            created_text,
            status,
            status_at_text,
            expires_text,
            commence_text,
            attempts,
            _score_status,
            _queue_ahead,
            _size,
        ) = row
        created = _utc(datetime.fromisoformat(str(created_text)))
        expires = _utc(datetime.fromisoformat(str(expires_text)))
        commence = _utc(datetime.fromisoformat(str(commence_text))) if commence_text else None
        if status in {"expired", "cancelled", "filled"}:
            end = _utc(datetime.fromisoformat(str(status_at_text or expires_text)))
        elif now_utc >= expires:
            end = expires
        elif commence is not None and now_utc >= commence:
            end = min(expires, commence)
        else:
            continue
        candidates.append(
            ((int(str(attempts)) > 0, -created.timestamp(), int(str(row[0]))), row, end)
        )
    candidates.sort(key=lambda item: item[0])

    calls = 0
    for _, row, end in candidates:
        if calls >= max_calls:
            break
        shadow_id, ticker, side, price, created_text, _status, *_rest = row
        created = _utc(datetime.fromisoformat(str(created_text)))
        if end <= created:
            ledger.record_maker_shadow_score(
                int(str(shadow_id)),
                scored_at=now_utc,
                trade_through_at=None,
                optimistic_at=None,
                status="scored",
            )
            continue
        attempts = int(str(row[9]))
        calls += 1
        try:
            candles = fetch_candles(kalshi, str(ticker), created, end)
            strict_at = trade_through_fill(
                candles, OutcomeSide(str(side)), Decimal(str(price)), created, end
            )
            optimistic_at = optimistic_fill(
                candles, OutcomeSide(str(side)), Decimal(str(price)), created, end
            )
        except Exception:
            log.exception("Failed to score maker shadow %s; it will be retried", ticker)
            ledger.record_maker_shadow_score(
                int(str(shadow_id)),
                scored_at=now_utc,
                trade_through_at=None,
                optimistic_at=None,
                status="unavailable" if attempts + 1 >= 3 else "pending",
            )
            continue
        side_enum = OutcomeSide(str(side))
        shadow_price = Decimal(str(price))
        queue_at = None
        queue_ahead, size = row[11], row[12]
        calls += 1
        trades: list[Trade] | None
        try:
            trades = kalshi.get_trades(
                str(ticker), int(created.timestamp()), int(end.timestamp()) + 1
            )
        except Exception:
            log.exception("Trade tape unavailable for maker shadow %s", ticker)
            trades = None
        if trades is not None and not trades and candle_volume(candles, created, end) > 0:
            log.warning(
                "Maker shadow %s: candles show volume in %s..%s but the tape parsed no "
                "trades; scored tape_unavailable",
                ticker,
                created.isoformat(),
                end.isoformat(),
            )
            trades = None
            tape_status = "tape_unavailable"
        else:
            tape_status = "unavailable"
        if trades is None:
            queue_status = tape_status if queue_ahead is not None else "no_queue"
            fill_at, fill_rule = None, "tape_unavailable"
        else:
            if queue_ahead is None:
                queue_status = "no_queue"
            else:
                queue_at = queue_fill(
                    trades,
                    side_enum,
                    shadow_price,
                    Decimal(str(queue_ahead)),
                    Decimal(str(size)),
                    created,
                    end,
                )
                queue_status = "filled" if queue_at is not None else "unfilled"
            run_over_at = run_over_fill(
                trades,
                candles,
                _quotes(ledger, str(ticker), created, end),
                side_enum,
                shadow_price,
                created,
                end,
            )
            fill_at, fill_rule = _earliest_fill(queue_at, run_over_at)
        ledger.record_maker_shadow_score(
            int(str(shadow_id)),
            scored_at=now_utc,
            trade_through_at=strict_at,
            optimistic_at=optimistic_at,
            status="scored",
            queue_at=queue_at,
            queue_status=queue_status,
            fill_at=fill_at,
            fill_rule=fill_rule,
        )
    return calls


def _earliest_fill(
    queue_at: datetime | None, run_over_at: datetime | None
) -> tuple[datetime | None, str]:
    """fill_at and fill_rule: the queue fill or the run-over, whichever came first."""
    if queue_at is not None and (run_over_at is None or queue_at <= run_over_at):
        return queue_at, "queue"
    if run_over_at is not None:
        return run_over_at, "run_over"
    return None, "unfilled"


def _quotes(
    ledger: Ledger, ticker: str, start: datetime, end: datetime
) -> list[tuple[datetime, Decimal | None, Decimal | None]]:
    """Stored quotes for `ticker` in [start, end): (time, yes_bid, yes_ask)."""
    out: list[tuple[datetime, Decimal | None, Decimal | None]] = []
    for table in ("market_snapshots", "quote_snapshots"):
        time_column = "fetched_at" if table == "market_snapshots" else "created_at"
        try:
            rows = ledger.conn.execute(
                f"SELECT {time_column},yes_bid,yes_ask FROM {table} WHERE ticker=? "
                f"AND {time_column}>=? AND {time_column}<?",
                (ticker, start.isoformat(), end.isoformat()),
            ).fetchall()
        except Exception:
            continue
        for at, bid, ask in rows:
            try:
                out.append(
                    (
                        _utc(datetime.fromisoformat(str(at))),
                        None if bid is None else Decimal(str(bid)),
                        None if ask is None else Decimal(str(ask)),
                    )
                )
            except (ValueError, ArithmeticError):
                continue
    return out
