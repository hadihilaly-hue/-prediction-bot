from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from prediction_bot.clv_report import MIN_GAMES, clustered_mean, event_of, fee_cents, fee_schedule
from prediction_bot.config import Settings
from prediction_bot.paper.ledger import Ledger
from prediction_bot.position_clv import weighted_position_clv


@dataclass(frozen=True)
class MakerGate:
    taker_n: int
    taker_mean: Decimal | None
    taker_ci_low: Decimal | None
    taker_ci_high: Decimal | None
    maker_scored: int
    maker_clv_n: int
    maker_mean: Decimal | None
    passed: bool
    reason: str
    # v5 Phase 4 evidence: queue-model maker fills with a clean closing price.
    queue_n: int = 0
    queue_mean: Decimal | None = None
    queue_ci_low: Decimal | None = None
    queue_ci_high: Decimal | None = None
    # PR 2: games behind the fills, and the taker comparison (net of the taker fee),
    # made only when the taker cohort covers MIN_GAMES games.
    queue_games: int = 0
    taker_games: int = 0
    taker_net_mean: Decimal | None = None
    taker_compared: bool = False


def _mean(values: list[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def _ci95(values: list[Decimal]) -> tuple[Decimal, Decimal] | None:
    if len(values) < 2:
        return None
    mean = sum(values, Decimal(0)) / len(values)
    variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / (len(values) - 1)
    margin = Decimal("1.96") * variance.sqrt() / Decimal(len(values)).sqrt()
    return mean - margin, mean + margin


def _clustered(
    values: list[float], events: list[str]
) -> tuple[Decimal | None, tuple[Decimal, Decimal] | None]:
    stats = clustered_mean(values, events)
    mean = None if stats["mean"] is None else Decimal(str(stats["mean"]))
    ci = stats["ci95"]
    return mean, None if ci is None else (Decimal(str(ci[0])), Decimal(str(ci[1])))


def evaluate_maker_gate(ledger: Ledger, settings: Settings, rules_version: str = "v3") -> MakerGate:
    """Decide whether `auto` may turn maker-first on.

    Evidence: v3 maker shadows with a fill (`fill_at`: the earlier of the queue fill and
    a run-over, so fills where price ran through a queue that cancelled are counted)
    and a clean Kalshi close. Each is judged NET: clv_kalshi minus the unrounded
    per-contract maker fee at its price (series_fees). It passes once there are at
    least `v3_maker_gate_min_queue_fills` fills from at least MIN_GAMES games and the
    lower end of the game-clustered 95% CI of net clv is above 0. The taker cohort
    (paper fills, net of the taker fee) is compared only when it covers MIN_GAMES
    games; otherwise the comparison is skipped (`taker_compared` False).

    Units: the queue_* fields are net cents per contract.
    """
    schedule = fee_schedule(ledger.conn)
    rows = ledger.conn.execute(
        "SELECT position_key,clv_kalshi,count,tags,ticker,price FROM fills "
        "WHERE rules_version=? AND position_key IS NOT NULL AND clv_kalshi IS NOT NULL "
        "AND clv_status='ok'",
        (rules_version,),
    ).fetchall()
    clvs_by_position: dict[str, list[tuple[Decimal, Decimal]]] = {}
    net_by_position: dict[str, list[tuple[Decimal, Decimal]]] = {}
    event_by_position: dict[str, str] = {}
    for position_key, clv, count, tags, ticker, price in rows:
        if "maker" in {tag.strip().casefold() for tag in str(tags or "").split(",") if tag.strip()}:
            continue
        key = str(position_key)
        clvs_by_position.setdefault(key, []).append((Decimal(str(clv)), Decimal(str(count))))
        fee = fee_cents(str(ticker), Decimal(str(price)), "taker", schedule)
        if fee is not None:
            net_by_position.setdefault(key, []).append(
                (Decimal(str(clv)) - Decimal(str(fee)), Decimal(str(count)))
            )
        event_by_position[key] = event_of(str(ticker))
    taker_values = [
        value
        for clv_counts in clvs_by_position.values()
        if (value := weighted_position_clv(clv_counts)) is not None
    ]
    taker_mean = _mean(taker_values)
    interval = _ci95(taker_values)
    taker_net = {
        key: value
        for key, clv_counts in net_by_position.items()
        if (value := weighted_position_clv(clv_counts)) is not None
    }
    taker_games = len({event_by_position[key] for key in taker_net})
    taker_net_mean, _ = _clustered(
        [float(v) for v in taker_net.values()], [event_by_position[k] for k in taker_net]
    )

    maker_rows = ledger.conn.execute(
        "SELECT fill_trade_through_at,clv_kalshi FROM maker_shadow "
        "WHERE rules_version=? AND score_status='scored'",
        (rules_version,),
    ).fetchall()
    maker_scored = len(maker_rows)
    maker_clvs = [
        Decimal(str(clv)) for fill_at, clv in maker_rows if fill_at is not None and clv is not None
    ]
    maker_mean = _mean(maker_clvs)

    net_values: list[float] = []
    events: list[str] = []
    for ticker, event_ticker, price, clv in ledger.conn.execute(
        "SELECT ticker,event_ticker,price,clv_kalshi FROM maker_shadow "
        "WHERE COALESCE(rules_version,'v2')=? AND fill_at IS NOT NULL AND clv_status='ok' "
        "AND clv_kalshi IS NOT NULL",
        (rules_version,),
    ):
        fee = fee_cents(str(ticker), Decimal(str(price)), "maker", schedule)
        if fee is None:
            continue
        net_values.append(float(clv) - fee)
        events.append(str(event_ticker or event_of(str(ticker))))
    games = len(set(events))
    queue_mean, queue_interval = _clustered(net_values, events)

    taker_compared = taker_games >= MIN_GAMES and taker_net_mean is not None
    reason = "passed"
    if len(net_values) < settings.v3_maker_gate_min_queue_fills:
        reason = "queue_fills"
    elif games < MIN_GAMES:
        reason = "fill_games"
    elif queue_interval is None or queue_interval[0] <= 0:
        reason = "queue_ci"
    elif queue_mean is None or (
        taker_compared and taker_net_mean is not None and queue_mean < taker_net_mean
    ):
        reason = "maker_worse_than_taker"

    return MakerGate(
        taker_n=len(taker_values),
        taker_mean=taker_mean,
        taker_ci_low=None if interval is None else interval[0],
        taker_ci_high=None if interval is None else interval[1],
        maker_scored=maker_scored,
        maker_clv_n=len(maker_clvs),
        maker_mean=maker_mean,
        passed=reason == "passed",
        reason=reason,
        queue_n=len(net_values),
        queue_mean=queue_mean,
        queue_ci_low=None if queue_interval is None else queue_interval[0],
        queue_ci_high=None if queue_interval is None else queue_interval[1],
        queue_games=games,
        taker_games=taker_games,
        taker_net_mean=taker_net_mean,
        taker_compared=taker_compared,
    )
