from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from prediction_bot.config import Settings
from prediction_bot.paper.ledger import Ledger
from scripts.replay import (
    Decision,
    ReplaySignal,
    _mapping_reason,
    _simulate,
    canonical_key,
    evaluate_signal,
    load_database,
    load_fee_map,
    render_report,
)


def signal(
    *,
    id: int = 1,
    ticker: str = "KXNFLGAME-26OCT04KCLV-LV",
    side: str = "yes",
    fair: str = "0.60",
    price: str = "0.50",
    at: str = "2026-10-03T12:00:00Z",
    rationale: str | None = None,
) -> ReplaySignal:
    return ReplaySignal(
        id=id,
        created_at=datetime.fromisoformat(at.replace("Z", "+00:00")),
        strategy="sportsbook_arb",
        ticker=ticker,
        side=side,
        fair=Decimal(fair),
        price=Decimal(price),
        edge=Decimal(fair) - Decimal(price),
        size=Decimal(1),
        rationale=rationale
        or f"Las Vegas Raiders consensus {fair} (Kansas City Chiefs vs Las Vegas Raiders)",
        acted=True,
    )


def settings(**kwargs: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "min_lead_hours": 0,
        "longshot_haircut": "none",
        "v2_min_net_edge": Decimal("0.02"),
    }
    values.update(kwargs)
    return Settings(**values)  # type: ignore[arg-type]


FEES = {"KXNFLGAME": ("quadratic_with_maker_fees", Decimal(1))}


def test_replay_reads_a_synthetic_ledger_without_writing(tmp_path) -> None:
    path = tmp_path / "synthetic.sqlite"
    ledger = Ledger(path, Decimal("1000"))
    rows = [
        (
            "2026-10-03T12:00:00+00:00",
            "KXNFLGAME-26OCT04KCLV-LV",
            "0.60",
            "0.05",
            "Las Vegas Raiders consensus reference",
        ),
        (
            "2026-10-03T12:05:00+00:00",
            "KXNCAAFGAME-26OCT03UTEPUNM-UTEP",
            "0.60",
            "0.06",
            "UTEP consensus reference",
        ),
        (
            "2026-10-03T12:10:00+00:00",
            "KXMLBGAME-26OCT041600SDMIL-SD",
            "0.60",
            "0.50",
            "San Diego Padres consensus reference",
        ),
        (
            "2026-10-04T19:59:00+00:00",
            "KXMLBGAME-26OCT041600SDMIL-SD",
            "0.60",
            "0.50",
            "San Diego Padres consensus reference",
        ),
        (
            "2026-10-03T12:20:00+00:00",
            "KXNBAGAME-26OCT04BOSNYK-BOS",
            "0.60",
            "0.50",
            "Boston Celtics consensus reference",
        ),
    ]
    for created_at, ticker, fair, price, rationale in rows:
        ledger.conn.execute(
            "INSERT INTO signals (created_at,strategy,ticker,side,fair_prob,limit_price,edge,size,"
            "rationale,acted) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                created_at,
                "sportsbook_arb",
                ticker,
                "yes",
                fair,
                price,
                str(Decimal(fair) - Decimal(price)),
                "1",
                rationale,
                1,
            ),
        )
    ledger.conn.commit()
    ledger.conn.close()

    signals, fills, _settlements, _runs, markets = load_database(path, "2026-09-28")
    decisions = {
        item.id: evaluate_signal(
            item,
            {**FEES, "KXMLBGAME": ("quadratic_with_maker_fees", Decimal(1))},
            settings(min_lead_hours=72),
            signals,
        )
        for item in signals
    }

    assert len(signals) == 5
    assert not fills
    assert not markets
    assert "price_floor" in decisions[1].reasons
    assert "mapping_failed" in decisions[2].reasons
    assert "entry_window" in decisions[3].reasons
    assert "kickoff_guard" in decisions[4].reasons
    assert "league_disabled" in decisions[5].reasons


def test_replay_marks_unknown_rules_and_fee_aware_edge() -> None:
    decision = evaluate_signal(signal(fair="0.53", price="0.50"), FEES, settings(), [])
    assert "net_edge" in decision.reasons
    assert set(decision.unknown) >= {
        "dispersion",
        "few_books",
        "wide_spread",
        "depth",
        "worst_case_devig",
    }


def test_replay_price_and_longshot_gates() -> None:
    floor = evaluate_signal(signal(fair="0.80", price="0.05"), FEES, settings(), [])
    ceiling = evaluate_signal(signal(fair="0.95", price="0.95"), FEES, settings(), [])
    longshot = evaluate_signal(signal(fair="0.23", price="0.20"), FEES, settings(), [])
    assert "price_floor" in floor.reasons
    assert "price_ceiling" in ceiling.reasons
    assert "longshot_edge" in longshot.reasons


