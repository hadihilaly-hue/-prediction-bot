from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from prediction_bot.engine import Engine
from prediction_bot.fees import (
    DEFAULT_FEE_SCHEDULE,
    FEE_MODEL_VERSION,
    FixedFee,
    SeriesFeeCache,
    fee_for_ticker,
)
from prediction_bot.models import OutcomeSide
from prediction_bot.paper.ledger import Ledger
from prediction_bot.strategies.base import Strategy
from prediction_bot.strategies.book_scanner import BookScannerStrategy
from tests.conftest import make_market

# Series schedules as Kalshi's public /series reported them (docs/PIPELINE.md, Oct 23).
SERIES = [
    {"ticker": "KXNFLGAME", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1},
    {"ticker": "KXMLBGAME", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": "0.5"},
    {"ticker": "KXMLSGAME", "fee_type": "quadratic", "fee_multiplier": 1},
    {"ticker": "KXUFCFIGHT", "fee_type": "quadratic", "fee_multiplier": 1},
]

# (ticker, price, contracts, style, rounding, expected) — hand-computed as
# coefficient (taker 0.07, maker 0.0175 or 0) x multiplier x C x P x (1-P), rounded up.
FEE_TABLE = [
    # NFL, multiplier 1, maker fees
    ("KXNFLGAME-26NOV01ARIDAL-ARI", "0.68", 1, "taker", "cent", "0.02"),  # 0.015232
    ("KXNFLGAME-26NOV01ARIDAL-ARI", "0.68", 48, "taker", "cent", "0.74"),  # 0.731136
    ("KXNFLGAME-26NOV01ARIDAL-ARI", "0.50", 100, "taker", "cent", "1.75"),  # 1.75 exact
    ("KXNFLGAME-26NOV01ARIDAL-ARI", "0.50", 100, "maker", "cent", "0.44"),  # 0.4375
    ("KXNFLGAME-26NOV01ARIDAL-ARI", "0.68", 1, "taker", "centicent", "0.0153"),  # 0.015232
    ("KXNFLGAME-26NOV01ARIDAL-ARI", "0.50", 1, "taker", None, "0.0175"),
    # MLB game, multiplier 0.5, maker fees
    ("KXMLBGAME-26OCT041600SDMIL-SD", "0.50", 100, "taker", "cent", "0.88"),  # 0.875
    ("KXMLBGAME-26OCT041600SDMIL-SD", "0.50", 100, "maker", "cent", "0.22"),  # 0.21875
    ("KXMLBGAME-26OCT041600SDMIL-SD", "0.40", 10, "taker", "cent", "0.09"),  # 0.084
    ("KXMLBGAME-26OCT041600SDMIL-SD", "0.50", 100, "maker", "centicent", "0.2188"),
    ("KXMLBGAME-26OCT041600SDMIL-SD", "0.50", 1, "taker", None, "0.00875"),
    # MLS, quadratic: taker fee only
    ("KXMLSGAME-26OCT28CHIORL-TIE", "0.21", 10, "taker", "cent", "0.12"),  # 0.11613
    ("KXMLSGAME-26OCT28CHIORL-TIE", "0.21", 10, "maker", "cent", "0.00"),
    # UFC, quadratic: taker fee only
    ("KXUFCFIGHT-26OCT24THICHO-THI", "0.55", 20, "taker", "cent", "0.35"),  # 0.3465
    ("KXUFCFIGHT-26OCT24THICHO-THI", "0.55", 20, "maker", "cent", "0.00"),
]


@pytest.mark.parametrize(("ticker", "price", "count", "style", "rounding", "expected"), FEE_TABLE)
def test_fee_for_ticker_reads_each_series_schedule(  # type: ignore[no-untyped-def]
    tmp_path, ticker, price, count, style, rounding, expected
) -> None:
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "fees.sqlite", Decimal("100"))
    cache = SeriesFeeCache(ledger, FakeKalshi(SERIES), clock=lambda: now)  # type: ignore[arg-type]
    fee = fee_for_ticker(
        ticker, Decimal(price), Decimal(count), style, schedule=cache, rounding=rounding
    )
    assert fee == Decimal(expected)


