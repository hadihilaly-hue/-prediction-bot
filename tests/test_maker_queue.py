from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pytest_httpx import HTTPXMock

from prediction_bot.config import KALSHI_DEMO_REST
from prediction_bot.maker_fill import queue_fill
from prediction_bot.maker_scoring import score_maker_shadows
from prediction_bot.models import MakerShadowCandidate, OutcomeSide, Trade
from prediction_bot.orderbooks import snapshot_orderbooks, snapshot_targets
from prediction_bot.paper.ledger import Ledger
from prediction_bot.venues.kalshi import KalshiClient, parse_orderbook_levels, parse_trade
from tests.test_sportsbook_v2 import (
    FakeOdds,
    FakeV3Kalshi,
    SportsbookV3Strategy,
    fixed_clock,
    make_market,
    v3_odds_event,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 23, 12, tzinfo=UTC)
TICKER = "KXNFLGAME-26OCT25KCLV-KC"


def _trade(minutes: int, count: int, yes_price: str, taker: str = "no") -> Trade:
    return Trade(
        TICKER,
        T0 + timedelta(minutes=minutes),
        Decimal(count),
        Decimal(yes_price),
        OutcomeSide(taker),
    )


def _fill(
    trades: list[Trade], *, queue: int, size: int = 100, side: str = "yes", price: str = "0.45"
) -> datetime | None:
    return queue_fill(
        trades, side, Decimal(price), Decimal(queue), Decimal(size), T0, T0 + timedelta(hours=8)
    )


# --- acceptance fixture ------------------------------------------------------------


def test_order_behind_300_does_not_fill_on_200_traded() -> None:
    tape = [_trade(5, 100, "0.45"), _trade(10, 100, "0.45")]
    assert _fill(tape, queue=300) is None


def test_order_behind_300_fills_on_400_traded() -> None:
    tape = [_trade(5 * i, 100, "0.45") for i in range(1, 5)]
    assert _fill(tape, queue=300) == T0 + timedelta(minutes=20)  # the 400th contract


def test_improving_order_fills_on_the_first_trade_at_its_price() -> None:
    tape = [_trade(7, 100, "0.45"), _trade(9, 100, "0.45")]
    assert _fill(tape, queue=0, size=50) == T0 + timedelta(minutes=7)


def test_only_trades_that_hit_our_side_at_or_through_the_price_count() -> None:
    tape = [
        _trade(1, 500, "0.46"),  # above our YES bid: other bids fill first
        _trade(2, 500, "0.44", taker="yes"),  # a YES buyer lifts asks, never our bid
        _trade(-5, 500, "0.45"),  # before placement
        _trade(3, 60, "0.44"),  # trades through our level: counts
    ]
    assert _fill(tape, queue=0, size=50) == T0 + timedelta(minutes=3)
    assert _fill(tape[:3], queue=0, size=50) is None
    # A NO bid at 0.55 is hit by YES-taker trades at yes_price >= 0.45.
    # A NO bid at 0.55 is hit by YES-taker trades with no price (1 - yes) <= 0.55,
    # i.e. yes_price >= 0.45; a YES trade at 0.44 (no price 0.56) does not reach it.
    no_tape = [
        _trade(2, 500, "0.44", taker="yes"),
        _trade(4, 30, "0.45", taker="yes"),
        _trade(6, 30, "0.46", taker="yes"),
    ]
    assert _fill(no_tape, queue=0, size=50, side="no", price="0.55") == T0 + timedelta(minutes=6)


# --- client parsing ----------------------------------------------------------------


def test_parse_trade_reads_fixed_point_and_legacy_fields() -> None:
    fixed = parse_trade(
        {
            "ticker": TICKER,
            "created_time": "2026-10-23T12:05:00Z",
            "count_fp": "12.00",
            "yes_price_dollars": "0.4500",
            "taker_side": "no",
        }
    )
    assert fixed == Trade(
        TICKER, T0 + timedelta(minutes=5), Decimal(12), Decimal("0.45"), OutcomeSide.no
    )
    legacy = parse_trade(
        {
            "ticker": TICKER,
            "created_time": "2026-10-23T12:05:00Z",
            "count": 3,
            "no_price": 1,
            "taker_side": "yes",
        }
    )
    assert legacy is not None and legacy.yes_price == Decimal("0.99") and legacy.count == 3
    assert parse_trade({"ticker": TICKER, "count": 1, "yes_price": 45}) is None


