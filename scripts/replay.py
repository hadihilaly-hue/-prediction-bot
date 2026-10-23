from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from prediction_bot.config import KALSHI_PROD_REST, Settings
from prediction_bot.devig import longshot_haircut
from prediction_bot.fees import taker_fee, taker_fee_rate
from prediction_bot.teams import KALSHI_SERIES_TO_SPORT, team_for_code, team_for_odds_name
from prediction_bot.venues.kalshi import KalshiClient

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
UNKNOWN_RULES = ("dispersion", "few_books", "wide_spread", "depth", "worst_case_devig")
MONTHS = {
    month: index + 1
    for index, month in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
    )
}
EVENT_RE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{4})?([A-Z0-9]+)$")
KNOWN_STARTS = {
    "KXNFLGAME-26OCT04KCLV": datetime(2026, 10, 4, 20, 25, tzinfo=UTC),
    "KXNFLGAME-26OCT04DENSF": datetime(2026, 10, 4, 20, 25, tzinfo=UTC),
    "KXNFLGAME-26OCT04MIAMIN": datetime(2026, 10, 4, 20, 5, tzinfo=UTC),
}
THREE_WAY_SERIES = {
    "KXMLSGAME",
    "KXEPLGAME",
    "KXLALALIGAGAME",
    "KXSERIEAGAME",
    "KXBUNDESLIGAGAME",
    "KXLIGUE1GAME",
    "KXUCLGAME",
    "KXUELGAME",
    "KXUECLGAME",
    "KXLIGAMXGAME",
}


