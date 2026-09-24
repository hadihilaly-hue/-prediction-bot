from __future__ import annotations

from decimal import Decimal

import pytest

from prediction_bot.config import Settings
from prediction_bot.models import Market, Quote


@pytest.fixture
def settings(tmp_path):  # type: ignore[no-untyped-def]
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        paper_db_path=tmp_path / "paper.sqlite",
        paper_starting_cash=Decimal("100"),
        max_order_notional=Decimal("10"),
        min_edge=Decimal("0.02"),
    )


def make_market(ticker: str, yes_bid: str | None, yes_ask: str | None, **kw: object) -> Market:
    defaults: dict[str, object] = dict(
        event_ticker=ticker.rsplit("-", 1)[0],
        title=ticker,
        subtitle=str(kw.pop("subtitle", "")),
        status="active",
        close_time=None,
        volume=Decimal(100),
        result=str(kw.pop("result", "")),
    )
    defaults.update(kw)
    q = Quote(
        ticker=ticker,
        yes_bid=None if yes_bid is None else Decimal(yes_bid),
        yes_ask=None if yes_ask is None else Decimal(yes_ask),
        yes_bid_size=Decimal(50),
        yes_ask_size=Decimal(50),
    )
    return Market(ticker=ticker, quote=q, **defaults)  # type: ignore[arg-type]
