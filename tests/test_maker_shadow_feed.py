"""Maker shadow (PR 2): tape parsing, run-over fills, the net gate and the shadow feed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pytest_httpx import HTTPXMock

from prediction_bot.config import KALSHI_DEMO_REST, Settings
from prediction_bot.maker_fill import queue_fill, run_over_fill
from prediction_bot.maker_gate import evaluate_maker_gate
from prediction_bot.maker_scoring import score_maker_shadows
from prediction_bot.models import MakerShadowCandidate, OutcomeSide, Trade
from prediction_bot.paper.ledger import Ledger
from prediction_bot.venues.kalshi import KalshiClient, parse_trade
from tests.test_sportsbook_v2 import v3_markets
from tests.test_v5_entry_rules import _strategy

TICKER = "KXNFLGAME-26OCT25KCLV-KC"
T0 = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


# --- 2.1 trade parsing --------------------------------------------------------------


def _row(**fields: object) -> dict[str, object]:
    return {
        "ticker": TICKER,
        "created_time": "2026-10-23T12:05:00Z",
        "count_fp": "10.00",
        "yes_price_dollars": "0.4500",
        **fields,
    }


def test_tape_with_only_taker_outcome_side_parses() -> None:
    trade = parse_trade(_row(taker_outcome_side="no"))
    assert trade is not None and trade.taker_side is OutcomeSide.no
    # taker_outcome_side wins over the deprecated field when both are present.
    both = parse_trade(_row(taker_outcome_side="yes", taker_side="no"))
    assert both is not None and both.taker_side is OutcomeSide.yes


def test_block_trades_are_ignored(httpx_mock: HTTPXMock) -> None:
    assert parse_trade(_row(taker_outcome_side="no", is_block_trade=True)) is None
    base = f"{KALSHI_DEMO_REST}/markets/trades?ticker={TICKER}&min_ts=1&max_ts=9&limit=1000"
    httpx_mock.add_response(
        url=base,
        json={
            "trades": [
                _row(trade_id="a", taker_outcome_side="no"),
                _row(trade_id="b", taker_outcome_side="no", is_block_trade=True),
            ],
            "cursor": "",
        },
    )
    trades = KalshiClient().get_trades(TICKER, 1, 9)
    assert len(trades) == 1 and trades[0].count == Decimal(10)


def test_get_trades_follows_the_cursor_past_five_pages(httpx_mock: HTTPXMock) -> None:
    base = f"{KALSHI_DEMO_REST}/markets/trades?ticker={TICKER}&min_ts=1&max_ts=9&limit=1000"
    for page in range(8):
        httpx_mock.add_response(
            url=base if page == 0 else f"{base}&cursor=c{page}",
            json={
                "trades": [_row(trade_id=f"t{page}", taker_outcome_side="no")],
                "cursor": f"c{page + 1}" if page < 7 else "",
            },
        )
    assert len(KalshiClient().get_trades(TICKER, 1, 9)) == 8


# --- 2.2 fill definition --------------------------------------------------------------


def _trade(minute: int, count: int, yes_price: str, taker: str = "no") -> Trade:
    return Trade(
        TICKER,
        T0 + timedelta(minutes=minute),
        Decimal(count),
        Decimal(yes_price),
        OutcomeSide(taker),
    )


def test_queue_that_never_trades_while_price_runs_through_is_a_run_over_fill() -> None:
    # Resting YES at 0.45 behind 10,000. The queue cancels and price runs to 0.43.
    tape = [_trade(5, 20, "0.44", taker="yes"), _trade(9, 30, "0.43", taker="yes")]
    end = T0 + timedelta(hours=2)
    assert queue_fill(tape, "yes", Decimal("0.45"), Decimal(10_000), Decimal(1), T0, end) is None
    assert run_over_fill(tape, [], [], "yes", Decimal("0.45"), T0, end) == T0 + timedelta(minutes=5)
    # A quote snapshot with the ask at our price counts too (NO side: ask = 1 - yes_bid).
    quotes = [(T0 + timedelta(minutes=3), Decimal("0.55"), Decimal("0.57"))]
    assert run_over_fill([], [], quotes, "no", Decimal("0.45"), T0, end) == T0 + timedelta(
        minutes=3
    )
    # Trading at (not through) our price is not a run-over.
    assert run_over_fill([_trade(5, 20, "0.45")], [], [], "yes", Decimal("0.45"), T0, end) is None


class _Kalshi:
    def __init__(self, trades: list[Trade], candles: list[dict[str, object]] | None = None):
        self.trades, self.candles = trades, candles or []

    def get_candlesticks(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return self.candles

    def get_trades(self, ticker: str, min_ts: int, max_ts: int) -> list[Trade]:
        return self.trades


def _shadow(ledger: Ledger, queue_ahead: Decimal | None = Decimal(10_000)) -> None:
    ledger.record_maker_shadows(
        [
            MakerShadowCandidate(
                created_at=T0,
                ticker=TICKER,
                side=OutcomeSide.yes,
                event_ticker="KXNFLGAME-26OCT25KCLV",
                position_key="KXNFLGAME-26OCT25KCLV:kc",
                price=Decimal("0.45"),
                size=Decimal(1),
                fair=Decimal("0.48"),
                maker_fee=Decimal("0.01"),
                expires_at=T0 + timedelta(hours=2),
                commence_time=T0 + timedelta(hours=3),
                rules_version="v3",
                queue_ahead=queue_ahead,
            )
        ]
    )


def test_scoring_records_run_over_when_the_queue_never_clears(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "s.sqlite", Decimal("100"))
    _shadow(ledger)
    tape = [_trade(5, 20, "0.44", taker="yes")]
    score_maker_shadows(ledger, _Kalshi(tape), now=T0 + timedelta(hours=3))  # type: ignore[arg-type]
    row = ledger.conn.execute(
        "SELECT fill_queue_at,queue_status,fill_at,fill_rule FROM maker_shadow"
    ).fetchone()
    assert row == (None, "unfilled", (T0 + timedelta(minutes=5)).isoformat(), "run_over")


def test_window_with_volume_but_no_parsed_trades_is_tape_unavailable(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "s.sqlite", Decimal("100"))
    _shadow(ledger)
    candles = [{"end_period_ts": int((T0 + timedelta(minutes=1)).timestamp()), "volume": 40}]
    score_maker_shadows(ledger, _Kalshi([], candles), now=T0 + timedelta(hours=3))  # type: ignore[arg-type]
    row = ledger.conn.execute("SELECT queue_status,fill_at,fill_rule FROM maker_shadow").fetchone()
    assert row == ("tape_unavailable", None, "tape_unavailable")
    assert "tape parsed no trades" in caplog.text
    # No volume and no trades: a genuine "unfilled".
    quiet = Ledger(tmp_path / "q.sqlite", Decimal("100"))
    _shadow(quiet)
    score_maker_shadows(quiet, _Kalshi([]), now=T0 + timedelta(hours=3))  # type: ignore[arg-type]
    assert quiet.conn.execute("SELECT queue_status,fill_rule FROM maker_shadow").fetchone() == (
        "unfilled",
        "unfilled",
    )


# --- 2.3 gate -------------------------------------------------------------------------


def _gate_ledger(clvs: list[str], games: int) -> Ledger:
    ledger = Ledger(":memory:", Decimal("1000"))
    ledger.save_series_fee("KXNFLGAME", "quadratic_with_maker_fees", Decimal(1), T0)
    for index, clv in enumerate(clvs):
        event = f"KXNFLGAME-G{index % games}"
        ledger.conn.execute(
            "INSERT INTO maker_shadow (created_at,ticker,side,position_key,price,size,fair,"
            "maker_fee,status,status_at,event_ticker,expires_at,rules_version,score_status,"
            "fill_at,fill_rule,clv_kalshi,clv_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                T0.isoformat(),
                f"{event}-HOME",
                "yes",
                f"p{index}",
                "0.50",
                "1",
                "0.55",
                "0.01",
                "expired",
                T0.isoformat(),
                event,
                T0.isoformat(),
                "v3",
                "scored",
                T0.isoformat(),
                "run_over",
                clv,
                "ok",
            ),
        )
    ledger.conn.commit()
    return ledger


def test_gate_fails_on_positive_gross_that_loses_to_the_maker_fee() -> None:
    # +0.3c gross with a 0.4375c maker fee at 0.50: net is negative.
    clvs = [
        str(Decimal("0.3") + (Decimal("0.05") if i % 2 else Decimal("-0.05"))) for i in range(60)
    ]
    gate = evaluate_maker_gate(_gate_ledger(clvs, 60), Settings(_env_file=None))  # type: ignore[call-arg]
    assert not gate.passed and gate.reason == "queue_ci"
    assert gate.queue_mean is not None and gate.queue_mean < 0


def test_gate_passes_on_a_clean_synthetic_set_and_needs_thirty_games() -> None:
    clvs = [str(Decimal("2") + (Decimal("0.1") if i % 2 else Decimal("-0.1"))) for i in range(60)]
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    gate = evaluate_maker_gate(_gate_ledger(clvs, 60), settings)
    assert gate.passed and gate.queue_games == 60
    assert gate.queue_mean == pytest.approx(Decimal("2") - Decimal("0.4375"))
    assert not gate.taker_compared  # no taker cohort of 30+ games: comparison skipped
    few = evaluate_maker_gate(_gate_ledger(clvs, 29), settings)
    assert not few.passed and few.reason == "fill_games"


# --- 2.5 the shadow feed ----------------------------------------------------------------

MIA = "KXNFLGAME-26OCT25MIAMIN-MIA"


def test_near_miss_with_maker_edge_and_no_taker_signal_creates_one_shadow(settings) -> None:  # type: ignore[no-untyped-def]
    settings.rules_version = "v3"
    settings.v3_devig_method = "power"  # fixture fairs are tuned under power
    strategy = _strategy(settings, miami=Decimal("0.562"))
    signals = strategy.evaluate(v3_markets())
    assert MIA not in {sig.ticker for sig in signals}  # no taker signal on Miami
    [(near, reason)] = [
        (sig, r)
        for sig, r in strategy.seen_near_miss_decisions()
        if sig.ticker == MIA and sig.side is OutcomeSide.yes
    ]
    assert reason == "net_edge"
    shadows = [s for s in strategy.seen_maker_shadows() if s.ticker == MIA]
    assert len(shadows) == 1
    [shadow] = shadows
    maker_fee = Decimal("0.0175") * shadow.price * (1 - shadow.price)
    edge = shadow.fair - shadow.price - maker_fee
    assert Decimal("0.012") <= edge < Decimal("0.013")  # ~1.2c of maker edge
    assert shadow.price == Decimal("0.55") and shadow.maker_price_rule == "bid_plus_1"
    assert shadow.tags == ("blocked_net_edge",) and shadow.origin == "maker_feed"
    assert shadow.size == Decimal(1)
    # The same side below the 1c bar makes no shadow.
    settings.v3_maker_shadow_min_edge = Decimal("0.013")
    strict = _strategy(settings, miami=Decimal("0.562"))
    strict.evaluate(v3_markets())
    assert [s for s in strict.seen_maker_shadows() if s.ticker == MIA] == []


def test_unfavoured_side_gets_no_shadow(settings) -> None:  # type: ignore[no-untyped-def]
    settings.rules_version = "v3"
    strategy = _strategy(settings, miami=Decimal("0.562"))
    strategy.evaluate(v3_markets())
    keys = [s.position_key for s in strategy.seen_maker_shadows()]
    assert len(keys) == len(set(keys))  # one per position
    for shadow in strategy.seen_maker_shadows():
        assert shadow.fair > shadow.price  # favoured sides only


def test_second_scan_while_the_shadow_is_live_creates_none(settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings.rules_version = "v3"
    ledger = Ledger(tmp_path / "feed.sqlite", Decimal("1000"))
    first = _strategy(settings, miami=Decimal("0.562"))
    first.evaluate(v3_markets())
    created = ledger.record_maker_shadows(first.seen_maker_shadows())
    assert created == len(first.seen_maker_shadows()) >= 1
    again = _strategy(settings, miami=Decimal("0.562"))
    again.evaluate(v3_markets())
    assert ledger.record_maker_shadows(again.seen_maker_shadows()) == 0
    assert ledger.conn.execute(
        "SELECT COUNT(*) FROM maker_shadow WHERE ticker=? AND status='open'", (MIA,)
    ).fetchone() == (1,)
    assert {r[0] for r in ledger.conn.execute("SELECT origin FROM maker_shadow")} == {"maker_feed"}


def test_fresh_quote_queue_beats_a_stale_orderbook_snapshot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "q.sqlite", Decimal("100"))
    ledger.record_orderbook_snapshot(
        TICKER, {"yes": [(Decimal("0.45"), Decimal(999))], "no": []}, T0 - timedelta(minutes=6)
    )
    _shadow(ledger, queue_ahead=Decimal(100))
    assert ledger.conn.execute("SELECT queue_ahead,queue_source FROM maker_shadow").fetchone() == (
        "100",
        "quote",
    )
    fresh = Ledger(tmp_path / "f.sqlite", Decimal("100"))
    fresh.record_orderbook_snapshot(
        TICKER, {"yes": [(Decimal("0.45"), Decimal(999))], "no": []}, T0 - timedelta(minutes=4)
    )
    _shadow(fresh, queue_ahead=Decimal(100))
    assert fresh.conn.execute("SELECT queue_ahead,queue_source FROM maker_shadow").fetchone() == (
        "999",
        "orderbook",
    )
