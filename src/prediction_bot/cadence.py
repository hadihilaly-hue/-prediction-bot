"""Credit cadence: move the monthly Odds API quota toward the days that feed
the 72-168h entry window.

Two layers per the cadence brief, both implemented here and consumed by
`OddsPacer.start_run`:

- static(d): base(d) x cluster(d) x deadzone(d) from data/cadence_calendar.json.
  base(d) sums the weekly_profile weight of each active sport for pull weekday
  d; a sport is active when some game day in [d+3, d+7] falls inside one of its
  season ranges and outside its blackouts.
- dyn(d): mapped-games count from the free /events endpoint, fetched once a day
  by the pacer for each active sport over [today+3, today+13], then bucketed as
  games commencing in [d+3, d+7]. weight(d) = static(d) x (0.5 + 0.5 x
  dyn(d)/median_dyn) — a +/-50% adjustment, never a replacement. Days beyond
  the 7-day events horizon use static only, and so does every day when the
  fetch failed or /events proved not to be free.

budget(today) = remaining x weight(today) / sum(weight(d)), remaining =
quota_left x 0.95 (5% reserve for manual runs/retries). Per-slot share =
budget/slots_left_today, carried fractionally by the pacer. Floors: >=2 pulls
per day while any sport is active; <=40 per day; after the 20th a projected
under-spend raises the breadth multiplier so slots pull a second feed.
Modes: 'legacy' bypasses all of this (existing run_share path, unchanged);
'off' uses static only; 'calendar' is the full blend.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
UEFA_MIDWEEK_PROFILE = "soccer_uefa_midweek"
SOCCER_WEEKEND_PROFILE = "soccer_weekend_leagues"
DEFAULT_SPORT_WEIGHT = 1.0
DAY_PULLS_FLOOR = 2.0
DAY_PULLS_CAP = 40.0
RESERVE_FRACTION = 0.05
UNDERSPEND_CHECK_DAY = 20
UNDERSPEND_TOLERANCE = 0.15
BREADTH_MULTIPLIER = 2.0
DYN_HORIZON_DAYS = 7

DEFAULT_CALENDAR_PATH = Path("data/cadence_calendar.json")


def _day(value: str) -> date:
    return date.fromisoformat(value)


def _today(now: datetime) -> date:
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).date()


def _days_in_month(year: int, month: int) -> int:
    nxt = date(year + month // 12, month % 12 + 1, 1)
    return (nxt - timedelta(days=1)).day


def slots_left_today(now: datetime, runs_per_day: float) -> int:
    """Remaining pacer slots today, mirroring run_share's day-fraction math."""
    now = _utc(now)
    nxt = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)
    return max(1, math.ceil((nxt - now).total_seconds() / 86400 * runs_per_day))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_day_overrides(raw: str | None) -> dict[date, float]:
    """`YYYY-MM-DD=weight[,YYYY-MM-DD=weight]` -> {day: weight}."""
    out: dict[date, float] = {}
    for piece in (raw or "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            day_s, weight_s = piece.split("=", 1)
            out[_day(day_s.strip())] = float(weight_s.strip())
        except ValueError:
            log.warning("cadence: ignoring malformed day override %r", piece)
    return out


@dataclass(frozen=True)
class Window:
    start: date
    end: date
    why: str = ""

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


class CadenceCalendar:
    """Parsed cadence_calendar.json. Dates are GAME dates; pull windows are the
    game date shifted back by entry_window_hours/24 days (3..7 here)."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.credits_per_month = float(data.get("credits_per_month") or 500)
        hours = data.get("entry_window_hours") or [72, 168]
        assert isinstance(hours, list)
        self.shift_lo = int(hours[0]) // 24
        self.shift_hi = int(hours[1]) // 24
        self._seasons: dict[str, list[Window]] = {}
        for sport, ranges in (data.get("seasons") or {}).items():
            windows: list[Window] = []
            for r in ranges:
                if "dates" in r:
                    windows += [
                        Window(_day(d), _day(d), str(r.get("note") or "")) for d in r["dates"]
                    ]
                elif "start" in r and "end" in r:
                    windows.append(
                        Window(_day(r["start"]), _day(r["end"]), str(r.get("note") or ""))
                    )
                else:
                    # Note-only entries (e.g. UEL/UECL derived-date notes):
                    # no concrete range, so the sport simply never activates.
                    log.info("cadence: skipping note-only season entry for %s", sport)
            self._seasons[str(sport)] = windows
        self._blackouts: dict[str, list[Window]] = {}
        for b in data.get("blackouts") or []:
            window = Window(_day(b["start"]), _day(b["end"]), str(b.get("note") or ""))
            for sport in b["sports"]:
                self._blackouts.setdefault(str(sport), []).append(window)
        self._profiles: dict[str, dict[str, float]] = {}
        for key, profile in (data.get("weekly_profile") or {}).items():
            if str(key).startswith("_") or not isinstance(profile, dict):
                continue
            weights = {
                weekday: float(profile[weekday]) for weekday in WEEKDAYS if weekday in profile
            }
            if weights:
                self._profiles[str(key)] = weights
        self._clusters_full = [
            (
                Window(_day(c["pull_start"]), _day(c["pull_end"]), str(c.get("why") or "")),
                float(c["weight"]),
            )
            for c in data.get("clusters") or []
        ]
        self.clusters = [w for w, _ in self._clusters_full]
        self.dead_zones = [
            Window(_day(z["start"]), _day(z["end"]), str(z.get("why") or ""))
            for z in data.get("dead_zones") or []
        ]

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CALENDAR_PATH) -> CadenceCalendar:
        return cls(json.loads(Path(path).read_text()))

    def game_window(self, pull_day: date) -> tuple[date, date]:
        """Game dates a pull on `pull_day` can still reach inside 72-168h."""
        return (
            pull_day + timedelta(days=self.shift_lo),
            pull_day + timedelta(days=self.shift_hi),
        )

    def active_sports(self, pull_day: date) -> set[str]:
        lo, hi = self.game_window(pull_day)
        out: set[str] = set()
        for sport, seasons in self._seasons.items():
            blackouts = self._blackouts.get(sport, [])
            n = (hi - lo).days + 1
            for i in range(n):
                game_day = lo + timedelta(days=i)
                if any(b.contains(game_day) for b in blackouts):
                    continue
                if any(s.contains(game_day) for s in seasons):
                    out.add(sport)
                    break
        return out

    def _profile_key(self, sport: str) -> str:
        if sport.startswith("soccer_uefa_"):
            return UEFA_MIDWEEK_PROFILE
        if sport.startswith("soccer_"):
            return SOCCER_WEEKEND_PROFILE
        return sport

    def base_weight(self, pull_day: date) -> float:
        weekday = WEEKDAYS[pull_day.weekday()]
        total = 0.0
        for sport in self.active_sports(pull_day):
            profile = self._profiles.get(self._profile_key(sport))
            total += profile.get(weekday, 0.0) if profile else DEFAULT_SPORT_WEIGHT
        return total

    def cluster_weight(self, pull_day: date) -> float:
        hits = [w for c, w in self._clusters_full if c.contains(pull_day)]
        return max(hits) if hits else 1.0

    def deadzone_factor(self, pull_day: date) -> float:
        return 0.5 if any(z.contains(pull_day) for z in self.dead_zones) else 1.0

    def static_weight(self, pull_day: date) -> float:
        return (
            self.base_weight(pull_day)
            * self.cluster_weight(pull_day)
            * self.deadzone_factor(pull_day)
        )

    def active_effects(self, pull_day: date) -> list[str]:
        """Reason strings for clusters/dead zones in effect — the dashboard lists them."""
        out = [c.why for c, _ in self._clusters_full if c.contains(pull_day)]
        out += [z.why for z in self.dead_zones if z.contains(pull_day)]
        return [w for w in out if w]


@dataclass
class CadencePlan:
    day: date
    static_w: float
    dyn_count: int | None
    blended_w: float
    budget: float
    share: float
    slots_left: int
    breadth: float = 1.0
    weights: dict[date, float] = field(default_factory=dict)


def dynamic_counts(
    events: list[dict[str, object]],
    today: date,
    calendar: CadenceCalendar,
    mapped_teams: Callable[[str], set[str] | None],
) -> dict[date, int]:
    """Bucket mapped events into dyn(d) for d in [today, today+6]: a game counts
    toward d when its commence DATE falls in d's pull window [d+3, d+7].
    `mapped_teams(sport)` returns the set of odds-display names we can map, or
    None when the sport is unmapped; a game is mapped if either side maps."""
    counts = {today + timedelta(days=i): 0 for i in range(DYN_HORIZON_DAYS)}
    for event in events:
        sport = str(event.get("sport_key") or "")
        home = str(event.get("home_team") or "")
        away = str(event.get("away_team") or "")
        commence = str(event.get("commence_time") or "")
        if not sport or not commence:
            continue
        try:
            game_day = datetime.fromisoformat(commence.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        names = mapped_teams(sport)
        if names is None or (home not in names and away not in names):
            continue
        for d in counts:
            lo, hi = calendar.game_window(d)
            if lo <= game_day <= hi:
                counts[d] += 1
    return counts


def blend(static: float, dyn: int | None, median_dyn: float) -> float:
    """static x (0.5 + 0.5 x dyn/median); a day with zero mapped games still
    gets half its static share, and a degenerate median leaves static alone."""
    if dyn is None or median_dyn <= 0:
        return static
    return static * min(1.5, 0.5 + 0.5 * dyn / median_dyn)


def build_plan(
    now: datetime,
    credits_left: int,
    runs_per_day: float,
    calendar: CadenceCalendar,
    *,
    dyn: dict[date, int] | None = None,
    overrides: dict[date, float] | None = None,
) -> CadencePlan:
    """Today's cadence plan: weights for every remaining day, then today's
    budget share of what's actually left to spend."""
    today = _today(now)
    month_end = date(today.year, today.month, _days_in_month(today.year, today.month))
    remaining_days = [today + timedelta(days=i) for i in range((month_end - today).days + 1)]
    statics = {d: calendar.static_weight(d) for d in remaining_days}
    dyn_values = list(dyn.values()) if dyn else None
    median_dyn = statistics.median(dyn_values) if dyn_values else 0.0
    weights = {
        d: blend(statics[d], dyn.get(d) if dyn else None, median_dyn) for d in remaining_days
    }
    for day, weight in (overrides or {}).items():
        if day in weights:
            weights[day] = weight
    total = sum(weights.values())
    remaining = max(0.0, float(credits_left)) * (1 - RESERVE_FRACTION)
    # Nothing active anywhere: fall back to a flat day share.
    budget = remaining / len(remaining_days) if total <= 0 else remaining * weights[today] / total
    if calendar.active_sports(today):
        budget = max(budget, DAY_PULLS_FLOOR)
    budget = min(budget, DAY_PULLS_CAP)

    # Under-spend breadth rule: on/after the 20th, if the capped per-day
    # allocation cannot absorb what's left, pull a second feed per slot.
    breadth = 1.0
    if today.day >= UNDERSPEND_CHECK_DAY:
        allocatable = sum(
            min(
                DAY_PULLS_CAP,
                max(
                    DAY_PULLS_FLOOR if calendar.active_sports(d) else 0.0,
                    remaining * weights[d] / total
                    if total > 0
                    else remaining / len(remaining_days),
                ),
            )
            for d in remaining_days
        )
        month_spent = calendar.credits_per_month - credits_left
        projected = month_spent + min(float(credits_left), allocatable)
        if projected < (1 - UNDERSPEND_TOLERANCE) * calendar.credits_per_month:
            breadth = BREADTH_MULTIPLIER

    slots = slots_left_today(now, runs_per_day)
    return CadencePlan(
        day=today,
        static_w=statics[today],
        dyn_count=None if dyn is None else dyn.get(today),
        blended_w=weights[today],
        budget=budget,
        share=min(DAY_PULLS_CAP, budget * breadth) / slots,
        slots_left=slots,
        breadth=breadth,
        weights=weights,
    )
