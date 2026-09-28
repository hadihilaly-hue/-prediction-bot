from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from prediction_bot.config import Settings
from prediction_bot.engine import Engine
from prediction_bot.models import Market, OutcomeSide, Signal
from prediction_bot.paper.ledger import Ledger, Prediction
from prediction_bot.strategies import STRATEGIES, Strategy
from prediction_bot.venues.kalshi import KalshiClient
from prediction_bot.venues.kalshi_auth import KalshiSigner

app = typer.Typer(help="Prediction-market bot (Kalshi demo first).", no_args_is_help=True)
console = Console()


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


def _strategies(settings: Settings, names: list[str]) -> list[Strategy]:
    unknown = set(names) - set(STRATEGIES)
    if unknown:
        raise typer.BadParameter(f"unknown strategies {sorted(unknown)}; have {sorted(STRATEGIES)}")
    return [STRATEGIES[n](settings) for n in names]


def _fmt(d: Decimal | None) -> str:
    return "-" if d is None else f"{d:.4f}"


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


@app.command()
def scan(
    strategy: list[str] = typer.Option(["book_scanner"], "--strategy", "-s"),
    series: list[str] = typer.Option([], "--series", help="Restrict to these series tickers"),
) -> None:
    """Evaluate strategies once and print signals without trading."""
    s = _settings()
    client = _client(s)
    ms = _fetch(client, series)
    signals = [sig for st in _strategies(s, strategy) for sig in st.evaluate(ms)]
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
    engine = Engine(s, client, _strategies(s, strategy), ledger, allow_live=False)
    n = 0
    while True:
        n += 1
        settled = engine.settle_open_positions()
        res = engine.run_cycle(_fetch(client, series))
        console.print(
            f"[cycle {n}] scanned={res.markets_scanned} signals={len(res.signals)}"
            f" filled={len(res.acted)} settled={len(settled)} cash={ledger.cash:.2f}"
        )
        _print_signals(res.acted)
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
        "ticker",
        "side",
        "price",
        "fair",
        "edge",
        "traded",
        "result",
    ):
        t.add_column(col)
    for p in preds:
        t.add_row(
            p.created_at[:16].replace("T", " "),
            p.strategy,
            p.ticker,
            p.side.value,
            _fmt(p.limit_price),
            _fmt(p.fair_prob),
            _fmt(p.edge),
            "yes" if p.acted else "no",
            _outcome(p),
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
        "`price` is what we (paper) pay for `side`; `fair` is the strategy's probability that"
        " `side` wins; `edge` is fair − price − fees. `traded` = no means the risk limits or"
        " cash blocked the fill.",
        "",
        "| when (UTC) | strategy | market | side | price | fair | edge | size | traded"
        " | result | why |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    if not preds:
        lines.append("| _no signals yet_ | | | | | | | | | | |")
    for p in preds:
        when = p.created_at[:16].replace("T", " ")
        lines.append(
            f"| {when} | {_md(p.strategy)} | {_md(p.ticker)} | {p.side.value}"
            f" | {p.limit_price:.4f} | {p.fair_prob:.4f} | {p.edge:+.4f} | {p.size:.0f}"
            f" | {'yes' if p.acted else 'no'} | {_outcome(p)} | {_md(p.rationale)} |"
        )
    return "\n".join(lines) + "\n"


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
