"""Taker entries (PR 3): conservative devig, devig_power_only and road_dog shadows, and
the sharp close under three devig methods."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from prediction_bot.clv import record_sharp_clv
from prediction_bot.clv_report import PER_METHOD, clv_report
from prediction_bot.config import Settings
from prediction_bot.devig import devig_all
from prediction_bot.engine import Engine
from prediction_bot.models import Market
from prediction_bot.paper.ledger import Ledger
from tests.test_sportsbook_v2 import (
    FakeOdds,
    FakeV3Kalshi,
    SportsbookV3Strategy,
    fixed_clock,
    v3_markets,
    v3_odds_event,
)

INSIDE_WINDOW = "2026-10-22T10:00:00Z"
MIA = "KXNFLGAME-26OCT25MIAMIN-MIA"
MIN = "KXNFLGAME-26OCT25MIAMIN-MIN"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        rules_version="v3",
        paper_db_path=tmp_path / "paper.sqlite",
        paper_starting_cash=Decimal("1000"),
        **overrides,
    )
    settings.max_gross_edge = Decimal("0.20")
    return settings


def _run(
    settings: Settings, ledger: Ledger, miami: str, markets: list[Market] | None = None
) -> tuple[SportsbookV3Strategy, list[str]]:
    strategy = SportsbookV3Strategy(
        settings,
        FakeOdds([v3_odds_event(miami_probability=Decimal(miami))]),
        kalshi_client=FakeV3Kalshi(),
        ledger=ledger,
        clock=fixed_clock(INSIDE_WINDOW),
    )
    engine = Engine(
        settings,
        FakeV3Kalshi(),  # type: ignore[arg-type]
        [strategy],
        ledger,
        clock=fixed_clock(INSIDE_WINDOW),
    )
    result = engine.run_cycle(markets or v3_markets())
    return strategy, [sig.ticker for sig in result.acted]


def _reasons(ledger: Ledger, reason: str) -> list[tuple[str, int, str | None]]:
    return ledger.conn.execute(
        "SELECT ticker,would_trade,devig_json FROM shadow_decisions WHERE reason=?", (reason,)
    ).fetchall()


# --- 3.1 / 3.2 conservative devig ---------------------------------------------------------


def test_conservative_is_the_default_and_takes_the_lower_of_power_and_shin() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.v3_devig_method == "conservative"
    assert settings.exp_road_dog_mode == "shadow"
    fairs = devig_all({"A": Decimal("1.50"), "B": Decimal("2.80")})
    assert fairs["power"]["A"] > fairs["shin"]["A"]  # power lifts the favourite


def test_favourite_clearing_only_under_power_gives_no_signal_and_one_shadow(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # At this fixture's vig power sits ~0.23c above Shin: MIA clears the 2c bar by
    # 2.17c under power but only reaches 1.94c under the conservative (Shin) fair.
    settings = _settings(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    strategy, acted = _run(settings, ledger, "0.592")
    assert MIA not in acted and not any(s.ticker == MIA for s in strategy.seen_shadow_decisions())
    [(ticker, would_trade, raw)] = _reasons(ledger, "devig_power_only")
    assert (ticker, would_trade) == (MIA, 1)
    methods = json.loads(raw or "{}")
    assert Decimal(methods["power"]) > Decimal(methods["shin"])
    edge = ledger.conn.execute(
        "SELECT net_edge FROM shadow_decisions WHERE reason='devig_power_only'"
    ).fetchone()[0]
    assert Decimal("0.02") <= Decimal(edge) < Decimal("0.0225")
    near = ledger.conn.execute(
        "SELECT net_edge,devig_json FROM shadow_decisions WHERE reason='net_edge' AND ticker=? "
        "AND side='yes'",
        (MIA,),
    ).fetchone()
    assert Decimal(near[0]) < Decimal("0.02") and near[1] is not None  # near miss keeps all fairs
    assert ledger.conn.execute("SELECT COUNT(*) FROM fills").fetchone() == (0,)
    # A second scan the same day adds no second row.
    _run(settings, ledger, "0.592")
    assert len(_reasons(ledger, "devig_power_only")) == 1


def test_favourite_clearing_under_both_methods_trades_as_before(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(tmp_path)
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    _strategy, acted = _run(settings, ledger, "0.60")
    assert acted == [MIA]
    assert _reasons(ledger, "devig_power_only") == []
    signal = ledger.conn.execute(
        "SELECT fair_prob,devig_json FROM signals WHERE ticker=?", (MIA,)
    ).fetchone()
    methods = json.loads(signal[1])
    assert Decimal(signal[0]) == min(Decimal(methods["power"]), Decimal(methods["shin"]))


def test_power_setting_still_trades_on_the_power_fair(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(tmp_path, v3_devig_method="power")
    ledger = Ledger(settings.paper_db_path, Decimal("1000"))
    _strategy, acted = _run(settings, ledger, "0.592")
    assert acted == [MIA] and _reasons(ledger, "devig_power_only") == []


# --- 3.4 road dog ----------------------------------------------------------------------------

RIVALRY = ["Miami Dolphins vs Minnesota Vikings"]


def test_road_dog_below_threshold_is_a_shadow_in_shadow_mode_and_a_fill_in_trade_mode(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    # Minnesota (away, 36c, rivalry) at ~+0.4c net, under the 4c low-price bar.
    markets = v3_markets(yes_price="0.63")  # Miami priced out, so only the road dog matters
    shadow = _settings(tmp_path / "s", exp_road_dog_rivalries=RIVALRY)
    shadow_ledger = Ledger(shadow.paper_db_path, Decimal("1000"))
    _strategy, acted = _run(shadow, shadow_ledger, "0.611", markets)
    assert acted == []
    [(ticker, would_trade, _raw)] = _reasons(shadow_ledger, "road_dog")
    assert (ticker, would_trade) == (MIN, 1)
    edge = shadow_ledger.conn.execute(
        "SELECT net_edge FROM shadow_decisions WHERE reason='road_dog'"
    ).fetchone()[0]
    assert Decimal("0.003") < Decimal(edge) < Decimal("0.005")
    assert shadow_ledger.conn.execute("SELECT COUNT(*) FROM fills").fetchone() == (0,)

    trade = _settings(tmp_path / "t", exp_road_dog_rivalries=RIVALRY, exp_road_dog_mode="trade")
    trade_ledger = Ledger(trade.paper_db_path, Decimal("1000"))
    _strategy, acted = _run(trade, trade_ledger, "0.611", markets)
    assert acted == [MIN]
    assert trade_ledger.conn.execute("SELECT COUNT(*) FROM fills").fetchone() == (1,)
    assert _reasons(trade_ledger, "road_dog") == []

    off = _settings(tmp_path / "o", exp_road_dog_rivalries=RIVALRY, exp_road_dog_mode="off")
    off_ledger = Ledger(off.paper_db_path, Decimal("1000"))
    _strategy, acted = _run(off, off_ledger, "0.611", markets)
    assert acted == [] and _reasons(off_ledger, "road_dog") == []


# --- 3.3 sharp close under three methods -------------------------------------------------


def test_sharp_close_is_stored_under_power_shin_and_multiplicative(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = Ledger(tmp_path / "sharp.sqlite", Decimal("100"))
    event = "KXNFLGAME-26OCT25MIAMIN"
    kickoff = datetime(2026, 10, 25, 20, 25, tzinfo=timezone.utc)
    odds = {"Miami Dolphins": "1.55", "Minnesota Vikings": "2.60"}
    ledger.conn.execute(
        "INSERT INTO odds_snapshots (created_at,feed,event_id,home_team,away_team,"
        "commence_time,team,prob,books_used,event_ticker,book_odds_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            (kickoff - timedelta(minutes=5)).isoformat(),
            "americanfootball_nfl",
            "e1",
            "Miami Dolphins",
            "Minnesota Vikings",
            kickoff.isoformat(),
            "Miami Dolphins",
            "0.6",
            1,
            event,
            json.dumps(
                {
                    "pinnacle": {
                        "last_update": (kickoff - timedelta(minutes=6)).isoformat(),
                        "odds": odds,
                    }
                }
            ),
        ),
    )
    for ticker, price in ((MIA, "0.60"), (MIN, "0.36")):
        ledger.conn.execute(
            "INSERT INTO shadow_decisions (created_at,rules_version,window,strategy,ticker,side,"
            "would_trade,reason,price,commence_time,clv_status,model_side) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                (kickoff - timedelta(days=4)).isoformat(),
                "v3",
                "72h+",
                "sportsbook_v3",
                ticker,
                "yes",
                0,
                "net_edge",
                price,
                kickoff.isoformat(),
                "ok",
                1,
            ),
        )
    ledger.conn.commit()
    assert record_sharp_clv(ledger, now=kickoff + timedelta(hours=1)) == 2
    fair = devig_all({k: Decimal(v) for k, v in odds.items()})
    rows = {
        r[0]: r[1:]
        for r in ledger.conn.execute(
            "SELECT ticker,clv_sharp_close,clv_sharp_close_shin,clv_sharp_close_mult "
            "FROM shadow_decisions"
        )
    }
    power, shin, mult = (Decimal(v) for v in rows[MIA])
    assert power == (fair["power"]["Miami Dolphins"] - Decimal("0.60")) * 100
    assert shin == (fair["shin"]["Miami Dolphins"] - Decimal("0.60")) * 100
    assert mult == (fair["multiplicative"]["Miami Dolphins"] - Decimal("0.60")) * 100
    assert len({power, shin, mult}) == 3
    report = clv_report(ledger.conn)["cohorts"]["v3_net_edge"]["metrics"]
    for metric in ("clv_sharp_close", "clv_sharp_close_shin", "clv_sharp_close_mult"):
        bands = report[metric]["by_price_band"]
        assert bands and all(b["verdict"] == PER_METHOD for b in bands.values())
