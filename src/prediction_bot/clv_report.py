"""CLV evidence report (v5 Phase 0.2): cohort summaries with CIs and Buchdahl verdicts.

Every numeric column in the ledger is stored as TEXT, so values are parsed in Python;
never compare them in SQL without a CAST (string comparison silently mis-buckets).
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from prediction_bot.paper.ledger import decision_window

MIN_EVIDENCE_N = 50
Z95 = 1.96

# Metric column -> what it measures. All in cents per contract.
METRICS: dict[str, str] = {
    "clv_kalshi": "Kalshi side mid at T-5 minus entry",
    "clv_kalshi_bid": "Kalshi side bid at T-5 minus entry",
    "clv_sharp_close": "devigged sharp-book close minus entry",
}

# Cohort -> (label, SQL returning created_at, ticker, price, net_edge, window, commence,
# clv_kalshi, clv_kalshi_bid, clv_sharp_close). Only `clv_status='ok'` rows count.
_SHADOW_COLUMNS = (
    "created_at,ticker,price,net_edge,window,"
    "COALESCE(commence_time,commence_time_backfill),"
    "clv_kalshi,clv_kalshi_bid,clv_sharp_close"
)
COHORTS: dict[str, tuple[str, str]] = {
    "fills": (
        "Paper fills (all rules versions)",
        "SELECT created_at,ticker,price,net_edge,NULL,"
        "COALESCE(commence_time,commence_time_backfill),"
        "clv_kalshi,clv_kalshi_bid,clv_sharp_close FROM fills WHERE clv_status='ok'",
    ),
    "v3_net_edge": (
        "v3 near misses blocked by net_edge",
        f"SELECT {_SHADOW_COLUMNS} FROM shadow_decisions WHERE rules_version='v3' "
        "AND would_trade=0 AND reason='net_edge' AND clv_status='ok'",
    ),
    "v3_entry_window": (
        "v3 near misses blocked by entry_window",
        f"SELECT {_SHADOW_COLUMNS} FROM shadow_decisions WHERE rules_version='v3' "
        "AND would_trade=0 AND reason='entry_window' AND clv_status='ok'",
    ),
    "v3_price_floor": (
        "v3 near misses blocked by price_floor",
        f"SELECT {_SHADOW_COLUMNS} FROM shadow_decisions WHERE rules_version='v3' "
        "AND would_trade=0 AND reason='price_floor' AND clv_status='ok'",
    ),
    "v3_thin_market": (
        "v3 near misses blocked by thin_market",
        f"SELECT {_SHADOW_COLUMNS} FROM shadow_decisions WHERE rules_version='v3' "
        "AND would_trade=0 AND reason='thin_market' AND clv_status='ok'",
    ),
    "line_move_follower": (
        "v2 line_move_follower shadows",
        f"SELECT {_SHADOW_COLUMNS} FROM shadow_decisions WHERE strategy='line_move_follower' "
        "AND would_trade=1 AND clv_status='ok'",
    ),
}

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


def _rows(conn: sqlite3.Connection, sql: str, since: str | None) -> list[dict[str, Any]]:
    rows = []
    for (
        created_at,
        ticker,
        raw_price,
        raw_edge,
        stored_window,
        raw_commence,
        *metric_values,
    ) in conn.execute(sql):
        if since and str(created_at) < since:
            continue
        price = _decimal(raw_price)
        window = stored_window
        if not window:
            try:
                window = decision_window(
                    _utc(str(created_at)), _utc(str(raw_commence)) if raw_commence else None
                )
            except ValueError:
                window = "unknown"
        rows.append(
            {
                "window": str(window),
                "price_band": price_band(price) if price is not None else "unknown",
                "sport": str(ticker).split("-", 1)[0],
                "edge_bucket": edge_bucket(_decimal(raw_edge)),
                **{
                    metric: _decimal(value)
                    for metric, value in zip(METRICS, metric_values, strict=True)
                },
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


def _values(
    rows: list[dict[str, Any]], metric: str, key: str | None = None, group: str | None = None
) -> list[float]:
    return [
        float(r[metric])
        for r in rows
        if r[metric] is not None and (key is None or str(r[key]) == group)
    ]


def clv_report(conn: sqlite3.Connection, since: str | None = None) -> dict[str, Any]:
    cohorts: dict[str, Any] = {}
    for name, (label, sql) in COHORTS.items():
        rows = _rows(conn, sql, since)
        metrics: dict[str, Any] = {}
        for metric in METRICS:
            breakdown: dict[str, Any] = {"all": summarize(_values(rows, metric))}
            for key in ("window", "price_band", "sport", "edge_bucket"):
                present = {str(r[key]) for r in rows if r[metric] is not None}
                breakdown[f"by_{key}"] = {
                    group: summarize(_values(rows, metric, key, group))
                    for group in _ordered_groups(key, present)
                }
            metrics[metric] = breakdown
        cohorts[name] = {"label": label, "rows": len(rows), "metrics": metrics}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since,
        "rule": {
            "min_n": MIN_EVIDENCE_N,
            "supported": "n >= 50 and 95% CI lower bound > 0",
            "rejected": "n >= 50 and 95% CI upper bound < 0",
            "open": "otherwise",
            "beat_close": "share of rows with CLV > 0",
            "units": "cents per contract",
            "status_filter": "clv_status='ok'",
        },
        "metrics": METRICS,
        "cohorts": cohorts,
    }


def _fmt(value: float | None, digits: int = 2) -> str:
    return "–" if value is None else f"{value:+.{digits}f}"


def format_report(report: dict[str, Any]) -> str:
    lines = []
    since = report.get("since")
    for name, cohort in report["cohorts"].items():
        lines.append(f"== {cohort['label']} [{name}]" + (f" since {since}" if since else ""))
        for metric, description in report["metrics"].items():
            breakdown = cohort["metrics"][metric]
            if breakdown["all"]["n"] == 0:
                lines.append(f"  {metric} ({description}): no scored rows")
                continue
            lines.append(f"  {metric} ({description}), cents")
            lines.append(
                f"    {'group':<22}{'n':>5}{'mean':>8}{'sd':>7}{'se':>7}"
                f"{'95% CI':>18}{'beat':>6}  verdict"
            )
            groups = [("all", breakdown["all"])] + [
                (f"{key.removeprefix('by_')}={group}", summary)
                for key, by in breakdown.items()
                if key != "all"
                for group, summary in by.items()
            ]
            for group, s in groups:
                ci = s["ci95"]
                ci_text = "–" if ci is None else f"[{ci[0]:+.2f},{ci[1]:+.2f}]"
                sd = "–" if s["sd"] is None else f"{s['sd']:.2f}"
                se = "–" if s["se"] is None else f"{s['se']:.2f}"
                beat = "–" if s["beat_close"] is None else f"{s['beat_close']:.0%}"
                lines.append(
                    f"    {group:<22}{s['n']:>5}{_fmt(s['mean']):>8}{sd:>7}{se:>7}"
                    f"{ci_text:>18}{beat:>6}  {s['verdict']}"
                )
        lines.append("")
    return "\n".join(lines)
