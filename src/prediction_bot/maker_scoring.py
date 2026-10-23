from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from prediction_bot.candles import fetch_candles
from prediction_bot.maker_fill import optimistic_fill, trade_through_fill
from prediction_bot.models import OutcomeSide
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
    max_calls: int = 50,
) -> int:
    """Score ended maker-shadow windows using strict and optimistic candle rules."""
    now_utc = _utc(now or datetime.now(timezone.utc))
    rows = ledger.conn.execute(
        "SELECT id,ticker,side,price,created_at,status,status_at,expires_at,commence_time,"
        "score_attempts,score_status FROM maker_shadow WHERE score_status NOT IN "
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
        ledger.record_maker_shadow_score(
            int(str(shadow_id)),
            scored_at=now_utc,
            trade_through_at=strict_at,
            optimistic_at=optimistic_at,
            status="scored",
        )
    return calls