def test_get_trades_paginates_and_sorts(httpx_mock: HTTPXMock) -> None:
    base = f"{KALSHI_DEMO_REST}/markets/trades?ticker={TICKER}&min_ts=1&max_ts=9&limit=1000"
    row = {"ticker": TICKER, "count_fp": "1.00", "yes_price_dollars": "0.45", "taker_side": "no"}
    httpx_mock.add_response(
        url=base,
        json={"trades": [{**row, "created_time": "2026-10-23T12:09:00Z"}], "cursor": "c2"},
    )
    httpx_mock.add_response(
        url=f"{base}&cursor=c2",
        json={"trades": [{**row, "created_time": "2026-10-23T12:01:00Z"}], "cursor": ""},
    )
    trades = KalshiClient().get_trades(TICKER, 1, 9)
    assert [t.created_at.minute for t in trades] == [1, 9]


def test_parse_orderbook_levels_sorts_best_first() -> None:
    levels = parse_orderbook_levels(
        {"orderbook_fp": {"yes": [["0.44", "50"], ["0.45", "300"]], "no": [["0.53", "20"]]}}
    )
    assert levels["yes"] == [(Decimal("0.45"), Decimal(300)), (Decimal("0.44"), Decimal(50))]
    assert levels["no"] == [(Decimal("0.53"), Decimal(20))]


# --- queue at placement --------------------------------------------------------------


def _candidate(price: str, queue: Decimal | None = None, at: datetime = T0) -> MakerShadowCandidate:
    return MakerShadowCandidate(
        created_at=at,
        ticker=TICKER,
        side=OutcomeSide.yes,
        event_ticker="KXNFLGAME-26OCT25KCLV",
        position_key=f"key-{price}-{at.hour}",
        price=Decimal(price),
        size=Decimal(100),
        fair=Decimal("0.60"),
        maker_fee=Decimal("0.01"),
        expires_at=at + timedelta(hours=8),
        commence_time=at + timedelta(days=2),
        rules_version="v3",
        queue_ahead=queue,
        maker_price_rule="at_bid",
    )


def test_queue_ahead_comes_from_a_fresh_snapshot_else_the_quote(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "q.sqlite", Decimal("1000"))
    levels = {"yes": [(Decimal("0.45"), Decimal(300)), (Decimal("0.44"), Decimal(50))], "no": []}
    ledger.record_orderbook_snapshot(TICKER, levels, T0 - timedelta(minutes=5))
    ledger.record_maker_shadows([_candidate("0.45"), _candidate("0.46"), _candidate("0.43")])
    rows = ledger.conn.execute(
        "SELECT price,queue_ahead,queue_source,maker_price_rule FROM maker_shadow ORDER BY price"
    ).fetchall()
    assert rows == [
        ("0.43", "0", "orderbook", "at_bid"),  # no displayed size at 0.43
        ("0.45", "300", "orderbook", "at_bid"),
        ("0.46", "0", "orderbook", "at_bid"),  # improves the book
    ]
    late = T0 + timedelta(hours=2)  # snapshot now too old: fall back to the quote
    ledger.record_maker_shadows(
        [_candidate("0.45", Decimal(120), at=late), _candidate("0.44", at=late)]
    )
    later = ledger.conn.execute(
        "SELECT price,queue_ahead,queue_source FROM maker_shadow WHERE created_at=? ORDER BY price",
        (late.isoformat(),),
    ).fetchall()
    assert later == [("0.44", None, None), ("0.45", "120", "quote")]


