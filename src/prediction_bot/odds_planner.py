from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from prediction_bot.odds import per_run_budget

EVENT_DRIVEN_RUNS_PER_DAY = 3
ENTRY_WINDOW = timedelta(hours=72)


@dataclass(frozen=True)
class PlannedGame:
    kickoff: datetime
    large_gap_pending: bool = False
    event_ticker: str = ""


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _priority(game: PlannedGame, now: datetime) -> int | None:
    if game.large_gap_pending:
        return 0
    lead = _utc(game.kickoff) - now
    if timedelta(hours=1) <= lead <= timedelta(hours=2):
        return 1
    if timedelta(hours=5) <= lead <= timedelta(hours=7):
        return 2
    if timedelta(hours=22) <= lead <= timedelta(hours=26):
        return 3
    return None


def plan_odds_pulls(
    games_by_sport: Mapping[str, Sequence[PlannedGame]],
    now: datetime,
    remaining_credits: int,
    days_left_in_month: int,
    runs_per_day: float = EVENT_DRIVEN_RUNS_PER_DAY,
) -> list[str]:
    """Select high-value sports feeds while respecting the monthly request share."""
    now = _utc(now)
    budget = per_run_budget(
        remaining_credits,
        runs_per_day,
        now,
        days_left_in_month,
    )
    if budget <= 0:
        return []

    urgent: list[tuple[int, datetime, str]] = []
    entry: list[tuple[datetime, str]] = []
    for sport, games in games_by_sport.items():
        sport_priorities = [
            (priority, _utc(game.kickoff))
            for game in games
            if (priority := _priority(game, now)) is not None
        ]
        if sport_priorities:
            priority, kickoff = min(sport_priorities)
            urgent.append((priority, kickoff, sport))
        elif any(_utc(game.kickoff) - now >= ENTRY_WINDOW for game in games):
            entry.append((min(_utc(game.kickoff) for game in games), sport))

    candidates = (
        [sport for _priority_rank, _kickoff, sport in sorted(urgent)]
        if urgent
        else [sport for _kickoff, sport in sorted(entry)]
    )
    return candidates[:budget]