def parse_datetime(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def event_day(event_ticker: str) -> datetime | None:
    code = event_ticker.split("-", 1)[1] if "-" in event_ticker else ""
    match = EVENT_RE.match(code)
    if not match:
        return None
    year, month, day, _start, _teams = match.groups()
    return datetime(2000 + int(year), MONTHS[month], int(day), tzinfo=ET)


def start_time(event_ticker: str) -> datetime | None:
    if event_ticker in KNOWN_STARTS:
        return KNOWN_STARTS[event_ticker]
    day = event_day(event_ticker)
    match = EVENT_RE.match(event_ticker.split("-", 1)[1]) if "-" in event_ticker else None
    if day is None or match is None or not event_ticker.startswith("KXMLBGAME-"):
        return None
    encoded = match.group(4)
    if encoded is None:
        return None
    return day.replace(hour=int(encoded[:2]), minute=int(encoded[2:])).astimezone(UTC)


def canonical_key(event_ticker: str, code: str, side: str) -> tuple[str, str]:
    if side == "yes":
        return event_ticker, code
    series = event_ticker.split("-", 1)[0]
    if series in THREE_WAY_SERIES:
        return event_ticker, f"not:{code}"
    event_code = event_ticker.split("-", 1)[1] if "-" in event_ticker else ""
    match = EVENT_RE.match(event_code)
    teams = match.group(5) if match else ""
    if teams.startswith(code):
        return event_ticker, teams[len(code) :]
    if teams.endswith(code):
        return event_ticker, teams[: -len(code)]
    return event_ticker, f"not:{code}"


@dataclass(frozen=True)
class ReplaySignal:
    id: int
    created_at: datetime
    strategy: str
    ticker: str
    side: str
    fair: Decimal
    price: Decimal
    edge: Decimal
    size: Decimal
    rationale: str
    acted: bool

    @property
    def event_ticker(self) -> str:
        return self.ticker.rsplit("-", 1)[0]

    @property
    def series(self) -> str:
        return self.event_ticker.split("-", 1)[0]

    @property
    def code(self) -> str:
        return self.ticker.rsplit("-", 1)[-1]


@dataclass
class Decision:
    signal: ReplaySignal
    reasons: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    fair: Decimal = Decimal(0)
    net_edge: Decimal = Decimal(0)
    position_key: tuple[str, str] = ("", "")
    fee_multiplier: Decimal = Decimal(0)
    run_key: str = ""
    v2_would_trade: bool = False
    contracts: int = 0
    pnl: Decimal | None = None
    clv_kalshi: Decimal | None = None
    clv_gameday: Decimal | None = None

    @property
    def first_reason(self) -> str:
        return self.reasons[0] if self.reasons else ""


def _rationale_team(signal: ReplaySignal) -> str | None:
    match = re.match(r"^\s*(.+?)\s+consensus\s+", signal.rationale, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.match(r"^\s*(.+?)\s+(?:Pinnacle|Circa|BetOnline)\s+reference", signal.rationale)
    return match.group(1).strip() if match else None


def _mapping_reason(signal: ReplaySignal) -> str | None:
    sport = KALSHI_SERIES_TO_SPORT.get(signal.series)
    if sport is None:
        return "mapping_failed"
    if signal.code in {"TIE", "DRAW"}:
        return None if "draw" in signal.rationale.lower() else "title_mismatch"
    code_team = team_for_code(signal.series, signal.code)
    name = _rationale_team(signal)
    odds_team = team_for_odds_name(sport, name) if name else None
    if code_team is None or odds_team is None:
        return "mapping_failed"
    return "title_mismatch" if code_team != odds_team else None


def evaluate_signal(
    signal: ReplaySignal,
    fee_map: dict[str, tuple[str, Decimal]],
    settings: Settings,
    history: list[ReplaySignal],
) -> Decision:
    decision = Decision(signal=signal, fair=signal.fair)
    decision.unknown.extend(UNKNOWN_RULES)
    sport = KALSHI_SERIES_TO_SPORT.get(signal.series)
    if sport in getattr(settings, "v2_disabled_sports", []):
        decision.reasons.append("league_disabled")
    mapping_reason = _mapping_reason(signal)
    if mapping_reason:
        decision.reasons.append(mapping_reason)

    game_day = event_day(signal.event_ticker)
    commence = start_time(signal.event_ticker)
    if commence is None:
        decision.unknown.extend(("kickoff_guard", "commence_past", "entry_window"))
    else:
        seconds_to_start = (commence - signal.created_at).total_seconds()
        buffer = settings.kickoff_buffer_minutes.get(
            sport or "", settings.kickoff_buffer_default_minutes
        )
        if seconds_to_start < 0:
            decision.reasons.append("in_play:commence_past")
        elif seconds_to_start <= buffer * 60:
            decision.reasons.append("kickoff_guard")
        elif settings.min_lead_hours and seconds_to_start < settings.min_lead_hours * 3600:
            decision.reasons.append("entry_window")

    earlier = [
        item
        for item in history
        if item.id != signal.id
        and item.event_ticker == signal.event_ticker
        and item.created_at < signal.created_at
    ]
    ticker_history = [
        item for item in earlier if item.ticker == signal.ticker and item.side == signal.side
    ]
    if ticker_history:
        first = min(ticker_history, key=lambda item: (item.created_at, item.id))
        if (
            settings.in_play_kalshi_price_min
            < first.price
            < settings.in_play_kalshi_price_max
            and (
                signal.price <= settings.in_play_kalshi_price_min
                or signal.price >= settings.in_play_kalshi_price_max
            )
        ):
            decision.reasons.append("in_play:kalshi_extreme")
        if abs(signal.fair - first.fair) > settings.in_play_consensus_jump:
            decision.reasons.append("in_play:consensus_jump")
    else:
        decision.unknown.extend(("in_play:kalshi_extreme", "in_play:consensus_jump"))
    event_history = [item for item in earlier if item.code == signal.code]
    if event_history:
        first_signal = min(event_history, key=lambda item: (item.created_at, item.id))
        first_fair = first_signal.fair if first_signal.side == "yes" else Decimal(1) - first_signal.fair
        current_team_fair = signal.fair if signal.side == "yes" else Decimal(1) - signal.fair
        if (
            settings.in_play_first_consensus_min
            <= first_fair
            <= settings.in_play_first_consensus_max
            and (
                current_team_fair <= settings.in_play_consensus_low
                or current_team_fair >= settings.in_play_consensus_high
            )
        ):
            decision.reasons.append("in_play:consensus_extreme")
    else:
        decision.unknown.append("in_play:consensus_extreme")

    if game_day is None:
        decision.unknown.append("ticker_date")
        hours_to_proxy = Decimal(0)
    else:
        proxy = game_day.replace(hour=12)
        hours_to_proxy = Decimal(str((proxy.astimezone(UTC) - signal.created_at).total_seconds() / 3600))
    fair = longshot_haircut(signal.fair, hours_to_proxy, settings.longshot_haircut)
    decision.fair = fair
    if signal.price < settings.v2_price_floor:
        decision.reasons.append("price_floor")
    if signal.price > settings.v2_price_ceiling:
        decision.reasons.append("price_ceiling")

    fee = fee_map.get(signal.series)
    if fee is None:
        decision.unknown.append("series_fee")
        fee_type, multiplier = "", Decimal(0)
    else:
        fee_type, multiplier = fee
        decision.unknown = [rule for rule in decision.unknown if rule != "series_fee"]
    decision.fee_multiplier = multiplier
    if fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
        decision.reasons.append("unknown_fee_type")
    gross_edge = fair - signal.price
    decision.net_edge = gross_edge - taker_fee_rate(signal.price, multiplier)
    if decision.net_edge < settings.v2_min_net_edge:
        decision.reasons.append("net_edge")
    if signal.price < settings.v2_longshot_price and gross_edge < settings.v2_longshot_min_gross:
        decision.reasons.append("longshot_edge")
    if gross_edge >= settings.max_gross_edge and signal.ticker not in settings.big_edge_whitelist:
        decision.reasons.append("big_edge")
    decision.position_key = canonical_key(signal.event_ticker, signal.code, signal.side)
    return decision


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def load_database(
    db_path: Path, since: str
) -> tuple[
    list[ReplaySignal],
    list[dict[str, Any]],
    dict[str, str],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    signals: list[ReplaySignal] = []
    for row in conn.execute(
        "SELECT id,created_at,strategy,ticker,side,fair_prob,limit_price,edge,size,rationale,acted "
        "FROM signals ORDER BY created_at,id"
    ):
        if str(row["created_at"])[:10] < since:
            continue
        signals.append(
            ReplaySignal(
                int(row["id"]),
                parse_datetime(str(row["created_at"])),
                str(row["strategy"]),
                str(row["ticker"]),
                str(row["side"]),
                Decimal(str(row["fair_prob"])),
                Decimal(str(row["limit_price"])),
                Decimal(str(row["edge"])),
                Decimal(str(row["size"])),
                str(row["rationale"] or ""),
                bool(row["acted"]),
            )
        )
    fills: list[dict[str, Any]] = []
    fill_columns = _table_columns(conn, "fills")
    settlement_columns = _table_columns(conn, "settlements")
    if {"ticker", "created_at", "price", "count", "fee"} <= fill_columns:
        result_select = "s.result" if "result" in settlement_columns else "NULL AS result"
        settled_select = "s.settled_at" if "settled_at" in settlement_columns else "NULL AS settled_at"
        for row in conn.execute(
            f"SELECT f.id,f.created_at,f.strategy,f.ticker,f.side,f.price,f.count,f.fee,"
            f"f.fair_prob,{result_select},{settled_select} FROM fills f "
            "LEFT JOIN settlements s ON s.ticker=f.ticker ORDER BY f.created_at,f.id"
        ):
            if str(row["created_at"])[:10] < since:
                continue
            fills.append(dict(row))
    settlements = {
        str(row[0]): str(row[1])
        for row in conn.execute("SELECT ticker,result FROM settlements")
    } if settlement_columns >= {"ticker", "result"} else {}
    runs: list[dict[str, Any]] = []
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "runs" in tables:
        runs = [
            dict(row)
            for row in conn.execute(
                "SELECT id,ran_at,strategies,cash,open_cost FROM runs ORDER BY ran_at"
            )
        ]
    markets = [dict(row) for row in conn.execute("SELECT * FROM markets")] if "markets" in tables else []
    conn.close()
    return signals, fills, settlements, runs, markets


def load_fee_map(
    db_path: Path,
    cache_dir: Path,
    client: KalshiClient | None = None,
) -> dict[str, tuple[str, Decimal]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "series_fees.json"
    if cache_path.is_file():
        raw = json.loads(cache_path.read_text())
        return {key: (value[0], Decimal(value[1])) for key, value in raw.items()}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    fee_map: dict[str, tuple[str, Decimal]] = {}
    if "series_fees" in tables:
        columns = _table_columns(conn, "series_fees")
        if {"series_ticker", "fee_type", "fee_multiplier"} <= columns:
            fee_map = {
                str(ticker): (str(kind), Decimal(str(multiplier)))
                for ticker, kind, multiplier in conn.execute(
                    "SELECT series_ticker,fee_type,fee_multiplier FROM series_fees"
                )
            }
    conn.close()
    if not fee_map:
        series = (client or KalshiClient(KALSHI_PROD_REST)).get_series()
        for item in series:
            ticker = item.get("ticker") or item.get("series_ticker")
            fee_type = item.get("fee_type")
            multiplier = item.get("fee_multiplier")
            if isinstance(ticker, str) and isinstance(fee_type, str) and multiplier not in (None, ""):
                fee_map[ticker] = (fee_type, Decimal(str(multiplier)))
    cache_path.write_text(
        json.dumps({key: (value[0], str(value[1])) for key, value in fee_map.items()}, indent=2) + "\n"
    )
    return fee_map


def _run_keys(signals: list[ReplaySignal], runs: list[dict[str, Any]]) -> dict[int, str]:
    out: dict[int, str] = {}
    eligible_runs = [
        (str(run["id"]), parse_datetime(str(run["ran_at"])))
        for run in runs
        if "sportsbook" in str(run.get("strategies", ""))
    ]
    for signal in signals:
        following = [(run_id, at) for run_id, at in eligible_runs if at >= signal.created_at]
        if following:
            run_id, at = min(following, key=lambda item: item[1])
            if (at - signal.created_at).total_seconds() <= 600:
                out[signal.id] = run_id
                continue
        out[signal.id] = signal.created_at.replace(microsecond=0).isoformat()
    return out


def _associate_fills(
    fills: list[dict[str, Any]], signals: list[ReplaySignal]
) -> dict[int, ReplaySignal | None]:
    by_side: dict[tuple[str, str], list[ReplaySignal]] = defaultdict(list)
    for signal in signals:
        by_side[(signal.ticker, signal.side)].append(signal)
    out: dict[int, ReplaySignal | None] = {}
    for fill in fills:
        key = (str(fill["ticker"]), str(fill["side"]))
        created = parse_datetime(str(fill["created_at"]))
        possible = [signal for signal in by_side.get(key, []) if signal.created_at <= created]
        out[int(fill["id"])] = (
            min(possible, key=lambda signal: abs((created - signal.created_at).total_seconds()))
            if possible
            else None
        )
    return out


def _load_candles(
    decision: Decision,
    cache_dir: Path,
    client: KalshiClient,
) -> list[dict[str, Any]]:
    ticker = decision.signal.ticker
    cache_path = cache_dir / f"{ticker.replace('/', '_')}.json"
    if cache_path.is_file():
        try:
            return json.loads(cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    day = event_day(decision.signal.event_ticker)
    if day is None:
        return []
    day_target = day.replace(hour=10)
    start = start_time(decision.signal.event_ticker)
    first_target = (
        min(start - timedelta(minutes=65), day_target - timedelta(minutes=2))
        if start
        else day_target - timedelta(minutes=2)
    )
    start_ts = int(first_target.timestamp())
    end_target = max(
        day_target + timedelta(minutes=1),
        start + timedelta(minutes=1) if start else day_target + timedelta(minutes=1),
    )
    try:
        candles = client.get_candlesticks(
            decision.signal.series, ticker, start_ts, int(end_target.timestamp()), 1
        )
    except Exception:
        return []
    cache_path.write_text(json.dumps(candles, separators=(",", ":")) + "\n")
    return candles


def _candle_mid(candles: list[dict[str, Any]], target: datetime) -> Decimal | None:
    candidates = []
    for candle in candles:
        stamp = candle.get("end_period_ts", candle.get("start_period_ts"))
        if stamp is None:
            continue
        try:
            at = datetime.fromtimestamp(int(stamp), UTC)
            bid_obj = candle.get("yes_bid") or {}
            ask_obj = candle.get("yes_ask") or {}
            bid = bid_obj.get("close_dollars") if isinstance(bid_obj, dict) else None
            ask = ask_obj.get("close_dollars") if isinstance(ask_obj, dict) else None
            if bid is None or ask is None:
                yes_price = candle.get("price", candle.get("close", candle.get("yes_price")))
                if yes_price is None:
                    continue
                bid = ask = yes_price
            if at <= target:
                candidates.append((at, (Decimal(str(bid)) + Decimal(str(ask))) / 2))
        except (TypeError, ValueError, ArithmeticError):
            continue
    return max(candidates, key=lambda pair: pair[0])[1] if candidates else None


def _record_clv(
    decision: Decision,
    cache_dir: Path,
    client: KalshiClient,
) -> None:
    candles = _load_candles(decision, cache_dir, client)
    if not candles:
        return
    day = event_day(decision.signal.event_ticker)
    if day is None:
        return
    for target_name, target in (
        (
            "clv_kalshi",
            start_time(decision.signal.event_ticker) - timedelta(minutes=5)
            if start_time(decision.signal.event_ticker)
            else None,
        ),
        ("clv_gameday", day.replace(hour=10).astimezone(UTC)),
    ):
        if target is None:
            continue
        yes_mid = _candle_mid(candles, target)
        if yes_mid is None:
            continue
        side_mid = yes_mid if decision.signal.side == "yes" else Decimal(1) - yes_mid
        setattr(decision, target_name, (side_mid - decision.signal.price) * 100)


def _simulate(
    decisions: list[Decision],
    fills: list[dict[str, Any]],
    settlements: dict[str, str],
    runs: list[dict[str, Any]],
    settings: Settings,
) -> list[Decision]:
    run_keys = _run_keys([decision.signal for decision in decisions], runs)
    batches: dict[tuple[str, str], list[Decision]] = defaultdict(list)
    for decision in decisions:
        if not decision.reasons and "series_fee" not in decision.unknown:
            decision.run_key = run_keys[decision.signal.id]
            batches[(decision.run_key, decision.position_key[0] + ":" + decision.position_key[1])].append(
                decision
            )
    ordered = sorted(
        (decision for batch in batches.values() for decision in batch),
        key=lambda decision: (decision.signal.created_at, decision.signal.id),
    )
    chosen: list[Decision] = []
    seen_run_positions: set[tuple[str, tuple[str, str]]] = set()
    for decision in ordered:
        run_position = (decision.run_key, decision.position_key)
        if run_position in seen_run_positions:
            decision.reasons.append("duplicate_position")
            continue
        seen_run_positions.add(run_position)
        chosen.append(decision)

    cash = settings.paper_starting_cash
    open_cost = Decimal(0)
    position_cost: dict[tuple[str, str], Decimal] = {}
    event_cost: dict[str, Decimal] = {}
    daily_cost: dict[str, Decimal] = {}
    total_open = Decimal(0)
    fill_by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fill in fills:
        fill_by_ticker[str(fill["ticker"])].append(fill)
    for decision in chosen:
        signal = decision.signal
        effective_price = signal.price + taker_fee_rate(signal.price, decision.fee_multiplier)
        bankroll = cash + open_cost
        if bankroll <= 0 or effective_price >= 1:
            decision.reasons.append("size_zero")
            continue
        kelly = (decision.fair - effective_price) / (1 - effective_price)
        stake = max(Decimal(0), kelly * settings.v2_kelly_fraction * bankroll)
        event = signal.event_ticker
        key = decision.position_key
        day = signal.created_at.astimezone(UTC).date().isoformat()
        budget = min(
            stake,
            bankroll * settings.v2_position_bankroll_fraction - position_cost.get(key, Decimal(0)),
            bankroll * settings.v2_event_bankroll_fraction - event_cost.get(event, Decimal(0)),
            bankroll * settings.v2_daily_bankroll_fraction - daily_cost.get(day, Decimal(0)),
            bankroll * settings.v2_total_open_bankroll_fraction - total_open,
            cash,
        )
        contracts = int(max(Decimal(0), budget / effective_price).to_integral_value(rounding=ROUND_DOWN))
        while contracts > 0:
            fee = taker_fee(
                signal.price,
                Decimal(contracts),
                decision.fee_multiplier,
                settings.fee_rounding,
            )
            cost = signal.price * contracts + fee
            if cost <= budget:
                break
            contracts -= 1
        if contracts <= 0:
            decision.reasons.append("size_zero")
            continue
        decision.contracts = contracts
        decision.v2_would_trade = True
        cash -= cost
        open_cost += cost
        total_open += cost
        position_cost[key] = position_cost.get(key, Decimal(0)) + cost
        event_cost[event] = event_cost.get(event, Decimal(0)) + cost
        daily_cost[day] = daily_cost.get(day, Decimal(0)) + cost

        matching = next(
            (
                fill for fill in fill_by_ticker.get(signal.ticker, [])
                if str(fill["side"]) == signal.side
            ),
            None,
        )
        result = str(matching["result"]) if matching and matching.get("result") is not None else settlements.get(signal.ticker)
        if result in {"yes", "no"}:
            won = result == signal.side
            payout = Decimal(contracts) if won else Decimal(0)
            decision.pnl = payout - cost
            cash += payout
            open_cost -= cost
            total_open -= cost
            position_cost[key] -= cost
            event_cost[event] -= cost
    return decisions


def _actual_pnl(fills: list[dict[str, Any]]) -> Decimal:
    total = Decimal(0)
    for fill in fills:
        result = fill.get("result")
        if result not in {"yes", "no"}:
            continue
        count = Decimal(str(fill["count"]))
        cost = Decimal(str(fill["price"])) * count + Decimal(str(fill["fee"]))
        total += (count if result == fill["side"] else Decimal(0)) - cost
    return total


def _md_table(headers: list[str], rows: list[list[object]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def render_report(
    rules: str,
    since: str,
    signals: list[ReplaySignal],
    decisions: list[Decision],
    fills: list[dict[str, Any]],
) -> str:
    if rules == "v1":
        return "\n".join(
            [
                "# Historical replay (v1)",
                "",
                f"Signals since {since}: {len(signals)}; historical fills: {len(fills)}; "
                f"v1 acted signals: {sum(signal.acted for signal in signals)}.",
                "",
                f"v1 actual settled P&L: ${_actual_pnl(fills):.2f}.",
                "",
                _md_table(
                    ["ID", "Ticker", "Side", "V1 acted"],
                    [
                        [signal.id, signal.ticker, signal.side, "yes" if signal.acted else "no"]
                        for signal in signals
                    ],
                ),
                "",
            ]
        )
    associated = _associate_fills(fills, signals)
    blocked = []
    histogram: Counter[str] = Counter()
    first_histogram: Counter[str] = Counter()
    for fill in fills:
        signal = associated[int(fill["id"])]
        decision = next((item for item in decisions if signal and item.signal.id == signal.id), None)
        if decision is None or not decision.v2_would_trade:
            blocked.append((fill, decision))
            reasons = decision.reasons if decision else ["unmatched_signal"]
            first_histogram[reasons[0]] += 1
            histogram.update(reasons)
    positions = [decision for decision in decisions if decision.v2_would_trade]
    reason_rows = [[reason, count] for reason, count in sorted(histogram.items())]
    first_rows = [[reason, count] for reason, count in sorted(first_histogram.items())]
    position_rows = [
        [
            decision.signal.ticker,
            decision.signal.side,
            decision.signal.price,
            decision.contracts,
            "—" if decision.pnl is None else f"${decision.pnl:.2f}",
            "—" if decision.clv_kalshi is None else f"{decision.clv_kalshi:.2f}¢",
            "—" if decision.clv_gameday is None else f"{decision.clv_gameday:.2f}¢",
        ]
        for decision in positions
    ]
    v2_pnl = sum((decision.pnl or Decimal(0) for decision in positions), Decimal(0))
    actual_pnl = _actual_pnl(fills)
    rows = [
        f"# Historical replay ({rules})",
        "",
        f"Signals since {since}: {len(signals)}; historical fills: {len(fills)}; "
        f"v2-blocked fills: {len(blocked)} / {len(fills)}.",
        "",
        "Replay uses stored v1 weighted multiplicative fair values. Dispersion, few books, "
        "wide spread, displayed depth, and worst-case devig are unknown; because alternative "
        "devig methods can only lower the stored fair, trade counts are an upper bound. "
        "Longshot haircut timing uses ticker date at 12:00 ET as a commencement proxy. "
        "Only MLB ticker ET starts and published Appendix D kickoffs are used as exact starts.",
        "",
        "## All observed block reasons (a fill can have multiple reasons)",
        _md_table(["Reason", "Fills"], reason_rows),
        "",
        "## First blocking reason",
        _md_table(["Reason", "Fills"], first_rows),
        "",
        f"## v2 would-trade positions ({len(positions)}; settled P&L ${v2_pnl:.2f})",
        _md_table(
            ["Ticker", "Side", "Entry", "Contracts", "Settled P&L", "CLV T-5", "CLV game-day"],
            position_rows,
        ),
        "",
        f"v1 actual settled P&L: ${actual_pnl:.2f}; v2 replay settled P&L: ${v2_pnl:.2f}. "
        "CLV is side-adjusted midpoint minus entry price, in cents. T-5 uses only verified "
        "commencement times; game-day uses the 10:00 ET candle. Missing historical candles "
        "remain unavailable rather than being inferred.",
        "",
        "## Per-signal decisions",
        _md_table(
            ["ID", "Ticker", "Side", "V1 acted", "V2 decision", "Reasons", "Unknown rules"],
            [
                [
                    signal.id,
                    signal.ticker,
                    signal.side,
                    "yes" if signal.acted else "no",
                    "would-trade" if decision.v2_would_trade else "blocked",
                    ", ".join(decision.reasons) or "—",
                    ", ".join(sorted(set(decision.unknown))) or "—",
                ]
                for signal, decision in (
                    (item.signal, item) for item in decisions
                )
            ],
        ),
        "",
    ]
    return "\n".join(rows)


def run_replay(
    db_path: Path,
    since: str,
    cache_dir: Path,
    rules: str = "both",
    settings: Settings | None = None,
    fee_map: dict[str, tuple[str, Decimal]] | None = None,
    kalshi: KalshiClient | None = None,
) -> tuple[str, dict[str, Any]]:
    settings = settings or Settings(_env_file=None)
    cache_dir.mkdir(parents=True, exist_ok=True)
    signals, fills, settlements, runs, markets = load_database(db_path, since)
    fees = (
        fee_map
        if fee_map is not None
        else load_fee_map(db_path, cache_dir)
        if rules != "v1"
        else {}
    )
    history = signals
    decisions = [evaluate_signal(signal, fees, settings, history) for signal in signals]
    decisions_by_id = {decision.signal.id: decision for decision in decisions}
    if rules != "v1":
        _simulate(decisions, fills, settlements, runs, settings)
        for fill in fills:
            signal = _associate_fills([fill], signals)[int(fill["id"])]
            decision = decisions_by_id.get(signal.id) if signal else None
            if decision is not None and decision.v2_would_trade:
                _record_clv(decision, cache_dir, kalshi or KalshiClient(KALSHI_PROD_REST))
    report = render_report(rules, since, signals, decisions, fills)
    report = report.replace(
        f"Signals since {since}:",
        f"Stored markets loaded: {len(markets)}; signals since {since}:",
        1,
    )
    blocked = [
        fill
        for fill in fills
        if (
            (signal := _associate_fills([fill], signals)[int(fill["id"])]) is None
            or not decisions_by_id[signal.id].v2_would_trade
        )
    ]
    return report, {
        "signals": len(signals),
        "fills": len(fills),
        "markets": len(markets),
        "blocked_fills": len(blocked),
        "decisions": decisions,
        "v2_positions": [decision for decision in decisions if decision.v2_would_trade],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only historical v1/v2 replay.")
    parser.add_argument("--rules", choices=("v1", "v2", "both"), default="both")
    parser.add_argument("--since", default="2026-09-28")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--out", choices=("md",), default="md")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path("replay_cache"))
    args = parser.parse_args()
    report, _headline = run_replay(args.db, args.since, args.cache_dir, args.rules)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report)
    else:
        sys.stdout.write(report)


if __name__ == "__main__":
    main()
