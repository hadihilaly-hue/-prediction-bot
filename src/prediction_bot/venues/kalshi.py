from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from prediction_bot.config import KALSHI_DEMO_REST
from prediction_bot.models import Market, OutcomeSide, Quote, Trade
from prediction_bot.venues.kalshi_auth import KalshiSigner

log = logging.getLogger(__name__)

JSON = dict[str, Any]


class KalshiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Kalshi HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def _dec(value: object, default: str = "0") -> Decimal:
    if value in (None, ""):
        return Decimal(default)
    return Decimal(str(value))


def _opt_dec(value: object) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def _ts(value: object) -> datetime | None:
    """Kalshi timestamps as aware UTC; a value without an offset is read as UTC."""
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def parse_market(m: JSON) -> Market:
    yes_bid = _opt_dec(m.get("yes_bid_dollars"))
    yes_ask = _opt_dec(m.get("yes_ask_dollars"))
    # Kalshi reports 0.0000 / 1.0000 when a side of the book is empty.
    if yes_bid is not None and yes_bid <= 0:
        yes_bid = None
    if yes_ask is not None and yes_ask >= 1:
        yes_ask = None
    quote = Quote(
        ticker=str(m["ticker"]),
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        yes_bid_size=_dec(m.get("yes_bid_size_fp")),
        yes_ask_size=_dec(m.get("yes_ask_size_fp")),
    )
    return Market(
        ticker=str(m["ticker"]),
        event_ticker=str(m.get("event_ticker", "")),
        title=str(m.get("title", "")),
        subtitle=str(m.get("yes_sub_title") or m.get("subtitle") or ""),
        status=str(m.get("status", "")),
        close_time=_ts(m.get("close_time")),
        quote=quote,
        expected_expiration=_ts(m.get("expected_expiration_time")),
        volume=_dec(m.get("volume_fp")),
        open_interest=_dec(m.get("open_interest_fp")),
        result=str(m.get("result") or ""),
        category=str(m.get("category") or ""),
        raw=m,
        last_price=_opt_dec(m.get("last_price_dollars")),
    )


def _book_levels(ob: JSON, side: str) -> list[list[Any]]:
    """One side's [price, size] levels, prices in dollars.

    Accepts `orderbook_fp` or `orderbook`, with `<side>_dollars` or `<side>` keys. An
    integer price above 1 is the legacy cents form and is converted. v5 Phase 7.1a: the
    live key names were not verified from this environment, so all forms are read.
    """
    book = ob.get("orderbook_fp") or ob.get("orderbook") or {}
    levels = book.get(f"{side}_dollars") or book.get(side) or []
    out = []
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price = level[0]
        if isinstance(price, int) and price > 1:
            price = Decimal(price) / 100
        out.append([price, level[1]])
    return out


def parse_orderbook(ticker: str, ob: JSON) -> Quote:
    """The `orderbook` endpoint returns YES bids and NO bids only. A NO bid at p is a
    YES ask at 1-p. Levels are [price_dollars, size_fp] pairs."""
    yes_levels = _book_levels(ob, "yes")
    no_levels = _book_levels(ob, "no")
    yes_bid = yes_bid_size = None
    yes_ask = yes_ask_size = None
    if yes_levels:
        p, s = max(yes_levels, key=lambda lv: Decimal(str(lv[0])))
        yes_bid, yes_bid_size = _dec(p), _dec(s)
    if no_levels:
        p, s = max(no_levels, key=lambda lv: Decimal(str(lv[0])))
        yes_ask, yes_ask_size = Decimal(1) - _dec(p), _dec(s)
    return Quote(
        ticker=ticker,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        yes_bid_size=yes_bid_size or Decimal(0),
        yes_ask_size=yes_ask_size or Decimal(0),
    )


def parse_orderbook_levels(ob: JSON) -> dict[str, list[tuple[Decimal, Decimal]]]:
    """All displayed bid levels per side: `yes` (YES bids) and `no` (NO bids), each a
    best-first list of (price_dollars, contracts)."""
    out: dict[str, list[tuple[Decimal, Decimal]]] = {}
    for side in ("yes", "no"):
        levels = [(_dec(level[0]), _dec(level[1])) for level in _book_levels(ob, side)]
        out[side] = sorted(levels, key=lambda level: level[0], reverse=True)
    return out


def _price_dollars(trade: JSON, key: str) -> Decimal | None:
    """`<key>_dollars` (fixed-point string) when present, else a legacy integer in cents."""
    dollars = trade.get(f"{key}_dollars")
    if dollars not in (None, ""):
        return _dec(dollars)
    cents = trade.get(key)
    if cents in (None, ""):
        return None
    # The legacy field is an integer number of cents; a decimal string is dollars.
    if isinstance(cents, int) or str(cents).strip().isdigit():
        return _dec(cents) / 100
    return _dec(cents)


