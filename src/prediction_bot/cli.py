from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from prediction_bot.analysis import event_datetime as _event_datetime
from prediction_bot.analysis import start_time
from prediction_bot.clv import backfill_clv_commence_times, record_clv, refresh_clv_statuses
from prediction_bot.config import Settings
from prediction_bot.dashboard import render_dashboard
from prediction_bot.engine import Engine
from prediction_bot.feed_selection import (
    RotationScore,
    compute_feed_stats,
    eligible,
    feed_scores,
    liquidity_weights,
    rotation_scores,
    select_entry_feeds,
    select_rotation_feeds,
)
from prediction_bot.maker_gate import MakerGate, evaluate_maker_gate
from prediction_bot.maker_scoring import score_maker_shadows
from prediction_bot.models import Market, OutcomeSide, Signal
from prediction_bot.odds import (
    KALSHI_SERIES_TO_OUTRIGHT,
    KALSHI_SERIES_TO_SPORT,
    OddsClient,
    OddsPacer,
)
from prediction_bot.odds_planner import PlannedGame, plan_odds_pulls
from prediction_bot.paper.ledger import Ledger, Prediction
from prediction_bot.series import SPORTSBOOK_SERIES
from prediction_bot.strategies import (
    STRATEGIES,
    SportsbookArbStrategy,
    SportsbookV2Strategy,
    Strategy,
)
from prediction_bot.venues.kalshi import KalshiClient
from prediction_bot.venues.kalshi_auth import KalshiSigner

app = typer.Typer(help="Prediction-market bot (Kalshi demo first).", no_args_is_help=True)
console = Console()
log = logging.getLogger(__name__)


def _settings() -> Settings:
    return Settings()


def _client(settings: Settings, need_auth: bool = False) -> KalshiClient:
    signer = None
    if settings.has_kalshi_credentials:
        assert settings.kalshi_api_key_id
        if settings.kalshi_private_key_pem:
            signer = KalshiSigner.from_pem(
                settings.kalshi_api_key_id, settings.kalshi_private_key_pem
            )
        else:
            assert settings.kalshi_private_key_path
            signer = KalshiSigner.from_file(
                settings.kalshi_api_key_id, settings.kalshi_private_key_path
            )
    elif need_auth:
        raise typer.BadParameter(
            "Set PBOT_KALSHI_API_KEY_ID and PBOT_KALSHI_PRIVATE_KEY_PATH or"
            " PBOT_KALSHI_PRIVATE_KEY_PEM (demo keys from"
            " https://demo.kalshi.co → Account & security → API Keys)"
        )
    return KalshiClient(settings.kalshi_api_base, signer=signer)


def _odds_client(settings: Settings) -> OddsClient | None:
    if not settings.odds_api_key:
        return None
    return OddsClient(settings.odds_api_key, settings.odds_api_base)


def _strategies(
    settings: Settings,
    names: list[str],
    odds: OddsClient | None = None,
    kalshi: KalshiClient | None = None,
    ledger: Ledger | None = None,
) -> list[Strategy]:
    unknown = set(names) - set(STRATEGIES)
    if unknown:
        raise typer.BadParameter(f"unknown strategies {sorted(unknown)}; have {sorted(STRATEGIES)}")
    odds = odds or _odds_client(settings)
    out: list[Strategy] = []
    for n in names:
        cls = STRATEGIES[n]
        if issubclass(cls, SportsbookV2Strategy):
            out.append(cls(settings, odds_client=odds, kalshi_client=kalshi, ledger=ledger))
        elif issubclass(cls, SportsbookArbStrategy):
            out.append(cls(settings, odds_client=odds))  # one client: odds fetched once per cycle
        else:
            out.append(cls(settings))
    return out


def _fmt(d: Decimal | None) -> str:
    return "-" if d is None else f"{d:.4f}"


def _resolve_v3_execution(
    settings: Settings, ledger: Ledger, strategy_names: list[str]
) -> tuple[Settings, MakerGate | None]:
    if settings.v3_execution != "auto":
        return settings, None
    if settings.is_live or settings.rules_version != "v3" or "sportsbook_v3" not in strategy_names:
        return settings.model_copy(update={"v3_execution": "taker"}), None
    gate = evaluate_maker_gate(ledger, settings)
    return (
        settings.model_copy(update={"v3_execution": "maker_first" if gate.passed else "taker"}),
        gate,
    )


