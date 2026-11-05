from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

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


def _mean(values: list[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


def _ci95(values: list[Decimal]) -> tuple[Decimal, Decimal] | None:
    if len(values) < 2:
        return None
    mean = sum(values, Decimal(0)) / len(values)
    variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / (len(values) - 1)
    margin = Decimal("1.96") * variance.sqrt() / Decimal(len(values)).sqrt()
    return mean - margin, mean + margin


def evaluate_maker_gate(ledger: Ledger, settings: Settings, rules_version: str = "v3") -> MakerGate:
    """Decide whether `auto` may turn maker-first on.

    v5: the evidence is the queue-model maker shadow itself. It passes once at least
    `v3_maker_gate_min_queue_fills` queue fills have `clv_status='ok'`, the 95% CI
    lower bound of their `clv_kalshi` is above 0, and their mean is not below the
    taker cohort's (when one exists). The taker and candle-rule figures are still
    computed and logged, but no longer decide.
    """
    rows = ledger.conn.execute(
        "SELECT position_key,clv_kalshi,count,tags FROM fills "
        "WHERE rules_version=? AND position_key IS NOT NULL AND clv_kalshi IS NOT NULL "
        "AND clv_status='ok'",
        (rules_version,),
    ).fetchall()
    clvs_by_position: dict[str, list[tuple[Decimal, Decimal]]] = {}
    for position_key, clv, count, tags in rows:
        if "maker" in {tag.strip().casefold() for tag in str(tags or "").split(",") if tag.strip()}:
            continue
        clvs_by_position.setdefault(str(position_key), []).append(
            (Decimal(str(clv)), Decimal(str(count)))
        )
    taker_values = [
        value
        for clv_counts in clvs_by_position.values()
        if (value := weighted_position_clv(clv_counts)) is not None
    ]
    taker_mean = _mean(taker_values)
    interval = _ci95(taker_values)

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

    queue_clvs = [
        Decimal(str(clv))
        for (clv,) in ledger.conn.execute(
            "SELECT clv_kalshi FROM maker_shadow WHERE COALESCE(rules_version,'v2')=? "
            "AND fill_queue_at IS NOT NULL AND clv_status='ok' AND clv_kalshi IS NOT NULL",
            (rules_version,),
        )
    ]
    queue_mean = _mean(queue_clvs)
    queue_interval = _ci95(queue_clvs)

    reason = "passed"
    if len(queue_clvs) < settings.v3_maker_gate_min_queue_fills:
        reason = "queue_fills"
    elif queue_interval is None or queue_interval[0] <= 0:
        reason = "queue_ci"
    elif queue_mean is None or (taker_mean is not None and queue_mean < taker_mean):
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
        queue_n=len(queue_clvs),
        queue_mean=queue_mean,
        queue_ci_low=None if queue_interval is None else queue_interval[0],
        queue_ci_high=None if queue_interval is None else queue_interval[1],
    )
