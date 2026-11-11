from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from prediction_bot import kickoffs, ledger_archive, ledger_store
from prediction_bot.analysis import event_datetime as _event_datetime
from prediction_bot.analysis import start_time
from prediction_bot.cadence import CadenceCalendar, parse_day_overrides
from prediction_bot.clv import (
    backfill_clv_commence_times,
    reanchor_kickoffs,
    record_clv,
    record_polymarket_clv,
    record_sharp_clv,
    refresh_clv_statuses,
)
from prediction_bot.clv_report import clv_report, format_report
from prediction_bot.config import Settings
from prediction_bot.dashboard import render_dashboard
from prediction_bot.engine import Engine
from prediction_bot.feed_selection import (
    MeasurePull,
    RotationScore,
    _utc,
    compute_feed_stats,
    eligible,
    entry_lookahead,
    feed_scores,
    liquidity_weights,
    market_ask_move_sizes,
    rotation_scores,
    select_entry_feeds,
    select_measure_pull,
    select_rotation_feeds,
)
from prediction_bot.fees import SeriesFeeCache
from prediction_bot.injuries import InjuryClient, snapshot_injuries
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
from prediction_bot.orderbooks import snapshot_orderbooks
from prediction_bot.paper.ledger import Ledger, Prediction
from prediction_bot.pull_plan import (
    CALENDAR_HORIZON,
    close_out,
    eligible_feeds,
    mark_pulled_ids,
    merge_near_targets,
    plan_allowance_left,
    plan_feed_targets,
    plan_hold,
    plan_kickoffs,
    plan_pulls,
    primary_target,
    record_pull_outcome,
    select_plan_pull,
    tag_pull_rows,
)
from prediction_bot.reference_quotes import poll_reference_quotes
from prediction_bot.release_calendar import ReleaseCalendar
from prediction_bot.series import SPORTSBOOK_SERIES
from prediction_bot.strategies import (
    STRATEGIES,
    SportsbookArbStrategy,
    SportsbookV2Strategy,
    Strategy,
)
from prediction_bot.teams import evaluable_feed
from prediction_bot.venues.kalshi import KalshiClient
from prediction_bot.venues.kalshi_auth import KalshiSigner
from prediction_bot.venues.polymarket import PolymarketClient

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
    # One per-series fee schedule for every strategy in the run; without a Kalshi client
    # and ledger (offline scans) strategies keep Kalshi's general schedule.
    fee_cache = (
        SeriesFeeCache(ledger, kalshi) if kalshi is not None and ledger is not None else None
    )
    out: list[Strategy] = []
    for n in names:
        cls = STRATEGIES[n]
        strat: Strategy
        if issubclass(cls, SportsbookV2Strategy):
            strat = cls(settings, odds_client=odds, kalshi_client=kalshi, ledger=ledger)
        elif issubclass(cls, SportsbookArbStrategy):
            strat = cls(settings, odds_client=odds)  # one client: odds fetched once per cycle
        else:
            strat = cls(settings)
        if fee_cache is not None:
            strat.fee_schedule = fee_cache
        out.append(strat)
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
def odds_trigger(
    series: list[str] = typer.Option([], "--series", help="Restrict to these series tickers"),
) -> None:
    """Print one sportsbook feed whose eligible Kalshi ask moved since its last pull."""
    s = _settings()
    client = _client(s)
    ledger = Ledger(s.paper_db_path, s.paper_starting_cash)
    requested_series = series or list(SPORTSBOOK_SERIES)
    markets = _fetch(client, requested_series)
    ledger.record_market_snapshots(markets)
    now = datetime.now(timezone.utc)
    games_by_sport = _planner_games(client, markets, ledger, now)
    feeds = sorted(games_by_sport)
    stats = compute_feed_stats(ledger, feeds, now)
    scores = rotation_scores(ledger, games_by_sport, stats, s, now)
    ledger.record_feed_stats(
        stats,
        {feed: eligible(stat, s) for feed, stat in stats.items()},
        now,
    )
    if ledger.odds_triggers_pulled_today(now) >= s.odds_trigger_daily_cap:
        return
    last_trigger = ledger.last_odds_trigger_pulled_at()
    if last_trigger is not None and now - last_trigger < timedelta(
        minutes=s.odds_trigger_min_gap_minutes
    ):
        return

    candidates: list[tuple[Decimal, Decimal, datetime, str, str, Decimal, datetime]] = []
    for feed, games in games_by_sport.items():
        score = scores[feed]
        if score.value <= 0:
            continue
        last_row = ledger.conn.execute(
            "SELECT created_at FROM feed_pulls "
            "WHERE feed=? ORDER BY created_at DESC,id DESC LIMIT 1",
            (feed,),
        ).fetchone()
        if last_row is None:
            continue
        try:
            last_pull = _utc(datetime.fromisoformat(str(last_row[0]).replace("Z", "+00:00")))
        except ValueError:
            continue
        entry_start = now + timedelta(hours=s.min_lead_hours)
        entry_end = now + entry_lookahead(s)
        for game in games:
            if not entry_start <= _utc(game.kickoff) <= entry_end:
                continue
            # A market with no earlier snapshot is not a move.
            for ticker, ask, move in market_ask_move_sizes(
                ledger,
                game.event_ticker,
                last_pull,
                now,
                spread_gate=s.v2_max_spread,
                min_move=s.odds_trigger_min_move,
                new_is_move=False,
            ):
                if not ledger.has_odds_trigger(feed, ticker, ask, since=last_pull):
                    candidates.append(
                        (
                            move,
                            score.score,
                            score.soonest_kickoff or datetime.max.replace(tzinfo=timezone.utc),
                            feed,
                            ticker,
                            ask,
                            last_pull,
                        )
                    )
    if not candidates:
        return
    # The largest move first, then the feed's rotation score.
    move, _score, _kickoff, feed, ticker, ask, last_pull = min(
        candidates,
        key=lambda item: (-item[0], -item[1], item[2], item[3], item[4], item[5]),
    )
    if ledger.record_odds_trigger(feed, ticker, ask, now, since=last_pull, move=move) is not None:
        typer.echo(feed)