@app.callback()
def _main(verbose: bool = typer.Option(False, "-v", help="Debug logging")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command()
def markets(
    series: str | None = typer.Option(None, help="Series ticker, e.g. KXNFLGAME"),
    limit: int = typer.Option(25),
    min_volume: float = typer.Option(0.0),
) -> None:
    """List open markets with best quotes (public, no credentials needed)."""
    s = _settings()
    ms = _client(s).get_markets(series_ticker=series, limit=limit, max_pages=1)
    t = Table(title=f"Open markets @ {s.kalshi_api_base}")
    for col in ("ticker", "title", "yes_bid", "yes_ask", "spread", "volume"):
        t.add_column(col)
    for m in ms:
        if m.volume < Decimal(str(min_volume)):
            continue
        q = m.quote
        t.add_row(
            m.ticker,
            m.title[:50],
            _fmt(q.yes_bid),
            _fmt(q.yes_ask),
            _fmt(q.spread),
            f"{m.volume:.0f}",
        )
    console.print(t)


@app.command("feed-plan")
def feed_plan(
    budget: int | None = typer.Option(None, min=0, help="Limit the displayed plan"),
) -> None:
    """Show the lead-aware feed plan using public Kalshi data and the local ledger."""
    settings = _settings()
    client = _client(settings)
    ledger = Ledger(settings.paper_db_path, settings.paper_starting_cash)
    now = datetime.now(timezone.utc)
    markets = _fetch(client, list(SPORTSBOOK_SERIES))
    games_by_sport = _planner_games(client, markets, ledger, now)
    feeds = sorted(games_by_sport)
    stats = compute_feed_stats(ledger, feeds, now)
    weights = liquidity_weights(ledger, games_by_sport, settings)
    scores = feed_scores(games_by_sport, weights, settings, now)
    ranked = select_entry_feeds(games_by_sport, stats, weights, settings, ledger, now)
    skips: dict[str, str] = {}
    rotation: dict[str, RotationScore] = {}
    if settings.odds_feed_selection == "rotation":
        odds = _odds_client(settings)
        rotation = rotation_scores(ledger, games_by_sport, stats, settings, now)
        ranked = sorted(
            (feed for feed, score in rotation.items() if score.score > 0),
            key=lambda feed: (
                -rotation[feed].score,
                rotation[feed].soonest_kickoff or datetime.max.replace(tzinfo=timezone.utc),
                feed,
            ),
        )
        _selected, skips, _rotation = select_rotation_feeds(
            ledger, games_by_sport, stats, settings, odds, now, budget
        )

    table = Table(
        title="Rotation odds feed plan"
        if settings.odds_feed_selection == "rotation"
        else "Lead-aware odds feed plan"
    )
    for column in (
        "feed",
        "lead p50 (h)",
        "lead p90 (h)",
        "provisional",
        "eligible",
        "value",
        "staleness",
        "score",
        "skip",
        "rank",
        "within budget",
    ):
        table.add_column(column)
    for feed, stat in sorted(stats.items()):
        rank = ranked.index(feed) + 1 if feed in ranked else None
        within_budget = rank is not None and (budget is None or rank <= budget)
        score = scores.get(feed)
        rotation_score = rotation.get(feed)
        table.add_row(
            feed,
            f"{stat.lead_p50_h:.1f}",
            f"{stat.lead_p90_h:.1f}",
            "yes" if stat.provisional else "no",
            "yes" if eligible(stat, settings) else "no",
            str(
                rotation_score.value
                if rotation_score is not None
                else score.weighted_game_count
                if score
                else 0
            ),
            f"{rotation_score.staleness_hours:.1f}h" if rotation_score is not None else "-",
            str(rotation_score.score if rotation_score is not None else "-"),
            skips.get(feed, "-"),
            str(rank) if rank is not None else "-",
            "yes" if within_budget else "no",
        )
    console.print(table)
    if budget is not None:
        console.print(f"Budget: {budget} feeds")


@app.command()
def scan(
    strategy: list[str] = typer.Option(["book_scanner"], "--strategy", "-s"),
    series: list[str] = typer.Option([], "--series", help="Restrict to these series tickers"),
) -> None:
    """Evaluate strategies once and print signals without trading."""
    s = _settings()
    run_settings = s
    if s.v3_execution == "auto" and "sportsbook_v3" in strategy:
        ledger = Ledger(s.paper_db_path, s.paper_starting_cash)
        run_settings, gate = _resolve_v3_execution(s, ledger, strategy)
        if gate is not None:
            console.print(f"v3 execution: auto -> {run_settings.v3_execution} ({gate.reason})")
    client = _client(s)
    ms = _fetch(client, series)
    signals = [
        sig for st in _strategies(run_settings, strategy, kalshi=client) for sig in st.evaluate(ms)
    ]
    console.print(f"scanned {len(ms)} markets, {len(signals)} signals")
    _print_signals(signals)


@app.command()
def paper(
    strategy: list[str] = typer.Option(["book_scanner"], "--strategy", "-s"),
    series: list[str] = typer.Option([], "--series"),
    interval: int = typer.Option(60, help="Seconds between cycles"),
    cycles: int = typer.Option(0, help="Stop after N cycles (0 = run forever)"),
) -> None:
    """Paper-trade: fill signals at the ask in the local ledger, settle when Kalshi resolves."""
    s = _settings()
    client = _client(s)
    ledger = Ledger(s.paper_db_path, s.paper_starting_cash)
    odds = _odds_client(s)
    unknown = set(strategy) - set(STRATEGIES)
    if unknown:
        raise typer.BadParameter(f"unknown strategies {sorted(unknown)}; have {sorted(STRATEGIES)}")
    pacer = None
    uses_odds = any(issubclass(STRATEGIES[name], SportsbookArbStrategy) for name in strategy)
    if (
        odds is not None
        and uses_odds
        and (s.odds_runs_per_day > 0 or s.odds_planner == "event_driven")
    ):
        # A one-shot run is one of the scheduled runs; a loop paces per cycle.
        per_day = (
            s.odds_runs_per_day
            if s.odds_runs_per_day > 0 and cycles == 1
            else max(s.odds_runs_per_day, 3)
            if s.odds_planner == "event_driven"
            else 86400 / max(interval, 1)
        )
        pacer = OddsPacer(odds, max(per_day, s.odds_runs_per_day), ledger=ledger)
    shadow_names = list(s.shadow_strategies)
    if s.rules_version == "v3" and "sportsbook_v3" in strategy:
        shadow_names = list(
            dict.fromkeys(["sportsbook_v2", "sportsbook_arb", "underdog_value", *shadow_names])
        )
    clv_max_calls = (
        50
        if any(
            issubclass(STRATEGIES[name], SportsbookArbStrategy)
            for name in (*strategy, *shadow_names)
        )
        else s.clv_max_calls_scanner
    )
    n = 0
    while True:
        n += 1
        run_settings, gate = _resolve_v3_execution(s, ledger, strategy)
        if gate is not None:
            resolved = run_settings.v3_execution
            detail = gate.reason
            if gate.reason == "taker_positions":
                detail = f"taker_positions: {gate.taker_n}/{s.v3_maker_gate_min_positions}"
            elif gate.reason == "maker_scored":
                detail = f"maker_scored: {gate.maker_scored}/{s.v3_maker_gate_min_scored}"
            elif gate.reason == "maker_filled_clv":
                detail = f"maker_filled_clv: {gate.maker_clv_n}/{s.v3_maker_gate_min_filled_clv}"
            console.print(f"v3 execution: auto -> {resolved} ({detail})")
            ledger.record_execution_mode(
                rules_version="v3",
                requested="auto",
                resolved=resolved,
                reason=gate.reason,
                taker_n=gate.taker_n,
                taker_mean=gate.taker_mean,
                taker_ci_low=gate.taker_ci_low,
                maker_scored=gate.maker_scored,
                maker_clv_n=gate.maker_clv_n,
                maker_mean=gate.maker_mean,
            )
        strats = _strategies(run_settings, strategy, odds, kalshi=client, ledger=ledger)
        shadow_strategies = _strategies(run_settings, shadow_names, odds, client, ledger)
        engine = Engine(
            run_settings,
            client,
            strats,
            ledger,
            allow_live=False,
            shadow_strategies=shadow_strategies,
        )
        if pacer is not None:
            pacer.start_run()
        settled = engine.settle_open_positions()
        try:
            score_maker_shadows(ledger, client, max_calls=50)
        except Exception:
            log.exception("Maker-shadow scoring failed; continuing paper cycle")
        try:
            backfill_clv_commence_times(ledger, client, s, max_calls=50)
        except Exception:
            log.exception("CLV commence-time backfill failed; continuing paper cycle")
        try:
            record_clv(ledger, client, max_calls=clv_max_calls)
        except Exception:
            log.exception("CLV recording failed; continuing paper cycle")
        try:
            refresh_clv_statuses(ledger, s)
        except Exception:
            log.exception("CLV status refresh failed; continuing paper cycle")
        markets = _fetch(client, series)
        snapshot_markets = markets
        if not series and strategy == ["book_scanner"]:

            def fetch_missing(series_to_fetch: list[str]) -> list[Market]:
                additional: list[Market] = []
                for missing_series in series_to_fetch:
                    try:
                        additional.extend(_fetch(client, [missing_series]))
                    except Exception:
                        log.exception("Market snapshot fetch failed for %s", missing_series)
                return additional

            snapshot_markets = _snapshot_markets(
                markets,
                fetch_missing,
                datetime.now(timezone.utc),
            )
        ledger.record_market_snapshots(snapshot_markets)
        if odds is not None and s.odds_planner == "event_driven" and uses_odds:
            now = datetime.now(timezone.utc)
            try:
                planned = plan_odds_pulls(
                    _planner_games(client, markets, ledger, now),
                    now,
                    int(odds.requests_remaining or 0),
                    _days_left_in_month(now),
                    runs_per_day=max(s.odds_runs_per_day, 3),
                )
                if odds.budget is not None:
                    planned = planned[: odds.budget]
                odds.allowed_sports = set(planned)
            except Exception:
                odds.allowed_sports = set()
                log.exception("Event-driven odds planning failed; no odds feeds selected")
        elif (
            odds is not None
            and uses_odds
            and s.odds_planner == "fixed"
            and s.odds_feed_selection in {"lead_aware", "rotation"}
        ):
            now = datetime.now(timezone.utc)
            try:
                games_by_sport = _planner_games(client, markets, ledger, now)
                feeds = sorted(games_by_sport)
                stats = compute_feed_stats(ledger, feeds, now)
                weights = liquidity_weights(ledger, games_by_sport, s)
                if s.odds_feed_selection == "rotation":
                    planned, skips, _rotation = select_rotation_feeds(
                        ledger,
                        games_by_sport,
                        stats,
                        s,
                        odds,
                        now,
                        odds.budget,
                    )
                    ledger.record_feed_skips(skips, now)
                else:
                    planned = select_entry_feeds(games_by_sport, stats, weights, s, ledger, now)
                ledger.record_feed_stats(
                    stats,
                    {feed: eligible(stat, s) for feed, stat in stats.items()},
                    now,
                )
                if odds.budget is not None:
                    planned = planned[: odds.budget]
                odds.allowed_sports = set(planned)
            except Exception:
                odds.allowed_sports = set()
                log.exception("Lead-aware odds planning failed; no odds feeds selected")
        elif odds is not None:
            odds.allowed_sports = None
        res = engine.run_cycle(markets)
        if pacer is not None:
            pacer.finish_run()
        ledger.record_run(
            strategy,
            res.markets_scanned,
            len(res.signals),
            len(res.acted) + res.paper_fills_resolved,
            len(settled),
            odds_used=int(odds.requests_used) if odds and odds.requests_used else None,
            odds_remaining=int(odds.requests_remaining)
            if odds and odds.requests_remaining
            else None,
        )
        console.print(
            f"[cycle {n}] scanned={res.markets_scanned} signals={len(res.signals)}"
            f" filled={len(res.acted) + res.paper_fills_resolved}"
            f" orders_placed={len(res.orders_placed)} settled={len(settled)} cash={ledger.cash:.2f}"
        )
        if res.halted:
            console.print(f"[bold red]HALTED, no entries: {res.halted}[/bold red]")
        _print_signals([*res.acted, *res.orders_placed])
        if cycles and n >= cycles:
            break
        time.sleep(interval)


@app.command()
def report() -> None:
    """Paper-trading performance summary."""
    s = _settings()
    ledger = Ledger(s.paper_db_path, s.paper_starting_cash)
    p = ledger.performance()
    t = Table(title="Paper performance")
    t.add_column("metric")
    t.add_column("value", justify="right")
    rows = [
        ("starting cash", f"{p.starting_cash:.2f}"),
        ("cash", f"{p.cash:.2f}"),
        ("open positions", str(p.open_positions)),
        ("open cost", f"{p.open_cost:.2f}"),
        ("settled fills", str(p.settled_count)),
        ("wins", str(p.wins)),
        ("realized pnl", f"{p.realized_pnl:.2f}"),
        ("fees paid", f"{p.fees_paid:.2f}"),
        ("brier (strategy)", _fmt(p.brier_score)),
        ("brier (market price)", _fmt(p.market_brier_score)),
    ]
    for k, v in rows:
        t.add_row(k, v)
    console.print(t)
    by = ledger.performance_by_strategy()
    if by:
        st = Table(title="Per strategy")
        for col in ("strategy", "settled", "wins", "realized pnl", "fees", "brier", "market brier"):
            st.add_column(col, justify="right" if col != "strategy" else "left")
        for name, sp in by.items():
            st.add_row(
                name,
                str(sp.settled_count),
                str(sp.wins),
                f"{sp.realized_pnl:.2f}",
                f"{sp.fees_paid:.2f}",
                _fmt(sp.brier_score),
                _fmt(sp.market_brier_score),
            )
        console.print(st)
    by_league = ledger.performance_by_league()
    if by_league:
        lt = Table(title="Per league")
        for col in ("league", "open", "settled", "wins", "realized pnl", "brier", "market brier"):
            lt.add_column(col, justify="right" if col != "league" else "left")
        for name, lp in by_league.items():
            lt.add_row(
                name,
                str(lp.open_positions),
                str(lp.settled_count),
                str(lp.wins),
                f"{lp.realized_pnl:.2f}",
                _fmt(lp.brier_score),
                _fmt(lp.market_brier_score),
            )
        console.print(lt)
    pos = ledger.positions()
    if pos:
        pt = Table(title="Open positions")
        for col in ("ticker", "side", "count", "cost", "avg fair"):
            pt.add_column(col)
        for x in pos:
            pt.add_row(
                x.ticker, x.side.value, f"{x.count:.0f}", f"{x.cost:.2f}", f"{x.fair_prob_avg:.3f}"
            )
        console.print(pt)


@app.command()
def predictions(
    limit: int = typer.Option(50, help="Newest N signals (0 = all)"),
    markdown: Path | None = typer.Option(None, help="Also write a Markdown table to this file"),
) -> None:
    """Every signal the strategies produced, with the outcome once the market settles."""
    s = _settings()
    ledger = Ledger(s.paper_db_path, s.paper_starting_cash)
    preds = ledger.predictions(limit)
    t = Table(title=f"Predictions ({len(preds)})")
    for col in (
        "when (UTC)",
        "strategy",
        "prediction",
        "edge",
        "traded",
        "result",
        "ticker",
    ):
        t.add_column(col)
    for p in preds:
        t.add_row(
            p.created_at[:16].replace("T", " "),
            p.strategy,
            p.summary,
            _fmt(p.edge),
            "yes" if p.acted else "no",
            _outcome(p),
            p.ticker,
        )
    console.print(t)
    if markdown is not None:
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(predictions_markdown(ledger, preds))


def _outcome(p: Prediction) -> str:
    if p.result is None:
        return "open"
    return f"{'WON' if p.won else 'lost'} ({p.result.value})"


def predictions_markdown(ledger: Ledger, preds: list[Prediction]) -> str:
    perf = ledger.performance()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Paper-trading predictions",
        "",
        f"Updated {now}. Cash **${perf.cash:.2f}** (started ${perf.starting_cash:.2f}),"
        f" realized PnL **${perf.realized_pnl:.2f}**, fees ${perf.fees_paid:.2f},"
        f" settled {perf.settled_count}, wins {perf.wins}, open positions {perf.open_positions}.",
        "",
        "Brier (lower is better): strategy "
        f"{_fmt(perf.brier_score)} vs market price {_fmt(perf.market_brier_score)}.",
        "",
        *[
            f"- `{name}`: settled {sp.settled_count}, wins {sp.wins}, realized PnL"
            f" ${sp.realized_pnl:.2f}, Brier {_fmt(sp.brier_score)} vs market"
            f" {_fmt(sp.market_brier_score)}"
            for name, sp in ledger.performance_by_strategy().items()
        ],
        "",
        "Per league (Kalshi series):",
        "",
        *[
            f"- `{name}`: open {lp.open_positions}, settled {lp.settled_count}, wins {lp.wins},"
            f" realized PnL ${lp.realized_pnl:.2f}, Brier {_fmt(lp.brier_score)} vs market"
            f" {_fmt(lp.market_brier_score)}"
            for name, lp in ledger.performance_by_league().items()
        ],
        "",
        "`price` is what we (paper) pay for `side`; `fair` is the strategy's probability that"
        " `side` wins; `edge` is fair − price − fees. `traded` = no means the risk limits or"
        " cash blocked the fill.",
        "",
        "| when (UTC) | strategy | prediction | side | price | fair | edge | size | traded"
        " | result | why | ticker |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    if not preds:
        lines.append("| _no signals yet_ | | | | | | | | | | | |")
    for p in preds:
        when = p.created_at[:16].replace("T", " ")
        lines.append(
            f"| {when} | {_md(p.strategy)} | {_md(p.summary)} | {p.side.value}"
            f" | {p.limit_price:.4f} | {p.fair_prob:.4f} | {p.edge:+.4f} | {p.size:.0f}"
            f" | {'yes' if p.acted else 'no'} | {_outcome(p)} | {_md(p.rationale)}"
            f" | {_md(p.ticker)} |"
        )
    return "\n".join(lines) + "\n"


@app.command()
def dashboard(
    out: Path = typer.Option(Path("data/index.html"), help="Write the HTML dashboard here"),
) -> None:
    """Write a self-contained HTML dashboard of every prediction, trade and the equity curve."""
    s = _settings()
    ledger = Ledger(s.paper_db_path, s.paper_starting_cash)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_dashboard(ledger, s))
    console.print(f"wrote {out}")


