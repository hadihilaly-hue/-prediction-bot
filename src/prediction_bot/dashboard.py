"""Self-contained HTML dashboard rendered from the paper ledger.

No external assets: data is embedded as JSON and drawn with vanilla JS/SVG so the
page works from GitHub Pages, a file:// URL, or a phone with no build step.
"""

from __future__ import annotations

import html
import json
import random
import statistics
from datetime import datetime, timezone
from decimal import Decimal
from typing import cast

from prediction_bot.analysis import Fill, band, monte_carlo, outcome_key
from prediction_bot.paper.ledger import Ledger
from prediction_bot.teams import team_name_for_code


def _num(d: Decimal | None) -> float | None:
    return None if d is None else float(d)


def _mean_ci95(values: list[float]) -> tuple[float, float] | None:
    if len(values) < 5:
        return None
    mean = statistics.mean(values)
    margin = 1.96 * statistics.stdev(values) / (len(values) ** 0.5)
    return mean - margin, mean + margin


def _measurement_data(ledger: Ledger) -> dict[str, object]:
    rows = ledger.conn.execute(
        "SELECT f.id,f.created_at,f.strategy,f.ticker,f.side,f.price,f.count,f.fee,"
        "f.fair_prob,s.result,f.rules_version,f.position_key,f.event_ticker,f.tags,"
        "f.commence_time,f.clv_kalshi,f.clv_gameday,f.line_move_since_entry,f.clv_exit FROM fills f "
        "LEFT JOIN settlements s ON s.ticker=f.ticker ORDER BY f.id"
    ).fetchall()
    fills: list[tuple[Fill, str, str | None, str | None, str | None, str | None]] = []
    groups: dict[
        tuple[str, tuple[str, str]],
        list[tuple[Fill, str | None, str | None, str | None, str | None, str | None]],
    ] = {}
    exit_clv_by_id = {int(row[0]): str(row[18]) for row in rows if row[18] is not None}
    for row in rows:
        ts = datetime.fromisoformat(row[1])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        fill = Fill(
            int(row[0]),
            ts,
            str(row[2]),
            str(row[3]),
            str(row[4]),
            float(row[5]),
            float(row[6]),
            float(row[7]),
            float(row[8]),
            row[9],
        )
        version = str(row[10] or "v1")
        key = outcome_key(fill) if "-" in fill.ticker else (fill.event, f"outcome:{fill.side}")
        clv = None if row[15] is None else str(row[15])
        gameday = None if row[16] is None else str(row[16])
        commence = None if row[14] is None else str(row[14])
        tags = None if row[13] is None else str(row[13])
        line_move = None if row[17] is None else str(row[17])
        fills.append((fill, version, clv, gameday, commence, tags))
        groups.setdefault((version, key), []).append(
            (fill, clv, gameday, commence, line_move, tags)
        )

    positions: list[dict[str, object]] = []
    for (version, key), legs in groups.items():
        count = sum(fill.count for fill, *_ in legs)
        avg_price = sum(fill.price * fill.count for fill, *_ in legs) / count
        avg_fair = sum(fill.fair * fill.count for fill, *_ in legs) / count
        settled = [fill for fill, *_ in legs if fill.won is not None]
        clvs = [(float(clv), fill.count) for fill, clv, *_ in legs if clv is not None]
        day_clvs = [(float(day), fill.count) for fill, _, day, *_ in legs if day is not None]
        line_moves = [
            (float(move), fill.count) for fill, _, _, _, move, _ in legs if move is not None
        ]
        exit_clvs = [
            (float(exit_clv_by_id[fill.id]), fill.count)
            for fill, *_ in legs
            if fill.id in exit_clv_by_id
        ]
        commence = next((row[3] for row in legs if row[3]), None)
        created = min(fill.created_at for fill, *_ in legs)
        window = "unknown"
        if commence:
            start = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            hours = (start - created).total_seconds() / 3600
            window = (
                "72h+"
                if hours >= 72
                else "24-72h"
                if hours >= 24
                else "2-24h"
                if hours >= 2
                else "<2h"
            )
        pos_clv = sum(value * n for value, n in clvs) / sum(n for _, n in clvs) if clvs else None
        pos_day = (
            sum(value * n for value, n in day_clvs) / sum(n for _, n in day_clvs)
            if day_clvs
            else None
        )
        outcome_name = team_name_for_code(key[0].split("-", 1)[0], key[1])
        if key[1].startswith("not "):
            excluded_name = team_name_for_code(key[0].split("-", 1)[0], key[1][4:])
            summary = (
                f"Any outcome except {excluded_name} in {key[0]}"
                if excluded_name
                else f"{key[1]} in {key[0]}"
            )
        else:
            summary = (
                f"{outcome_name} win in {key[0]}"
                if outcome_name
                else f"{key[1]} outcome in {key[0]}"
            )
        strategy_tags = {fill.strategy for fill, *_ in legs}
        strategy_tags.update(
            tag.strip()
            for _, _, _, _, _, tags in legs
            if tags
            for tag in tags.split(",")
            if tag.strip()
        )
        position: dict[str, object] = {
            "version": version,
            "summary": summary,
            "event": key[0],
            "outcome": key[1],
            "contracts": count,
            "price": avg_price,
            "fair": avg_fair,
            "result": "open" if not settled else ("W" if settled[0].won else "L"),
            "pnl": sum(fill.pnl or 0.0 for fill in settled),
            "strategy_tags": ", ".join(sorted(strategy_tags)),
            "clv": pos_clv,
            "clv_gameday": pos_day,
            "clv_exit": (
                sum(value * n for value, n in exit_clvs) / sum(n for _, n in exit_clvs)
                if exit_clvs
                else None
            ),
            "line_move": (
                sum(value * n for value, n in line_moves) / sum(n for _, n in line_moves)
                if line_moves
                else None
            ),
            "league": key[0].split("-", 1)[0],
            "window": window,
            "price_band": band(avg_price),
            "n_fills": len(legs),
        }
        positions.append(position)

    settled_fills = [fill for fill, *_ in fills if fill.won is not None]
    settled_positions = [position for position in positions if position["result"] != "open"]
    brier_fill = (
        sum((f.fair - (1.0 if f.won else 0.0)) ** 2 for f in settled_fills) / len(settled_fills)
        if settled_fills
        else None
    )
    brier_pos = (
        sum(
            (float(cast(float, p["fair"])) - (1.0 if p["result"] == "W" else 0.0)) ** 2
            for p in settled_positions
        )
        / len(settled_positions)
        if settled_positions
        else None
    )
    rng = random.Random(7)

    def brier_interval(items: list[tuple[str, float]]) -> list[float] | None:
        by_event: dict[str, list[float]] = {}
        for event, score in items:
            by_event.setdefault(event, []).append(score)
        keys = list(by_event)
        if not keys:
            return None
        resamples = []
        for _ in range(2000):
            values = [score for _ in keys for score in by_event[rng.choice(keys)]]
            resamples.append(sum(values) / len(values))
        resamples.sort()
        return [resamples[49], resamples[1949]]

    fill_scores = [
        (fill.event, (fill.fair - (1.0 if fill.won else 0.0)) ** 2) for fill in settled_fills
    ]
    fill_market_scores = [
        (fill.event, (fill.price - (1.0 if fill.won else 0.0)) ** 2) for fill in settled_fills
    ]
    position_scores = [
        (
            str(position["event"]),
            (float(cast(float, position["fair"])) - (1.0 if position["result"] == "W" else 0.0))
            ** 2,
        )
        for position in settled_positions
    ]
    position_market_scores = [
        (
            str(position["event"]),
            (float(cast(float, position["price"])) - (1.0 if position["result"] == "W" else 0.0))
            ** 2,
        )
        for position in settled_positions
    ]
    pnl_values = [float(cast(float, p["pnl"])) for p in settled_positions]
    top_three = sum(sorted(pnl_values, reverse=True)[:3])
    clv_groups: dict[str, dict[str, object]] = {}
    for dimension in ("league", "window", "price_band"):
        buckets: dict[str, list[float]] = {}
        for position in positions:
            if position["clv"] is not None:
                buckets.setdefault(str(position[dimension]), []).append(
                    float(cast(float, position["clv"]))
                )
        clv_groups[dimension] = {
            name: {
                "n": len(values),
                "mean": statistics.mean(values),
                "median": statistics.median(values),
                "histogram": {
                    "<-10c": sum(value < -10 for value in values),
                    "-10..-5c": sum(-10 <= value < -5 for value in values),
                    "-5..0c": sum(-5 <= value < 0 for value in values),
                    "0..5c": sum(0 <= value < 5 for value in values),
                    "5..10c": sum(5 <= value < 10 for value in values),
                    "10c+": sum(value >= 10 for value in values),
                },
                "too_few_to_be_evidence": len(values) < 50,
                "evidence_note": "too few to be evidence" if len(values) < 50 else None,
            }
            for name, values in buckets.items()
        }
    calibration: dict[str, dict[str, float]] = {}
    for position in settled_positions:
        lo = int(float(cast(float, position["fair"])) * 10) * 10
        bucket = f"{lo}-{lo + 9}%"
        entry = calibration.setdefault(bucket, {"n": 0, "wins": 0, "expected_wins": 0})
        entry["n"] += 1
        entry["wins"] += int(position["result"] == "W")
        entry["expected_wins"] += float(cast(float, position["fair"]))
    for entry in calibration.values():
        entry["observed_win_rate"] = entry["wins"] / entry["n"]
        entry["predicted_win_rate"] = entry["expected_wins"] / entry["n"]
    blocks = ledger.conn.execute(
        "SELECT reason,COUNT(*) FROM blocks GROUP BY reason ORDER BY COUNT(*) DESC"
    ).fetchall()
    recent_cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
    recent_blocks = []
    for reason, count in blocks:
        recent = ledger.conn.execute(
            "SELECT COUNT(*) FROM blocks WHERE reason=? AND created_at >= ?",
            (reason, datetime.fromtimestamp(recent_cutoff, timezone.utc).isoformat()),
        ).fetchone()[0]
        recent_blocks.append({"reason": reason, "7d": recent, "all": count})
    latest_run = ledger.conn.execute(
        "SELECT odds_used,odds_remaining FROM runs WHERE odds_used IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    shadow = ledger.conn.execute(
        "SELECT COALESCE(rules_version,'v1'),window,COUNT(*),SUM(would_trade) "
        "FROM shadow_decisions GROUP BY 1,2 ORDER BY 1,2"
    ).fetchall()
    maker = ledger.conn.execute(
        "SELECT status,COUNT(*) FROM maker_shadow GROUP BY status ORDER BY status"
    ).fetchall()
    maker_fills = ledger.conn.execute(
        "SELECT m.side,m.price,m.size,m.maker_fee,m.clv_kalshi,s.result "
        "FROM maker_shadow m LEFT JOIN settlements s ON s.ticker=m.ticker "
        "WHERE m.status='filled'"
    ).fetchall()
    maker_settled = [
        (side, Decimal(price), Decimal(size), Decimal(fee), result)
        for side, price, size, fee, _clv, result in maker_fills
        if result is not None
    ]
    maker_clvs = [float(clv) for _s, _p, _n, _f, clv, _r in maker_fills if clv is not None]
    maker_pnl = sum(
        (size if result == side else Decimal(0)) - price * size - fee
        for side, price, size, fee, result in maker_settled
    )
    maker_metrics = ledger.maker_shadow_metrics()

    def maker_metric_float(name: str) -> float | None:
        value = maker_metrics[name]
        return None if value is None else float(value)

    quality = {
        "blocks": recent_blocks,
        "edge_reviews": ledger.conn.execute("SELECT COUNT(*) FROM edge_reviews").fetchone()[0],
        "odds_used": None if latest_run is None else latest_run[0],
        "odds_remaining": None if latest_run is None else latest_run[1],
        "maker_shadow": {str(status): count for status, count in maker},
        "maker_shadow_pnl": {
            "n": len(maker_settled),
            "value": float(maker_pnl),
            "unit": "USD",
            "too_few_to_be_evidence": len(maker_settled) < 50,
            "evidence_note": "too few to be evidence" if len(maker_settled) < 50 else None,
        },
        "maker_shadow_clv": {
            "n": len(maker_clvs),
            "mean": statistics.mean(maker_clvs) if maker_clvs else None,
            "median": statistics.median(maker_clvs) if maker_clvs else None,
            "unit": "cents",
            "too_few_to_be_evidence": len(maker_clvs) < 50,
            "evidence_note": "too few to be evidence" if len(maker_clvs) < 50 else None,
        },
        "maker_shadow_scoring": {
            rule: {
                "scored": maker_metrics[f"{rule}_scored"],
                "filled": maker_metrics[f"{rule}_filled"],
                "fill_rate": maker_metric_float(f"{rule}_fill_rate"),
                "settled": maker_metrics[f"{rule}_settled"],
                "pnl": maker_metric_float(f"{rule}_pnl"),
                "clv_count": maker_metrics[f"{rule}_clv_count"],
                "mean_clv": maker_metric_float(f"{rule}_mean_clv"),
                "exit_clv_count": maker_metrics[f"{rule}_exit_clv_count"],
                "mean_exit_clv": maker_metric_float(f"{rule}_mean_exit_clv"),
            }
            for rule in ("trade_through", "optimistic")
        },
        "depth_limited_all_time": sum(
            int(row["all"]) for row in recent_blocks if "depth" in str(row["reason"])
        ),
        "mapping_failures_all_time": sum(
            int(row["all"]) for row in recent_blocks if "mapping" in str(row["reason"])
        ),
        "shadow_decisions": [
            {"version": v, "window": w, "n": n, "would_trade": traded} for v, w, n, traded in shadow
        ],
    }
    quality["entry_window_clv"] = {
        version: {
            window: _entry_window_clv_summary(ledger, version, window)
            for window in ("72h+", "24-72h", "2-24h")
        }
        for version in ("v3", "v2")
    }
    road_dog_positions = [
        position for position in positions if "exp_road_dog" in str(position["strategy_tags"])
    ]
    road_dog_settled = [position for position in road_dog_positions if position["result"] != "open"]
    road_dog_clv = [
        float(cast(float, position["clv"]))
        for position in road_dog_positions
        if position["clv"] is not None
    ]
    quality["exp_road_dog"] = {
        "positions": len(road_dog_positions),
        "settled": len(road_dog_settled),
        "pnl": sum(float(cast(float, position["pnl"])) for position in road_dog_settled),
        "clv_n": len(road_dog_clv),
        "clv_mean": statistics.mean(road_dog_clv) if road_dog_clv else None,
        "evaluation_note": "Evaluate at 50 and 100 positions; rankings and neutral-site data are absent.",
    }
    line_move = ledger.conn.execute(
        "SELECT COUNT(*),SUM(would_trade),COUNT(clv_kalshi),AVG(CAST(clv_kalshi AS REAL)) "
        "FROM shadow_decisions WHERE strategy='line_move_follower'"
    ).fetchone()
    quality["line_move_follower"] = {
        "observations": int(line_move[0] or 0),
        "would_trade": int(line_move[1] or 0),
        "settled_clv_n": int(line_move[2] or 0),
        "settled_clv_mean": None if line_move[3] is None else float(line_move[3]),
        "note": "No trading until at least 50 observations.",
    }
    monte_carlo_result = None
    if settled_fills:
        p_market, mean_market = monte_carlo(
            settled_fills, "price", sum(f.pnl or 0 for f in settled_fills), 20_000, rng
        )
        p_fair, mean_fair = monte_carlo(
            settled_fills, "fair", sum(f.pnl or 0 for f in settled_fills), 20_000, rng
        )
        monte_carlo_result = {
            "runs": 20_000,
            "market": {"p_ge_realized": p_market, "mean_pnl": mean_market},
            "bot_fair": {"p_ge_realized": p_fair, "mean_pnl": mean_fair},
        }

    by_version: dict[str, dict[str, object]] = {}
    for version in ("v3", "v2", "v1"):
        version_positions = [p for p in positions if p["version"] == version]
        version_fills = [fill for fill, v, *_ in fills if v == version]
        version_settled_fills = [fill for fill in version_fills if fill.won is not None]
        version_settled_positions = [p for p in version_positions if p["result"] != "open"]
        version_fill_scores = [
            (fill.event, (fill.fair - (1.0 if fill.won else 0.0)) ** 2)
            for fill in version_settled_fills
        ]
        version_fill_market_scores = [
            (fill.event, (fill.price - (1.0 if fill.won else 0.0)) ** 2)
            for fill in version_settled_fills
        ]
        version_position_scores = [
            (
                str(position["event"]),
                (float(cast(float, position["fair"])) - (1.0 if position["result"] == "W" else 0.0))
                ** 2,
            )
            for position in version_settled_positions
        ]
        version_position_market_scores = [
            (
                str(position["event"]),
                (
                    float(cast(float, position["price"]))
                    - (1.0 if position["result"] == "W" else 0.0)
                )
                ** 2,
            )
            for position in version_settled_positions
        ]
        version_clv: dict[str, dict[str, object]] = {}
        for dimension in ("league", "window", "price_band"):
            version_buckets: dict[str, list[float]] = {}
            for position in version_positions:
                if position["clv"] is not None:
                    version_buckets.setdefault(str(position[dimension]), []).append(
                        float(cast(float, position["clv"]))
                    )
            version_clv[dimension] = {
                name: {
                    "n": len(values),
                    "mean": statistics.mean(values),
                    "median": statistics.median(values),
                    "histogram": {
                        "<-10c": sum(value < -10 for value in values),
                        "-10..-5c": sum(-10 <= value < -5 for value in values),
                        "-5..0c": sum(-5 <= value < 0 for value in values),
                        "0..5c": sum(0 <= value < 5 for value in values),
                        "5..10c": sum(5 <= value < 10 for value in values),
                        "10c+": sum(value >= 10 for value in values),
                    },
                    "evidence_note": "too few to be evidence" if len(values) < 50 else None,
                }
                for name, values in version_buckets.items()
            }
        version_calibration: dict[str, dict[str, float]] = {}
        for position in version_settled_positions:
            lo = int(float(cast(float, position["fair"])) * 10) * 10
            bucket = f"{lo}-{lo + 9}%"
            entry = version_calibration.setdefault(bucket, {"n": 0, "wins": 0, "expected_wins": 0})
            entry["n"] += 1
            entry["wins"] += int(position["result"] == "W")
            entry["expected_wins"] += float(cast(float, position["fair"]))
        for entry in version_calibration.values():
            entry["observed_win_rate"] = entry["wins"] / entry["n"]
            entry["predicted_win_rate"] = entry["expected_wins"] / entry["n"]
        version_monte_carlo = None
        if version_settled_fills:
            realized = sum(fill.pnl or 0 for fill in version_settled_fills)
            p_market, mean_market = monte_carlo(
                version_settled_fills, "price", realized, 20_000, rng
            )
            p_fair, mean_fair = monte_carlo(version_settled_fills, "fair", realized, 20_000, rng)
            version_monte_carlo = {
                "runs": 20_000,
                "market": {"p_ge_realized": p_market, "mean_pnl": mean_market},
                "bot_fair": {"p_ge_realized": p_fair, "mean_pnl": mean_fair},
            }
        version_pnls = [float(cast(float, p["pnl"])) for p in version_settled_positions]
        version_top_three = sum(sorted(version_pnls, reverse=True)[:3])
        by_version[version] = {
            "top_three_pnl": version_top_three,
            "other_pnl": sum(version_pnls) - version_top_three,
            "clv": version_clv,
            "calibration": version_calibration,
            "brier_fills": {
                "n": len(version_settled_fills),
                "value": (
                    sum(score for _, score in version_fill_scores) / len(version_fill_scores)
                    if version_fill_scores
                    else None
                ),
                "ci95": brier_interval(version_fill_scores),
                "market_value": (
                    sum(score for _, score in version_fill_market_scores)
                    / len(version_fill_market_scores)
                    if version_fill_market_scores
                    else None
                ),
                "market_ci95": brier_interval(version_fill_market_scores),
            },
            "brier_positions": {
                "n": len(version_settled_positions),
                "value": (
                    sum(score for _, score in version_position_scores)
                    / len(version_position_scores)
                    if version_position_scores
                    else None
                ),
                "ci95": brier_interval(version_position_scores),
                "market_value": (
                    sum(score for _, score in version_position_market_scores)
                    / len(version_position_market_scores)
                    if version_position_market_scores
                    else None
                ),
                "market_ci95": brier_interval(version_position_market_scores),
            },
            "monte_carlo": version_monte_carlo,
        }
    return {
        "positions": positions,
        "top_three_pnl": top_three,
        "other_pnl": sum(pnl_values) - top_three,
        "clv": clv_groups,
        "calibration": calibration,
        "brier_fills": {
            "n": len(settled_fills),
            "value": brier_fill,
            "ci95": brier_interval(fill_scores),
            "market_value": (
                sum(score for _, score in fill_market_scores) / len(fill_market_scores)
                if fill_market_scores
                else None
            ),
            "market_ci95": brier_interval(fill_market_scores),
        },
        "brier_positions": {
            "n": len(settled_positions),
            "value": brier_pos,
            "ci95": brier_interval(position_scores),
            "market_value": (
                sum(score for _, score in position_market_scores) / len(position_market_scores)
                if position_market_scores
                else None
            ),
            "market_ci95": brier_interval(position_market_scores),
        },
        "monte_carlo": monte_carlo_result,
        "quality": quality,
        "by_version": by_version,
    }