def test_fee_for_ticker_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported fee rounding"):
        fee_for_ticker(
            "KXNFLGAME-X-A", Decimal("0.5"), Decimal(1), "taker",
            schedule=DEFAULT_FEE_SCHEDULE, rounding="mill",
        )  # fmt: skip
    for style in ("taker", "maker"):
        with pytest.raises(ValueError, match="unknown fee type"):
            fee_for_ticker(
                "KXNFLGAME-X-A", Decimal("0.5"), Decimal(1), style,
                schedule=FixedFee("unknown", Decimal(1)),
            )  # fmt: skip

    class Empty:
        def get(self, series_ticker: str) -> None:
            return None

    with pytest.raises(ValueError, match="no fee schedule for series KXNFLGAME"):
        fee_for_ticker("KXNFLGAME-X-A", Decimal("0.5"), Decimal(1), "taker", schedule=Empty())
    with pytest.raises(ValueError, match="price must be between"):
        fee_for_ticker(
            "KXNFLGAME-X-A", Decimal("1.5"), Decimal(1), "taker", schedule=DEFAULT_FEE_SCHEDULE
        )
    assert FEE_MODEL_VERSION == "kalshi-series-v2"


class FakeKalshi:
    def __init__(self, series: list[dict[str, object]] | None = None, fail: bool = False):
        self.series = series or []
        self.fail = fail
        self.calls = 0

    def get_series(self) -> list[dict[str, object]]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("offline")
        return self.series


def test_series_fee_cache_refreshes_once_and_persists(tmp_path) -> None:
    now = datetime(2026, 10, 23, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "fees.sqlite", Decimal("100"))
    kalshi = FakeKalshi(
        [{"ticker": "KXNHLGAME", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}]
    )
    cache = SeriesFeeCache(ledger, kalshi, clock=lambda: now)  # type: ignore[arg-type]

    assert cache.get("KXNHLGAME") == ("quadratic_with_maker_fees", Decimal(1))
    assert cache.get("KXNHLGAME") == ("quadratic_with_maker_fees", Decimal(1))
    assert kalshi.calls == 1


def test_series_fee_cache_fails_closed_for_unknown_and_uses_stale_value(tmp_path) -> None:
    now = datetime(2026, 10, 23, tzinfo=timezone.utc)
    ledger = Ledger(tmp_path / "fees.sqlite", Decimal("100"))
    cache = SeriesFeeCache(
        ledger,  # type: ignore[arg-type]
        FakeKalshi([{"ticker": "KXNHLGAME", "fee_type": "unknown", "fee_multiplier": 1}]),
        clock=lambda: now,
    )
    assert cache.get("KXNHLGAME") == ("unknown", Decimal(1))

    ledger.save_series_fee(
        "KXNHLGAME",
        "quadratic_with_maker_fees",
        Decimal("0.5"),
        now - timedelta(hours=25),
    )
    stale_cache = SeriesFeeCache(
        ledger,  # type: ignore[arg-type]
        FakeKalshi(fail=True),
        clock=lambda: now,
    )
    assert stale_cache.get("KXNHLGAME") == ("quadratic_with_maker_fees", Decimal("0.5"))


def test_series_fee_cache_refreshes_after_24_hours_only_from_successful_fetch(tmp_path) -> None:
    now = [datetime(2026, 10, 23, tzinfo=timezone.utc)]
    ledger = Ledger(tmp_path / "refresh-fees.sqlite", Decimal("100"))
    kalshi = FakeKalshi(
        [{"ticker": "KXNHLGAME", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}]
    )
    cache = SeriesFeeCache(ledger, kalshi, clock=lambda: now[0])  # type: ignore[arg-type]

    assert cache.get("KXNHLGAME") == ("quadratic_with_maker_fees", Decimal(1))
    first_fetched_at = ledger.get_series_fee("KXNHLGAME")[2]  # type: ignore[index]

    now[0] += timedelta(hours=25)
    kalshi.fail = True
    assert cache.get("KXNHLGAME") == ("quadratic_with_maker_fees", Decimal(1))
    assert ledger.get_series_fee("KXNHLGAME")[2] == first_fetched_at  # type: ignore[index]

    kalshi.fail = False
    kalshi.series = [
        {"ticker": "KXNHLGAME", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 2}
    ]
    now[0] += timedelta(hours=1)
    assert cache.get("KXNHLGAME") == ("quadratic_with_maker_fees", Decimal(2))
    assert kalshi.calls == 3