_MD_SPECIAL = "\\`*_[]()<>|!#~"


def _md(text: str) -> str:
    """Neutralise Markdown in venue-supplied text so it renders literally in a table cell."""
    out = "".join(f"\\{ch}" if ch in _MD_SPECIAL else ch for ch in text)
    return out.replace("\n", " ")


@app.command()
def balance() -> None:
    """Show the (demo) account balance — requires API credentials."""
    s = _settings()
    client = _client(s, need_auth=True)
    console.print(client.get_balance())
    console.print(client.get_positions())


@app.command()
def order(
    ticker: str,
    side: OutcomeSide,
    price: str = typer.Argument(..., help="Max price to pay for that side, in dollars"),
    count: str = typer.Argument("1"),
    yes_i_mean_it: bool = typer.Option(False, "--yes-i-mean-it"),
) -> None:
    """Place one real limit order on the configured venue (demo by default)."""
    s = _settings()
    if s.kill_switch:
        raise typer.BadParameter("PBOT_KILL_SWITCH is on; no orders of any kind are placed")
    client = _client(s, need_auth=True)
    if not client.is_demo and not yes_i_mean_it:
        raise typer.BadParameter("Non-demo venue: pass --yes-i-mean-it to place a real-money order")
    px, qty = _validate_order(price, count, s)
    held = client.live_exposure(ticker, side)
    if held + qty > s.max_position_contracts:
        raise typer.BadParameter(
            f"position would be {held + qty} contracts; "
            f"PBOT_MAX_POSITION_CONTRACTS={s.max_position_contracts}"
        )
    console.print(client.create_order(ticker, side, qty, px))