def _entry_window_clv_summary(ledger: Ledger, version: str, window: str) -> dict[str, object]:
    rows = ledger.conn.execute(
        "SELECT CAST(clv_kalshi AS REAL),CAST(clv_exit AS REAL) FROM shadow_decisions "
        "WHERE rules_version=? AND window=? AND reason='entry_window' AND clv_kalshi IS NOT NULL",
        (version, window),
    ).fetchall()
    clvs = [float(row[0]) for row in rows]
    exits = [float(row[1]) for row in rows if row[1] is not None]
    ci95 = _mean_ci95(clvs)
    return {
        "n": len(clvs),
        "mean_clv": statistics.mean(clvs) if clvs else None,
        "ci95": None if ci95 is None else [ci95[0], ci95[1]],
        "mean_clv_exit": statistics.mean(exits) if exits else None,
    }


def dashboard_data(ledger: Ledger) -> dict[str, object]:
    perf = ledger.performance()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "measurement": _measurement_data(ledger),
        "performance": {
            "cash": _num(perf.cash),
            "starting_cash": _num(perf.starting_cash),
            "open_positions": perf.open_positions,
            "open_cost": _num(perf.open_cost),
            "settled_count": perf.settled_count,
            "realized_pnl": _num(perf.realized_pnl),
            "fees_paid": _num(perf.fees_paid),
            "wins": perf.wins,
            "brier": _num(perf.brier_score),
            "market_brier": _num(perf.market_brier_score),
        },
        "by_strategy": [
            {
                "strategy": name,
                "settled_count": sp.settled_count,
                "wins": sp.wins,
                "realized_pnl": _num(sp.realized_pnl),
                "fees_paid": _num(sp.fees_paid),
                "open_positions": sp.open_positions,
                "brier": _num(sp.brier_score),
                "market_brier": _num(sp.market_brier_score),
            }
            for name, sp in ledger.performance_by_strategy().items()
        ],
        "by_league": [
            {
                "league": name,
                "settled_count": lp.settled_count,
                "wins": lp.wins,
                "realized_pnl": _num(lp.realized_pnl),
                "fees_paid": _num(lp.fees_paid),
                "open_positions": lp.open_positions,
                "brier": _num(lp.brier_score),
                "market_brier": _num(lp.market_brier_score),
            }
            for name, lp in ledger.performance_by_league().items()
        ],
        "predictions": [
            {
                "when": p.created_at,
                "strategy": p.strategy,
                "ticker": p.ticker,
                "summary": p.summary,
                "side": p.side.value,
                "price": _num(p.limit_price),
                "fair": _num(p.fair_prob),
                "edge": _num(p.edge),
                "size": _num(p.size),
                "traded": p.acted,
                "result": None if p.result is None else p.result.value,
                "won": p.won,
                "why": p.rationale,
            }
            for p in ledger.predictions()
        ],
        "trades": [
            {
                "when": t.created_at,
                "strategy": t.strategy,
                "ticker": t.ticker,
                "summary": t.summary,
                "side": t.side.value,
                "price": _num(t.price),
                "count": _num(t.count),
                "fee": _num(t.fee),
                "cost": _num(t.cost),
                "fair": _num(t.fair_prob),
                "result": None if t.result is None else t.result.value,
                "settled_at": t.settled_at,
                "pnl": _num(t.pnl),
            }
            for t in ledger.trades()
        ],
        "runs": [
            {
                "when": r.ran_at,
                "strategies": r.strategies,
                "scanned": r.scanned,
                "signals": r.signals,
                "filled": r.filled,
                "settled": r.settled,
                "cash": _num(r.cash),
                "equity": _num(r.cash + r.open_cost),
            }
            for r in ledger.runs()
        ],
    }


