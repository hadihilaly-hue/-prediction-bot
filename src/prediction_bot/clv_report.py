"""CLV evidence report (v5 Phase 0.2): cohort summaries with CIs and Buchdahl verdicts.

Every numeric column in the ledger is stored as TEXT, so values are parsed in Python;
never compare them in SQL without a CAST (string comparison silently mis-buckets).
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from prediction_bot.fees import DEFAULT_FEE_SCHEDULE, FeeStyle, fee_for_ticker
from prediction_bot.paper.ledger import UNDERDOG_LANE_REASON, decision_window, lane_fill_sql

MIN_EVIDENCE_N = 50  # legacy row-count rule; `summarize` (Kelly sizing) still uses it
MIN_GAMES = 30
Z95 = 1.96

# Metric column -> what it measures. All in cents per contract.
METRICS: dict[str, str] = {
    "clv_kalshi": "Kalshi side mid at T-5 minus entry",
    "clv_kalshi_bid": "Kalshi side bid at T-5 minus entry",
    "clv_sharp_close": "power-devigged sharp-book close minus entry",
    "clv_sharp_close_shin": "Shin-devigged sharp-book close minus entry",
    "clv_sharp_close_mult": "multiplicatively devigged sharp-book close minus entry",
    "clv_polymarket": "Polymarket mid at the Kalshi close minus entry",
}

VERDICT_RULE = (
    f"supported: >= {MIN_GAMES} games and the lower end of the game-clustered 95% CI of "
    f"NET clv > 0; rejected: >= {MIN_GAMES} games and its upper end < 0; open otherwise. "
    "Net = clv minus the per-contract Kalshi fee at the entry price (taker fee for taker "
    "cohorts, maker fee for maker cohorts, from series_fees)."
)
ALL_ROWS_LABEL = "both sides: equals minus half the spread by construction"


@dataclass(frozen=True)
class Cohort:
    label: str
    table: str
    where: str
    fee_style: FeeStyle
    # Near-miss cohorts log both sides of a game, so their headline is the side the
    # model favoured (model_side = 1); the all-rows mean is kept, labelled.
    favoured_headline: bool = False


_LEGACY_LINE_MOVE = "(',' || COALESCE(tags,'') || ',') NOT LIKE '%,legacy_double_mid,%'"
_HAS_MAKER_TAG = "(',' || COALESCE(tags,'') || ',') LIKE '%,maker,%'"


def _near_miss(reason: str, favoured: bool = False) -> Cohort:
    return Cohort(
        f"v3 near misses blocked by {reason}",
        "shadow_decisions",
        f"rules_version='v3' AND would_trade=0 AND reason='{reason}' AND clv_status='ok'",
        "taker",
        favoured,
    )


COHORTS: dict[str, Cohort] = {
    "fills": Cohort("Paper fills (all rules versions)", "fills", "clv_status='ok'", "taker"),
    "v3_net_edge": _near_miss("net_edge", favoured=True),
    "v3_entry_window": _near_miss("entry_window", favoured=True),
    "v3_price_floor": _near_miss("price_floor"),
    "v3_thin_market": _near_miss("thin_market"),
    "v3_sharp_move": Cohort(
        "v3 sharp-book moves of 2c+ (observations)",
        "shadow_decisions",
        "rules_version='v3' AND would_trade=0 AND reason='sharp_move' AND clv_status='ok'",
        "taker",
    ),
    "v3_sharp_move_follow": Cohort(
        "v3 sharp moves Kalshi lagged that passed the v3 gates (shadow)",
        "shadow_decisions",
        "rules_version='v3' AND would_trade=1 AND reason='sharp_move_follow' AND clv_status='ok'",
        "taker",
    ),
    "maker_queue": Cohort(
        "v3 maker shadows filled under the queue model",
        "maker_shadow",
        "COALESCE(rules_version,'v2')='v3' AND fill_queue_at IS NOT NULL AND clv_status='ok'",
        "maker",
    ),
    "line_move_follower": Cohort(
        "v2 line_move_follower shadows (priced at the ask; old double-recorded mid rows excluded)",
        "shadow_decisions",
        f"strategy='line_move_follower' AND would_trade=1 AND clv_status='ok' "
        f"AND {_LEGACY_LINE_MOVE}",
        "taker",
    ),
    # The underdog lane. The first two are the same resting-order picks under two fill
    # rules: the gap between them is what assuming an instant fill was worth.
    "underdog_lane": Cohort(
        "Underdog lane: resting orders the market traded through (cash)",
        "fills",
        f"{lane_fill_sql()} AND {_HAS_MAKER_TAG} AND clv_status='ok'",
        "maker",
    ),
    "underdog_instant": Cohort(
        "Underdog lane: the same resting-order picks booked as filled at once (no cash)",
        "shadow_decisions",
        f"reason='{UNDERDOG_LANE_REASON}' AND would_trade=1 AND {_HAS_MAKER_TAG} "
        "AND clv_status='ok'",
        "maker",
    ),
    "underdog_lane_ask": Cohort(
        "Underdog lane: picks with no room inside the spread, bought at the ask (cash)",
        "fills",
        f"{lane_fill_sql()} AND NOT {_HAS_MAKER_TAG} AND clv_status='ok'",
        "taker",
    ),
}

# Column -> SQL per table; a column missing from an older ledger reads as NULL.
_COLUMNS: tuple[tuple[str, dict[str, str]], ...] = (
    ("created_at", {}),
    ("ticker", {}),
    ("price", {}),
    ("net_edge", {}),
    ("window", {}),
    (
        "commence",
        {
            "fills": "COALESCE(commence_time,commence_time_backfill)",
            "shadow_decisions": "COALESCE(commence_time,commence_time_backfill)",
            "maker_shadow": "commence_time",
        },
    ),
    ("position_key", {}),
    ("fair", {"fills": "fair_prob"}),
    ("mid", {}),
    ("model_side", {}),
    *((metric, {}) for metric in METRICS),
    ("pull_source", {}),
)

# (fair - mid) buckets in cents for the dose-response table: (label, upper bound).
DOSE_BUCKETS: tuple[tuple[str, float | None], ...] = (
    ("< -2", -2.0),
    ("-2..-1", -1.0),
    ("-1..0", 0.0),
    ("0..1", 1.0),
    ("1..2", 2.0),
    ("2..3", 3.0),
    (">= 3", None),
)

PRICE_BANDS = ("<0.25", "0.25-0.40", "0.40-0.60", "0.60-0.80", ">=0.80")
EDGE_BUCKETS = ("<-5c", "-5..-3c", "-3..-2c", "-2..-1c", "-1..0c", "0..+1c", ">=+1c")
WINDOWS = ("72h+", "24-72h", "2-24h", "<2h")


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def price_band(price: Decimal) -> str:
    if price < Decimal("0.25"):
        return "<0.25"
    if price < Decimal("0.40"):
        return "0.25-0.40"
    if price < Decimal("0.60"):
        return "0.40-0.60"
    if price < Decimal("0.80"):
        return "0.60-0.80"
    return ">=0.80"


def edge_bucket(net_edge: Decimal | None) -> str:
    if net_edge is None:
        return "unknown"
    for upper, label in (
        (Decimal("-0.05"), "<-5c"),
        (Decimal("-0.03"), "-5..-3c"),
        (Decimal("-0.02"), "-3..-2c"),
        (Decimal("-0.01"), "-2..-1c"),
        (Decimal("0"), "-1..0c"),
        (Decimal("0.01"), "0..+1c"),
    ):
        if net_edge < upper:
            return label
    return ">=+1c"


def summarize(values: list[float]) -> dict[str, Any]:
    """n, mean, sd, se, 95% CI (normal), share beating the close (> 0), and verdict."""
    n = len(values)
    if n == 0:
        return {
            "n": 0,
            "mean": None,
            "sd": None,
            "se": None,
            "ci95": None,
            "beat_close": None,
            "verdict": "open",
        }
    mean = sum(values) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in values) / (n - 1)) if n > 1 else None
    se = sd / math.sqrt(n) if sd is not None else None
    ci = [mean - Z95 * se, mean + Z95 * se] if se is not None else None
    verdict = "open"
    if n >= MIN_EVIDENCE_N and ci is not None:
        if ci[0] > 0:
            verdict = "supported"
        elif ci[1] < 0:
            verdict = "rejected"
    return {
        "n": n,
        "mean": mean,
        "sd": sd,
        "se": se,
        "ci95": ci,
        "beat_close": sum(1 for x in values if x > 0) / n,
        "verdict": verdict,
    }


def event_of(ticker: str) -> str:
    """The game a contract belongs to: its ticker without the outcome segment."""
    return str(ticker).rsplit("-", 1)[0]


def clustered_mean(values: Sequence[float], clusters: Sequence[str]) -> dict[str, Any]:
    """Mean with a cluster-robust standard error (clusters = games).

    se^2 = G/(G-1) * sum_g (sum_{i in g} (x_i - mean))^2 / n^2, the CR1 estimator for a
    mean. Rows from one game (both sides, repeated scans) are not independent, so the
    row-count formula overstates the evidence. None when fewer than two games.
    """
    n = len(values)
    if n == 0:
        return {"mean": None, "se": None, "ci95": None}
    mean = sum(values) / n
    sums: dict[str, float] = {}
    for value, cluster in zip(values, clusters, strict=True):
        sums[cluster] = sums.get(cluster, 0.0) + (value - mean)
    groups = len(sums)
    if groups < 2:
        return {"mean": mean, "se": None, "ci95": None}
    variance = groups / (groups - 1) * sum(total**2 for total in sums.values()) / n**2
    se = math.sqrt(variance)
    return {"mean": mean, "se": se, "ci95": [mean - Z95 * se, mean + Z95 * se]}


def verdict(games: int, ci95: Sequence[float] | None) -> str:
    """The scoreboard rule (VERDICT_RULE), applied to the clustered CI of NET clv."""
    if games < MIN_GAMES or ci95 is None:
        return "open"
    if ci95[0] > 0:
        return "supported"
    if ci95[1] < 0:
        return "rejected"
    return "open"


def summarize_rows(rows: Sequence[dict[str, Any]], metric: str = "clv_kalshi") -> dict[str, Any]:
    """Rows, positions, games, gross and net means, game-clustered 95% CIs, the share
    beating the close and the verdict on net clv. Each row carries `event`,
    `position_key`, `fee` (cents per contract, None when unknown) and the metric."""
    scored = [r for r in rows if r.get(metric) is not None]
    gross = [float(r[metric]) for r in scored]
    events = [str(r["event"]) for r in scored]
    netted = [r for r in scored if r.get("fee") is not None]
    net_values = [float(r[metric]) - float(r["fee"]) for r in netted]
    gross_stats = clustered_mean(gross, events)
    net_stats = clustered_mean(net_values, [str(r["event"]) for r in netted])
    games = len(set(events))
    moves = [float(r["move"]) for r in scored if r.get("move") is not None]
    sd = (
        math.sqrt(sum((x - gross_stats["mean"]) ** 2 for x in gross) / (len(gross) - 1))
        if len(gross) > 1
        else None
    )
    return {
        "n": len(scored),
        "positions": len({r.get("position_key") or r["event"] for r in scored}),
        "games": games,
        "mean": gross_stats["mean"],
        "sd": sd,
        "se": gross_stats["se"],
        "ci95_gross": gross_stats["ci95"],
        "fee": sum(float(r["fee"]) for r in netted) / len(netted) if netted else None,
        "mean_net": net_stats["mean"],
        "se_net": net_stats["se"],
        "ci95": net_stats["ci95"],
        "mean_move": sum(moves) / len(moves) if moves else None,
        "beat_close": sum(1 for x in gross if x > 0) / len(gross) if gross else None,
        "verdict": verdict(games, net_stats["ci95"]),
    }


def _columns(conn: sqlite3.Connection, table: str) -> str:
    present = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    exprs = []
    for name, by_table in _COLUMNS:
        expr = by_table.get(table, name)
        bare = expr.replace("COALESCE(", "").replace(")", "").split(",")
        exprs.append(expr if all(part in present for part in bare) else "NULL")
    return ",".join(exprs)


def fee_schedule(conn: sqlite3.Connection) -> _Schedule:
    """The ledger's series_fees as a fee lookup (Kalshi's general schedule otherwise)."""
    return _Schedule(_fee_schedule(conn))


def _fee_schedule(conn: sqlite3.Connection) -> dict[str, tuple[str, Decimal]]:
    try:
        return {
            str(series): (str(fee_type), Decimal(str(multiplier)))
            for series, fee_type, multiplier in conn.execute(
                "SELECT series_ticker,fee_type,fee_multiplier FROM series_fees"
            )
        }
    except sqlite3.OperationalError:
        return {}


class _Schedule:
    """series_fees from the ledger; Kalshi's general schedule for a series it lacks."""

    def __init__(self, known: dict[str, tuple[str, Decimal]]) -> None:
        self.known = known

    def get(self, series_ticker: str) -> tuple[str, Decimal]:
        return self.known.get(series_ticker) or DEFAULT_FEE_SCHEDULE.get(series_ticker)