def _validate_order(price: str, count: str, s: Settings) -> tuple[Decimal, Decimal]:
    try:
        px, qty = Decimal(price), Decimal(count)
    except InvalidOperation as e:
        raise typer.BadParameter(f"price/count must be decimal numbers: {e}") from None
    if not (px.is_finite() and qty.is_finite()):
        raise typer.BadParameter("price/count must be finite")
    if not Decimal("0.01") <= px <= Decimal("0.99"):
        raise typer.BadParameter("price must be between 0.01 and 0.99 dollars")
    if qty <= 0:
        raise typer.BadParameter("count must be positive")
    if qty > s.max_position_contracts:
        raise typer.BadParameter(
            f"count exceeds PBOT_MAX_POSITION_CONTRACTS={s.max_position_contracts}"
        )
    if px * qty > s.max_order_notional:
        raise typer.BadParameter(f"notional exceeds PBOT_MAX_ORDER_NOTIONAL={s.max_order_notional}")
    return px, qty


def _fetch(client: KalshiClient, series: list[str]) -> list[Market]:
    if not series:
        return client.get_markets(limit=1000, max_pages=3)
    out: list[Market] = []
    for st in series:
        out.extend(client.get_markets(series_ticker=st, limit=1000, max_pages=2))
    return out


def _snapshot_markets(
    markets: list[Market],
    fetch_missing: Callable[[list[str]], list[Market]],
    now: datetime,
    series_list: tuple[str, ...] = SPORTSBOOK_SERIES,
) -> list[Market]:
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    cutoff = now + timedelta(days=8)
    held_series = set()
    for market in markets:
        expiration = market.expected_expiration
        if expiration is None:
            continue
        expiration = (
            expiration.replace(tzinfo=timezone.utc)
            if expiration.tzinfo is None
            else expiration.astimezone(timezone.utc)
        )
        if now <= expiration <= cutoff:
            held_series.add(market.event_ticker.split("-", 1)[0])
    missing = [series for series in series_list if series not in held_series]
    if not missing:
        return markets
    by_ticker = {market.ticker: market for market in markets}
    by_ticker.update({market.ticker: market for market in fetch_missing(missing)})
    return list(by_ticker.values())


