"""Information-release calendar for odds pulls (v5 Phase 3-E).

Odds pulls are worth most right after the sharp books reprice on scheduled news:
injury and status reports, inactives, lineups, goalie confirmations. The calendar says
when that news lands, per feed, in `America/New_York` local time:

- `clock` entries fire at a local weekday and time (NFL status reports at 16:00 ET,
  so a pull at 16:20). They are feed-level: one pull covers the whole slate.
- `kickoff` entries fire a fixed number of minutes before each game's kickoff (EPL
  lineups 75 minutes out, so a pull at -70). Games of one feed that share a target
  time share one pull.

Priorities use the brief's 1-5 scale (5 highest); `pull_plan` stores `6 - priority`
so its existing "1 is first" ordering is unchanged. Every local time goes through
`zoneinfo`, never a fixed UTC offset, so DST changes need no special handling.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from prediction_bot.odds import KALSHI_SERIES_TO_SPORT

DEFAULT_RELEASE_CALENDAR_PATH = Path("data/release_calendar.json")

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun", "daily"]
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
Priority = Annotated[int, Field(ge=1, le=5)]


def plan_priority(priority: int) -> int:
    """Brief scale (5 = highest) -> `pull_plan.priority` (1 = first)."""
    return 6 - priority


def _parse_hhmm(value: str) -> dtime:
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return dtime(hour, minute)
    except (ValueError, TypeError):
        raise ValueError(f"time must be HH:MM, got {value!r}") from None


class ClockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["clock"]
    days: list[Weekday] = Field(min_length=1)
    time: str
    priority: Priority
    max_per_day: int | None = Field(default=None, ge=1)
    # Plan the pull only while the feed has a known game kicking off within this many
    # hours after it, so off-weeks and the off-season cost nothing.
    requires_game_within_hours: int = Field(default=168, ge=1)
    note: str = ""

    @field_validator("time")
    @classmethod
    def _time(cls, value: str) -> str:
        _parse_hhmm(value)
        return value

    @property
    def local_time(self) -> dtime:
        return _parse_hhmm(self.time)

    def on(self, day: date) -> bool:
        return "daily" in self.days or WEEKDAYS[day.weekday()] in self.days

    @property
    def key(self) -> str:
        return f"clock:{','.join(self.days)}@{self.time}"


class KickoffEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["kickoff"]
    minutes_before: int = Field(ge=3, le=24 * 60)
    priority: Priority
    max_per_day: int | None = Field(default=None, ge=1)
    note: str = ""

    @property
    def target(self) -> str:
        return f"K-{self.minutes_before}m"

    @property
    def key(self) -> str:
        return f"kickoff:-{self.minutes_before}"


Entry = Annotated[ClockEntry | KickoffEntry, Field(discriminator="kind")]


class FeedCalendar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    note: str = ""
    entries: list[Entry] = Field(min_length=1)


class ReleaseCalendar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = "America/New_York"
    # Priority of the T-10m sharp close (used by `clv_sharp_close`) on every feed.
    close_priority: Priority = 5
    # Priority of the Phase 3 T-24h/T-6h/T-90m targets on feeds without entries.
    default_priority: Priority = 2
    # ODDS_GRID=fallback: one pull at this local time for an eligible feed that has no
    # other target that local day.
    grid_time: str = "15:00"
    grid_priority: Priority = 2
    # Most distinct soccer feeds the plan funds per local Saturday and Sunday
    # (0 = no cap). Feeds are chosen by number of games that day.
    soccer_weekend_feed_cap: int = Field(default=2, ge=0)
    feeds: dict[str, FeedCalendar]

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {value!r}") from None
        return value

    @field_validator("grid_time")
    @classmethod
    def _grid_time(cls, value: str) -> str:
        _parse_hhmm(value)
        return value

    @model_validator(mode="after")
    def _known_feeds(self) -> ReleaseCalendar:
        known = set(KALSHI_SERIES_TO_SPORT.values())
        unknown = sorted(set(self.feeds) - known)
        if unknown:
            raise ValueError(f"unknown feeds in release calendar: {unknown}")
        return self

    @classmethod
    def load(
        cls, path: Path | str = DEFAULT_RELEASE_CALENDAR_PATH, enable: Iterable[str] = ()
    ) -> ReleaseCalendar:
        """Parse and validate the calendar; `enable` switches on disabled feeds."""
        raw = json.loads(Path(path).read_text())
        calendar = cls.model_validate({k: v for k, v in raw.items() if not k.startswith("_")})
        for feed in enable:
            if feed in calendar.feeds:
                calendar.feeds[feed].enabled = True
        return calendar

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def local_date(self, at: datetime) -> date:
        return _utc(at).astimezone(self.zone).date()

    def local_at(self, day: date, at: dtime) -> datetime:
        """The UTC instant of local wall time `at` on `day` (zoneinfo handles DST)."""
        return datetime.combine(day, at, tzinfo=self.zone).astimezone(timezone.utc)

    def entries(self, feed: str) -> list[ClockEntry | KickoffEntry]:
        cal = self.feeds.get(feed)
        return list(cal.entries) if cal is not None and cal.enabled else []

    def has_entries(self, feed: str) -> bool:
        return bool(self.entries(feed))

    def kickoff_entries(self, feed: str) -> list[KickoffEntry]:
        return [e for e in self.entries(feed) if isinstance(e, KickoffEntry)]

    def clock_entries(self, feed: str) -> list[ClockEntry]:
        return [e for e in self.entries(feed) if isinstance(e, ClockEntry)]

    def clock_targets(self, feed: str, start: datetime, end: datetime) -> Iterator[ClockTarget]:
        """Every clock target of `feed` with `start <= target_at < end` (UTC)."""
        start, end = _utc(start), _utc(end)
        day = self.local_date(start) - timedelta(days=1)
        last = self.local_date(end) + timedelta(days=1)
        while day <= last:
            for entry in self.clock_entries(feed):
                if not entry.on(day):
                    continue
                at = self.local_at(day, entry.local_time)
                if start <= at < end:
                    yield ClockTarget(feed, at, day, entry)
            day += timedelta(days=1)

    def grid_at(self, day: date) -> datetime:
        return self.local_at(day, _parse_hhmm(self.grid_time))


@dataclass(frozen=True)
class ClockTarget:
    feed: str
    target_at: datetime
    local_date: date
    entry: ClockEntry

    @property
    def name(self) -> str:
        return f"R-{self.local_date.isoformat()}T{self.entry.time}"


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