def render_dashboard(ledger: Ledger) -> str:
    payload = json.dumps(dashboard_data(ledger), separators=(",", ":"))
    # A closing </script> inside market text must not terminate the data block.
    payload = payload.replace("</", "<\\/")
    return _TEMPLATE.replace("__DATA__", payload).replace(
        "__TITLE__", html.escape("Paper-trading dashboard")
    )


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { --bg:#f4f5f7; --card:#fff; --fg:#1d2330; --muted:#667085; --line:#e4e7ec;
          --good:#12875b; --goodbg:#e7f6ef; --bad:#c4321f; --badbg:#fdecea;
          --warn:#a15c00; --warnbg:#fff4e0; --accent:#2f5fd0; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
         background:var(--bg); color:var(--fg); }
  header { background:#1d2330; color:#fff; padding:20px 24px; }
  header h1 { margin:0; font-size:22px; }
  header .sub { color:#c5cad3; font-size:13px; margin-top:4px; }
  .badge { display:inline-block; background:#fff3; border-radius:999px; padding:2px 10px;
           font-size:12px; margin-left:8px; vertical-align:middle; }
  main { max-width:1180px; margin:0 auto; padding:20px 16px 40px; display:grid; gap:20px; }
  section.block { background:var(--card); border:1px solid var(--line); border-radius:14px;
                  padding:20px; }
  section.block > h2 { margin:0 0 4px; font-size:18px; }
  .hint { color:var(--muted); font-size:13px; margin:0 0 14px; }
  .story { font-size:17px; margin:0 0 16px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px; }
  .stat { border:1px solid var(--line); border-radius:12px; padding:14px; }
  .stat .k { color:var(--muted); font-size:13px; }
  .stat .v { font-size:26px; font-weight:700; margin:2px 0; }
  .stat .d { color:var(--muted); font-size:13px; }
  .good { color:var(--good); } .bad { color:var(--bad); } .muted { color:var(--muted); }
  .verdicts { display:grid; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:12px; }
  .verdict { border-radius:12px; padding:14px; border:1px solid var(--line); }
  .verdict.good { background:var(--goodbg); border-color:#bfe6d3; color:var(--fg); }
  .verdict.bad { background:var(--badbg); border-color:#f5c6bf; color:var(--fg); }
  .verdict.warn { background:var(--warnbg); border-color:#f3d9a8; color:var(--fg); }
  .verdict h3 { margin:0 0 6px; font-size:15px; }
  .verdict .big { font-size:20px; font-weight:700; }
  .verdict p { margin:6px 0 0; font-size:13px; color:#3d4555; }
  .evidence-head { margin:18px 0 4px; font-size:16px; }
  .meter { display:grid; grid-template-columns:minmax(150px,220px) 1fr auto; gap:10px; align-items:center; margin:6px 0; font-size:13px; }
  .meter .bar { height:10px; border-radius:999px; background:var(--line); overflow:hidden; }
  .meter .bar span { display:block; height:100%; background:var(--good); }
  .tabs { display:flex; gap:8px; flex-wrap:wrap; margin:0 0 14px; }
  .tabs button { border:1px solid var(--line); background:#fff; border-radius:999px;
                 padding:6px 14px; cursor:pointer; font:inherit; font-size:14px; }
  .tabs button.on { background:var(--accent); color:#fff; border-color:var(--accent); }
  .note { background:var(--warnbg); border-radius:10px; padding:8px 12px; font-size:13px;
          margin:0 0 12px; }
  .tablewrap { overflow:auto; border:1px solid var(--line); border-radius:10px; }
  table { border-collapse:collapse; width:100%; }
  th, td { padding:9px 12px; text-align:left; border-bottom:1px solid var(--line);
           font-variant-numeric:tabular-nums; vertical-align:top; }
  th { background:#f9fafb; color:var(--muted); font-size:13px; font-weight:600; }
  tr:last-child td { border-bottom:0; }
  td.num, th.num { text-align:right; white-space:nowrap; }
  td.what { font-weight:600; min-width:240px; }
  td.why { color:var(--muted); font-size:13px; min-width:220px; }
  caption { caption-side:top; text-align:left; padding:10px 12px; color:var(--muted);
            font-size:13px; }
  .pill { display:inline-block; padding:1px 10px; border-radius:999px; font-size:12px;
          font-weight:600; white-space:nowrap; }
  .pill.won { background:var(--goodbg); color:var(--good); }
  .pill.lost { background:var(--badbg); color:var(--bad); }
  .pill.open { background:#eef2fb; color:var(--accent); }
  .pill.skip { background:#f2f4f7; color:var(--muted); }
  .empty { padding:18px; text-align:center; color:var(--muted); }
  .two { display:grid; grid-template-columns:repeat(auto-fit,minmax(460px,1fr)); gap:20px; }
  svg { width:100%; height:200px; display:block; }
  details { border:1px solid var(--line); border-radius:12px; padding:0 16px; margin-top:12px; }
  details > summary { cursor:pointer; padding:12px 0; font-weight:600; }
  details .tablewrap { margin-bottom:14px; }
  .filter { width:100%; max-width:360px; padding:8px 12px; border:1px solid var(--line);
            border-radius:8px; font:inherit; margin-bottom:10px; }
  footer { text-align:center; color:var(--muted); font-size:12px; padding:0 0 30px; }
</style>
</head>
<body>
<header>
  <h1>Kalshi bot <span class="badge">paper money only</span></h1>
  <div class="sub">No real orders are ever placed. <span id="generated"></span></div>
</header>
<main>
  <section class="block">
    <h2>Right now</h2>
    <p class="story" id="story"></p>
    <div class="cards" id="cards"></div>
  </section>

  <section class="block">
    <h2>Is it actually working?</h2>
    <p class="hint">Profit alone can be luck. These checks ask whether the bot is better than Kalshi's own prices.</p>
    <div class="tabs" id="rules-tabs">
      <button data-rules="v3">Newest rules (v3, trading now)</button>
      <button data-rules="v2">v2 rules (Oct 23 on, now in shadow)</button>
      <button data-rules="v1">Old rules (v1, before Oct 23)</button>
    </div>
    <div class="note" id="rules-note" hidden></div>
    <div class="verdicts" id="verdicts"></div>
    <h3 class="evidence-head">How much evidence is there?</h3>
    <p class="hint">Closing-price gain (CLV) is the main evidence: it shows up long before profit stops being luck. A result counts once 50 bets have it and the whole 95% range is above zero.</p>
    <div id="evidence"></div>
  </section>

  <section class="block">
    <h2>Open bets</h2>
    <p class="hint">Bets waiting for the game to finish. "Pays" is what you get back if the bet wins.</p>
    <div class="tablewrap"><table id="open"></table></div>
  </section>

  <div class="two">
    <section class="block">
      <h2>Recent results</h2>
      <p class="hint">The latest bets that have finished.</p>
      <div class="tablewrap"><table id="recent"></table></div>
    </section>
    <section class="block">
      <h2>Account value over time</h2>
      <p class="hint">Cash plus what open bets cost. The dashed line is the starting balance.</p>
      <svg id="equity"></svg>
      <p class="hint" id="last-run"></p>
    </section>
  </div>

  <section class="block">
    <h2>Details</h2>
    <p class="hint">Everything behind the numbers above. Click a heading to open it.</p>
    <details>
      <summary>Every bet, grouped by game outcome</summary>
      <div class="tablewrap"><table id="positions"></table></div>
    </details>
    <details>
      <summary>Accuracy, calibration and the luck test</summary>
      <div class="tablewrap"><table id="calibration"></table></div>
      <div class="tablewrap"><table id="brier"></table></div>
      <div class="tablewrap"><table id="montecarlo"></table></div>
      <div class="tablewrap"><table id="concentration"></table></div>
    </details>
    <details>
      <summary>Closing-line value (did the price move our way before kickoff?)</summary>
      <div id="clv"></div>
    </details>
    <details>
      <summary>Why bets were skipped, and data quality</summary>
      <div class="tablewrap"><table id="blocks"></table></div>
      <div class="tablewrap"><table id="quality"></table></div>
    </details>
    <details>
      <summary>Shadow tests (tracked, never traded)</summary>
      <div class="tablewrap"><table id="maker"></table></div>
      <div class="tablewrap"><table id="shadow"></table></div>
      <div class="tablewrap"><table id="experiments"></table></div>
    </details>
    <details>
      <summary>Results by league</summary>
      <div class="tablewrap"><table id="byleague"></table></div>
    </details>
    <details>
      <summary>Everything the bot considered</summary>
      <input class="filter" id="q" placeholder="Search team, league or ticker">
      <div class="tablewrap"><table id="predictions"></table></div>
    </details>
  </section>
</main>
<footer>Paper-trading dashboard. Times are shown in your local time zone.</footer>
<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  const D = JSON.parse(document.getElementById('data').textContent);
  const M = D.measurement || {};
  const P = D.performance || {};
  const Q = M.quality || {};
  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const num = x => typeof x === 'number' && isFinite(x);
  const money = (x, signed) => {
    if (!num(x)) return '–';
    const s = '$' + Math.abs(x).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    if (x < 0) return '−' + s;
    return signed && x > 0 ? '+' + s : s;
  };
  const cents = x => num(x) ? Math.round(x * 100) + '¢' : '–';
  const pct = x => num(x) ? Math.round(x * 100) + '%' : '–';
  const signedCents = x => num(x) ? (x > 0 ? '+' : '') + x.toFixed(1) + '¢' : '–';
  const tone = x => num(x) ? (x > 0 ? 'good' : x < 0 ? 'bad' : '') : 'muted';
  const when = s => {
    if (!s) return '–';
    const iso = /[zZ]|[+-][0-9][0-9]:[0-9][0-9]$/.test(s) ? s : s + 'Z';
    const d = new Date(iso);
    return isNaN(d) ? s : d.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
  };
  const league = t => {
    const s = String(t || '').split('-')[0].replace('GAME', '').replace(/^KX/, '');
    const names = {NFL: 'NFL', NCAAF: 'College football', MLB: 'MLB', NBA: 'NBA', NHL: 'NHL', MLS: 'MLS', EPL: 'Premier League', LALIGA: 'La Liga', UCL: 'Champions League', WNBA: 'WNBA', NCAAB: 'College basketball', SERIEA: 'Serie A', BUNDESLIGA: 'Bundesliga', LIGUE1: 'Ligue 1', UFCFIGHT: 'UFC'};
    return names[s] || s;
  };
  const table = (id, caption, heads, rows, emptyText) => {
    const numeric = heads.map(h => h.charAt(0) === '#');
    const head = '<tr>' + heads.map((h, i) => '<th class="' + (numeric[i] ? 'num' : '') + '">' + esc(numeric[i] ? h.slice(1) : h) + '</th>').join('') + '</tr>';
    const body = rows.length ? rows.map(r => '<tr>' + r.map((c, i) => {
      const cell = typeof c === 'object' && c !== null ? c : {v: c};
      const cls = [numeric[i] ? 'num' : '', cell.cls || ''].join(' ').trim();
      return '<td class="' + cls + '">' + (cell.html != null ? cell.html : esc(cell.v)) + '</td>';
    }).join('') + '</tr>').join('') : '<tr><td class="empty" colspan="' + heads.length + '">' + esc(emptyText || 'Nothing here yet.') + '</td></tr>';
    $(id).innerHTML = (caption ? '<caption>' + esc(caption) + '</caption>' : '') + '<thead>' + head + '</thead><tbody>' + body + '</tbody>';
  };
  const pill = (cls, text) => ({html: '<span class="pill ' + cls + '">' + esc(text) + '</span>'});

  $('generated').textContent = 'Updated ' + when(D.generated_at) + '.';

  const trades = D.trades || [];
  const openTrades = trades.filter(t => t.pnl == null);
  const settled = trades.filter(t => t.pnl != null);
  const positions = M.positions || [];
  const settledPositions = positions.filter(p => p.result === 'W' || p.result === 'L');
  const wonPositions = settledPositions.filter(p => p.result === 'W').length;
  const equity = (P.cash || 0) + (P.open_cost || 0);
  const change = equity - (P.starting_cash || 0);
  const openGames = new Set(openTrades.map(t => t.ticker.split('-').slice(0, 2).join('-'))).size;
  const openBets = new Set(openTrades.map(t => t.ticker + '|' + t.side)).size;

  $('story').innerHTML = 'The bot started with <b>' + money(P.starting_cash) + '</b> of paper money and is now worth <b>' + money(equity) + '</b> (' +
    '<span class="' + tone(change) + '">' + money(change, true) + '</span>). ' +
    'It has finished <b>' + settledPositions.length + '</b> bets and won <b>' + wonPositions + '</b>, and has <b>' + openBets + '</b> bets open on ' + openGames + ' games. ' +
    'Remember: a handful of big wins can make the total look better than the bot really is. See the checks below.';

  const cards = [
    ['Account value', money(equity), money(change, true) + ' since start', tone(change)],
    ['Profit from finished bets', money(P.realized_pnl, true), 'after ' + money(P.fees_paid) + ' in Kalshi fees', tone(P.realized_pnl)],
    ['Bets won', wonPositions + ' of ' + settledPositions.length, settledPositions.length ? pct(wonPositions / settledPositions.length) + ' win rate' : 'none finished yet', ''],
    ['Open bets', String(openBets), money(P.open_cost) + ' at stake', ''],
    ['Cash available', money(P.cash), 'not tied up in bets', '']
  ];
  $('cards').innerHTML = cards.map(c => '<div class="stat"><div class="k">' + esc(c[0]) + '</div><div class="v ' + c[3] + '">' + esc(c[1]) + '</div><div class="d">' + esc(c[2]) + '</div></div>').join('');

  const versions = M.by_version || {};
  const hasSettled = v => positions.some(p => p.version === v && (p.result === 'W' || p.result === 'L'));
  let rules = hasSettled('v3') ? 'v3' : hasSettled('v2') ? 'v2' : 'v1';

  const verdict = (cls, title, big, text) => '<div class="verdict ' + cls + '"><h3>' + esc(title) + '</h3><div class="big">' + esc(big) + '</div><p>' + esc(text) + '</p></div>';

  function clvSummary(V) {
    const groups = (V.clv || {}).league || {};
    let n = 0; let total = 0;
    Object.values(groups).forEach(g => {
      const mean = num(g.mean) ? g.mean : g.avg;
      if (num(g.n) && num(mean)) { n += g.n; total += g.n * mean; }
    });
    return {n: n, mean: n ? total / n : null};
  }

  function renderVerdicts() {
    document.querySelectorAll('#rules-tabs button').forEach(b => b.classList.toggle('on', b.dataset.rules === rules));
    const V = versions[rules] || {};
    const mine = positions.filter(p => p.version === rules);
    const done = mine.filter(p => p.result === 'W' || p.result === 'L');
    const note = $('rules-note');
    if ((rules === 'v2' || rules === 'v3') && !done.length) {
      note.hidden = false;
      note.textContent = (rules === 'v3' ? 'The v3 rules' : 'The v2 rules') + ' have no finished bets yet. These checks fill in as games end; they need about 50 finished bets to mean much.';
    } else { note.hidden = true; }
    const out = [];
    const mc = V.monte_carlo;
    if (mc && mc.market && num(mc.market.p_ge_realized)) {
      const p = mc.market.p_ge_realized;
      out.push(verdict(p < 0.05 ? 'good' : 'warn', 'Luck test', pct(p) + ' chance it was luck',
        'If Kalshi prices were exactly right, random game results would still give a profit this big ' + pct(p) + ' of the time. Under 5% would suggest real skill.'));
    } else {
      out.push(verdict('', 'Luck test', 'Not enough bets yet', 'Runs once some bets have finished.'));
    }
    const b = V.brier_positions || {};
    if (num(b.value) && num(b.market_value)) {
      const better = b.value < b.market_value;
      const overlap = b.ci95 && b.market_ci95 && b.ci95[0] <= b.market_ci95[1] && b.market_ci95[0] <= b.ci95[1];
      out.push(verdict(overlap ? 'warn' : better ? 'good' : 'bad', 'Better predictions than Kalshi?',
        overlap ? 'No clear difference' : better ? 'Yes' : 'No',
        'Brier score (lower = better forecasts): bot ' + b.value.toFixed(3) + ' vs Kalshi ' + b.market_value.toFixed(3) + ' over ' + b.n + ' bets.' + (overlap ? ' The gap is within the noise.' : '')));
    } else {
      out.push(verdict('', 'Better predictions than Kalshi?', 'Not enough bets yet', "Compares the bot's odds against Kalshi prices once games finish."));
    }
    if (done.length) {
      const top = V.top_three_pnl || 0; const rest = V.other_pnl || 0;
      out.push(verdict(rest >= 0 ? 'good' : 'bad', 'Without the 3 biggest wins', money(rest, true),
        'The 3 best outcomes made ' + money(top, true) + '. Everything else together made ' + money(rest, true) + '.'));
    }
    const c = clvSummary(V);
    if (c.n) {
      out.push(verdict(c.n < 50 ? 'warn' : c.mean > 0 ? 'good' : 'bad', 'Beating the closing price?', signedCents(c.mean) + ' per bet',
        'Average move in Kalshi price between buying and kickoff, over ' + c.n + ' bets. Positive means the market moved our way.' + (c.n < 50 ? ' Too few bets to be evidence yet (need 50).' : '')));
    } else {
      out.push(verdict('', 'Beating the closing price?', 'No data yet', 'Measured from Kalshi prices just before kickoff, after each game.'));
    }
    $('verdicts').innerHTML = out.join('');
    renderEvidence(mine, done);
    renderPositions();
    renderMeasurement(V);
  }
  function meanCi(values) {
    if (!values.length) return null;
    const m = values.reduce((a, b) => a + b, 0) / values.length;
    if (values.length < 2) return {n: values.length, mean: m, ci: null};
    const sd = Math.sqrt(values.reduce((a, b) => a + (b - m) * (b - m), 0) / (values.length - 1));
    const h = 1.96 * sd / Math.sqrt(values.length);
    return {n: values.length, mean: m, ci: [m - h, m + h]};
  }

  function evidenceVerdict(n, ci) {
    if (n < 50) return pill('', 'Not enough yet');
    if (ci && ci[0] > 0) return pill('won', 'Real edge');
    if (ci && ci[1] < 0) return pill('lost', 'Losing to the close');
    return pill('', 'No edge shown');
  }

  function renderEvidence(mine, done) {
    const withClv = mine.filter(p => num(p.clv));
    const meter = (label, n) => '<div class="meter"><div>' + esc(label) + '</div><div class="bar"><span style="width:' + Math.min(100, n / 50 * 100) + '%"></span></div><div><b>' + n + '</b> / 50</div></div>';
    const cents = meanCi(withClv.map(p => p.clv));
    const perDollar = meanCi(withClv.filter(p => num(p.price) && p.price > 0).map(p => p.clv / 100 / p.price));
    const range = (r, f) => r && r.ci ? f(r.ci[0]) + ' to ' + f(r.ci[1]) : '–';
    const per100 = x => (x >= 0 ? '+$' : '−$') + Math.abs(x * 100).toFixed(2);
    const rows = [
      ['Per contract (cents)', String(cents ? cents.n : 0), {v: cents ? signedCents(cents.mean) : '–', cls: cents ? tone(cents.mean) : ''}, range(cents, signedCents), cents ? evidenceVerdict(cents.n, cents.ci) : evidenceVerdict(0, null)],
      ['Per $100 bet', String(perDollar ? perDollar.n : 0), {v: perDollar ? per100(perDollar.mean) : '–', cls: perDollar ? tone(perDollar.mean) : ''}, range(perDollar, per100), perDollar ? evidenceVerdict(perDollar.n, perDollar.ci) : evidenceVerdict(0, null)]
    ];
    const windows = (Q.entry_window_clv || {})[rules] || {};
    const windowNames = {'72h+': 'Bought 3+ days before', '24-72h': 'Bought 1–3 days before', '2-24h': 'Bought on game day'};
    const windowRows = Object.keys(windowNames).filter(w => windows[w]).map(w => {
      const g = windows[w]; const n = g.n || 0;
      return [windowNames[w], String(n), {v: signedCents(g.mean_clv), cls: tone(g.mean_clv)}, g.ci95 ? signedCents(g.ci95[0]) + ' to ' + signedCents(g.ci95[1]) : '–', evidenceVerdict(n, g.ci95)];
    });
    $('evidence').innerHTML = meter('Bets with a closing price', withClv.length) + meter('Finished bets', done.length) +
      '<div class="tablewrap"><table id="expected"></table></div>' + (windowRows.length ? '<div class="tablewrap"><table id="windows"></table></div>' : '');
    table('expected', 'Expected return if the Kalshi closing price is the true chance, before fees on the way out.', ['Measure', '#Bets', '#Average', '95% range', 'Verdict'], rows, '');
    if (windowRows.length) table('windows', 'When to buy: closing-price gain by how far ahead of the game the bot would have bought (shadow test, no real trades).', ['Window', '#Bets', '#Average', '95% range', 'Verdict'], windowRows, '');
  }

  document.querySelectorAll('#rules-tabs button').forEach(b => b.addEventListener('click', () => { rules = b.dataset.rules; renderVerdicts(); }));

  const groupOpen = new Map();
  openTrades.forEach(t => {
    const k = t.ticker + '|' + t.side;
    const g = groupOpen.get(k) || {t: t, count: 0, cost: 0, last: t.when};
    g.count += t.count || 0; g.cost += t.cost || 0;
    if (t.when > g.last) g.last = t.when;
    groupOpen.set(k, g);
  });
  const openRows = [...groupOpen.values()].sort((a, b) => (b.last || '').localeCompare(a.last || '')).map(g => [
    {v: league(g.t.ticker)},
    {v: String(g.t.summary || '').split(' — ')[0] + (g.t.side === 'no' ? ' (betting NO)' : ''), cls: 'what'},
    {v: 'Kalshi ' + pct(g.t.price) + ' · bot ' + pct(g.t.fair)},
    {v: money(g.cost)},
    {v: money(g.count), cls: 'good'},
    {v: when(g.last)}
  ]);
  table('open', '', ['League', 'Bet', 'Chance of winning', '#Cost', '#Pays', 'Placed'], openRows, 'No open bets.');

  const recentRows = settled.slice().sort((a, b) => (b.settled_at || b.when || '').localeCompare(a.settled_at || a.when || '')).slice(0, 12).map(t => [
    t.pnl > 0 ? pill('won', 'Won') : pill('lost', 'Lost'),
    {v: String(t.summary || '').split(' — ')[0] + (t.side === 'no' ? ' (NO)' : ''), cls: 'what'},
    {v: money(t.pnl, true), cls: tone(t.pnl)}
  ]);
  table('recent', '', ['Result', 'Bet', '#Profit'], recentRows, 'No finished bets yet.');

  const runs = D.runs || [];
  (function drawEquity() {
    const svg = $('equity');
    const pts = runs.filter(r => num(r.equity));
    if (pts.length < 2) { svg.outerHTML = '<div class="empty">Not enough runs to chart yet.</div>'; return; }
    const W = 600, H = 200, pad = 34;
    const ys = pts.map(r => r.equity).concat([P.starting_cash || pts[0].equity]);
    const lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
    const x = i => pad + (W - pad - 8) * i / (pts.length - 1);
    const y = v => 10 + (H - 30) * (1 - (v - lo) / span);
    const line = pts.map((r, i) => x(i).toFixed(1) + ',' + y(r.equity).toFixed(1)).join(' ');
    const last = pts[pts.length - 1].equity;
    const color = last >= (P.starting_cash || 0) ? '#12875b' : '#c4321f';
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.innerHTML = '<line x1="' + pad + '" x2="' + (W - 8) + '" y1="' + y(P.starting_cash || 0) + '" y2="' + y(P.starting_cash || 0) + '" stroke="#98a2b3" stroke-dasharray="4 4"/>' +
      '<polyline fill="none" stroke="' + color + '" stroke-width="2.5" points="' + line + '"/>' +
      '<text x="2" y="16" font-size="11" fill="#667085">' + money(hi) + '</text>' +
      '<text x="2" y="' + (H - 18) + '" font-size="11" fill="#667085">' + money(lo) + '</text>' +
      '<text x="' + pad + '" y="' + (H - 2) + '" font-size="11" fill="#667085">' + esc(when(pts[0].when)) + '</text>' +
      '<text x="' + (W - 8) + '" y="' + (H - 2) + '" font-size="11" fill="#667085" text-anchor="end">' + esc(when(pts[pts.length - 1].when)) + '</text>';
    const r = runs[runs.length - 1];
    $('last-run').textContent = 'Last run ' + when(r.when) + ': looked at ' + r.scanned + ' markets, found ' + r.signals + ' possible bets, placed ' + r.filled + '.';
  })();

  function renderPositions() {
    const rows = positions.filter(p => p.version === rules).map(p => [
      p.result === 'W' ? pill('won', 'Won') : p.result === 'L' ? pill('lost', 'Lost') : pill('open', 'Open'),
      {v: String(p.summary || '').replace(/ in KX[A-Z0-9-]+$/, ''), cls: 'what'},
      {v: league(p.event)},
      {v: cents(p.price)},
      {v: pct(p.fair)},
      {v: String(Math.round(p.contracts || 0))},
      p.result === 'W' || p.result === 'L' ? {v: money(p.pnl, true), cls: tone(p.pnl)} : {v: '–'},
      {v: signedCents(p.clv)}
    ]);
    table('positions', 'One row per game outcome. Buying "Team A wins" and "Team B loses" counts as one bet.', ['Result', 'Bet', 'League', '#Avg price', '#Bot chance', '#Contracts', '#Profit', '#Price move to kickoff'], rows, 'No bets under these rules yet.');
  }

  function renderMeasurement(V) {
    const cal = Object.entries(V.calibration || {}).sort((a, b) => parseInt(a[0]) - parseInt(b[0])).map(([k, c]) => [
      k, String(c.n), (c.expected_wins || 0).toFixed(1), String(c.wins), pct(c.predicted_win_rate), pct(c.observed_win_rate)
    ]);
    table('calibration', 'Calibration by ten-cent bucket: when the bot says 30%, does it win about 30% of the time?', ['Bot chance', '#Bets', '#Expected wins', '#Actual wins', '#Predicted rate', '#Actual rate'], cal);
    const bRow = (label, b) => [label, String(b.n || 0), num(b.value) ? b.value.toFixed(4) : '–', b.ci95 ? b.ci95.map(x => x.toFixed(3)).join(' to ') : '–', num(b.market_value) ? b.market_value.toFixed(4) : '–', b.market_ci95 ? b.market_ci95.map(x => x.toFixed(3)).join(' to ') : '–'];
    table('brier', 'Brier scores use 2,000 game-bootstrap resamples for the 95% ranges. Lower is better.', ['Counted by', '#Bets', '#Bot Brier', 'Bot 95% CI', '#Market Brier', 'Market 95% CI'], [bRow('Each fill', V.brier_fills || {}), bRow('Each position', V.brier_positions || {})]);
    const mc = V.monte_carlo;
    const mcRows = mc ? [['Kalshi prices', mc.market], ['Bot fair values', mc.bot_fair]].filter(r => r[1]).map(r => [r[0], String(mc.runs), pct(r[1].p_ge_realized), money(r[1].mean_pnl, true)]) : [];
    table('montecarlo', 'Monte Carlo estimates the chance of a result at least this good if the given probabilities were true (games simulated together).', ['If the true odds were', '#Runs', '#P(≥ realized)', '#Mean simulated P&L'], mcRows);
    const total = (V.top_three_pnl || 0) + (V.other_pnl || 0);
    table('concentration', 'Settled position P&L with all outcomes, the top 3, and everything else.', ['Slice', '#P&L'], [['All outcomes', {v: money(total, true), cls: tone(total)}], ['Top 3 outcomes', {v: money(V.top_three_pnl, true), cls: tone(V.top_three_pnl)}], ['Excluding top 3', {v: money(V.other_pnl, true), cls: tone(V.other_pnl)}]]);
    const clv = V.clv || {};
    const parts = [['league', 'By league'], ['window', 'By how early we bought'], ['price_band', 'By price']];
    $('clv').innerHTML = parts.map(p => '<div class="tablewrap"><table id="clv-' + p[0] + '"></table></div>').join('');
    parts.forEach(p => {
      const rows = Object.entries(clv[p[0]] || {}).map(([k, g]) => [p[0] === 'league' ? league(k) : k, String(g.n || 0), signedCents(num(g.mean) ? g.mean : g.avg), signedCents(g.median), g.too_few_to_be_evidence || (g.n || 0) < 50 ? 'too few to be evidence' : '']);
      table('clv-' + p[0], p[1] + ': Kalshi price at kickoff minus our price, in cents.', ['Group', '#Bets', '#Average', '#Median', 'Note'], rows, 'No closing prices recorded yet.');
    });
  }

  const blockNames = {net_edge: 'Edge too small after fees', price_floor: 'Price under 10¢', price_ceiling: 'Price over 90¢', longshot_edge: 'Longshot without enough edge', big_edge: 'Edge suspiciously large', duplicate_position: 'Already have this bet', mapping_failed: 'Team name not recognised', mapping_mismatch: 'Odds matched to the wrong team', entry_window: 'Too close to the game', wide_spread: 'Bid/ask spread too wide', min_books: 'Too few sportsbooks quoting', book_spread: 'Sportsbooks disagree'};
  const blockLabel = r => { const base = String(r).split(':')[0]; return (blockNames[r] || blockNames[base] || (base === 'in_play' ? 'Game already started' : r)); };
  table('blocks', 'Block reasons show the last seven days and all time.', ['Reason', 'Code', '#Last 7 days', '#All time'], (Q.blocks || []).map(b => [blockLabel(b.reason), {v: b.reason, cls: 'why'}, String(b['7d'] || 0), String(b.all || 0)]), 'No bets blocked yet.');
  table('quality', 'Data quality and Odds API usage.', ['Check', '#Value'], [
    ['Odds API credits used this month', Q.odds_used == null ? 'unknown' : String(Q.odds_used)],
    ['Odds API credits left', Q.odds_remaining == null ? 'unknown' : String(Q.odds_remaining)],
    ['Big edges sent for review', String(Q.edge_reviews || 0)],
    ['Bets cut down for thin order books (all time)', String(Q.depth_limited_all_time || 0)],
    ['Team-name mapping failures (all time)', String(Q.mapping_failures_all_time || 0)]
  ]);
  const mp = Q.maker_shadow_pnl || {}; const mcv = Q.maker_shadow_clv || {};
  const makerRows = Object.entries(Q.maker_shadow || {}).map(([k, v]) => [k, String(v)]);
  makerRows.push(['Settled shadow P&L (' + (mp.n || 0) + ' bets)', money(mp.value, true) + (mp.too_few_to_be_evidence ? ' · too few to be evidence' : '')]);
  makerRows.push(['Shadow price move to kickoff (' + (mcv.n || 0) + ' bets)', signedCents(mcv.mean)]);
  table('maker', 'Maker-shadow counts by status: pretend limit orders resting one cent better than the ask.', ['Status', '#Count'], makerRows);
  table('shadow', 'Shadow decisions by entry window: what each rule set would have bought, without trading.', ['Window', 'Rules', '#Decisions', '#Would trade'], (Q.shadow_decisions || []).map(s => [s.window, s.version, String(s.n), String(s.would_trade || 0)]));
  const rd = Q.exp_road_dog || {}; const lm = Q.line_move_follower || {};
  table('experiments', 'Line-move follower is shadow-only; no trading until at least 50 observations. Road-dog experiment evaluation at 50 and 100 positions; rankings and neutral-site data are absent.', ['Experiment', '#Count', '#Profit', '#Avg price move'], [
    ['Line-move follower (shadow)', String(lm.observations || 0) + ' seen, ' + String(lm.would_trade || 0) + ' would trade', '–', signedCents(lm.settled_clv_mean)],
    ['Road underdogs (small stakes)', String(rd.positions || 0) + ' bets', money(rd.pnl, true), signedCents(rd.clv_mean)]
  ]);
  table('byleague', 'Fill-level results by league, all rules versions.', ['League', '#Finished fills', '#Won', '#Profit', '#Fees', '#Open'], (D.by_league || []).map(l => [league(l.league), String(l.settled_count), String(l.wins), {v: money(l.realized_pnl, true), cls: tone(l.realized_pnl)}, money(l.fees_paid), String(l.open_positions)]));

  const preds = (D.predictions || []).slice().sort((a, b) => (b.when || '').localeCompare(a.when || ''));
  function renderPreds() {
    const q = $('q').value.trim().toLowerCase();
    const rows = preds.filter(p => !q || (p.summary + ' ' + p.ticker + ' ' + league(p.ticker)).toLowerCase().includes(q)).slice(0, 200).map(p => [
      {v: when(p.when)},
      p.traded ? pill('open', 'Bought') : pill('skip', 'Skipped'),
      {v: p.summary, cls: 'what'},
      {v: (num(p.edge) ? signedCents(p.edge * 100) : '–')},
      {v: p.why, cls: 'why'}
    ]);
    table('predictions', 'Newest first, up to 200 rows.', ['When', 'Action', 'What', '#Edge', 'Why'], rows, 'Nothing matches.');
  }
  $('q').addEventListener('input', renderPreds);
  renderPreds();
  renderVerdicts();
})();
</script>
</body>
</html>
"""