@app.command()
def paper(
    strategy: list[str] = typer.Option(["book_scanner"], "--strategy", "-s"),
    series: list[str] = typer.Option([], "--series"),
    interval: int = typer.Option(60, help="Seconds between cycles"),
    cycles: int = typer.Option(0, help="Stop after N cycles (0 = run forever)"),
    trigger: str | None = typer.Option(
        None, "--trigger", help="Restrict this run to one moved feed"
    ),
    plan: str | None = typer.Option(
        None, "--plan", help="Restrict this run to one feed with due pull-plan targets"
    ),
) -> None:
    """Paper-trade: fill signals at the ask in the local ledger, settle when Kalshi resolves."""
    trigger = trigger if isinstance(trigger, str) else None
    plan = plan if isinstance(plan, str) else None
    if plan is not None and trigger is not None:
        raise typer.BadParameter("--plan and --trigger are exclusive")
    s = _settings()
    client = _client(s)
    ledger = Ledger(s.paper_db_path, s.paper_starting_cash)
    odds = _odds_client(s)
    unknown = set(strategy) - set(STRATEGIES)
    if unknown:
        raise typer.BadParameter(f"unknown strategies {sorted(unknown)}; have {sorted(STRATEGIES)}")
    pacer = None
    uses_odds = any(issubclass(STRATEGIES[name], SportsbookArbStrategy) for name in strategy)
    if trigger is not None and trigger not in {
        *KALSHI_SERIES_TO_SPORT.values(),
        *KALSHI_SERIES_TO_OUTRIGHT.values(),
    }:
        raise typer.BadParameter(f"unknown sportsbook feed {trigger!r}")
    if trigger is not None and not uses_odds:
        raise typer.BadParameter("--trigger requires a sportsbook strategy")
    if plan is not None and (plan not in KALSHI_SERIES_TO_SPORT.values() or not uses_odds):
        raise typer.BadParameter(f"--plan needs a sportsbook strategy and a game feed: {plan!r}")
    if (
        odds is not None
        and uses_odds
        and (
            trigger is not None
            or plan is not None
            or s.odds_runs_per_day > 0
            or s.odds_planner == "event_driven"
        )
    ):
        # A one-shot run is one of the scheduled runs; a loop paces per cycle.
        per_day = (
            s.odds_runs_per_day
            if s.odds_runs_per_day > 0 and cycles == 1
            else max(s.odds_runs_per_day, 3)
            if s.odds_planner == "event_driven"
            else 86400 / max(interval, 1)
        )
        calendar = None
        if s.cadence_mode != "legacy":
            try:
                calendar = CadenceCalendar.load(s.cadence_calendar_path)
            except (OSError, ValueError, KeyError):
                log.exception(
                    "cadence calendar load failed (%s); falling back to legacy pacing",
                    s.cadence_calendar_path,
                )
        pacer = OddsPacer(
            odds,
            max(per_day, s.odds_runs_per_day),
            ledger=ledger,
            trigger_mode=trigger is not None,
            plan_mode=plan is not None,
            cadence_mode=s.cadence_mode,
            calendar=calendar,
            day_overrides=parse_day_overrides(s.cadence_day_override),
        )
        if s.rules_version == "v3" and s.odds_pull_plan == "hybrid":
            pacer.move_reserve = max(0, s.odds_move_reserve)
            pacer.rotation_holds_floor = s.odds_release_calendar == "on"
        if (
            s.odds_pull_plan == "hybrid"
            and s.rules_version == "v3"
            and s.odds_feed_selection == "rotation"
            and trigger is None
            and plan is None
        ):
            # ODDS_GRID=fallback (RESERVED): no 72h+ rotation; the plan's grid targets
            # keep one pull a day per eligible feed instead.
            pacer.rotation_share = (
                0.0 if s.odds_grid == "fallback" else min(1.0, max(0.0, s.odds_entry_share))
            )
    release_cal = _release_calendar(s) if s.rules_version == "v3" else None
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
        measure: MeasurePull | None = None
        run_settings, gate = _resolve_v3_execution(s, ledger, strategy)
        if (
            gate is None
            and s.rules_version == "v3"
            and "sportsbook_v3" in strategy
            and not s.is_live
        ):
            # A configured mode (taker / maker_first) is logged too, with the gate's
            # current evidence, so the log shows what every run executed as.
            configured = evaluate_maker_gate(ledger, s)
            ledger.record_execution_mode(
                rules_version="v3",
                requested=s.v3_execution,
                resolved=run_settings.v3_execution,
                reason=f"configured ({configured.reason})",
                taker_n=configured.taker_n,
                taker_mean=configured.taker_mean,
                taker_ci_low=configured.taker_ci_low,
                maker_scored=configured.maker_scored,
                maker_clv_n=configured.maker_clv_n,
                maker_mean=configured.maker_mean,
                queue_n=configured.queue_n,
                queue_mean=configured.queue_mean,
                queue_ci_low=configured.queue_ci_low,
            )
        if gate is not None:
            resolved = run_settings.v3_execution
            detail = gate.reason
            if gate.reason == "queue_fills":
                detail = f"queue_fills: {gate.queue_n}/{s.v3_maker_gate_min_queue_fills}"
            elif gate.reason == "fill_games":
                detail = f"fill_games: {gate.queue_games} games"
            elif gate.reason == "queue_ci":
                detail = f"queue_ci: net low {gate.queue_ci_low}"
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
                queue_n=gate.queue_n,
                queue_mean=gate.queue_mean,
                queue_ci_low=gate.queue_ci_low,
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
        injury_client = InjuryClient()
        if pacer is not None:
            if pacer.rotation_holds_floor and release_cal is not None:
                try:
                    pacer.plan_hold = plan_hold(ledger, s, release_cal, datetime.now(timezone.utc))
                except Exception:
                    log.exception("Plan demand estimate failed; rotation holds no extra")
                    pacer.plan_hold = 0
            pacer.start_run()
        if odds is not None:
            try:
                kickoffs.refresh_free(ledger.conn, odds, datetime.now(timezone.utc))
            except Exception:
                log.exception("Free kickoff refresh failed; continuing paper cycle")
        try:
            reanchor_kickoffs(ledger, s)
        except Exception:
            log.exception("Kickoff re-anchoring failed; continuing paper cycle")
        settled = engine.settle_open_positions()
        try:
            score_maker_shadows(ledger, client, max_calls=120)
        except Exception:
            log.exception("Maker-shadow scoring failed; continuing paper cycle")
        try:
            backfill_clv_commence_times(ledger, client, s, max_calls=50)
        except Exception:
            log.exception("CLV commence-time backfill failed; continuing paper cycle")
        clv_calls = 0
        try:
            clv_calls = record_clv(ledger, client, max_calls=clv_max_calls)
        except Exception:
            log.exception("CLV recording failed; continuing paper cycle")
        try:
            snapshot_orderbooks(ledger, client, max_calls=max(0, clv_max_calls - clv_calls))
        except Exception:
            log.exception("Orderbook snapshots failed; continuing paper cycle")
        try:
            refresh_clv_statuses(ledger, s)
        except Exception:
            log.exception("CLV status refresh failed; continuing paper cycle")
        try:
            record_sharp_clv(ledger)
        except Exception:
            log.exception("Sharp-close CLV scoring failed; continuing paper cycle")
        if s.polymarket_enabled and not uses_odds:
            # Scanner runs only: Polymarket mids for tracked events (never traded).
            try:
                poll_reference_quotes(
                    ledger,
                    PolymarketClient(s.polymarket_gamma_base, s.polymarket_clob_base),
                    s,
                )
            except Exception:
                log.exception("Polymarket reference quotes failed; continuing paper cycle")
        try:
            record_polymarket_clv(ledger)
        except Exception:
            log.exception("Polymarket CLV scoring failed; continuing paper cycle")
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
        plan_feed: str | None = None
        plan_primary: tuple[int, str] | None = None
        if plan is not None:
            now = datetime.now(timezone.utc)
            if odds is not None:
                odds.allowed_sports = set()
                if (
                    pacer is not None
                    and odds.budget
                    and _plan_games_listed(odds, ledger, plan, now)
                ):
                    odds.allowed_sports = {plan}
                    plan_feed = plan
                    plan_primary = primary_target(ledger, plan, now)
        elif trigger is not None:
            now = datetime.now(timezone.utc)
            if odds is not None:
                odds.allowed_sports = {trigger}
            try:
                if odds is not None and pacer is not None and odds.budget:
                    games_by_sport = _planner_games(client, markets, ledger, now)
                    games = {trigger: games_by_sport.get(trigger, [])}
                    stats = compute_feed_stats(ledger, [trigger], now)
                    planned, skips, _rotation = select_rotation_feeds(
                        ledger,
                        games,
                        stats,
                        run_settings,
                        odds,
                        now,
                        1,
                    )
                    ledger.record_feed_skips(skips, now)
                    ledger.record_feed_stats(
                        stats,
                        {feed: eligible(stat, run_settings) for feed, stat in stats.items()},
                        now,
                    )
                    odds.allowed_sports = set(planned[:1])
                elif odds is not None:
                    odds.allowed_sports = set()
            except Exception:
                if odds is not None:
                    odds.allowed_sports = set()
                log.exception("Move-trigger odds selection failed; no odds feeds selected")
        elif odds is not None and s.odds_planner == "event_driven" and uses_odds:
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
                if (
                    pacer is not None
                    and s.odds_feed_selection == "rotation"
                    and s.rules_version == "v3"
                ):
                    measure = select_measure_pull(ledger, games_by_sport, s, now, exclude=planned)
                    if measure is not None:
                        # Measurement pulls are paid from the plan's allowance.
                        allowance = plan_allowance_left(ledger, s, now)
                        if allowance is not None and allowance < 1:
                            ledger.record_feed_skips({measure.feed: "measure_no_allowance"}, now)
                            measure = None
                    if measure is not None and not _measure_game_listed(odds, measure):
                        ledger.record_feed_skips({measure.feed: "measure_no_events"}, now)
                        measure = None
                    if measure is not None and pacer.grant_measure_credit(
                        measure.feed, s.measure_daily_credit_cap, now
                    ):
                        planned = [*planned, measure.feed]
                        for strat in (*strats, *shadow_strategies):
                            if isinstance(strat, SportsbookV2Strategy):
                                strat.measure_only_feeds = {measure.feed}
                if pacer is not None and s.odds_feed_selection == "rotation":
                    _refresh_pull_plan(ledger, now, s, release_cal)
                    pick = select_plan_pull(ledger, s, now, exclude=planned, calendar=release_cal)
                    if (
                        pick is not None
                        and evaluable_feed(pick.feed)
                        and _plan_games_listed(odds, ledger, pick.feed, now)
                        and pacer.grant_extra_credit(pick.feed, floor=pacer.plan_floor(now))
                    ):
                        planned = [*planned, pick.feed]
                        plan_feed = pick.feed
                        if pick.primary_id is not None:
                            plan_primary = (pick.primary_id, pick.source or "default")
                odds.allowed_sports = set(planned)
            except Exception:
                odds.allowed_sports = set()
                log.exception("Lead-aware odds planning failed; no odds feeds selected")
        elif odds is not None:
            odds.allowed_sports = None
        if odds is not None and odds.allowed_sports is not None:
            # No credit on a feed v3 cannot evaluate (unmapped teams, MMA), whichever
            # path selected it.
            odds.allowed_sports = {f for f in odds.allowed_sports if evaluable_feed(f)}
        cycle_started_at = datetime.now(timezone.utc)
        credits_before = dict(getattr(odds, "pulled_credits", {}) or {})
        res = engine.run_cycle(markets)
        if odds is not None:
            spent = {
                feed: n - credits_before.get(feed, 0)
                for feed, n in (getattr(odds, "pulled_credits", {}) or {}).items()
            }
            try:
                ledger.record_shadow_only_pulls(spent, cycle_started_at, s.rules_version)
            except Exception:
                log.exception("Shadow-only feed_pulls accounting failed")
        for strat in (*strats, *shadow_strategies):
            if isinstance(strat, SportsbookV2Strategy):
                strat.measure_only_feeds = set()
        ledger.record_feed_pull_gate_candidates(res.gate_candidates, cycle_started_at)
        if trigger is not None:
            status = "pulled" if odds is not None and odds.odds_requests_made > 0 else "skipped"
            ledger.finish_odds_trigger(trigger, status)
        if odds is not None:
            # `pulled_feeds`, not `requests_last_by_sport`: the engine clears the
            # latter at the end of run_cycle, which left every plan target unmarked
            # (and so re-pulled on the next tick) until 2026-11-06.
            pulled_this_cycle = set(getattr(odds, "pulled_feeds", ())) | set(
                getattr(odds, "requests_last_by_sport", {})
            )
            for pulled_feed in sorted(pulled_this_cycle):
                try:
                    _record_feed_pull_source(
                        ledger,
                        pulled_feed,
                        cycle_started_at,
                        plan_feed=plan_feed,
                        plan_primary=plan_primary,
                        trigger=trigger,
                        measure_feed=measure.feed if measure is not None else None,
                    )
                except Exception:
                    log.exception("Pull-plan bookkeeping failed for %s", pulled_feed)
        if measure is not None and pacer is not None and pacer.measure_pulled():
            ledger.record_measure_pull(
                measure.feed, measure.event_ticker, measure.target, measure.kickoff, now
            )
        if pacer is not None:
            pacer.finish_run(feeds=sorted(odds.allowed_sports or []) if odds is not None else None)
        run_id = ledger.record_run(
            strategy,
            res.markets_scanned,
            len(res.signals),
            len(res.acted) + res.paper_fills_resolved,
            len(settled),
            odds_used=int(odds.requests_used) if odds and odds.requests_used else None,
            odds_remaining=int(odds.requests_remaining)
            if odds and odds.requests_remaining
            else None,
            trigger_reason=f"move:{trigger}"
            if trigger is not None
            else f"plan:{plan}"
            if plan is not None
            else None,
        )
        try:
            snapshot_injuries(ledger, injury_client, run_id=run_id)
            # Retag everything so fills settled before this cycle's first
            # relevant snapshot pick up the fresh coverage.
            ledger.tag_injuries()
        except Exception:
            log.exception("Injury snapshot failed; continuing paper cycle")
        console.print(
            f"[cycle {n}] scanned={res.markets_scanned} signals={len(res.signals)}"
            f" filled={len(res.acted) + res.paper_fills_resolved}"
            f" orders_placed={len(res.orders_placed)} settled={len(settled)} cash={ledger.cash:.2f}"
        )
        if res.halted:
            console.print(f"[bold red]HALTED, no entries: {res.halted}[/bold red]")
        if res.lane is not None:
            blocked = ", ".join(f"{k}={v}" for k, v in sorted(res.lane.blocked.items()))
            console.print(
                f"underdog lane: orders_placed={len(res.lane.orders_placed)}"
                f" bought_at_ask={len(res.lane.filled)} pulled={res.lane.cancelled}"
                f" skipped=[{blocked}]"
            )
            if res.lane.halted:
                console.print(f"[bold red]{res.lane.halted}[/bold red]")
        _print_signals([*res.acted, *res.orders_placed])
        if trigger is not None or plan is not None or (cycles and n >= cycles):
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
        for col in ("bet", "game", "side", "count", "cost", "avg fair", "ticker"):
            pt.add_column(col)
        titles = ledger.event_titles()
        for x in pos:
            bet, game = ledger.position_label(x.ticker, x.side, titles)
            pt.add_row(
                bet,
                game,
                x.side.value,
                f"{x.count:.0f}",
                f"{x.cost:.2f}",
                f"{x.fair_prob_avg:.3f}",
                x.ticker,
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


def _open_positions_markdown(ledger: Ledger) -> list[str]:
    """'## Open positions' table: what each open paper bet is, in plain English."""
    pos = ledger.positions()
    if not pos:
        return []
    titles = ledger.event_titles()
    lines = [
        f"## Open positions ({len(pos)})",
        "",
        "| bet | game | contracts | cost | our chance | ticker |",
        "|---|---|---|---|---|---|",
    ]
    for x in pos:
        bet, game = ledger.position_label(x.ticker, x.side, titles)
        lines.append(
            f"| {_md(bet)} | {_md(game)} | {x.count:.0f} | ${x.cost:.2f} |"
            f" {x.fair_prob_avg * 100:.0f}% | `{x.ticker}` |"
        )
    return [*lines, ""]


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
        *_open_positions_markdown(ledger),
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


@app.command("clv-report")
def clv_report_command(
    since: str | None = typer.Option(
        None, help="Only rows created on/after this ISO date or timestamp, e.g. 2026-10-24"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the report as JSON"),
    by_source: bool = typer.Option(
        False, "--by-source", help="Also break each cohort down by why the odds pull ran"
    ),
) -> None:
    """CLV evidence by cohort: n, mean, sd, se, 95% CI, share beating the close, verdict."""
    s = _settings()
    ledger = Ledger(s.paper_db_path, s.paper_starting_cash)
    data = clv_report(
        ledger.conn, since=since, by_source=by_source if isinstance(by_source, bool) else False
    )
    if as_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        typer.echo(format_report(data))


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


def _release_calendar(s: Settings) -> ReleaseCalendar | None:
    """The Phase 3-E release calendar, or None (off, or the file failed to load)."""
    if s.odds_release_calendar != "on":
        return None
    try:
        return ReleaseCalendar.load(s.release_calendar_path, enable=s.release_enable_feeds)
    except (OSError, ValueError):
        log.exception(
            "release calendar load failed (%s); using Phase 3 targets", s.release_calendar_path
        )
        return None


def _refresh_pull_plan(
    ledger: Ledger, now: datetime, s: Settings, calendar: ReleaseCalendar | None = None
) -> int:
    """Rebuild planned targets for eligible feeds; local SQL only.

    Without a release calendar: Phase 3's T-24h/T-6h/T-90m/T-10m for events within 26h.
    With one: calendar targets for the next 48h (kickoff and clock entries, the T-10m
    close, `grid` targets under ODDS_GRID=fallback), then same-feed targets within 20
    minutes are merged.
    """
    feeds = eligible_feeds(ledger.conn)
    if calendar is None:
        return plan_pulls(ledger, plan_kickoffs(ledger.conn, feeds, now), now)
    planned = plan_pulls(
        ledger, plan_kickoffs(ledger.conn, feeds, now, CALENDAR_HORIZON), now, calendar
    )
    planned += plan_feed_targets(ledger, calendar, feeds, now, grid=s.odds_grid == "fallback")
    merge_near_targets(ledger, now)
    close_out(ledger, now)
    return planned


def _plan_games_listed(odds: OddsClient, ledger: Ledger, feed: str, now: datetime) -> bool:
    """Free /events check that the books list a game with a due target for `feed`."""
    if not getattr(odds, "events_free", True):
        return True
    stamp = now.isoformat()
    kickoffs = [
        _utc(datetime.fromisoformat(str(row[0])))
        for row in ledger.conn.execute(
            "SELECT kickoff FROM pull_plan WHERE feed=? AND status='planned' "
            "AND window_start<=? AND window_end>=?",
            (feed, stamp, stamp),
        )
    ]
    if not kickoffs:
        return False
    try:
        events = odds.events(
            feed, min(kickoffs) - timedelta(hours=1), max(kickoffs) + timedelta(hours=1)
        )
    except Exception as exc:
        log.warning("Pull-plan events check failed for %s: %s", feed, exc)
        return False
    return bool(events)


def _record_feed_pull_source(
    ledger: Ledger,
    feed: str,
    cycle_started_at: datetime,
    *,
    plan_feed: str | None,
    plan_primary: tuple[int, str] | None,
    trigger: str | None,
    measure_feed: str | None,
) -> None:
    """Mark the feed's due plan targets pulled and tag this cycle's rows with why the
    feed was pulled: the plan row's source when the plan paid, else move, measure or
    rotation (targets that rode along keep `credits=0` and note who paid)."""
    now = datetime.now(timezone.utc)
    if feed == plan_feed:
        source, plan_id = (plan_primary[1], plan_primary[0]) if plan_primary else ("default", None)
        ids = mark_pulled_ids(ledger, feed, now, 1.0)
    else:
        source = (
            "move" if trigger is not None else "measure" if feed == measure_feed else "rotation"
        )
        plan_id = None
        ids = mark_pulled_ids(ledger, feed, now, 0.0, paid_by=source)
    candidates, gate_candidates = tag_pull_rows(ledger, feed, cycle_started_at, source, plan_id)
    if ids:
        record_pull_outcome(ledger, ids, candidates, gate_candidates)


def _run_times(ledger: Ledger, since: datetime) -> list[tuple[datetime, str, str | None]]:
    out = []
    for ran_at, strategies, reason in ledger.conn.execute(
        "SELECT ran_at,strategies,trigger_reason FROM runs WHERE ran_at>=? ORDER BY ran_at",
        (since.isoformat(),),
    ):
        try:
            out.append((_utc(datetime.fromisoformat(str(ran_at))), str(strategies), reason))
        except ValueError:
            continue
    return out


def sportsbook_slot_start(now: datetime) -> datetime:
    """H:17 of the most recent even UTC hour H with H:17 at or before `now`: the
    Worker and the GitHub cron both fire the sportsbook slot at :17 of even hours."""
    now = _utc(now)
    start = now.replace(hour=now.hour - now.hour % 2, minute=17, second=0, microsecond=0)
    return start if start <= now else start - timedelta(hours=2)


@app.command("slot-guard")
def slot_guard(
    slot: str = typer.Option(..., help="scanner or sportsbook"),
    within_minutes: int = typer.Option(20, help="A run this recent means the slot already ran"),
    plan_due: bool = typer.Option(
        False,
        "--plan-due",
        help="Light tick: run a scanner slot that already ran if a funded plan target is due",
    ),
) -> None:
    """Print `skip` if this slot already ran (both the Cloudflare dispatch and the GitHub
    cron fallback fired), else `run`. Move-trigger and pull-plan runs don't count.

    With `--plan-due` (the 10-minute Cloudflare tick), a scanner slot that already ran
    still runs when a pull-plan target is due and funded; otherwise the tick is idle
    and writes nothing, so it costs no ledger commit.
    """
    if slot not in {"scanner", "sportsbook"}:
        raise typer.BadParameter("slot must be scanner or sportsbook")
    plan_due = plan_due if isinstance(plan_due, bool) else False
    s = _settings()
    ledger = Ledger(s.paper_db_path, s.paper_starting_cash)
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=within_minutes)
    if slot == "sportsbook":
        # The nominal slot, not a window: a late cron run 77 minutes after the
        # Worker's dispatch repeated the slot (14 times on Nov 6-9).
        since = sportsbook_slot_start(now)
    for _at, strategies, reason in _run_times(ledger, since):
        if reason is not None:
            continue
        sportsbook = any(name.startswith("sportsbook") for name in strategies.split(","))
        if (slot == "sportsbook" and sportsbook) or (slot == "scanner" and not sportsbook):
            if plan_due and slot == "scanner":
                try:
                    due = _due_plan_feed(ledger, s, now)
                except Exception:
                    # Fail closed: the scanner already ran within the window, so a
                    # broken plan check costs at most one plan pull, never the run.
                    log.exception("slot-guard: pull-plan check failed; skipping the tick")
                    due = None
                if due is not None:
                    typer.echo("run")
                    return
            typer.echo("skip")
            return
    typer.echo("run")


def _due_plan_feed(ledger: Ledger, s: Settings, now: datetime) -> str | None:
    if s.odds_pull_plan == "off" or s.rules_version != "v3":
        return None
    calendar = _release_calendar(s)
    _refresh_pull_plan(ledger, now, s, calendar)
    pick = select_plan_pull(ledger, s, now, calendar=calendar)
    return pick.feed if pick is not None else None


@app.command()
def health(
    max_age_minutes: int = typer.Option(90, help="Fail if the last run is older than this"),
    min_slots: Annotated[
        int,
        typer.Option(help="Also fail when fewer of the last 24h's 48 half-hour slots ran (0=off)"),
    ] = 0,
    manifest: Annotated[
        Path | None,
        typer.Option(
            help="Read last_run_at and slots_last_24h from this ledger_manifest.json instead "
            "of the database; falls back to the database when the file does not exist",
        ),
    ] = None,
) -> None:
    """Dead-man check: exit 1 if the last run is older than `max_age_minutes`, or fewer
    than `min_slots` half-hour slots ran in the last 24h."""
    now = datetime.now(timezone.utc)
    if manifest is not None and manifest.is_file():
        data = ledger_store.Manifest.from_json(manifest.read_text())
        raw_last, slots, source = data.last_run_at, data.slots_last_24h, "manifest"
    else:
        s = _settings()
        ledger = Ledger(s.paper_db_path, s.paper_starting_cash)
        slots = ledger_store.slots_last_24h(ledger.conn, now)
        row = ledger.conn.execute("SELECT MAX(ran_at) FROM runs").fetchone()
        raw_last, source = (str(row[0]) if row and row[0] else None), "database"
    last = _utc(datetime.fromisoformat(raw_last)) if raw_last else None
    if last is None:
        typer.echo("no runs recorded")
        raise typer.Exit(1)
    age = (now - last).total_seconds() / 60
    typer.echo(
        f"last run {last.isoformat(timespec='minutes')} ({age:.0f} min ago); "
        f"{slots} of 48 half-hour slots ran in the last 24h (from the {source})"
    )
    failed = False
    if age > max_age_minutes:
        typer.echo(f"FAIL: the last run is older than {max_age_minutes} minutes")
        failed = True
    if min_slots > 0 and slots < min_slots:
        typer.echo(
            f"FAIL: {slots} slots is below {min_slots} of 48; check the Cloudflare Worker "
            "token (infra/cloudflare-trigger)"
        )
        failed = True
    if failed:
        raise typer.Exit(1)


@app.command("ledger-restore")
def ledger_restore(
    out: Path = typer.Option(Path("ledger/paper.sqlite"), help="Write the verified ledger here"),
    bootstrap: bool = typer.Option(
        False,
        envvar="PBOT_LEDGER_BOOTSTRAP",
        help="Start an empty ledger when the branch or its ledger does not exist",
    ),
    require_manifest: bool = typer.Option(
        False, help="Fail unless the branch tip has a ledger_manifest.json"
    ),
) -> None:
    """Restore the paper ledger from the ledger-store release (fails closed)."""
    try:
        result = ledger_store.restore(
            ledger_store.SubprocessRunner(),
            out,
            bootstrap=bootstrap,
            require_manifest=require_manifest,
        )
    except ledger_store.LedgerStoreError as exc:
        typer.echo(f"ledger-restore failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"ledger restored from {result.source}: {result.detail}")


@app.command("ledger-save")
def ledger_save(
    db: Path = typer.Option(Path("ledger/paper.sqlite"), help="The ledger to save"),
    dir: Path = typer.Option(Path("ledger"), help="Folder with the report files"),
    worktree: Path = typer.Option(Path("wt"), help="Scratch worktree of paper-ledger"),
) -> None:
    """Upload the ledger as a release asset and commit its manifest to paper-ledger."""
    try:
        result = ledger_store.save(ledger_store.SubprocessRunner(), db, dir, worktree=worktree)
    except ledger_store.LedgerStoreError as exc:
        typer.echo(f"ledger-save failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    m = result.manifest
    typer.echo(
        f"ledger saved: {m.asset} ({m.gz_bytes} bytes, {m.runs} runs, "
        f"{'uploaded' if result.uploaded else 'unchanged, not re-uploaded'}); "
        f"pruned {len(result.pruned)} old assets"
    )
    for error in result.prune_errors:
        typer.echo(f"warning: prune: {error}", err=True)


@app.command("ledger-archive")
def ledger_archive_command(
    db: Path = typer.Option(Path("ledger/paper.sqlite"), help="The hot ledger"),
    keep_days: int = typer.Option(
        ledger_archive.KEEP_DAYS, help="Keep closed markets' rows this many days"
    ),
    orderbook_days: int = typer.Option(
        ledger_archive.ORDERBOOK_DAYS, help="Keep order books this many days"
    ),
) -> None:
    """Once per UTC day, move old rows of closed markets to the ledger-archive release.

    Never fails the job: any error leaves the ledger untouched and only warns."""
    s = _settings()
    try:
        ledger = Ledger(db, s.paper_starting_cash)
        result = ledger_archive.run_archive(
            ledger_store.SubprocessRunner(),
            ledger.conn,
            db.parent,
            keep_days=keep_days,
            orderbook_days=orderbook_days,
        )
    except Exception as exc:  # noqa: BLE001 - an archive problem must not stop the cycle
        log.exception("ledger-archive failed")
        typer.echo(f"warning: ledger-archive skipped: {exc}", err=True)
        return
    rows = ", ".join(f"{t} {n}" for t, n in result.rows.items()) or "no rows"
    typer.echo(f"ledger-archive {result.day}: {result.status} {result.asset} ({rows})")


@app.command("ledger-archive-fetch")
def ledger_archive_fetch(
    out: Path = typer.Option(Path("analysis/paper.sqlite"), help="Write the full history here"),
) -> None:
    """Restore the hot ledger and merge every archive asset into it (for analysis)."""
    runner = ledger_store.SubprocessRunner()
    try:
        ledger_store.restore(runner, out)
        conn = sqlite3.connect(str(out))
        merged = ledger_archive.fetch_full_history(runner, conn, out.parent)
        conn.close()
    except (ledger_store.LedgerStoreError, ledger_archive.ArchiveError) as exc:
        typer.echo(f"ledger-archive-fetch failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    for name, added in merged.items():
        typer.echo(f"{name}: " + ", ".join(f"{t} +{n}" for t, n in added.items()))
    typer.echo(f"full history in {out} ({len(merged)} archive files merged)")


@app.command("ledger-backfill-model-side")
def ledger_backfill_model_side(
    db: Path = typer.Option(Path("ledger/paper.sqlite"), help="The hot ledger to update"),
) -> None:
    """One-time: fill shadow_decisions.mid/model_side from archived market snapshots.

    The scoreboard backfill only searched the hot ledger, after the daily archive had
    already moved older snapshots out. This reads every ledger-archive asset once,
    writes the rows it can, and marks itself done; later runs return at once. Failures
    only warn (the next run retries) so a paper cycle is never stopped by it.
    """
    s = _settings()
    ledger = Ledger(db, s.paper_starting_cash)
    try:
        result = ledger_archive.backfill_model_side_from_archives(
            ledger_store.SubprocessRunner(), ledger, db.parent
        )
    except (ledger_store.LedgerStoreError, ledger_archive.ArchiveError, sqlite3.Error) as exc:
        typer.echo(f"ledger-backfill-model-side: skipped ({exc}); will retry next run", err=True)
        return
    finally:
        ledger.close()
    if result is None:
        typer.echo("ledger-backfill-model-side: already done")
        return
    updated, assets = result
    typer.echo(f"ledger-backfill-model-side: {updated} rows set from {len(assets)} archive files")


@app.command("reference-gaps")
def reference_gaps(
    days: int = typer.Option(14, help="Look back this many days"),
) -> None:
    """Markdown list of tracked Kalshi events Polymarket could not be matched to."""
    s = _settings()
    ledger = Ledger(s.paper_db_path, s.paper_starting_cash)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = ledger.conn.execute(
        "SELECT event_ticker,reason,COALESCE(unknown_names,''),MAX(seen_at),COUNT(*) "
        "FROM reference_unmatched WHERE source='polymarket' AND seen_at>=? "
        "GROUP BY event_ticker,reason,unknown_names ORDER BY event_ticker",
        (since,),
    ).fetchall()
    names: dict[str, int] = {}
    for _event, _reason, unknown, _seen, count in rows:
        for name in filter(None, str(unknown).split(",")):
            names[name] = names.get(name, 0) + int(count)
    lines = [
        "# Polymarket alias gaps",
        "",
        f"Tracked Kalshi events with no Polymarket game market match, last {days} days.",
        "Names are Polymarket outcome or subject names no `teams.py` alias resolved; add",
        "them as `polymarket_names` in `teams.json` once checked.",
        "",
        "## Unresolved Polymarket names",
        "",
        *([f"- `{n}` ({c} events)" for n, c in sorted(names.items())] or ["- none"]),
        "",
        "## Unmatched events",
        "",
        "| Kalshi event | Reason | Days seen | Last seen |",
        "|---|---|---:|---|",
        *[
            f"| `{event}` | {reason} | {count} | {str(seen)[:16]} |"
            for event, reason, _unknown, seen, count in rows
        ],
    ]
    typer.echo("\n".join(lines))


@app.command("audit-kickoffs")
def audit_kickoffs(
    day: str | None = typer.Option(
        None, "--day", help="Only events whose resolved or stored kickoff is on this UTC date"
    ),
    unscored: bool = typer.Option(False, "--unscored", help="Only rows not yet scored"),
) -> None:
    """Rows whose stored kickoff differs from the resolved one by more than 5 minutes.

    Read-only: compares against `event_kickoffs` as it stands (odds-based kickoffs only).
    """
    s = _settings()
    ledger = Ledger(s.paper_db_path, s.paper_starting_cash)
    rows = kickoffs.kickoff_rows(ledger.conn)
    if day is not None:
        rows = [
            r
            for r in rows
            if day in (r["resolved"].date().isoformat(), r["stored"].date().isoformat())
        ]
    if unscored:
        rows = [r for r in rows if not r["scored"]]
    lines = [
        "| table | id | ticker | stored | resolved | off (min) | status |",
        "|---|---:|---|---|---|---:|---|",
    ]
    for r in sorted(rows, key=lambda r: (r["event_ticker"], r["table"], r["id"])):
        minutes = round((r["resolved"] - r["stored"]).total_seconds() / 60)
        status = r["status"] or ("scored" if r["scored"] else "pending")
        lines.append(
            f"| {r['table']} | {r['id']} | {r['ticker']} | {r['stored']:%Y-%m-%d %H:%MZ} "
            f"| {r['resolved']:%Y-%m-%d %H:%MZ} | {minutes:+d} | {status} |"
        )
    pending = sum(1 for r in rows if not r["scored"])
    lines.append("")
    lines.append(f"{len(rows)} rows more than 5 minutes off ({pending} not yet scored).")
    typer.echo("\n".join(lines))


@app.command("pull-due")
def pull_due(
    target: int | None = typer.Option(
        None, "--target", help="A pull_plan id (workflow_dispatch input): print its feed if due"
    ),
) -> None:
    """Print one feed whose pull-plan targets are due and funded (local data only)."""
    target = target if isinstance(target, int) else None
    s = _settings()
    if s.odds_pull_plan == "off" or s.rules_version != "v3":
        return
    ledger = Ledger(s.paper_db_path, s.paper_starting_cash)
    now = datetime.now(timezone.utc)
    if target is not None:
        # A dispatched target runs only that target's feed, and only while it is due.
        row = ledger.conn.execute(
            "SELECT feed FROM pull_plan WHERE id=? AND status='planned' "
            "AND window_start<=? AND window_end>=?",
            (target, now.isoformat(), now.isoformat()),
        ).fetchone()
        if row is not None:
            typer.echo(str(row[0]))
        return
    feed = _due_plan_feed(ledger, s, now)
    if feed is not None:
        typer.echo(feed)


def _measure_game_listed(odds: OddsClient, measure: MeasurePull) -> bool:
    """Free /events check that the books list the measured game before spending a credit."""
    if not getattr(odds, "events_free", True):
        return True
    try:
        events = odds.events(
            measure.feed,
            measure.kickoff - timedelta(hours=1),
            measure.kickoff + timedelta(hours=1),
        )
    except Exception as exc:
        log.warning("Measure events check failed for %s: %s", measure.feed, exc)
        return False
    return bool(events)


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
            # A merged row spans [created_at, last_seen_at] within one UTC day; overlap with
            # the window means an observation inside it (Ledger.has_recent_large_gap_record).
            "SELECT DISTINCT ticker FROM blocks WHERE rules_version='v3' "
            "AND reason IN ('big_edge','large_gap') AND created_at<=? "
            "AND COALESCE(last_seen_at,created_at)>=?",
            (
                (now - timedelta(hours=2)).isoformat(),
                (now - timedelta(hours=36)).isoformat(),
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
    offsets = kickoffs.expiry_offsets(
        ledger.conn, {ticker.split("-", 1)[0] for ticker in event_markets}, now
    )
    for event_ticker, legs in event_markets.items():
        series = event_ticker.split("-", 1)[0]
        sport = series_to_sport.get(series)
        if sport is None:
            continue
        # Books' kickoff first, then Kalshi's own start, then any estimate on record.
        resolved = kickoffs.resolve(ledger.conn, event_ticker)
        kickoff = (
            kickoffs.resolve_odds(ledger.conn, event_ticker)
            or event_starts.get(event_ticker)
            or start_time(event_ticker)[0]
            or (resolved[0] if resolved is not None else None)
        )
        if kickoff is None and series not in KALSHI_SERIES_TO_OUTRIGHT:
            expiration = next(
                (market.expected_expiration for market in legs if market.expected_expiration),
                None,
            )
            if expiration is not None:
                kickoff = expiration - offsets[series]
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