def fee_cents(ticker: str, price: Decimal, style: FeeStyle, schedule: _Schedule) -> float | None:
    """Unrounded per-contract fee at `price`, in cents."""
    try:
        return float(
            fee_for_ticker(ticker, price, Decimal(1), style, schedule=schedule, rounding=None) * 100
        )
    except ValueError:
        return None


def _rows(
    conn: sqlite3.Connection, cohort: Cohort, since: str | None, schedule: _Schedule
) -> list[dict[str, Any]]:
    sql = f"SELECT {_columns(conn, cohort.table)} FROM {cohort.table} WHERE {cohort.where}"
    rows = []
    names = [name for name, _ in _COLUMNS]
    for raw in conn.execute(sql):
        record = dict(zip(names, raw, strict=True))
        created_at = record["created_at"]
        if since and str(created_at) < since:
            continue
        ticker = str(record["ticker"])
        price = _decimal(record["price"])
        window = record["window"]
        if not window:
            try:
                commence = record["commence"]
                window = decision_window(
                    _utc(str(created_at)), _utc(str(commence)) if commence else None
                )
            except ValueError:
                window = "unknown"
        mid = _decimal(record["mid"])
        fair = _decimal(record["fair"])
        model_side = record["model_side"]
        if model_side is None and mid is not None and fair is not None:
            model_side = 1 if fair > mid else 0
        metrics = {metric: _decimal(record[metric]) for metric in METRICS}
        clv_mid = metrics["clv_kalshi"]
        rows.append(
            {
                "window": str(window),
                "price_band": price_band(price) if price is not None else "unknown",
                "sport": ticker.split("-", 1)[0],
                "edge_bucket": edge_bucket(_decimal(record["net_edge"])),
                "pull_source": str(record["pull_source"]) if record["pull_source"] else "untagged",
                "event": event_of(ticker),
                "position_key": record["position_key"],
                "model_side": None if model_side is None else int(model_side),
                "edge_vs_mid": (
                    float((fair - mid) * 100) if fair is not None and mid is not None else None
                ),
                # Mid-to-mid move to T-5: clv_kalshi is (mid at T-5 - entry price).
                "move": (
                    float(clv_mid) - float((mid - price) * 100)
                    if clv_mid is not None and mid is not None and price is not None
                    else None
                ),
                "fee": (
                    fee_cents(ticker, price, cohort.fee_style, schedule)
                    if price is not None
                    else None
                ),
                **metrics,
            }
        )
    return rows


