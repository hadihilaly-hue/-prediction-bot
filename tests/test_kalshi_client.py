from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from pytest_httpx import HTTPXMock

from prediction_bot.config import KALSHI_DEMO_REST
from prediction_bot.models import OutcomeSide
from prediction_bot.venues.kalshi import KalshiClient, KalshiError, parse_market, parse_orderbook
from prediction_bot.venues.kalshi_auth import KalshiSigner

MARKET = {
    "ticker": "KXNFLGAME-26OCT01AB-A",
    "event_ticker": "KXNFLGAME-26OCT01AB",
    "title": "A wins",
    "yes_sub_title": "A",
    "status": "active",
    "close_time": "2026-10-01T23:00:00Z",
    "yes_bid_dollars": "0.4500",
    "yes_ask_dollars": "0.4800",
    "yes_bid_size_fp": "10.00",
    "yes_ask_size_fp": "20.00",
    "volume_fp": "123.00",
    "result": "",
}


def test_parse_market_treats_empty_book_as_none() -> None:
    m = parse_market({**MARKET, "yes_bid_dollars": "0.0000", "yes_ask_dollars": "1.0000"})
    assert m.quote.yes_bid is None and m.quote.yes_ask is None
    m2 = parse_market(MARKET)
    assert m2.quote.yes_bid == Decimal("0.45")
    assert m2.quote.ask_for(OutcomeSide.no) == Decimal("0.55")
    assert m2.quote.spread == Decimal("0.03")
    assert m2.close_time is not None and m2.close_time.year == 2026


def test_parse_orderbook_converts_no_bids_to_yes_asks() -> None:
    q = parse_orderbook(
        "T",
        {
            "orderbook_fp": {
                "yes": [["0.40", "5"], ["0.42", "3"]],
                "no": [["0.55", "7"], ["0.50", "1"]],
            }
        },
    )
    assert q.yes_bid == Decimal("0.42") and q.yes_bid_size == 3
    assert q.yes_ask == Decimal("0.45") and q.yes_ask_size == 7


def test_get_markets_paginates(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{KALSHI_DEMO_REST}/markets?limit=200&status=open",
        json={"markets": [MARKET], "cursor": "c2"},
    )
    httpx_mock.add_response(
        url=f"{KALSHI_DEMO_REST}/markets?limit=200&status=open&cursor=c2",
        json={"markets": [{**MARKET, "ticker": "X-B"}], "cursor": ""},
    )
    ms = KalshiClient().get_markets()
    assert [m.ticker for m in ms] == ["KXNFLGAME-26OCT01AB-A", "X-B"]


def test_retries_on_429_then_raises_on_4xx(httpx_mock: HTTPXMock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("prediction_bot.venues.kalshi.time.sleep", lambda _s: None)
    httpx_mock.add_response(status_code=429)
    httpx_mock.add_response(json={"market": MARKET})
    assert KalshiClient().get_market("T").ticker == MARKET["ticker"]
    httpx_mock.add_response(status_code=404, text="not found")
    with pytest.raises(KalshiError) as ei:
        KalshiClient().get_market("nope")
    assert ei.value.status == 404


def test_auth_required_for_portfolio() -> None:
    with pytest.raises(PermissionError):
        KalshiClient().get_balance()


def test_create_order_signs_and_quotes_from_yes_leg(httpx_mock: HTTPXMock) -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    client = KalshiClient(signer=KalshiSigner("kid", key))
    httpx_mock.add_response(json={"order": {"order_id": "o1"}})
    client.create_order("T", OutcomeSide.no, Decimal(3), Decimal("0.30"), client_order_id="cid")
    req: httpx.Request = httpx_mock.get_requests()[0]
    body = json.loads(req.content)
    assert body == {
        "ticker": "T",
        "side": "ask",  # buying NO == selling YES
        "count": "3.00",
        "price": "0.7000",  # NO at 0.30 == YES leg at 0.70
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "client_order_id": "cid",
    }
    assert req.headers["KALSHI-ACCESS-KEY"] == "kid"
    assert req.headers["KALSHI-ACCESS-SIGNATURE"]
    assert req.url.path == "/trade-api/v2/portfolio/events/orders"


@pytest.mark.parametrize(
    ("url", "demo"),
    [
        (KALSHI_DEMO_REST, True),
        ("https://api.elections.kalshi.com/trade-api/v2", False),
        ("https://api.elections.kalshi.com/trade-api/v2/demo", False),
        ("https://demo.kalshi.co.evil.example/trade-api/v2", False),
        ("https://api.kalshi.com/demo-trade-api/v2", False),
    ],
)
def test_is_demo_checks_hostname_not_substring(url: str, demo: bool) -> None:
    assert KalshiClient(base_url=url).is_demo is demo


def test_live_exposure_counts_positions_and_resting_orders(httpx_mock: HTTPXMock) -> None:
    client = KalshiClient(signer=KalshiSigner("kid", ed25519.Ed25519PrivateKey.generate()))
    pos_url = f"{KALSHI_DEMO_REST}/portfolio/positions?limit=1000&ticker=T"
    ord_url = f"{KALSHI_DEMO_REST}/portfolio/orders?limit=1000&status=resting&ticker=T"
    httpx_mock.add_response(
        url=pos_url,
        json={"market_positions": [{"ticker": "T", "position_fp": "-4.00"}]},
        is_reusable=True,
    )
    # resting orders span two pages; the cap must count both
    httpx_mock.add_response(
        url=ord_url,
        json={
            "orders": [{"ticker": "T", "outcome_side": "no", "remaining_count_fp": "2.00"}],
            "cursor": "c2",
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=ord_url + "&cursor=c2",
        json={
            "orders": [
                {"ticker": "T", "outcome_side": "no", "remaining_count_fp": "5.00"},
                {"ticker": "T", "outcome_side": "yes", "remaining_count_fp": "7.00"},
            ]
        },
        is_reusable=True,
    )
    assert client.live_exposure("T", OutcomeSide.no) == Decimal(11)
    assert client.live_exposure("T", OutcomeSide.yes) == Decimal(7)
