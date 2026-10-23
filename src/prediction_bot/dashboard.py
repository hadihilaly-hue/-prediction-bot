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


def _measurement_data(ledger: Ledger) -> dict[str, object]:
    rows = ledger.conn.execute(
        "SELECT f.id,f.created_at,f.strategy,f.ticker,f.side,f.price,f.count,f.fee,"
        "f.fair_prob,s.result,f.rules_version,f.position_key,f.event_ticker,f.tags,"
        "f.commence_time,f.clv_kalshi,f.clv_gameday,f.line_move_since_entry FROM fills f "
        "LEFT JOIN settlements s ON s.ticker=f.ticker ORDER BY f.id"
    ).fetchall()
    fills: list[tuple[Fill, str, str | None, str | None, str | None, str | None]] = []
    groups: dict[
        tuple[str, tuple[str, str]],
        list[tuple[Fill, str | None, str | None, str | None, str | None, str | None]],
    ] = {}
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
        "SELECT odds_used,odds_remaining FROM runs ORDER BY id DESC LIMIT 1"
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
    for version in ("v2", "v1"):
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
  :root { --bg:#0f1115; --card:#181b22; --fg:#e6e6e6; --muted:#9aa0aa; --good:#3ddc84;
          --bad:#ff5d5d; --accent:#6ea8fe; --line:#2a2f3a; }
  * { box-sizing: border-box; }
  body { margin:0; font: 14px/1.4 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         background:var(--bg); color:var(--fg); }
  header { padding:16px 20px; border-bottom:1px solid var(--line); display:flex;
           flex-wrap:wrap; gap:8px 16px; align-items:baseline; }
  header h1 { font-size:18px; margin:0; }
  header .sub { color:var(--muted); font-size:12px; }
  main { padding:16px 20px; display:grid; gap:16px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; }
  .card .k { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.04em; }
  .card .v { font-size:20px; font-weight:600; margin-top:4px; }
  .good { color:var(--good); } .bad { color:var(--bad); } .muted { color:var(--muted); }
  .charts { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px; }
  .chart h2, section h2 { font-size:14px; margin:0 0 8px; color:var(--muted); font-weight:600; }
  svg { width:100%; height:180px; display:block; }
  .tabs { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
  .tabs button { background:var(--card); color:var(--fg); border:1px solid var(--line);
                 border-radius:8px; padding:6px 12px; cursor:pointer; }
  .tabs button.on { border-color:var(--accent); color:var(--accent); }
  .tabs input, .tabs select { background:var(--card); color:var(--fg); border:1px solid var(--line);
                              border-radius:8px; padding:6px 10px; }
  .tabs input { flex:1; min-width:160px; }
  .tablewrap { overflow:auto; border:1px solid var(--line); border-radius:10px; }
  table { border-collapse:collapse; width:100%; min-width:900px; }
  th, td { padding:8px 10px; text-align:left; border-bottom:1px solid var(--line);
           white-space:nowrap; font-variant-numeric:tabular-nums; }
  th { position:sticky; top:0; background:var(--card); color:var(--muted); font-weight:600;
       cursor:pointer; user-select:none; }
  table.measurement-table { min-width:600px; }
  .measurement-table caption { text-align:left; padding:10px; color:var(--muted);
                               white-space:normal; caption-side:top; }
  td.why { white-space:normal; min-width:260px; max-width:520px; color:var(--muted); }
  td.summary { white-space:normal; min-width:280px; max-width:420px; font-weight:600; }
  tr:hover td { background:#1d2129; }
  .pill { display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px;
          border:1px solid var(--line); }
  .pill.won { color:var(--good); border-color:var(--good); }
  .pill.lost { color:var(--bad); border-color:var(--bad); }
  .empty { padding:24px; text-align:center; color:var(--muted); }
  .clv-hist { width:180px; height:44px; vertical-align:middle; }
  .clv-note { color:var(--muted); font-size:12px; }
  footer { padding:12px 20px 24px; color:var(--muted); font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>Paper-trading dashboard</h1>
  <span class="sub" id="generated"></span>
<span class="sub">Paper mode only &mdash; no real orders are placed.</span>
</header>
<main>
  <section class="card">
    <h2>Positions and measurement</h2>
    <div class="tabs" style="margin-bottom:8px">
      <button data-rules="v2">v2</button>
      <button data-rules="v1">v1 historical</button>
    </div>
    <div class="tablewrap"><table id="positions"></table></div>
    <div id="clv-summary"></div>
    <div id="measurement-summary"></div>
  </section>
  <section class="card">
    <h2>Ledger-wide overview (all rules versions)</h2>
    <div class="cards" id="cards"></div>
  </section>
  <section class="card" style="margin-bottom:16px">
    <h2>Legacy fill-level per league (all rules versions)</h2>
    <div class="tablewrap"><table id="byleague"></table></div>
  </section>
  <div class="charts">
    <div class="card chart"><h2>Equity (cash + open cost)</h2><svg id="equity"></svg></div>
    <div class="card chart"><h2>Per-run activity (signals / fills)</h2><svg id="activity"></svg></div>
  </div>
  <section>
    <div class="tabs" style="margin-bottom:8px">
      <button class="on" data-tab="predictions">Predictions</button>
      <button data-tab="trades">Trades</button>
      <button data-tab="runs">Runs</button>
      <input id="q" placeholder="filter: ticker, strategy, text…">
      <select id="status">
        <option value="">all</option>
        <option value="open">open</option>
        <option value="won">won</option>
        <option value="lost">lost</option>
        <option value="traded">traded only</option>
      </select>
    </div>
    <div class="tablewrap"><table id="table"></table></div>
  </section>
</main>
<footer>
  <b>price</b> is what we (paper) pay for <b>side</b>; <b>fair</b> is the strategy's probability
  that side wins; <b>edge</b> = fair &minus; price &minus; fees. <b>Brier</b>: lower is better;
  the strategy has skill only if its Brier beats the market price's.
</footer>
<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  const D = JSON.parse(document.getElementById('data').textContent);
  const P = D.performance;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = (x) => x == null ? '-' : (x < 0 ? '-' : '') + '$' + Math.abs(x).toFixed(2);
  const f4 = (x) => x == null ? '-' : x.toFixed(4);
  const when = (s) => s ? s.slice(0, 16).replace('T', ' ') : '-';
  const sign = (x) => x == null ? '' : x > 0 ? 'good' : x < 0 ? 'bad' : '';

  $('generated').textContent = 'Updated ' + when(D.generated_at) + ' UTC';

  const cards = [
    ['Cash', money(P.cash), ''],
    ['Realized PnL', money(P.realized_pnl), sign(P.realized_pnl)],
    ['Fees paid', money(P.fees_paid), ''],
    ['Open positions', P.open_positions + ' (' + money(P.open_cost) + ')', ''],
    ['Settled / wins', P.settled_count + ' / ' + P.wins, ''],
    ['Brier strategy vs market', f4(P.brier) + ' vs ' + f4(P.market_brier),
      P.brier == null ? 'muted' : (P.brier < P.market_brier ? 'good' : 'bad')],
    ['Signals', D.predictions.length + ' (' + D.predictions.filter(p => p.traded).length + ' traded)', ''],
    ['Runs', D.runs.length, ''],
  ];
  $('cards').innerHTML = cards.map(([k, v, c]) =>
    '<div class="card"><div class="k">' + esc(k) + '</div><div class="v ' + c + '">' + esc(v) + '</div></div>').join('');

  function sliceTable(id, rows, key, empty) {
    $(id).innerHTML = '<thead><tr><th>' + key + '</th><th>open</th><th>settled</th><th>wins</th>' +
      '<th>realized PnL</th><th>fees</th><th>Brier vs market</th></tr></thead><tbody>' +
      (rows.length ? rows.map(s => '<tr><td>' + esc(s[key]) + '</td><td>' + s.open_positions + '</td><td>' +
        s.settled_count + '</td><td>' + s.wins + '</td><td class="' + sign(s.realized_pnl) + '">' + money(s.realized_pnl) +
        '</td><td>' + money(s.fees_paid) + '</td><td class="' + (s.brier == null ? 'muted' : (s.brier < s.market_brier ? 'good' : 'bad')) +
        '">' + f4(s.brier) + ' vs ' + f4(s.market_brier) + '</td></tr>').join('')
        : '<tr><td colspan="7" class="empty">' + empty + '</td></tr>') + '</tbody>';
  }
  sliceTable('byleague', D.by_league || [], 'league', 'no fills yet');

  function renderPositions(version) {
    const M = D.measurement || {};
    const V = (M.by_version || {})[version] || M;
    const rows = (M.positions || []).filter(p => p.version === version);
    $('positions').innerHTML = '<thead><tr><th>Summary</th><th>Outcome key</th><th>contracts</th>' +
      '<th>avg entry</th><th>fair</th><th>result</th><th>P&amp;L</th><th>CLV T-5</th>' +
      '<th>Odds move</th>' +
      '<th>game-day proxy CLV</th><th>strategy tags</th></tr></thead><tbody>' +
      (rows.length ? rows.map(p => '<tr><td class="summary">' + esc(p.summary) + '</td><td>' +
        esc(p.event + ':' + p.outcome) + '</td><td>' + p.contracts.toFixed(0) + '</td><td>' +
        (p.price * 100).toFixed(1) + '¢</td><td>' + p.fair.toFixed(3) + '</td><td>' +
        esc(p.result) + '</td><td>' + money(p.pnl) + '</td><td>' +
        (p.clv == null ? 'unknown' : p.clv.toFixed(1) + '¢') + '</td><td>' +
        (p.line_move == null ? 'unknown' : (p.line_move * 100).toFixed(1) + ' pp') + '</td><td>' +
        (p.clv_gameday == null ? 'unknown' : p.clv_gameday.toFixed(1) + '¢ (proxy)') +
        '</td><td>' + esc(p.strategy_tags) + '</td></tr>').join('') :
        '<tr><td colspan="11" class="empty">no positions in this rules-version view</td></tr>') +
      '</tbody>';
    const bf = V.brier_fills || {n: 0, value: null, ci95: null};
    const bp = V.brier_positions || {n: 0, value: null, ci95: null};
    const dimensions = V.clv || {};
    const bins = ['<-10c', '-10..-5c', '-5..0c', '0..5c', '5..10c', '10c+'];
    const clvRows = Object.entries(dimensions).flatMap(([dimension, groups]) =>
      Object.entries(groups).map(([group, stats]) => {
        const counts = bins.map(name => stats.histogram[name] || 0);
        const high = Math.max(1, ...counts);
        const bars = counts.map((count, index) => {
          const height = count ? Math.max(1, 34 * count / high) : 0;
          const x = index * 28 + 2;
          return '<rect x="' + x + '" y="' + (38 - height) + '" width="20" height="' +
            height + '" fill="#6ea8fe"><title>' + esc(bins[index] + ': ' + count) +
            '</title></rect>';
        }).join('');
        return '<tr><td>' + esc(dimension) + '</td><td>' + esc(group) + '</td><td>' +
          stats.n + '</td><td>' + stats.mean.toFixed(2) + '¢</td><td>' +
          stats.median.toFixed(2) + '¢</td><td><svg class="clv-hist" viewBox="0 0 172 42">' +
          bars + '</svg></td><td class="clv-note">' +
          esc(stats.evidence_note || '') + '</td></tr>';
      })).join('');
    $('clv-summary').innerHTML = '<h3>CLV by league, entry window, and price band</h3>' +
      '<div class="tablewrap"><table><thead><tr><th>grouping</th><th>bucket</th><th>n</th>' +
      '<th>mean</th><th>median</th><th>histogram (¢)</th><th>evidence</th></tr></thead><tbody>' +
      (clvRows || '<tr><td colspan="7" class="empty">no CLV quotes yet</td></tr>') +
      '</tbody></table></div>';
    function measurementTable(caption, headers, dataRows, emptyText = 'No data recorded yet.') {
      const head = '<thead><tr>' + headers.map(value => '<th>' + esc(value) + '</th>').join('') + '</tr></thead>';
      const body = dataRows.length ?
        dataRows.map(row => '<tr>' + row.map(value => '<td>' + esc(value == null ? '-' : value) +
          '</td>').join('') + '</tr>').join('') :
        '<tr><td colspan="' + headers.length + '" class="empty">' + esc(emptyText) + '</td></tr>';
      return '<div class="tablewrap"><table class="measurement-table"><caption>' +
        esc(caption) + '</caption>' + head + '<tbody>' + body + '</tbody></table></div>';
    }
    const ciText = ci => ci && ci.length === 2 ?
      f4(ci[0]) + ' to ' + f4(ci[1]) : 'not available';
    const brierRows = [
      ['Fills', bf.n, f4(bf.value), f4(bf.market_value), ciText(bf.ci95),
        ciText(bf.market_ci95),
        bf.n < 50 ? 'too few to be evidence' : ''],
      ['Positions', bp.n, f4(bp.value), f4(bp.market_value), ciText(bp.ci95),
        ciText(bp.market_ci95),
        bp.n < 50 ? 'too few to be evidence' : ''],
    ];
    const calibrationRows = Object.entries(V.calibration || {})
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([bucket, stats]) => [
        bucket, stats.n, Number(stats.expected_wins).toFixed(2), stats.wins,
      ]);
    const monteCarlo = V.monte_carlo;
    const monteCarloRows = monteCarlo ? [
      ['Market prices', f4(monteCarlo.market.p_ge_realized),
        money(monteCarlo.market.mean_pnl), monteCarlo.runs],
      ['Bot fair', f4(monteCarlo.bot_fair.p_ge_realized),
        money(monteCarlo.bot_fair.mean_pnl), monteCarlo.runs],
    ] : [];
    const topThree = V.top_three_pnl || 0;
    const otherPnl = V.other_pnl || 0;
    const q = M.quality || {};
    const qualityRows = (q.blocks || []).map(block => [
      block.reason, block['7d'], block.all,
    ]);
    if (!qualityRows.length) qualityRows.push(['No blocks', 0, 0]);
    qualityRows.push(
      ['Depth-limited', 'not tracked by day', q.depth_limited_all_time || 0],
      ['Edge reviews', 'not tracked by day', q.edge_reviews || 0],
      ['Mapping failures', 'not tracked by day', q.mapping_failures_all_time || 0],
      ['Odds credits used / remaining', 'not applicable',
        (q.odds_used == null ? 'unknown' : q.odds_used) + ' / ' +
          (q.odds_remaining == null ? 'unknown' : q.odds_remaining)],
    );
    const makerCounts = q.maker_shadow || {};
    const makerRows = ['open', 'filled', 'cancelled'].map(status => [
      status, makerCounts[status] || 0, '-', '-',
    ]);
    const makerPnl = q.maker_shadow_pnl || {};
    const makerClv = q.maker_shadow_clv || {};
    makerRows.push(
      ['Settled P&L', 'n=' + (makerPnl.n || 0), money(makerPnl.value), '-'],
      ['CLV mean / median', 'n=' + (makerClv.n || 0), '-',
        makerClv.mean == null ? 'unknown' :
          makerClv.mean.toFixed(1) + '¢ / ' + makerClv.median.toFixed(1) + '¢'],
    );
    const shadowRows = (q.shadow_decisions || []).map(decision => [
      decision.window, decision.version, decision.n, decision.would_trade,
    ]);
    const lineMove = q.line_move_follower || {};
    const roadDog = q.exp_road_dog || {};
    $('measurement-summary').innerHTML = [
      measurementTable(
        'Calibration by ten-cent bucket compares expected wins with actual wins.',
        ['Bucket', 'n', 'Expected wins', 'Actual wins'],
        calibrationRows,
        'No settled fills are available for calibration.',
      ),
      measurementTable(
        'Brier scores use 2,000 game-bootstrap resamples; samples under 50 are too few to be evidence.',
        ['Unit', 'n', 'Bot Brier', 'Market Brier', 'Bot 95% CI', 'Market 95% CI', 'Evidence'],
        brierRows,
      ),
      measurementTable(
        'Monte Carlo estimates the chance of matching realized P&L under market prices and bot fair.',
        ['Model', 'P(≥ realized)', 'Mean simulated P&L', 'Runs'],
        monteCarloRows,
        'No settled fills are available for simulation.',
      ),
      measurementTable(
        'Settled position P&L with all outcomes and after excluding the three largest outcomes.',
        ['View', 'Realized P&L'],
        [
          ['All settled outcomes', money(topThree + otherPnl)],
          ['Excluding top three', money(otherPnl)],
          ['Top three contribution', money(topThree)],
        ],
      ),
      measurementTable(
        'Block reasons show the last seven days and all-time totals; supporting quality checks are all-time or latest.',
        ['Reason or measure', 'Last 7 days', 'All time / latest'],
        qualityRows,
      ),
      measurementTable(
        'Maker-shadow counts by status, with settled P&L and available CLV.',
        ['Status or measure', 'Count / n', 'P&L', 'CLV'],
        makerRows,
      ),
      measurementTable(
        'Shadow decisions by entry window and rules version, including would-trade counts.',
        ['Entry window', 'Rules version', 'Decisions', 'Would trade'],
        shadowRows,
        'No shadow decisions have been recorded yet.',
      ),
      measurementTable(
        'Line-move follower is shadow-only; no trading until at least 50 observations.',
        ['Observations', 'Would trade', 'Settled CLV n', 'Mean settled CLV', 'Status'],
        [[lineMove.observations || 0, lineMove.would_trade || 0,
          lineMove.settled_clv_n || 0,
          lineMove.settled_clv_mean == null ? 'unknown' :
            lineMove.settled_clv_mean.toFixed(1) + '¢',
          lineMove.note || 'No trading until at least 50 observations.']],
      ),
      measurementTable(
        'Road-dog experiment evaluation at 50 and 100 positions; rankings and neutral-site data are absent.',
        ['Positions', 'Settled', 'Settled P&L', 'CLV n', 'Mean CLV', 'Evaluation note'],
        [[roadDog.positions || 0, roadDog.settled || 0, money(roadDog.pnl || 0),
          roadDog.clv_n || 0,
          roadDog.clv_mean == null ? 'unknown' : roadDog.clv_mean.toFixed(1) + '¢',
          roadDog.evaluation_note || 'Evaluate at 50 and 100 positions.']],
      ),
    ].join('');
  }
  const versionHasData = version =>
    (D.measurement.positions || []).some(position => position.version === version) ||
    (((D.measurement.by_version || {})[version] || {}).brier_fills || {}).n > 0;
  const initialVersion = versionHasData('v2') || !versionHasData('v1') ? 'v2' : 'v1';
  document.querySelectorAll('[data-rules]').forEach(b => {
    b.classList.toggle('on', b.dataset.rules === initialVersion);
    b.onclick = () => {
      document.querySelectorAll('[data-rules]').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      renderPositions(b.dataset.rules);
    };
  });
  renderPositions(initialVersion);

  function lineChart(svg, pts, opts) {
    const W = 600, H = 180, L = 48, R = 10, T = 10, B = 24;
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    if (pts.length < 1) { svg.innerHTML = '<text x="50%" y="50%" fill="#9aa0aa" text-anchor="middle">no runs yet</text>'; return; }
    let ys = pts.map(p => p.y); let lo = Math.min(...ys, opts.base ?? Infinity), hi = Math.max(...ys, opts.base ?? -Infinity);
    if (hi === lo) { hi += 1; lo -= 1; }
    const x = (i) => L + (pts.length === 1 ? (W - L - R) / 2 : i * (W - L - R) / (pts.length - 1));
    const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
    let out = '';
    for (let k = 0; k <= 4; k++) {
      const v = lo + (hi - lo) * k / 4, yy = y(v);
      out += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + yy + '" y2="' + yy + '" stroke="#2a2f3a"/>';
      out += '<text x="' + (L - 6) + '" y="' + (yy + 4) + '" fill="#9aa0aa" font-size="10" text-anchor="end">' + opts.fmt(v) + '</text>';
    }
    if (opts.base != null) out += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(opts.base) + '" y2="' + y(opts.base) + '" stroke="#6ea8fe" stroke-dasharray="4 4"/>';
    out += '<polyline fill="none" stroke="#3ddc84" stroke-width="2" points="' + pts.map((p, i) => x(i) + ',' + y(p.y)).join(' ') + '"/>';
    pts.forEach((p, i) => { out += '<circle cx="' + x(i) + '" cy="' + y(p.y) + '" r="3" fill="#3ddc84"><title>' + esc(p.label) + '</title></circle>'; });
    out += '<text x="' + L + '" y="' + (H - 6) + '" fill="#9aa0aa" font-size="10">' + esc(pts[0].t) + '</text>';
    out += '<text x="' + (W - R) + '" y="' + (H - 6) + '" fill="#9aa0aa" font-size="10" text-anchor="end">' + esc(pts[pts.length - 1].t) + '</text>';
    svg.innerHTML = out;
  }
  function barChart(svg, runs) {
    const W = 600, H = 180, L = 32, R = 10, T = 10, B = 24;
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    if (!runs.length) { svg.innerHTML = '<text x="50%" y="50%" fill="#9aa0aa" text-anchor="middle">no runs yet</text>'; return; }
    const hi = Math.max(1, ...runs.map(r => r.signals));
    const bw = (W - L - R) / runs.length, y = (v) => T + (H - T - B) * (1 - v / hi);
    let out = '<text x="' + (L - 6) + '" y="' + (T + 4) + '" fill="#9aa0aa" font-size="10" text-anchor="end">' + hi + '</text>';
    out += '<text x="' + (L - 6) + '" y="' + (H - B + 4) + '" fill="#9aa0aa" font-size="10" text-anchor="end">0</text>';
    runs.forEach((r, i) => {
      const x0 = L + i * bw + bw * 0.15, w = Math.max(1, bw * 0.7);
      out += '<rect x="' + x0 + '" y="' + y(r.signals) + '" width="' + w + '" height="' + (H - B - y(r.signals)) + '" fill="#6ea8fe" opacity=".5"><title>' + esc(when(r.when) + ': ' + r.signals + ' signals, ' + r.filled + ' fills, ' + r.scanned + ' markets') + '</title></rect>';
      out += '<rect x="' + x0 + '" y="' + y(r.filled) + '" width="' + w + '" height="' + (H - B - y(r.filled)) + '" fill="#3ddc84"/>';
    });
    out += '<text x="' + L + '" y="' + (H - 6) + '" fill="#9aa0aa" font-size="10">' + esc(when(runs[0].when)) + '</text>';
    out += '<text x="' + (W - R) + '" y="' + (H - 6) + '" fill="#9aa0aa" font-size="10" text-anchor="end">' + esc(when(runs[runs.length - 1].when)) + '</text>';
    svg.innerHTML = out;
  }
  lineChart($('equity'), D.runs.map(r => ({ y: r.equity, t: when(r.when), label: when(r.when) + '  ' + money(r.equity) })),
            { base: P.starting_cash, fmt: (v) => '$' + v.toFixed(0) });
  barChart($('activity'), D.runs);

  // column: [header, display, cssClass, isRawHtml, tdClass, sortKey]; sortKey defaults to display
  const num = (k) => (r) => r[k] == null ? -Infinity : r[k];
  const COLS = {
    predictions: [
      ['when (UTC)', r => when(r.when), null, false, null, r => r.when], ['prediction', r => r.summary, null, false, 'summary'], ['strategy', r => r.strategy],
      ['side', r => r.side], ['price', r => f4(r.price), null, false, null, num('price')], ['fair', r => f4(r.fair), null, false, null, num('fair')],
      ['edge', r => (r.edge > 0 ? '+' : '') + f4(r.edge), r => sign(r.edge), false, null, num('edge')], ['size', r => r.size, null, false, null, num('size')],
      ['traded', r => r.traded ? 'yes' : 'no', r => r.traded ? '' : 'muted'],
      ['result', r => r.result == null ? '<span class="pill">open</span>'
        : '<span class="pill ' + (r.won ? 'won' : 'lost') + '">' + (r.won ? 'WON' : 'lost') + ' (' + esc(r.result) + ')</span>', null, true, null,
        r => r.result == null ? 0 : r.won ? 2 : 1],
      ['why', r => r.why, null, false, 'why'], ['ticker', r => r.ticker, () => 'muted'],
    ],
    trades: [
      ['when (UTC)', r => when(r.when), null, false, null, r => r.when], ['trade', r => r.summary, null, false, 'summary'], ['strategy', r => r.strategy],
      ['side', r => r.side], ['price', r => f4(r.price), null, false, null, num('price')], ['contracts', r => r.count, null, false, null, num('count')],
      ['fee', r => money(r.fee), null, false, null, num('fee')], ['cost', r => money(r.cost), null, false, null, num('cost')], ['fair', r => f4(r.fair), null, false, null, num('fair')],
      ['result', r => r.result == null ? '<span class="pill">open</span>'
        : '<span class="pill ' + (r.pnl >= 0 ? 'won' : 'lost') + '">' + esc(r.result) + '</span>', null, true, null,
        r => r.result == null ? 0 : r.pnl >= 0 ? 2 : 1],
      ['pnl', r => money(r.pnl), r => sign(r.pnl), false, null, num('pnl')], ['settled (UTC)', r => when(r.settled_at), null, false, null, r => r.settled_at || ''],
      ['ticker', r => r.ticker, () => 'muted'],
    ],
    runs: [
      ['when (UTC)', r => when(r.when), null, false, null, r => r.when], ['strategies', r => r.strategies], ['markets scanned', r => r.scanned, null, false, null, num('scanned')],
      ['signals', r => r.signals, null, false, null, num('signals')], ['fills', r => r.filled, null, false, null, num('filled')], ['settled', r => r.settled, null, false, null, num('settled')],
      ['cash', r => money(r.cash), null, false, null, num('cash')], ['equity', r => money(r.equity), null, false, null, num('equity')],
    ],
  };
  let tab = 'predictions', sortKey = null, sortDir = -1;
  function rows() {
    const q = $('q').value.trim().toLowerCase(), st = $('status').value;
    return D[tab].filter(r => {
      if (q && !JSON.stringify(r).toLowerCase().includes(q)) return false;
      if (tab === 'runs' || !st) return true;
      if (st === 'open') return r.result == null;
      if (st === 'traded') return tab === 'trades' || r.traded;
      const won = tab === 'trades' ? (r.pnl != null && r.pnl >= 0) : r.won;
      return r.result != null && (st === 'won') === !!won;
    });
  }
  function render() {
    const cols = COLS[tab];
    let rs = rows();
    if (sortKey != null) {
      const get = cols[sortKey][5] || cols[sortKey][1];
      rs = rs.slice().sort((a, b) => { const x = get(a), y = get(b); return (x > y ? 1 : x < y ? -1 : 0) * sortDir; });
    }
    let h = '<thead><tr>' + cols.map(([n], i) => '<th data-i="' + i + '">' + esc(n) + (sortKey === i ? (sortDir > 0 ? ' ▲' : ' ▼') : '') + '</th>').join('') + '</tr></thead><tbody>';
    if (!rs.length) h += '<tr><td colspan="' + cols.length + '" class="empty">nothing here yet — the scheduled run adds rows every 30 min</td></tr>';
    for (const r of rs) h += '<tr>' + cols.map(([, get, cls, raw, td]) =>
      '<td class="' + (td || '') + ' ' + (cls ? cls(r) : '') + '">' + (raw ? get(r) : esc(get(r) ?? '-')) + '</td>').join('') + '</tr>';
    $('table').innerHTML = h + '</tbody>';
    $('table').querySelectorAll('th').forEach(th => th.onclick = () => {
      const i = +th.dataset.i; sortDir = sortKey === i ? -sortDir : -1; sortKey = i; render();
    });
  }
  document.querySelectorAll('.tabs button[data-tab]').forEach(b => b.onclick = () => {
    document.querySelectorAll('.tabs button').forEach(x => x.classList.remove('on'));
    b.classList.add('on'); tab = b.dataset.tab; sortKey = null; render();
  });
  $('q').oninput = render; $('status').onchange = render;
  render();
})();
</script>
</body>
</html>
"""
