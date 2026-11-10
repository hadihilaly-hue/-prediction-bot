from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from prediction_bot import maker_backtest as mb
from prediction_bot import maker_backtest_io as bt_io
from prediction_bot.fees import MAKER_FEE_TYPE, FixedFee
from prediction_bot.models import OutcomeSide
from prediction_bot.paper.ledger import Ledger
from prediction_bot.trade_cache import (
    MISSING,
    OK,
    UNAVAILABLE,
    TradeCache,
    coverage_status,
    covered,
)
from prediction_bot.venues.kalshi import (
    HISTORICAL_TRADES_PATH,
    LIVE_TRADES_PATH,
    BudgetExhausted,
    PoliteFetcher,
    PublicTrade,
    TapeUnavailable,
    iter_trades,
    parse_cutoff_ts,
    parse_public_trade,
    trade_segments,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
TICKER = "KXNFLGAME-26NOV01AAABBB-AAA"
EVENT = "KXNFLGAME-26NOV01AAABBB"
FEES = FixedFee(MAKER_FEE_TYPE, Decimal(1))
FIXTURE = Path(__file__).parent / "fixtures" / "maker_backtest_report.md"


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


_ids = iter(range(1, 10**6))


def trade(
    minutes: float,
    yes_price: str,
    count: str,
    taker: str,
    *,
    block: bool = False,
    ticker: str = TICKER,
    trade_id: str | None = None,
) -> PublicTrade:
    return PublicTrade(
        trade_id=trade_id or f"t{next(_ids)}",
        ticker=ticker,
        created_at=at(minutes),
        yes_price=Decimal(yes_price),
        count=Decimal(count),
        taker_side=OutcomeSide(taker),
        is_block=block,
        raw_taker_outcome_side=taker,
    )


def snap(
    minutes: float,
    bid: str | None,
    ask: str | None,
    bid_size: str = "0",
    ask_size: str = "0",
) -> mb.Snapshot:
    return mb.Snapshot(
        at=at(minutes),
        yes_bid=None if bid is None else Decimal(bid),
        yes_ask=None if ask is None else Decimal(ask),
        yes_bid_size=Decimal(bid_size),
        yes_ask_size=Decimal(ask_size),
        event_ticker=EVENT,
    )


def decision(
    minutes: float = 0,
    *,
    side: str = "yes",
    price: str = "0.50",
    fair: str = "0.53",
    key: str | None = None,
    commence: datetime | None = None,
    clv: str | None = "2",
    status: str | None = "ok",
    ticker: str = TICKER,
    row_id: int | None = None,
) -> mb.Decision:
    return mb.Decision(
        id=row_id if row_id is not None else next(_ids),
        created_at=at(minutes),
        ticker=ticker,
        side=OutcomeSide(side),
        position_key=key or f"{ticker}|{side}",
        fair=Decimal(fair),
        price=Decimal(price),
        window="24-72h",
        commence_time=commence or at(24 * 60),
        clv_kalshi=None if clv is None else Decimal(clv),
        clv_status=status,
    )


def order(
    *,
    side: str = "yes",
    price: str = "0.49",
    queue: str = "100",
    size: str = "51",
    start: float = 0,
    end: float = 480,
    edge: str = "1.5",
    event: str = EVENT,
    dec: mb.Decision | None = None,
) -> mb.Order:
    p = Decimal(price)
    return mb.Order(
        decision=dec or decision(start, side=side),
        event_ticker=event,
        league="KXNFLGAME",
        book_at=at(start),
        bid=p,
        ask=p + Decimal("0.01"),
        rule="at_bid",
        price=p,
        queue_ahead=Decimal(queue),
        size=Decimal(size),
        start=at(start),
        end=at(end),
        kickoff=at(end + 5),
        kickoff_source="decision_row",
        maker_fee_c=Decimal("0.5"),
        taker_fee_c=Decimal("1.8"),
        edge_c=Decimal(edge),
    )


# ---- parser -----------------------------------------------------------------


def test_parse_public_trade_new_fields() -> None:
    t = parse_public_trade(
        {
            "trade_id": "abc",
            "ticker": TICKER,
            "created_time": "2026-11-01T12:00:00.123456789Z",
            "yes_price_dollars": "0.4500",
            "no_price_dollars": "0.5500",
            "count_fp": "12.00",
            "taker_outcome_side": "no",
            "taker_side": "no",
            "is_block_trade": False,
        }
    )
    assert t is not None
    assert t.yes_price == Decimal("0.45") and t.count == Decimal("12")
    assert t.taker_side is OutcomeSide.no and not t.is_block
    assert t.raw_taker_side == "no" and t.raw_taker_outcome_side == "no"
    assert t.created_at == T0 + timedelta(microseconds=123456)


def test_parse_public_trade_legacy_fields() -> None:
    t = parse_public_trade(
        {
            "trade_id": "x",
            "ticker": TICKER,
            "created_time": "2026-11-01T12:00:00Z",
            "yes_price": 37,
            "count": 5,
            "taker_side": "yes",
        }
    )
    assert t is not None
    assert t.yes_price == Decimal("0.37") and t.count == Decimal(5)
    assert t.taker_side is OutcomeSide.yes
    assert t.raw_taker_outcome_side is None


def test_parse_public_trade_fallbacks_and_block() -> None:
    t = parse_public_trade(
        {
            "trade_id": "y",
            "created_time": "2026-11-01T12:00:00Z",
            "no_price_dollars": "0.6000",
            "count_fp": "3.00",
            "taker_outcome_side": "YES",
            "taker_side": "no",
            "is_block_trade": True,
        }
    )
    assert t is not None
    assert t.yes_price == Decimal("0.4")
    # taker_outcome_side wins over the deprecated taker_side.
    assert t.taker_side is OutcomeSide.yes and t.is_block


@pytest.mark.parametrize(
    "row",
    [
        {"created_time": "2026-11-01T12:00:00Z", "yes_price": 40, "count": 1, "taker_side": "yes"},
        {"trade_id": "a", "yes_price": 40, "count": 1, "taker_side": "yes"},
        {
            "trade_id": "a",
            "created_time": "not a time",
            "yes_price": 40,
            "count": 1,
            "taker_side": "yes",
        },
        {"trade_id": "a", "created_time": "2026-11-01T12:00:00Z", "count": 1, "taker_side": "yes"},
        {"trade_id": "a", "created_time": "2026-11-01T12:00:00Z", "yes_price": 40, "count": 1},
        {
            "trade_id": "a",
            "created_time": "2026-11-01T12:00:00Z",
            "yes_price": 40,
            "taker_side": "yes",
        },
    ],
)
def test_parse_public_trade_missing_fields(row: dict[str, Any]) -> None:
    assert parse_public_trade(row) is None


def test_parse_cutoff_ts_forms() -> None:
    assert parse_cutoff_ts(1_762_000_000) == 1_762_000_000
    assert parse_cutoff_ts(1_762_000_000_000) == 1_762_000_000
    assert parse_cutoff_ts("2026-11-01T12:00:00Z") == int(T0.timestamp())
    assert parse_cutoff_ts(None) is None


# ---- endpoint routing and the polite fetcher -------------------------------------


def test_trade_segments_route_around_cutoff() -> None:
    assert trade_segments(100, 200, None) == [(LIVE_TRADES_PATH, 100, 200)]
    assert trade_segments(100, 200, 100) == [(LIVE_TRADES_PATH, 100, 200)]
    assert trade_segments(100, 200, 50) == [(LIVE_TRADES_PATH, 100, 200)]
    assert trade_segments(100, 200, 300) == [(HISTORICAL_TRADES_PATH, 100, 200)]
    assert trade_segments(100, 200, 150) == [
        (HISTORICAL_TRADES_PATH, 100, 150),
        (LIVE_TRADES_PATH, 150, 200),
    ]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def make_fetcher(
    handler: Callable[[httpx.Request], httpx.Response], max_calls: int = 100
) -> tuple[PoliteFetcher, FakeClock]:
    clock = FakeClock()
    fetcher = PoliteFetcher(
        "https://kalshi.test/trade-api/v2",
        max_calls=max_calls,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    return fetcher, clock


def raw(trade_id: str, ts: int, price: str = "0.5000", side: str = "no") -> dict[str, Any]:
    return {
        "trade_id": trade_id,
        "ticker": TICKER,
        "created_time": datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z"),
        "yes_price_dollars": price,
        "count_fp": "1.00",
        "taker_outcome_side": side,
        "is_block_trade": False,
    }


def test_iter_trades_straddles_cutoff_follows_cursor_and_dedupes() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/trade-api/v2")
        params = dict(request.url.params)
        calls.append((path, params))
        if path == "/historical/cutoff":
            return httpx.Response(200, json={"trades_created_ts": 150})
        if path == HISTORICAL_TRADES_PATH:
            if params.get("cursor") == "p2":
                return httpx.Response(200, json={"trades": [raw("h2", 140)], "cursor": ""})
            return httpx.Response(200, json={"trades": [raw("h1", 120)], "cursor": "p2"})
        # The live page repeats h2 at the boundary: it must be de-duplicated.
        return httpx.Response(200, json={"trades": [raw("h2", 140), raw("l1", 160)]})

    fetcher, _ = make_fetcher(handler)
    got = [parsed.trade_id for _, parsed in iter_trades(fetcher, TICKER, 100, 200) if parsed]
    assert got == ["h1", "h2", "l1"]
    paths = [p for p, _ in calls]
    assert paths.count("/historical/cutoff") == 1
    assert paths == [
        "/historical/cutoff",
        HISTORICAL_TRADES_PATH,
        HISTORICAL_TRADES_PATH,
        LIVE_TRADES_PATH,
    ]
    assert calls[1][1]["max_ts"] == "150" and calls[3][1]["min_ts"] == "150"
    assert all(p.get("limit") == "1000" for path, p in calls if path != "/historical/cutoff")
    # A second call reuses the cutoff.
    list(iter_trades(fetcher, TICKER, 160, 200))
    assert [p for p, _ in calls].count("/historical/cutoff") == 1


def test_fetcher_retries_rate_limits_and_budget() -> None:
    statuses = iter([429, 503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(status, json={"trades": []} if status == 200 else {})

    fetcher, clock = make_fetcher(handler, max_calls=3)
    assert fetcher.get("/markets/trades") == {"trades": []}
    assert fetcher.calls == 3
    # Backoff after each retryable failure, and the spacing never drops below 0.25s.
    assert clock.sleeps[:1] == [1.0]
    with pytest.raises(BudgetExhausted):
        fetcher.get("/markets/trades")


def test_fetcher_rate_limit_spacing() -> None:
    fetcher, clock = make_fetcher(lambda r: httpx.Response(200, json={}))
    for _ in range(3):
        fetcher.get("/x")
    assert clock.sleeps == [0.25, 0.25]


def test_fetcher_refusal_is_tape_unavailable() -> None:
    fetcher, _ = make_fetcher(lambda r: httpx.Response(401, text="auth required"))
    with pytest.raises(TapeUnavailable):
        fetcher.get(HISTORICAL_TRADES_PATH)
    # A refused cutoff means "no cutoff": everything routes to the live endpoint.
    assert fetcher.cutoff_ts() is None


# ---- fill rules ---------------------------------------------------------------------


def test_queue_fill_yes() -> None:
    o = order(price="0.49", queue="100", size="51")
    tape = [
        trade(10, "0.49", "100", "no"),
        trade(20, "0.49", "50", "no"),
        trade(30, "0.49", "1", "no"),
    ]
    f = mb.evaluate_fills(o, tape, [])
    assert f.q_at == at(30) and f.c_at == at(30) and f.first_rule == "Q"
    assert f.t_at == at(10) and f.x_at is None and f.a_at == at(0)
    assert f.matched_volume == Decimal(151)


def test_queue_never_trades_but_price_runs_through_yes() -> None:
    o = order(price="0.49", queue="5000", size="51")
    tape = [trade(10, "0.49", "10", "no"), trade(40, "0.47", "3", "no")]
    f = mb.evaluate_fills(o, tape, [])
    assert f.q_at is None
    assert f.x_at == at(40) and f.x_source == "trade"
    assert f.c_at == at(40) and f.first_rule == "X"
    assert f.t_at == at(10)


def test_run_over_by_snapshot_yes() -> None:
    o = order(price="0.49", queue="5000")
    later = [snap(30, "0.47", "0.50"), snap(60, "0.46", "0.49")]
    f = mb.evaluate_fills(o, [], later)
    assert f.q_at is None and f.t_at is None
    assert f.x_at == at(60) and f.x_source == "snapshot" and f.c_at == at(60)


def test_wrong_side_and_block_trades_never_fill() -> None:
    o = order(price="0.49", queue="0", size="10")
    tape = [
        trade(5, "0.49", "100", "yes"),  # a YES buyer lifts the ask side, not our bid
        trade(6, "0.45", "100", "no", block=True),  # block: ignored even though through
        trade(7, "0.50", "100", "no"),  # a NO taker above our price does not reach us
    ]
    f = mb.evaluate_fills(o, tape, [])
    assert f.q_at is None and f.x_at is None and f.c_at is None and f.t_at is None


def test_trades_outside_window_ignored() -> None:
    o = order(start=0, end=60, queue="0", size="1")
    f = mb.evaluate_fills(o, [trade(-1, "0.40", "5", "no"), trade(60, "0.40", "5", "no")], [])
    assert f.c_at is None


def mirror(t: PublicTrade) -> PublicTrade:
    return PublicTrade(
        trade_id=t.trade_id,
        ticker=t.ticker,
        created_at=t.created_at,
        yes_price=Decimal(1) - t.yes_price,
        count=t.count,
        taker_side=t.taker_side.opposite(),
        is_block=t.is_block,
    )


def test_no_side_mirrors_yes_side() -> None:
    tape = [
        trade(10, "0.49", "40", "no"),
        trade(20, "0.49", "70", "no"),
        trade(30, "0.48", "2", "no"),
    ]
    snaps = [snap(25, "0.47", "0.50")]
    for queue in ("100", "5000"):
        yes = mb.evaluate_fills(order(side="yes", queue=queue), tape, snaps)
        mirrored_snaps = [
            snap(25, str(1 - s.yes_ask), str(1 - s.yes_bid))  # type: ignore[operator]
            for s in snaps
        ]
        no = mb.evaluate_fills(
            order(side="no", queue=queue), [mirror(t) for t in tape], mirrored_snaps
        )
        assert (yes.q_at, yes.x_at, yes.c_at, yes.t_at, yes.first_rule) == (
            no.q_at,
            no.x_at,
            no.c_at,
            no.t_at,
            no.first_rule,
        )
        assert yes.matched_volume == no.matched_volume
    no = mb.evaluate_fills(order(side="no", queue="5000"), [mirror(t) for t in tape], [])
    assert no.q_at is None and no.x_at == at(30) and no.first_rule == "X"


# ---- orders ----------------------------------------------------------------------------


def test_build_orders_price_rule_queue_and_no_side() -> None:
    snaps = mb.SnapshotIndex(
        {
            TICKER: [snap(-2, "0.48", "0.50", bid_size="300", ask_size="700")],
            "OTHER-X": [snap(-1, "0.49", "0.50", bid_size="300", ask_size="700")],
        }
    )
    yes = decision(0, side="yes", price="0.50")
    no = decision(0, side="no", price="0.51", ticker="OTHER-X")
    no_wide = decision(0, side="no", price="0.52")
    built = mb.build_orders([yes, no, no_wide], snaps, {}, FEES)
    by_id = {o.decision.id: o for o in built.orders}
    o_yes = by_id[yes.id]
    assert (o_yes.rule, o_yes.price, o_yes.queue_ahead) == ("bid_plus_1", Decimal("0.49"), 0)
    assert o_yes.size == Decimal(51)  # floor(25 / 0.49)
    o_no = by_id[no.id]
    # NO bid = 1 - yes_ask = 0.50, 1c spread: join the bid behind yes_ask_size.
    assert (o_no.rule, o_no.price, o_no.queue_ahead) == ("at_bid", Decimal("0.50"), 700)
    assert o_no.bid == Decimal("0.50") and o_no.ask == Decimal("0.51")
    o_wide = by_id[no_wide.id]
    assert (o_wide.rule, o_wide.price) == ("bid_plus_1", Decimal("0.51"))
    # Edge = (fair - price) x 100 - maker fee per contract (rounded up per order).
    fee = mb.per_contract_fee_cents(TICKER, Decimal("0.49"), Decimal(51), "maker", FEES)
    assert o_yes.edge_c == (Decimal("0.53") - Decimal("0.49")) * 100 - fee
    assert o_yes.end == at(480) and o_yes.kickoff_source == "decision_row"


def test_build_orders_skips_and_window() -> None:
    snaps = mb.SnapshotIndex(
        {TICKER: [snap(0, "0.48", "0.50")], "LATE-X": [snap(10, "0.4", "0.5")]}
    )
    rows = [
        decision(6, price="0.50"),  # book 6 minutes old
        decision(1, price="0.53", key="k2"),  # ask moved by more than 1c
        decision(1, price="0.50", key="k3", commence=at(60)),  # odds kickoff 2h later
        decision(1, price="0.50", key="k4", commence=at(3)),  # closes before it opens
        decision(1, price="0.50", key="k5", ticker="LATE-X"),  # no book yet
        decision(1, price="0.51", key="k6", commence=at(120)),  # ask within 1c, kickoff off
    ]
    built = mb.build_orders(rows, snaps, {EVENT: at(180)}, FEES)
    # k3, k4 and k6: the row's kickoff is more than 30 minutes from the odds kickoff.
    assert dict(built.skips) == {
        "stale_snapshot": 1,
        "ask_mismatch": 1,
        "kickoff_mismatch": 3,
        "no_snapshot": 1,
    }
    assert built.orders == []


def test_build_orders_kickoff_and_closed_window() -> None:
    snaps = mb.SnapshotIndex({TICKER: [snap(0, "0.48", "0.50")]})
    kept = mb.build_orders([decision(1, commence=at(120))], snaps, {EVENT: at(130)}, FEES)
    assert len(kept.orders) == 1
    o = kept.orders[0]
    assert o.kickoff == at(130) and o.kickoff_source == "odds_snapshots"
    assert o.end == at(125)
    closed = mb.build_orders([decision(1, commence=at(5))], snaps, {}, FEES)
    assert closed.skips["window_closed"] == 1 and not closed.orders


def test_one_live_order_per_position() -> None:
    snaps = mb.SnapshotIndex({TICKER: [snap(m, "0.48", "0.50") for m in range(0, 1000, 4)]})
    rows = [decision(m, key="pos") for m in (0, 30, 479, 481, 600)]
    built = mb.build_orders(rows, snaps, {}, FEES)
    starts = [o.start for o in built.orders]
    assert starts == [at(0), at(481)]
    assert built.skips["position_busy"] == 3


def test_run_backtest_stops_after_fill_and_after_unavailable_tape() -> None:
    snaps = mb.SnapshotIndex({TICKER: [snap(m, "0.48", "0.50") for m in range(0, 2000, 4)]})
    rows = [decision(m, key="pos") for m in (0, 481, 962)]
    built = mb.build_orders(rows, snaps, {}, FEES)
    assert len(built.orders) == 3
    # The second order fills: the third is never placed.
    tape = {TICKER: [trade(500, "0.40", "1", "no")]}
    run = mb.run_backtest(built.orders, tape, snaps, lambda o: "ok", {})
    assert [r.filled() for r in run.results] == [False, True]
    assert run.dropped == {"after_fill": 1}
    # An unavailable tape on the first order drops the rest of the position.
    run2 = mb.run_backtest(built.orders, tape, snaps, lambda o: UNAVAILABLE, {})
    assert len(run2.results) == 1 and not run2.results[0].scored
    assert run2.dropped == {"after_tape_unavailable": 2}
    assert mb.check_no_overlap(run.results).status == mb.PASS


def test_check_no_overlap_detects_overlap() -> None:
    a = order(start=0, end=100)
    b = order(start=50, end=200, dec=decision(50, key=a.position_key))
    results = [mb.OrderResult(o, "ok", None, None, None, None, None) for o in (a, b)]
    assert mb.check_no_overlap(results).status == mb.FAIL


def test_outcomes_clv_settle_and_taker() -> None:
    d = decision(0, side="no", price="0.50", clv="3", status="ok")
    o = order(side="no", price="0.49", dec=d)
    t5, clv, settle, taker = mb.outcomes(o, "no")
    assert t5 == Decimal("0.53")
    assert clv == Decimal("4") - Decimal("0.5")
    assert settle == Decimal("51") - Decimal("0.5")
    assert taker == Decimal("3") - Decimal("1.8")
    t5, clv, settle, taker = mb.outcomes(order(dec=decision(0, clv="3", status="too_late")), "no")
    assert t5 is None and clv is None and taker is None
    assert settle == Decimal("-49") - Decimal("0.5")


# ---- statistics -------------------------------------------------------------------------


def test_clustered_se_hand_computed() -> None:
    values = [("A", 1.0), ("A", 3.0), ("B", 5.0), ("C", 7.0), ("C", 9.0)]
    est = mb.clustered_mean(values)
    # mean 5; cluster residual sums -6, 0, 6; meat 72; G/(G-1) = 1.5; sqrt(108) / 5.
    assert est.mean == pytest.approx(5.0)
    assert est.se == pytest.approx(math.sqrt(108) / 5)
    assert est.lo == pytest.approx(5.0 - 1.96 * math.sqrt(108) / 5)
    assert (est.n, est.games) == (5, 3)
    assert mb.clustered_mean([("A", 1.0), ("A", 2.0)]).se is None
    assert mb.clustered_mean([]).mean is None


def test_verdict_line_requires_fills_games_and_positive_interval() -> None:
    empty = mb.headline_cohorts([])
    assert mb.verdict_line(empty, []).startswith("NOT YET")
    results = []
    for g in range(40):
        for k in range(2):
            d = decision(0, clv=str(3 + (g % 3) * 0.5 + k), key=f"g{g}-{k}", row_id=g * 10 + k)
            o = order(event=f"EV{g}", dec=d, edge="1.2")
            fills = mb.evaluate_fills(o, [trade(1, "0.40", "1", "no")], [])
            t5, clv, settle, taker = mb.outcomes(o, None)
            results.append(mb.OrderResult(o, "ok", fills, t5, clv, settle, taker))
    head = mb.headline_cohorts(results)
    line = mb.verdict_line(head, [])
    assert line.startswith("PASS"), line
    assert mb.verdict_line(head, [mb.Check("tape sanity", mb.FAIL, "x")]).startswith("NOT YET")


# ---- validity checks ---------------------------------------------------------------------


def test_tape_sanity_and_side_semantics() -> None:
    snaps = mb.SnapshotIndex({TICKER: [snap(m, "0.48", "0.50") for m in range(0, 120, 10)]})
    good = [trade(m + 0.5, "0.48", "1", "no") for m in range(0, 120, 10)] + [
        trade(m + 1, "0.50", "2", "yes") for m in range(0, 120, 10)
    ]
    assert mb.check_tape_sanity(mb.tape_sample(good), 0, snaps).status == mb.PASS
    off = good + [trade(5, "0.90", "1", "yes") for _ in range(10)]
    assert mb.check_tape_sanity(mb.tape_sample(off), 0, snaps).status == mb.FAIL
    assert mb.check_tape_sanity([], 0, snaps).status == mb.FAIL
    assert mb.check_side_semantics(good, snaps).status == mb.PASS
    # Same prints, sides reversed: prints at the bid are now `yes` takers.
    reversed_sides = [
        PublicTrade(
            trade_id=t.trade_id,
            ticker=t.ticker,
            created_at=t.created_at,
            yes_price=t.yes_price,
            count=t.count,
            taker_side=t.taker_side.opposite(),
        )
        for t in good
    ]
    assert mb.check_side_semantics(reversed_sides, snaps).status == mb.FAIL
    disagree = [
        PublicTrade(
            trade_id="d",
            ticker=TICKER,
            created_at=at(1),
            yes_price=Decimal("0.48"),
            count=Decimal(1),
            taker_side=OutcomeSide.no,
            raw_taker_side="yes",
            raw_taker_outcome_side="no",
        )
    ]
    assert mb.check_side_semantics(good + disagree, snaps).status == mb.FAIL
    assert mb.check_side_semantics(good[:3], snaps).status == mb.WARN


def test_replay_shadows() -> None:
    shadow = mb.ShadowRow(
        id=4,
        created_at=at(0),
        ticker=TICKER,
        side=OutcomeSide.yes,
        price=Decimal("0.49"),
        size=Decimal(10),
        queue_ahead=Decimal(5),
        expires_at=at(100),
        fill_queue_at=at(20),
    )
    tape = {TICKER: [trade(10, "0.49", "5", "no"), trade(20, "0.49", "10", "no")]}
    check, rows = mb.replay_shadows([shadow], tape, lambda *a: "ok")
    assert check.status == mb.PASS and rows[0]["result"] == "match"
    moved = mb.ShadowRow(**{**shadow.__dict__, "fill_queue_at": at(90)})
    check, rows = mb.replay_shadows([moved], tape, lambda *a: "ok")
    assert check.status == mb.FAIL and rows[0]["result"] == "differs"
    unfilled = mb.ShadowRow(**{**shadow.__dict__, "id": 1, "fill_queue_at": None})
    check, rows = mb.replay_shadows([unfilled], tape, lambda *a: "ok")
    assert check.status == mb.PASS and "replay fills" in rows[0]["result"]


# ---- trade cache ---------------------------------------------------------------------------


def test_cache_coverage_and_round_trip(tmp_path: Path) -> None:
    cache = TradeCache(tmp_path / "c.sqlite")
    row = raw("a", int(T0.timestamp()))
    parsed = parse_public_trade(row)
    assert parsed is not None
    cache.add_trades(TICKER, [(row, parsed), (row, parsed)])
    assert cache.trade_count() == 1
    assert cache.trades(TICKER) == [parsed]
    cache.mark_window(TICKER, 100, 200, OK)
    cache.mark_window(TICKER, 200, 300, OK)
    cache.mark_window(TICKER, 400, 500, UNAVAILABLE)
    windows = cache.windows(TICKER)
    assert covered(windows, 120, 280)
    assert coverage_status(windows, 120, 280) == OK
    assert coverage_status(windows, 250, 350) == MISSING
    assert coverage_status(windows, 450, 460) == UNAVAILABLE
    cache.close()


# ---- end to end -------------------------------------------------------------------------------


def _ts(minutes: float) -> str:
    return at(minutes).isoformat()


def make_ledger(path: Path) -> None:
    Ledger(path, Decimal(1000)).conn.close()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO series_fees VALUES (?,?,?,?)",
        ("KXNFLGAME", MAKER_FEE_TYPE, "1", _ts(0)),
    )
    for i, m in enumerate(range(-10, 1500, 5)):
        conn.execute(
            "INSERT INTO market_snapshots (fetched_at, ticker, event_ticker, series, yes_bid,"
            " yes_ask, yes_bid_size, yes_ask_size, open_interest, volume, expected_expiration)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_ts(m), TICKER, EVENT, "KXNFLGAME", "0.48", "0.50", "200", "300", "0", str(i), ""),
        )
    rows = [
        # Edge >= 1c YES order that fills by queue.
        (1, 0, "yes", "0.50", "0.53", "2", "ok", "pos-yes"),
        # NO order with negative edge (the control line).
        (2, 0, "no", "0.52", "0.49", "1", "ok", "pos-no"),
        # Different reason: ignored.
        (3, 1, "yes", "0.50", "0.60", "2", "ok", "pos-other"),
    ]
    for row_id, m, side, price, fair, clv, status, key in rows:
        conn.execute(
            "INSERT INTO shadow_decisions (id, created_at, rules_version, window, strategy,"
            " ticker, side, position_key, would_trade, reason, fair, price, commence_time,"
            " clv_kalshi, clv_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row_id,
                _ts(m),
                "v3",
                "24-72h",
                "sportsbook_v3",
                TICKER,
                side,
                key,
                0,
                "net_edge" if row_id != 3 else "taken",
                fair,
                price,
                _ts(24 * 60),
                clv,
                status,
            ),
        )
    conn.execute("INSERT INTO settlements VALUES (?,?,?,?)", (TICKER, _ts(2000), "yes", "0"))
    conn.commit()
    conn.close()


def tape_handler(calls: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    start = int(T0.timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/trade-api/v2")
        calls.append(path)
        if path == "/historical/cutoff":
            return httpx.Response(200, json={"trades_created_ts": start - 3600})
        trades = [raw(f"q{i}", start + 600 + i * 60, "0.4900", "no") for i in range(5)]
        # 5 x 1 contract at our 0.49 bid would not clear 51; one large print does.
        trades.append({**raw("big", start + 1200, "0.4900", "no"), "count_fp": "60.00"})
        trades.append({**raw("blk", start + 300, "0.3000", "no"), "is_block_trade": True})
        # Ordinary prints for the sanity checks: at the bid (no takers) and the ask (yes).
        trades += [raw(f"b{i}", start + i * 300 + 30, "0.4800", "no") for i in range(1, 30)]
        trades += [raw(f"a{i}", start + i * 300 + 40, "0.5000", "yes") for i in range(1, 30)]
        return httpx.Response(200, json={"trades": trades, "cursor": ""})

    return handler


def test_end_to_end_run_is_read_only_and_reproducible(tmp_path: Path) -> None:
    db = tmp_path / "paper.sqlite"
    make_ledger(db)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    calls: list[str] = []
    fetcher, _ = make_fetcher(tape_handler(calls))
    cache = tmp_path / "trade_cache.sqlite"
    since = datetime(2026, 10, 23, tzinfo=UTC)
    out = bt_io.run(db, cache, since=since, until=None, fetcher=fetcher)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    s = out.summary
    assert {c.name: c.status for c in s.checks}["warm-cache rerun"] == mb.PASS
    assert {c.name: c.status for c in s.checks}["one order per position"] == mb.PASS
    assert len(out.results) == 2
    by_side = {r.order.side.value: r for r in out.results}
    yes = by_side["yes"]
    assert yes.order.rule == "bid_plus_1" and yes.order.price == Decimal("0.49")
    assert yes.fills is not None and yes.fills.q_at == at(20) and yes.filled()
    no = by_side["no"]
    # NO bid = 1 - 0.50 = 0.50 with a 2c spread: rest at 0.51. YES takers paying 0.50
    # sold NO at 0.50, through our 0.51 bid, so the order is run over (X) at once.
    assert no.order.rule == "bid_plus_1" and no.order.price == Decimal("0.51")
    assert no.fills is not None and no.fills.first_rule == "X" and no.filled()
    assert s.headline[2].fills == 1  # edge >= 1.0c
    assert s.headline[-1].orders == 1  # control
    paths = bt_io.write_outputs(out, tmp_path / "out")
    text = paths["summary.md"].read_text()
    assert text.index("Validity checks") < text.index("Headline") < text.index("Verdict")
    assert json.loads(paths["summary.json"].read_text())["verdict"] == s.verdict
    assert paths["orders.csv"].read_text().count("\n") == 3

    # Warm cache, no network: identical numbers and no requests.
    calls.clear()
    again = bt_io.run(db, cache, since=since, until=None, fetcher=None)
    assert again.summary.numbers() == s.numbers()
    fetcher2, _ = make_fetcher(tape_handler(calls))
    third = bt_io.run(db, cache, since=since, until=None, fetcher=fetcher2)
    assert calls == ["/historical/cutoff"]
    assert third.summary.numbers() == s.numbers()


def test_unavailable_history_is_counted(tmp_path: Path) -> None:
    db = tmp_path / "paper.sqlite"
    make_ledger(db)
    start = int(T0.timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/trade-api/v2")
        if path == "/historical/cutoff":
            return httpx.Response(200, json={"trades_created_ts": start + 10**6})
        return httpx.Response(403, text="forbidden")

    fetcher, _ = make_fetcher(handler)
    out = bt_io.run(
        db,
        tmp_path / "c.sqlite",
        since=datetime(2026, 10, 23, tzinfo=UTC),
        until=None,
        fetcher=fetcher,
    )
    assert all(r.tape == UNAVAILABLE for r in out.results)
    assert out.summary.build["orders_unscored"] == {UNAVAILABLE: 2}
    assert out.summary.verdict.startswith("NOT YET")
    assert "INVALID" in mb.render_markdown(out.summary)


# ---- report snapshot -----------------------------------------------------------------------------


def snapshot_summary() -> mb.Summary:
    results = []
    for g in range(6):
        for k, edge in enumerate(("1.2", "-0.4")):
            d = decision(0, clv=str(g - 1 + k), key=f"g{g}-{k}", row_id=g * 10 + k)
            o = order(event=f"EV{g}", dec=d, edge=edge, queue=str(50 * g))
            tape = [trade(1, "0.49", "500", "no", trade_id=f"s{g}{k}")] if g % 2 == 0 else []
            fills = mb.evaluate_fills(o, tape, [])
            t5, clv, settle, taker = mb.outcomes(o, "yes" if g % 3 else "no")
            results.append(mb.OrderResult(o, "ok", fills, t5, clv, settle, taker))
    run = mb.BacktestRun(results=results)
    build = mb.BuildResult(orders=[r.order for r in results], decisions=15)
    build.skips.update({"ask_mismatch": 2, "position_busy": 1})
    checks = [
        mb.Check("tape sanity", mb.PASS, "ok"),
        mb.Check("side semantics", mb.PASS, "ok"),
        mb.check_no_overlap(results),
    ]
    return mb.summarize(
        run,
        build,
        checks,
        params={"since": "2026-10-23T00:00:00+00:00", "until": None},
        tape={"planned windows": 6},
    )


def test_report_snapshot() -> None:
    text = mb.render_markdown(snapshot_summary())
    if not FIXTURE.exists():  # pragma: no cover - first run writes the snapshot
        FIXTURE.write_text(text)
    assert text == FIXTURE.read_text()
    assert text.splitlines()[0] == "# Maker backtest from the public trade tape"
    assert "INVALID" not in text
    assert "**Verdict:** NOT YET" in text
    assert "## Limits" in text and "power-devig" in text