def parse_trade(trade: JSON) -> Trade | None:
    """Parse one `/markets/trades` row; None if a field we rely on is missing.

    Field names follow Kalshi's fixed-point convention (`count_fp`, `yes_price_dollars`)
    with the older integer forms (`count`, `yes_price` in cents) as fallbacks.
    """
    created = _ts(trade.get("created_time"))
    side = str(trade.get("taker_side") or "").lower()
    yes_price = _price_dollars(trade, "yes_price")
    if yes_price is None:
        no_price = _price_dollars(trade, "no_price")
        yes_price = None if no_price is None else Decimal(1) - no_price
    count = _opt_dec(trade.get("count_fp"))
    if count is None:
        count = _opt_dec(trade.get("count"))
    if created is None or side not in {"yes", "no"} or yes_price is None or count is None:
        return None
    return Trade(
        ticker=str(trade.get("ticker", "")),
        created_at=created,
        count=count,
        yes_price=yes_price,
        taker_side=OutcomeSide(side),
    )


class KalshiClient:
    """Thin REST client. Public market-data calls need no credentials; portfolio and
    order calls require a `KalshiSigner`."""

    def __init__(
        self,
        base_url: str = KALSHI_DEMO_REST,
        signer: KalshiSigner | None = None,
        timeout: float = 15.0,
        max_retries: int = 4,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.signer = signer
        self.max_retries = max_retries
        self._client = client or httpx.Client(timeout=timeout)

    @property
    def is_demo(self) -> bool:
        host = urlparse(self.base_url).hostname or ""
        return host.endswith(".demo.kalshi.co") or host == "demo.kalshi.co"

    # ---- transport -------------------------------------------------------

    def _request(
        self, method: str, path: str, *, params: JSON | None = None, json: JSON | None = None
    ) -> JSON:
        url = f"{self.base_url}{path}"
        headers = self.signer.headers(method, url) if self.signer else {}
        for attempt in range(self.max_retries + 1):
            if self.signer:
                headers = self.signer.headers(method, url)
            resp = self._client.request(method, url, params=params, json=json, headers=headers)
            if resp.status_code == 429 and attempt < self.max_retries:
                delay = 0.5 * 2**attempt
                log.warning("rate limited on %s; sleeping %.1fs", path, delay)
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise KalshiError(resp.status_code, resp.text)
            return cast(JSON, resp.json()) if resp.content else {}
        raise KalshiError(429, "rate limit retries exhausted")

    def _require_auth(self) -> None:
        if self.signer is None:
            raise PermissionError("This endpoint needs PBOT_KALSHI_API_KEY_ID and a private key")

    # ---- public market data ---------------------------------------------

    def get_markets(
        self,
        *,
        status: str | None = "open",
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        tickers: list[str] | None = None,
        limit: int = 200,
        max_pages: int = 5,
    ) -> list[Market]:
        params: JSON = {"limit": min(limit, 1000)}
        if status:
            params["status"] = status
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if tickers:
            params["tickers"] = ",".join(tickers)
        out: list[Market] = []
        for page in self._paginate("/markets", "markets", params, max_pages):
            out.append(parse_market(page))
        return out

    def _paginate(self, path: str, key: str, params: JSON, max_pages: int) -> Iterator[JSON]:
        cursor: str | None = None
        for _ in range(max_pages):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            data = self._request("GET", path, params=p)
            yield from data.get(key, [])
            cursor = data.get("cursor") or None
            if not cursor:
                break

    def get_market(self, ticker: str) -> Market:
        return parse_market(self._request("GET", f"/markets/{ticker}")["market"])

    def get_event(self, event_ticker: str) -> dict[str, Any]:
        return cast(dict[str, Any], self._request("GET", f"/events/{event_ticker}")["event"])

    def get_candlesticks(
        self,
        series_ticker: str,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 1,
    ) -> list[JSON]:
        data = self._request(
            "GET",
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params={
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": period_interval,
            },
        )
        return cast(list[JSON], data.get("candlesticks", []))

    def get_orderbook(self, ticker: str, depth: int = 10) -> Quote:
        data = self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        return parse_orderbook(ticker, data)

    def get_orderbook_levels(
        self, ticker: str, depth: int = 20
    ) -> dict[str, list[tuple[Decimal, Decimal]]]:
        data = self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        return parse_orderbook_levels(data)

    def get_trades(
        self,
        ticker: str,
        min_ts: int,
        max_ts: int,
        *,
        limit: int = 1000,
        max_pages: int = 5,
    ) -> list[Trade]:
        """Public trades for `ticker` between two Unix timestamps (seconds), oldest first."""
        params: JSON = {"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts, "limit": limit}
        trades = [
            parsed
            for row in self._paginate("/markets/trades", "trades", params, max_pages)
            if (parsed := parse_trade(row)) is not None
        ]
        return sorted(trades, key=lambda trade: trade.created_at)

    def get_events(
        self,
        *,
        series_ticker: str | None = None,
        status: str = "open",
        with_nested_markets: bool = False,
        limit: int = 200,
    ) -> list[JSON]:
        params: JSON = {
            "limit": limit,
            "status": status,
            "with_nested_markets": str(with_nested_markets).lower(),
        }
        if series_ticker:
            params["series_ticker"] = series_ticker
        return list(self._paginate("/events", "events", params, max_pages=5))

    def get_series(self, *, category: str | None = None) -> list[JSON]:
        params: JSON = {}
        if category:
            params["category"] = category
        return cast(list[JSON], self._request("GET", "/series", params=params).get("series", []))

    # ---- authenticated portfolio ----------------------------------------

    def get_balance(self) -> JSON:
        self._require_auth()
        return self._request("GET", "/portfolio/balance")

    def get_positions(self, ticker: str | None = None, max_pages: int = 50) -> list[JSON]:
        self._require_auth()
        params: JSON = {"limit": 1000}
        if ticker:
            params["ticker"] = ticker
        return list(self._paginate("/portfolio/positions", "market_positions", params, max_pages))

    def live_exposure(self, ticker: str, side: OutcomeSide) -> Decimal:
        """Contracts held on `side` plus contracts still resting in open orders for it."""
        held = Decimal(0)
        for p in self.get_positions(ticker=ticker):
            if p.get("ticker") == ticker:
                pos = _dec(p.get("position_fp"))
                if (pos > 0) == (side is OutcomeSide.yes):
                    held += abs(pos)
        for o in self.get_orders("resting", ticker=ticker):
            if o.get("ticker") == ticker and o.get("outcome_side") == side.value:
                held += _dec(o.get("remaining_count_fp"))
        return held

    def get_orders(
        self,
        status: str | None = "resting",
        ticker: str | None = None,
        max_pages: int = 50,
    ) -> list[JSON]:
        self._require_auth()
        params: JSON = {"limit": 1000}
        if status:
            params["status"] = status
        if ticker:
            params["ticker"] = ticker
        return list(self._paginate("/portfolio/orders", "orders", params, max_pages))

    def get_fills(self, ticker: str | None = None) -> list[JSON]:
        self._require_auth()
        params: JSON = {"ticker": ticker} if ticker else {}
        return cast(
            list[JSON], self._request("GET", "/portfolio/fills", params=params).get("fills", [])
        )

    def create_order(
        self,
        ticker: str,
        side: OutcomeSide,
        count: Decimal,
        price: Decimal,
        *,
        time_in_force: str = "good_till_canceled",
        post_only: bool = False,
        client_order_id: str | None = None,
    ) -> JSON:
        """Place a limit order to *buy* `count` contracts of `side` paying at most `price`
        dollars per contract *of that side*. Kalshi quotes everything from the YES leg:
        `bid` = buy YES at p, `ask` = sell YES at p (≡ buy NO at 1-p)."""
        self._require_auth()
        yes_price = price if side is OutcomeSide.yes else Decimal(1) - price
        body: JSON = {
            "ticker": ticker,
            "side": "bid" if side is OutcomeSide.yes else "ask",
            "count": f"{count:.2f}",
            "price": f"{yes_price:.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": post_only,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        return self._request("POST", "/portfolio/events/orders", json=body)

    def cancel_order(self, order_id: str) -> JSON:
        self._require_auth()
        return self._request("DELETE", f"/portfolio/events/orders/{order_id}")


# ---- public trade tape (maker backtest) ------------------------------------
#
# Added for scripts/maker_backtest.py. These sit beside `parse_trade`/`get_trades`
# and leave them untouched: the backtest needs the trade id, the block-trade flag,
# Kalshi's newer `taker_outcome_side` field and the /historical endpoints, and it
# pages without a cap under its own request budget.


class TapeUnavailable(RuntimeError):
    """An endpoint refused the request outright (e.g. /historical/trades without a key)."""


class BudgetExhausted(RuntimeError):
    """The run reached its request budget; resume from the cache on the next run."""


class PublicTrade:
    """One row of Kalshi's public trade tape, normalised.

    `taker_side` is the side the taker bought: `taker_outcome_side` when present,
    else the deprecated `taker_side`. A taker who sells YES into a resting YES bid
    shows `taker_outcome_side = no`.
    """

    __slots__ = (
        "trade_id",
        "ticker",
        "created_at",
        "yes_price",
        "count",
        "taker_side",
        "is_block",
        "raw_taker_side",
        "raw_taker_outcome_side",
    )

    def __init__(
        self,
        *,
        trade_id: str,
        ticker: str,
        created_at: datetime,
        yes_price: Decimal,
        count: Decimal,
        taker_side: OutcomeSide,
        is_block: bool = False,
        raw_taker_side: str | None = None,
        raw_taker_outcome_side: str | None = None,
    ) -> None:
        self.trade_id = trade_id
        self.ticker = ticker
        self.created_at = created_at
        self.yes_price = yes_price
        self.count = count
        self.taker_side = taker_side
        self.is_block = is_block
        self.raw_taker_side = raw_taker_side
        self.raw_taker_outcome_side = raw_taker_outcome_side

    def _key(self) -> tuple[Any, ...]:
        return tuple(getattr(self, name) for name in self.__slots__)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PublicTrade) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:
        return f"PublicTrade({self.trade_id!r}, {self.ticker!r}, {self.created_at.isoformat()})"

    def to_trade(self) -> Trade:
        return Trade(
            ticker=self.ticker,
            created_at=self.created_at,
            count=self.count,
            yes_price=self.yes_price,
            taker_side=self.taker_side,
        )


def _trade_ts(value: object) -> datetime | None:
    """A trade timestamp as aware UTC. Unlike `_ts`, tolerates any number of fractional
    digits (Python 3.10's fromisoformat takes only 3 or 6) and returns None when the
    text is not a timestamp."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    head, dot, rest = text.partition(".")
    if dot:
        digits = len(rest) - len(rest.lstrip("0123456789"))
        text = f"{head}.{rest[:digits][:6].ljust(6, '0')}{rest[digits:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _side_text(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def parse_public_trade(row: JSON) -> PublicTrade | None:
    """Parse one public trade row (live or historical); None when a needed field is missing.

    Side: `taker_outcome_side`, falling back to `taker_side` (deprecated by Kalshi).
    Price: `yes_price_dollars`, then `no_price_dollars`, then legacy `yes_price` in
    cents (then legacy `no_price`). Count: `count_fp`, then `count`.
    """
    outcome = _side_text(row.get("taker_outcome_side"))
    legacy = _side_text(row.get("taker_side"))
    side = outcome or legacy
    created = _trade_ts(row.get("created_time"))
    trade_id = str(row.get("trade_id") or "")
    yes_price: Decimal | None
    dollars = row.get("yes_price_dollars")
    no_dollars = row.get("no_price_dollars")
    if dollars not in (None, ""):
        yes_price = _dec(dollars)
    elif no_dollars not in (None, ""):
        yes_price = Decimal(1) - _dec(no_dollars)
    else:
        yes_price = _price_dollars({"yes_price": row.get("yes_price")}, "yes_price")
        if yes_price is None:
            no_price = _price_dollars({"no_price": row.get("no_price")}, "no_price")
            yes_price = None if no_price is None else Decimal(1) - no_price
    count = _opt_dec(row.get("count_fp"))
    if count is None:
        count = _opt_dec(row.get("count"))
    if (
        created is None
        or not trade_id
        or side not in {"yes", "no"}
        or yes_price is None
        or count is None
    ):
        return None
    return PublicTrade(
        trade_id=trade_id,
        ticker=str(row.get("ticker", "")),
        created_at=created.astimezone(timezone.utc),
        yes_price=yes_price,
        count=count,
        taker_side=OutcomeSide(side),
        is_block=bool(row.get("is_block_trade") or False),
        raw_taker_side=legacy,
        raw_taker_outcome_side=outcome,
    )


def parse_cutoff_ts(value: object) -> int | None:
    """`trades_created_ts` from /historical/cutoff as Unix seconds (ISO text, s or ms)."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if isinstance(value, (int, float)) or text.replace(".", "", 1).isdigit():
        number = int(float(text))
        return number // 1000 if number > 10**11 else number
    parsed = _ts(text)
    return None if parsed is None else int(parsed.timestamp())


class PoliteFetcher:
    """GET-only public Kalshi client with a request budget, a rate limit and retries.

    At most `rate_per_sec` requests a second; 429, 5xx and transport errors retry
    with exponential backoff up to `tries` attempts. Every attempt counts against
    `max_calls`; reaching it raises `BudgetExhausted` before the request is sent.
    401/403/404 raise `TapeUnavailable`; other 4xx raise `KalshiError`.
    """

    def __init__(
        self,
        base_url: str,
        *,
        max_calls: int = 6000,
        rate_per_sec: float = 4.0,
        tries: int = 5,
        client: httpx.Client | None = None,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_calls = max_calls
        self.min_interval = 1.0 / rate_per_sec
        self.tries = tries
        self.calls = 0
        self._client = client or httpx.Client(timeout=30.0)
        self._sleep = sleep
        self._monotonic = monotonic
        self._last: float | None = None
        self._cutoff: int | None = None
        self._cutoff_loaded = False

    def get(self, path: str, params: JSON | None = None) -> JSON:
        url = f"{self.base_url}{path}"
        for attempt in range(self.tries):
            if self.calls >= self.max_calls:
                raise BudgetExhausted(f"reached --max-calls={self.max_calls}")
            if self._last is not None:
                wait = self.min_interval - (self._monotonic() - self._last)
                if wait > 0:
                    self._sleep(wait)
            self._last = self._monotonic()
            self.calls += 1
            last_try = attempt == self.tries - 1
            try:
                resp = self._client.get(url, params=params)
            except httpx.TransportError as exc:
                if last_try:
                    raise KalshiError(0, f"transport error on {path}: {exc}") from exc
                self._sleep(2.0**attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if last_try:
                    raise KalshiError(resp.status_code, resp.text)
                self._sleep(2.0**attempt)
                continue
            if resp.status_code in (401, 403, 404):
                raise TapeUnavailable(f"{path}: HTTP {resp.status_code}: {resp.text[:200]}")
            if resp.status_code >= 400:
                raise KalshiError(resp.status_code, resp.text)
            return cast(JSON, resp.json()) if resp.content else {}
        raise KalshiError(0, f"retries exhausted on {path}")  # pragma: no cover

    def cutoff_ts(self) -> int | None:
        """GET /historical/cutoff once per fetcher; None when it is unavailable."""
        if not self._cutoff_loaded:
            self._cutoff_loaded = True
            try:
                data = self.get("/historical/cutoff")
            except TapeUnavailable:
                data = {}
            self._cutoff = parse_cutoff_ts(data.get("trades_created_ts"))
        return self._cutoff


LIVE_TRADES_PATH = "/markets/trades"
HISTORICAL_TRADES_PATH = "/historical/trades"


def trade_segments(min_ts: int, max_ts: int, cutoff: int | None) -> list[tuple[str, int, int]]:
    """Split [min_ts, max_ts] by the historical cutoff into (path, lo, hi) segments.

    Windows at or after the cutoff use /markets/trades, windows before it use
    /historical/trades, and a window that straddles it uses both, split at the
    cutoff. With no known cutoff everything goes to /markets/trades.
    """
    if cutoff is None or min_ts >= cutoff:
        return [(LIVE_TRADES_PATH, min_ts, max_ts)]
    if max_ts < cutoff:
        return [(HISTORICAL_TRADES_PATH, min_ts, max_ts)]
    return [(HISTORICAL_TRADES_PATH, min_ts, cutoff), (LIVE_TRADES_PATH, cutoff, max_ts)]


def iter_segment(
    fetcher: PoliteFetcher, path: str, ticker: str, min_ts: int, max_ts: int
) -> Iterator[JSON]:
    """Every raw trade row of one endpoint between two timestamps, following the cursor
    until it is empty (limit 1000, no page cap)."""
    params: JSON = {"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts, "limit": 1000}
    cursor: str | None = None
    while True:
        page = dict(params)
        if cursor:
            page["cursor"] = cursor
        data = fetcher.get(path, page)
        yield from data.get("trades") or []
        cursor = data.get("cursor") or None
        if not cursor:
            return


def iter_trades(
    fetcher: PoliteFetcher, ticker: str, min_ts: int, max_ts: int
) -> Iterator[tuple[JSON, PublicTrade | None]]:
    """(raw row, parsed trade) for `ticker` in [min_ts, max_ts], de-duplicated by trade_id.

    Reads the historical cutoff once (cached on the fetcher) and routes each part of
    the window to its endpoint (see `trade_segments`). Errors propagate, including
    `TapeUnavailable` and `BudgetExhausted`.
    """
    seen: set[str] = set()
    for path, lo, hi in trade_segments(min_ts, max_ts, fetcher.cutoff_ts()):
        for row in iter_segment(fetcher, path, ticker, lo, hi):
            key = str(row.get("trade_id") or "")
            if key in seen:
                continue
            if key:
                seen.add(key)
            yield row, parse_public_trade(row)