class _Probe(Strategy):
    name = "probe"

    def evaluate(self, markets):  # type: ignore[no-untyped-def]
        return []


def test_v1_make_signal_reads_the_series_schedule(settings) -> None:  # type: ignore[no-untyped-def]
    market = make_market("KXMLSGAME-26OCT28CHIORL-CHI", yes_bid="0.40", yes_ask="0.45")
    strat = _Probe(settings)
    general = strat.make_signal(market, OutcomeSide.yes, Decimal("0.50"), "", taker=False)
    assert general is not None and general.limit_price == Decimal("0.41")
    assert general.edge == Decimal("0.08")  # 0.0175 x 0.41 x 0.59 = 0.0042 -> 1c maker fee

    strat.fee_schedule = FixedFee("quadratic", Decimal(1))
    mls = strat.make_signal(market, OutcomeSide.yes, Decimal("0.50"), "", taker=False)
    assert mls is not None
    assert mls.edge == Decimal("0.09")  # quadratic series: no maker fee
    assert (mls.fee_type, mls.fee_multiplier) == ("quadratic", Decimal(1))

    strat.fee_schedule = FixedFee("flat", Decimal(1))
    assert strat.make_signal(market, OutcomeSide.yes, Decimal("0.50"), "", taker=False) is None


def test_book_scanner_and_engine_use_the_mlb_half_multiplier(settings) -> None:  # type: ignore[no-untyped-def]
    settings.min_edge = Decimal("0.01")
    # 3c gap at 20 contracts: general-schedule fees are 35c a leg (edge -0.5c), but the
    # MLB game series' 0.5 multiplier makes them 18c a leg (edge +1.2c).
    market = make_market("KXMLBGAME-26OCT041600SDMIL-SD", yes_bid="0.52", yes_ask="0.49")
    assert BookScannerStrategy(settings).evaluate([market]) == []

    strat = BookScannerStrategy(settings)
    strat.fee_schedule = FixedFee("quadratic_with_maker_fees", Decimal("0.5"))
    signals = strat.evaluate([market])
    assert [s.edge for s in signals] == [Decimal("0.012"), Decimal("0.012")]

    ledger = Ledger(settings.paper_db_path, settings.paper_starting_cash)
    Engine(settings, object(), [strat], ledger).run_cycle([market])  # type: ignore[arg-type]
    rows = ledger.conn.execute("SELECT fee,fee_model_version FROM fills ORDER BY id").fetchall()
    assert rows == [("0.18", "kalshi-series-v2"), ("0.18", "kalshi-series-v2")]


def test_cli_strategies_share_one_series_fee_cache(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from prediction_bot.cli import _strategies
    from prediction_bot.config import Settings

    settings = Settings(_env_file=None, paper_db_path=tmp_path / "p.sqlite")  # type: ignore[call-arg]
    ledger = Ledger(settings.paper_db_path, Decimal("100"))
    strategies = _strategies(
        settings,
        ["book_scanner", "sportsbook_arb"],
        kalshi=FakeKalshi(SERIES),
        ledger=ledger,  # type: ignore[arg-type]
    )
    schedules = {id(s.fee_schedule) for s in strategies}
    assert len(schedules) == 1
    assert isinstance(strategies[0].fee_schedule, SeriesFeeCache)
    assert _strategies(settings, ["book_scanner"])[0].fee_schedule is DEFAULT_FEE_SCHEDULE