def _ordered_groups(key: str, present: set[str]) -> list[str]:
    order: tuple[str, ...] = {
        "window": WINDOWS,
        "price_band": PRICE_BANDS,
        "edge_bucket": EDGE_BUCKETS,
    }.get(key, ())
    return [g for g in order if g in present] + sorted(present - set(order))


def dose_bucket(edge_vs_mid: float) -> str:
    for label, upper in DOSE_BUCKETS:
        if upper is None or edge_vs_mid < upper:
            return label
    return DOSE_BUCKETS[-1][0]


def dose_response(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """(fair - mid) buckets in cents: rows, games, mean mid-to-mid move to T-5, gross and
    net clv vs the Kalshi mid. Rows of both sides, so the slope is the evidence."""
    out = []
    for label, _ in DOSE_BUCKETS:
        group = [
            r
            for r in rows
            if r.get("edge_vs_mid") is not None and dose_bucket(float(r["edge_vs_mid"])) == label
        ]
        summary = summarize_rows(group)
        out.append(
            {
                "bucket": label,
                "n": summary["n"],
                "games": summary["games"],
                "mean_move": summary["mean_move"],
                "mean": summary["mean"],
                "mean_net": summary["mean_net"],
                "ci95": summary["ci95"],
                "verdict": summary["verdict"],
            }
        )
    return out


# Price-band results on the sharp close depend on the devig method (power lifts
# favourites, Shin lifts longshots), so they are shown under each and judged by none.
SHARP_METRICS = ("clv_sharp_close", "clv_sharp_close_shin", "clv_sharp_close_mult")
PER_METHOD = "per-method"


def _breakdowns(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for metric in METRICS:
        breakdown: dict[str, Any] = {"all": summarize_rows(rows, metric)}
        for key in keys:
            present = {str(r[key]) for r in rows if r[metric] is not None}
            breakdown[f"by_{key}"] = {
                group: summarize_rows([r for r in rows if str(r[key]) == group], metric)
                for group in _ordered_groups(key, present)
            }
            if key == "price_band" and metric in SHARP_METRICS:
                for summary in breakdown[f"by_{key}"].values():
                    summary["verdict"] = PER_METHOD
        metrics[metric] = breakdown
    return metrics


def clv_report(
    conn: sqlite3.Connection, since: str | None = None, by_source: bool = False
) -> dict[str, Any]:
    """CLV by cohort. `by_source` adds a breakdown by why the odds pull ran
    (`pull_source`: release, kickoff, close, default, grid, rotation, move, measure;
    `untagged` for rows written before Phase 3-E).

    Near-miss cohorts (`favoured_headline`) report the favoured side (model_side = 1) as
    their headline, the all-rows mean labelled as the half-spread artefact it is, and a
    dose-response table over (fair - mid)."""
    schedule = _Schedule(_fee_schedule(conn))
    cohorts: dict[str, Any] = {}
    keys = ("window", "price_band", "sport", "edge_bucket") + (
        ("pull_source",) if by_source else ()
    )
    for name, cohort in COHORTS.items():
        rows = _rows(conn, cohort, since, schedule)
        entry: dict[str, Any] = {
            "label": cohort.label,
            "fee_style": cohort.fee_style,
            "rows": len(rows),
            "positions": len({r["position_key"] or r["event"] for r in rows}),
            "games": len({r["event"] for r in rows}),
        }
        if cohort.favoured_headline:
            favoured = [r for r in rows if r["model_side"] == 1]
            entry.update(
                {
                    "headline": "favoured side only (model_side = 1: fair > side mid)",
                    "favoured_rows": len(favoured),
                    "unknown_side_rows": sum(1 for r in rows if r["model_side"] is None),
                    "metrics": _breakdowns(favoured, keys),
                    "all_rows": {
                        "label": ALL_ROWS_LABEL,
                        **summarize_rows(rows),
                    },
                    "dose_response": dose_response(rows),
                }
            )
        else:
            entry["metrics"] = _breakdowns(rows, keys)
        cohorts[name] = entry
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since,
        "rule": {
            "min_games": MIN_GAMES,
            "verdict": VERDICT_RULE,
            "supported": f"games >= {MIN_GAMES} and clustered 95% CI of net clv lower bound > 0",
            "rejected": f"games >= {MIN_GAMES} and clustered 95% CI of net clv upper bound < 0",
            "open": "otherwise",
            "beat_close": "share of rows with gross CLV > 0",
            "units": "cents per contract",
            "status_filter": "clv_status='ok'",
            "clusters": "games (ticker without its outcome segment)",
            "sharp_price_bands": "sharp-close price bands are shown under power, Shin and "
            "multiplicative devig with no verdict (per-method)",
        },
        "metrics": METRICS,
        "cohorts": cohorts,
    }


def _fmt(value: float | None, digits: int = 2) -> str:
    return "–" if value is None else f"{value:+.{digits}f}"


def _ci(ci: Sequence[float] | None) -> str:
    return "–" if ci is None else f"[{ci[0]:+.2f},{ci[1]:+.2f}]"


_HEADER = (
    f"    {'group':<22}{'rows':>6}{'pos':>5}{'games':>6}{'gross':>8}{'fee':>6}{'net':>8}"
    f"{'net 95% CI (clustered)':>24}{'beat':>6}  verdict"
)


def _line(group: str, s: dict[str, Any]) -> str:
    beat = "–" if s["beat_close"] is None else f"{s['beat_close']:.0%}"
    fee = "–" if s["fee"] is None else f"{s['fee']:.2f}"
    return (
        f"    {group:<22}{s['n']:>6}{s['positions']:>5}{s['games']:>6}{_fmt(s['mean']):>8}"
        f"{fee:>6}{_fmt(s['mean_net']):>8}{_ci(s['ci95']):>24}{beat:>6}  {s['verdict']}"
    )


def format_report(report: dict[str, Any]) -> str:
    lines = [f"Verdict rule: {report['rule']['verdict']}", ""]
    since = report.get("since")
    for name, cohort in report["cohorts"].items():
        lines.append(
            f"== {cohort['label']} [{name}]"
            + (f" since {since}" if since else "")
            + f": {cohort['rows']} rows, {cohort['positions']} positions, "
            f"{cohort['games']} games"
        )
        if "headline" in cohort:
            lines.append(
                f"  headline: {cohort['headline']} ({cohort['favoured_rows']} rows; "
                f"{cohort['unknown_side_rows']} rows with no mid are left out)"
            )
            all_rows = cohort["all_rows"]
            lines.append(f"  all rows, clv_kalshi ({all_rows['label']}):")
            lines.append(_HEADER)
            lines.append(_line("all rows", all_rows))
            lines.append(
                "  dose-response, (fair - mid) in cents, both sides, vs Kalshi mid at T-5:"
            )
            lines.append(
                f"    {'fair-mid':<10}{'rows':>6}{'games':>6}{'move':>8}{'gross':>8}{'net':>8}"
                f"{'net 95% CI':>20}  verdict"
            )
            for b in cohort["dose_response"]:
                lines.append(
                    f"    {b['bucket']:<10}{b['n']:>6}{b['games']:>6}{_fmt(b['mean_move']):>8}"
                    f"{_fmt(b['mean']):>8}{_fmt(b['mean_net']):>8}{_ci(b['ci95']):>20}  "
                    f"{b['verdict']}"
                )
        for metric, description in report["metrics"].items():
            breakdown = cohort["metrics"][metric]
            if breakdown["all"]["n"] == 0:
                lines.append(f"  {metric} ({description}): no scored rows")
                continue
            lines.append(f"  {metric} ({description}), cents")
            lines.append(_HEADER)
            groups = [("all", breakdown["all"])] + [
                (f"{key.removeprefix('by_')}={group}", summary)
                for key, by in breakdown.items()
                if key != "all"
                for group, summary in by.items()
            ]
            for group, s in groups:
                lines.append(_line(group, s))
        lines.append("")
    return "\n".join(lines)