def test_replay_big_edge_and_mapping_rules() -> None:
    big = evaluate_signal(signal(fair="0.61", price="0.50"), FEES, settings(), [])
    unmapped = evaluate_signal(
        signal(ticker="KXNFLGAME-26OCT04KCLV-ZZ", rationale="unknown consensus"),
        FEES,
        settings(),
        [],
    )
    assert "big_edge" in big.reasons
    assert "mapping_failed" in unmapped.reasons


def test_replay_mapping_distinguishes_aliases_from_fair_orientation() -> None:
    scar = signal(
        ticker="KXNCAAFGAME-26OCT10SCARFLA-SCAR",
        fair="0.163",
        rationale=(
            "Florida Gators consensus 0.837 from 1 books "
            "(Florida Gators vs South Carolina Gamecocks)"
        ),
    )
    lafc_lag = signal(
        ticker="KXMLSGAME-26OCT25LAFCLAG-LAG",
        fair="0.614",
        rationale="Los Angeles FC consensus 0.614 from 3 books (Los Angeles FC vs LA Galaxy)",
    )
    tampa_no = signal(
        ticker="KXNHLGAME-26OCT17VANTB-TB",
        side="no",
        fair="0.335",
        rationale="Tampa Bay Lightning consensus 0.665 from 3 books",
    )
    unc = signal(
        ticker="KXNCAAFGAME-26OCT10UNCPITT-UNC",
        fair="0.359",
        rationale=(
            "North Carolina Tar Heels consensus 0.359 from 3 books "
            "(Pittsburgh Panthers vs North Carolina Tar Heels)"
        ),
    )
    outsider = signal(
        ticker="KXNCAAFGAME-26OCT10SCARFLA-SCAR",
        fair="0.163",
        rationale=(
            "Alabama Crimson Tide consensus 0.837 from 1 books "
            "(Florida Gators vs South Carolina Gamecocks)"
        ),
    )

    assert _mapping_reason(scar) is None
    assert _mapping_reason(lafc_lag) == "mapping_mismatch"
    assert _mapping_reason(tampa_no) is None
    assert _mapping_reason(unc) is None
    assert _mapping_reason(outsider) == "mapping_mismatch"


def test_replay_report_counts_blocks_beyond_alias_coverage() -> None:
    item = signal(rationale="unknown consensus")
    decision = Decision(signal=item, fair=item.fair, reasons=["mapping_failed"])
    fill = {
        "id": item.id,
        "ticker": item.ticker,
        "side": item.side,
        "created_at": item.created_at.isoformat(),
    }
    report = render_report("both", "2026-09-28", [item], [decision], [fill])
    assert "fills blocked by a rule other than alias coverage (mapping_failed): 0." in report

    decision.reasons.append("net_edge")
    report = render_report("both", "2026-09-28", [item], [decision], [fill])
    assert "fills blocked by a rule other than alias coverage (mapping_failed): 1." in report


def test_replay_kickoff_entry_window_and_in_play_rules() -> None:
    kickoff = evaluate_signal(signal(at="2026-10-04T20:10:00Z"), FEES, settings(), [])
    entry = evaluate_signal(
        signal(at="2026-10-03T20:25:00Z"),
        FEES,
        settings(min_lead_hours=72),
        [],
    )
    past = evaluate_signal(signal(at="2026-10-04T20:30:00Z"), FEES, settings(), [])
    assert "kickoff_guard" in kickoff.reasons
    assert "entry_window" in entry.reasons
    assert "in_play:commence_past" in past.reasons


def test_replay_detects_in_play_price_and_consensus_jumps() -> None:
    earlier = signal(id=1, fair="0.50", price="0.50", at="2026-10-02T12:00:00Z")
    current = signal(id=2, fair="0.70", price="0.02", at="2026-10-03T12:00:00Z")
    decision = evaluate_signal(current, FEES, settings(), [earlier, current])
    assert "in_play:kalshi_extreme" in decision.reasons
    assert "in_play:consensus_jump" in decision.reasons


def test_disabled_league_blocks_and_canonical_no_collapses() -> None:
    nba = signal(
        ticker="KXNBAGAME-26OCT04BOSNYK-BOS",
        rationale="Boston Celtics consensus reference",
    )
    decision = evaluate_signal(nba, FEES, settings(), [])
    assert "league_disabled" in decision.reasons
    assert canonical_key("KXNFLGAME-26OCT04KCLV", "KC", "no") == (
        "KXNFLGAME-26OCT04KCLV",
        "LV",
    )
    assert canonical_key("KXNFLGAME-26OCT04KCLV", "LV", "yes") == (
        "KXNFLGAME-26OCT04KCLV",
        "LV",
    )


