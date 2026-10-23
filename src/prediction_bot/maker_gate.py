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
    rows = ledger.conn.execute(
        "SELECT position_key,clv_kalshi,count,tags FROM fills "
        "WHERE rules_version=? AND position_key IS NOT NULL AND clv_kalshi IS NOT NULL",
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

    reason = "passed"
    if len(taker_values) < settings.v3_maker_gate_min_positions:
        reason = "taker_positions"
    elif interval is None or interval[0] <= 0:
        reason = "taker_ci"
    elif maker_scored < settings.v3_maker_gate_min_scored:
        reason = "maker_scored"
    elif len(maker_clvs) < settings.v3_maker_gate_min_filled_clv:
        reason = "maker_filled_clv"
    elif maker_mean is None or taker_mean is None or maker_mean < taker_mean:
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
    )
