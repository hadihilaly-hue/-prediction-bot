from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_UP, Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prediction_bot.paper.ledger import Ledger
    from prediction_bot.venues.kalshi import KalshiClient

FEE_MODEL_VERSION = "kalshi-series-v1"
TAKER_COEFFICIENT = Decimal("0.07")
MAKER_COEFFICIENT = Decimal("0.0175")
MAKER_FEE_TYPE = "quadratic_with_maker_fees"
CENT = Decimal("0.01")
CENTICENT = Decimal("0.0001")


def _quantum(rounding: str) -> Decimal:
    if rounding == "cent":
        return CENT
    if rounding == "centicent":
        return CENTICENT
    raise ValueError(f"unsupported fee rounding: {rounding}")


def _raw_fee(
    price: Decimal, contracts: Decimal, multiplier: Decimal, coefficient: Decimal
) -> Decimal:
    if not Decimal(0) <= price <= Decimal(1):
        raise ValueError("price must be between zero and one")
    if contracts < 0 or multiplier < 0:
        raise ValueError("contracts and multiplier must be non-negative")
    return coefficient * multiplier * contracts * price * (Decimal(1) - price)


def taker_fee(
    price: Decimal, contracts: Decimal, multiplier: Decimal, rounding: str = "cent"
) -> Decimal:
    return _raw_fee(price, contracts, multiplier, TAKER_COEFFICIENT).quantize(
        _quantum(rounding), rounding=ROUND_UP
    )


def maker_fee(
    price: Decimal,
    contracts: Decimal,
    multiplier: Decimal,
    fee_type: str,
    rounding: str = "cent",
) -> Decimal:
    if fee_type == MAKER_FEE_TYPE:
        coefficient = MAKER_COEFFICIENT
    elif fee_type == "quadratic":
        coefficient = Decimal(0)
    else:
        raise ValueError(f"unknown fee type: {fee_type}")
    return _raw_fee(price, contracts, multiplier, coefficient).quantize(
        _quantum(rounding), rounding=ROUND_UP
    )


def taker_fee_rate(price: Decimal, multiplier: Decimal) -> Decimal:
    return _raw_fee(price, Decimal(1), multiplier, TAKER_COEFFICIENT)


class SeriesFeeCache:
    """Persistent daily cache for the public Kalshi series fee schedule."""

    def __init__(
        self,
        ledger: Ledger,
        kalshi: KalshiClient,
        clock: Any | None = None,
    ) -> None:
        self.ledger = ledger
        self.kalshi = kalshi
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_refresh_at: datetime | None = None
        self._series_data: dict[str, tuple[str, Decimal]] = {}

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )

    def get(self, series_ticker: str) -> tuple[str, Decimal] | None:
        now = self._utc(self.clock())
        cached = self.ledger.get_series_fee(series_ticker)
        if cached is not None and now - cached[2] < timedelta(hours=24):
            return cached[0], cached[1]
        refresh_due = self._last_refresh_at is None or now - self._last_refresh_at >= timedelta(
            hours=24
        )
        if refresh_due and self._refresh(now):
            self._last_refresh_at = now
        fetched = self._series_data.get(series_ticker)
        if fetched is not None:
            return fetched
        refreshed = self.ledger.get_series_fee(series_ticker)
        if refreshed is not None:
            return refreshed[0], refreshed[1]
        if cached is not None:
            return cached[0], cached[1]
        return None

    def _refresh(self, now: datetime) -> bool:
        try:
            series = self.kalshi.get_series()
        except Exception:
            self._series_data = {}
            return False
        fetched_data: dict[str, tuple[str, Decimal]] = {}
        for item in series:
            ticker = item.get("ticker") or item.get("series_ticker")
            fee_type = item.get("fee_type")
            multiplier = item.get("fee_multiplier")
            if (
                not isinstance(ticker, str)
                or not isinstance(fee_type, str)
                or multiplier in (None, "")
            ):
                continue
            try:
                parsed_multiplier = Decimal(str(multiplier))
            except Exception:
                continue
            fetched_data[ticker] = (fee_type, parsed_multiplier)
        self._series_data = fetched_data
        for ticker, (fee_type, multiplier) in fetched_data.items():
            self.ledger.save_series_fee(ticker, fee_type, multiplier, now)
        return True
