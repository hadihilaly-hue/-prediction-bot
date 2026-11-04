from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_UP, Decimal
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from prediction_bot.paper.ledger import Ledger
    from prediction_bot.venues.kalshi import KalshiClient

FEE_MODEL_VERSION = "kalshi-series-v2"
TAKER_COEFFICIENT = Decimal("0.07")
MAKER_COEFFICIENT = Decimal("0.0175")
MAKER_FEE_TYPE = "quadratic_with_maker_fees"
QUADRATIC_FEE_TYPE = "quadratic"
KNOWN_FEE_TYPES = frozenset({QUADRATIC_FEE_TYPE, MAKER_FEE_TYPE})
CENT = Decimal("0.01")
CENTICENT = Decimal("0.0001")

FeeStyle = Literal["taker", "maker"]


class FeeLookup(Protocol):
    """Anything that resolves a series ticker to `(fee_type, fee_multiplier)`."""

    def get(self, series_ticker: str) -> tuple[str, Decimal] | None: ...


@dataclass(frozen=True)
class FixedFee:
    """A lookup that answers every series with one schedule (e.g. one a signal carries)."""

    fee_type: str
    multiplier: Decimal

    def get(self, series_ticker: str) -> tuple[str, Decimal]:
        return self.fee_type, self.multiplier


# Kalshi's general schedule (taker 0.07, maker 0.0175, multiplier 1). Only for strategies
# built without a SeriesFeeCache (unit tests, offline scans); scheduled runs always pass
# the cache, so real series multipliers and fee types apply.
DEFAULT_FEE_SCHEDULE = FixedFee(MAKER_FEE_TYPE, Decimal(1))


def series_ticker(ticker: str) -> str:
    return ticker.split("-", 1)[0]


def _quantum(rounding: str) -> Decimal:
    if rounding == "cent":
        return CENT
    if rounding == "centicent":
        return CENTICENT
    raise ValueError(f"unsupported fee rounding: {rounding}")


def _coefficient(fee_type: str, style: FeeStyle) -> Decimal:
    if fee_type not in KNOWN_FEE_TYPES:
        raise ValueError(f"unknown fee type: {fee_type}")
    if style == "taker":
        return TAKER_COEFFICIENT
    if style == "maker":
        return MAKER_COEFFICIENT if fee_type == MAKER_FEE_TYPE else Decimal(0)
    raise ValueError(f"unknown fee style: {style}")


def fee_for_ticker(
    ticker: str,
    price: Decimal,
    contracts: Decimal,
    style: FeeStyle,
    *,
    schedule: FeeLookup,
    rounding: str | None = "cent",
) -> Decimal:
    """Kalshi fee for `contracts` at `price` on `ticker`'s series, in dollars.

    coefficient · fee_multiplier · C · P · (1 − P), where the coefficient is 0.07 for
    takers, 0.0175 for makers on `quadratic_with_maker_fees` series and 0 for makers on
    `quadratic` series. `rounding` is `cent` or `centicent` (rounded up), or None for
    the unrounded amount used in per-contract edge maths. Raises ValueError for an
    unknown series schedule or fee type, so callers fail closed.
    """
    found = schedule.get(series_ticker(ticker))
    if found is None:
        raise ValueError(f"no fee schedule for series {series_ticker(ticker)}")
    fee_type, multiplier = found
    if not Decimal(0) <= price <= Decimal(1):
        raise ValueError("price must be between zero and one")
    if contracts < 0 or multiplier < 0:
        raise ValueError("contracts and multiplier must be non-negative")
    raw = _coefficient(fee_type, style) * multiplier * contracts * price * (Decimal(1) - price)
    if rounding is None:
        return raw
    return raw.quantize(_quantum(rounding), rounding=ROUND_UP)


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