def _planner_games(
    client: KalshiClient, markets: list[Market], ledger: Ledger, now: datetime
) -> dict[str, list[PlannedGame]]:
    series_to_sport = {**KALSHI_SERIES_TO_SPORT, **KALSHI_SERIES_TO_OUTRIGHT}
    event_markets: dict[str, list[Market]] = {}
    for market in markets:
        series = market.event_ticker.split("-", 1)[0]
        if series in SPORTSBOOK_SERIES and series_to_sport.get(series):
            event_markets.setdefault(market.event_ticker, []).append(market)
    if not event_markets:
        return {}

    pending = {
        str(row[0])
        for row in ledger.conn.execute(
            "SELECT DISTINCT ticker FROM blocks WHERE rules_version='v3' "
            "AND reason IN ('big_edge','large_gap') AND created_at BETWEEN ? AND ?",
            (
                (now - timedelta(hours=36)).isoformat(),
                (now - timedelta(hours=2)).isoformat(),
            ),
        )
    }
    event_starts: dict[str, datetime] = {}
    series_values = sorted({ticker.split("-", 1)[0] for ticker in event_markets})
    for series in series_values:
        try:
            events = client.get_events(series_ticker=series, with_nested_markets=False)
        except Exception:
            log.exception("Kalshi event lookup failed for odds planning (%s)", series)
            continue
        for event in events:
            ticker = str(event.get("event_ticker", ""))
            kickoff = _event_datetime(event)
            if ticker and kickoff is not None:
                event_starts[ticker] = kickoff

    games_by_sport: dict[str, list[PlannedGame]] = {}
    for event_ticker, legs in event_markets.items():
        series = event_ticker.split("-", 1)[0]
        sport = series_to_sport.get(series)
        if sport is None:
            continue
        kickoff = event_starts.get(event_ticker) or start_time(event_ticker)[0]
        if kickoff is None and series not in KALSHI_SERIES_TO_OUTRIGHT:
            expiration = next(
                (market.expected_expiration for market in legs if market.expected_expiration),
                None,
            )
            if expiration is not None:
                duration = (
                    timedelta(hours=2)
                    if sport.startswith("soccer_")
                    else timedelta(hours=2.5)
                    if sport == "basketball_nba"
                    else timedelta(hours=3)
                )
                kickoff = expiration - duration
        if kickoff is None:
            continue
        games_by_sport.setdefault(sport, []).append(
            PlannedGame(
                kickoff,
                any(market.ticker in pending for market in legs),
                event_ticker,
            )
        )
    return games_by_sport


def _days_left_in_month(now: datetime) -> int:
    next_month = datetime(
        now.year + now.month // 12,
        now.month % 12 + 1,
        1,
        tzinfo=timezone.utc,
    )
    return max(1, math.ceil((next_month - now.astimezone(timezone.utc)).total_seconds() / 86400))


def _print_signals(signals: list[Signal]) -> None:
    if not signals:
        return
    t = Table()
    for col in ("strategy", "ticker", "side", "price", "fair", "edge", "size", "why"):
        t.add_column(col)
    for s in signals:
        t.add_row(
            s.strategy,
            s.ticker,
            s.side.value,
            _fmt(s.limit_price),
            _fmt(s.fair_prob),
            _fmt(s.edge),
            f"{s.size:.0f}",
            s.rationale[:60],
        )
    console.print(t)


if __name__ == "__main__":
    app()