def test_replay_deduplicates_a_run_but_caps_same_key_across_cycles() -> None:
    first = signal(id=1, at="2026-10-03T12:00:00Z", fair="0.90", price="0.50")
    opposite = signal(
        id=2,
        ticker="KXNFLGAME-26OCT04KCLV-KC",
        side="no",
        fair="0.90",
        price="0.50",
        at="2026-10-03T12:00:00Z",
        rationale="Kansas City Chiefs consensus reference",
    )
    second_cycle = signal(id=3, at="2026-10-03T13:00:00Z", fair="0.90", price="0.50")
    decisions = [
        Decision(
            signal=item,
            fair=item.fair,
            fee_multiplier=Decimal(0),
            position_key=canonical_key(item.event_ticker, item.code, item.side),
        )
        for item in (first, opposite, second_cycle)
    ]
    _simulate(decisions, [], {}, [], settings())
    assert decisions[0].v2_would_trade
    assert "duplicate_position" in decisions[1].reasons
    assert not decisions[2].v2_would_trade
    assert "size_zero" in decisions[2].reasons


def test_replay_chooses_cheapest_effective_equivalent_leg() -> None:
    expensive = signal(id=1, price="0.55")
    cheap = signal(
        id=2,
        ticker="KXNFLGAME-26OCT04KCLV-KC",
        side="no",
        price="0.45",
        rationale="Kansas City Chiefs consensus 0.90",
    )
    decisions = [
        Decision(
            signal=item,
            fair=Decimal("0.90"),
            fee_multiplier=Decimal(0),
            position_key=canonical_key(item.event_ticker, item.code, item.side),
        )
        for item in (expensive, cheap)
    ]

    _simulate(decisions, [], {}, [], settings())

    assert "duplicate_position" in decisions[0].reasons
    assert decisions[1].v2_would_trade


def test_replay_does_not_credit_payout_before_settled_at() -> None:
    first = signal(
        id=1,
        ticker="KXNFLGAME-26OCT04KCLV-LV",
        fair="0.90",
        price="0.50",
        at="2026-10-03T12:00:00Z",
    )
    second = signal(
        id=2,
        ticker="KXNFLGAME-26OCT04DENSF-SF",
        fair="0.90",
        price="0.50",
        at="2026-10-03T12:15:00Z",
    )
    decisions = [
        Decision(
            signal=item,
            fair=item.fair,
            fee_multiplier=Decimal(0),
            position_key=(item.event_ticker, item.code),
        )
        for item in (first, second)
    ]
    fill = {
        "ticker": first.ticker,
        "side": "yes",
        "result": "yes",
        "settled_at": "2026-10-03T12:30:00+00:00",
    }

    _simulate(
        decisions,
        [fill],
        {},
        [],
        settings(
            paper_starting_cash=Decimal("100"),
            v2_kelly_fraction=Decimal("0.25"),
            v2_position_bankroll_fraction=Decimal(1),
            v2_event_bankroll_fraction=Decimal(1),
            v2_daily_bankroll_fraction=Decimal(1),
            v2_total_open_bankroll_fraction=Decimal(1),
        ),
    )

    assert decisions[0].contracts == 40
    assert decisions[1].contracts == 40


def test_load_fee_map_fetches_missing_series_from_partial_database(tmp_path) -> None:
    path = tmp_path / "partial-fees.sqlite"
    ledger = Ledger(path, Decimal("100"))
    ledger.save_series_fee(
        "KXNFLGAME",
        "quadratic_with_maker_fees",
        Decimal(1),
        datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    ledger.conn.execute(
        "INSERT INTO signals (created_at,strategy,ticker,side,limit_price,fair_prob,edge,size,"
        "rationale,acted) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "2026-10-03T12:00:00+00:00",
            "sportsbook_v2",
            "KXNHLGAME-26OCT04KCLV-LV",
            "yes",
            "0.50",
            "0.60",
            "0.10",
            "1",
            "test",
            0,
        ),
    )
    ledger.conn.commit()
    ledger.close()

    class Series:
        calls = 0

        def get_series(self):
            self.calls += 1
            return [
                {
                    "ticker": "KXNHLGAME",
                    "fee_type": "quadratic_with_maker_fees",
                    "fee_multiplier": "0.5",
                }
            ]

    client = Series()
    fee_map = load_fee_map(path, tmp_path / "cache", client)  # type: ignore[arg-type]

    assert fee_map["KXNFLGAME"] == ("quadratic_with_maker_fees", Decimal(1))
    assert fee_map["KXNHLGAME"] == ("quadratic_with_maker_fees", Decimal("0.5"))
    assert client.calls == 1
