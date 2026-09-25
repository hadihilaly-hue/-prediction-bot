from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from prediction_bot.config import KALSHI_DEMO_REST
from prediction_bot.models import Market, OutcomeSide, Quote
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
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
    )


def parse_orderbook(ticker: str, ob: JSON) -> Quote:
    """The `orderbook` endpoint returns YES bids and NO bids only. A NO bid at p is a
    YES ask at 1-p. Levels are [price_dollars, size_fp] pairs."""
    book = ob.get("orderbook_fp") or ob.get("orderbook") or {}
    yes_levels = book.get("yes") or []
    no_levels = book.get("no") or []
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

    def get_orderbook(self, ticker: str, depth: int = 10) -> Quote:
        data = self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        return parse_orderbook(ticker, data)

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

    def get_positions(self) -> list[JSON]:
        self._require_auth()
        return cast(
            list[JSON], self._request("GET", "/portfolio/positions").get("market_positions", [])
        )

    def live_exposure(self, ticker: str, side: OutcomeSide) -> Decimal:
        """Contracts held on `side` plus contracts still resting in open orders for it."""
        held = Decimal(0)
        for p in self.get_positions():
            if p.get("ticker") == ticker:
                pos = _dec(p.get("position_fp"))
                if (pos > 0) == (side is OutcomeSide.yes):
                    held += abs(pos)
        for o in self.get_orders("resting"):
            if o.get("ticker") == ticker and o.get("outcome_side") == side.value:
                held += _dec(o.get("remaining_count_fp"))
        return held

    def get_orders(self, status: str | None = "resting") -> list[JSON]:
        self._require_auth()
        params: JSON = {"status": status} if status else {}
        return cast(
            list[JSON], self._request("GET", "/portfolio/orders", params=params).get("orders", [])
        )

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