def test_strategy_rests_at_the_bid_on_a_1c_spread_and_improves_on_2c(settings) -> None:  # type: ignore[no-untyped-def]
    settings.max_gross_edge = Decimal("0.20")

    def shadows(bid: str, ask: str) -> list[MakerShadowCandidate]:
        strategy = SportsbookV3Strategy(
            settings,
            FakeOdds([v3_odds_event()]),
            kalshi_client=FakeV3Kalshi(),
            ledger=Ledger(":memory:", Decimal("1000")),
            clock=fixed_clock("2026-10-22T10:00:00Z"),
        )
        rules = {"rules_primary": "If Miami wins the game, then the market resolves to Yes."}
        markets = [
            make_market(
                "KXNFLGAME-26OCT25MIAMIN-MIA",
                bid,
                ask,
                subtitle="Miami",
                raw=rules,
                open_interest=Decimal(200),
                yes_ask_size=Decimal(80),
                yes_bid_size=Decimal(70),
            ),
            make_market(
                "KXNFLGAME-26OCT25MIAMIN-MIN",
                "0.34",
                "0.36",
                subtitle="Minnesota",
                raw={
                    "rules_primary": "If Minnesota wins the game, then the market resolves to Yes."
                },
                open_interest=Decimal(200),
                yes_ask_size=Decimal(80),
            ),
        ]
        strategy.evaluate(markets)
        return [s for s in strategy.seen_maker_shadows() if s.ticker.endswith("-MIA")]

    (wide,) = shadows("0.54", "0.56")
    assert (wide.price, wide.maker_price_rule, wide.queue_ahead) == (
        Decimal("0.55"),
        "bid_plus_1",
        Decimal(0),
    )
    (tight,) = shadows("0.55", "0.56")
    assert (tight.price, tight.maker_price_rule, tight.queue_ahead) == (
        Decimal("0.55"),
        "at_bid",
        Decimal(70),
    )


# --- scoring and snapshots ---------------------------------------------------------


class FakeKalshi:
    def __init__(self, trades: list[Trade] | None = None, fail_trades: bool = False) -> None:
        self.trades = trades or []
        self.fail_trades = fail_trades
        self.book_calls: list[str] = []

    def get_candlesticks(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    def get_trades(self, ticker: str, min_ts: int, max_ts: int) -> list[Trade]:
        if self.fail_trades:
            raise RuntimeError("offline")
        return [t for t in self.trades if min_ts <= t.created_at.timestamp() <= max_ts]

    def get_orderbook_levels(self, ticker: str, depth: int = 20):  # type: ignore[no-untyped-def]
        self.book_calls.append(ticker)
        return {"yes": [(Decimal("0.45"), Decimal(10))], "no": []}


def test_scoring_records_queue_fills_beside_the_candle_rules(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("prediction_bot.maker_scoring.fetch_candles", lambda *_a: [])
    ledger = Ledger(tmp_path / "s.sqlite", Decimal("1000"))
    ledger.record_maker_shadows([_candidate("0.45", Decimal(300)), _candidate("0.46")])
    tape = [_trade(5 * i, 100, "0.45") for i in range(1, 5)]
    after = T0 + timedelta(hours=9)
    score_maker_shadows(ledger, FakeKalshi(tape), now=after)  # type: ignore[arg-type]
    rows = ledger.conn.execute(
        "SELECT price,fill_queue_at,queue_status,fill_trade_through_at,score_status "
        "FROM maker_shadow ORDER BY price"
    ).fetchall()
    assert rows[0] == ("0.45", (T0 + timedelta(minutes=20)).isoformat(), "filled", None, "scored")
    assert rows[1][1:3] == (None, "no_queue")  # no snapshot and no quote queue

    ledger2 = Ledger(tmp_path / "s2.sqlite", Decimal("1000"))
    ledger2.record_maker_shadows([_candidate("0.45", Decimal(0))])
    score_maker_shadows(ledger2, FakeKalshi(fail_trades=True), now=after)  # type: ignore[arg-type]
    assert ledger2.conn.execute(
        "SELECT queue_status,score_status FROM maker_shadow"
    ).fetchone() == (
        "unavailable",
        "scored",
    )


def test_snapshot_targets_prioritise_open_shadows_and_skip_fresh(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "t.sqlite", Decimal("1000"))
    ledger.record_maker_shadows([_candidate("0.45", Decimal(0))])
    ledger.conn.execute(
        "INSERT INTO shadow_decisions (created_at,rules_version,window,strategy,ticker,side,"
        "would_trade,reason,commence_time) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            (T0 - timedelta(hours=1)).isoformat(),
            "v3",
            "72h+",
            "sportsbook_v3",
            "KXNFLGAME-26OCT25NEAR-A",
            "yes",
            0,
            "net_edge",
            (T0 + timedelta(days=3)).isoformat(),
        ),
    )
    ledger.conn.commit()
    assert snapshot_targets(ledger, T0) == [TICKER, "KXNFLGAME-26OCT25NEAR-A"]
    client = FakeKalshi()
    assert snapshot_orderbooks(ledger, client, now=T0, max_calls=1) == 1  # type: ignore[arg-type]
    assert client.book_calls == [TICKER]
    assert snapshot_targets(ledger, datetime.now(UTC)) == []  # both snapshotted or past
