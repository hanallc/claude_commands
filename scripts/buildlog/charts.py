"""Read data for the shared build page."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from scripts.lib import conf
from scripts.buildlog import cost_actions, costs, index
from scripts.lib.user_zone import user_zone
from scripts.buildlog.costs import CostUnavailable as CostUnavailable


@dataclass(frozen=True)
class MeasuredCost:
    seconds: float


@dataclass(frozen=True)
class MacIdleUpperBound:
    known_seconds: float
    unknown_seconds: float


class NoDataYet(StrEnum):
    VALUE = "no data yet"


type ChartCost = MeasuredCost | MacIdleUpperBound | CostUnavailable | NoDataYet

@dataclass(frozen=True)
class DailyBucket:
    start: datetime
    end: datetime
    costs: costs.BuildCosts
    observed_machines: frozenset[str]
    first_sources: dict[str, float]


@dataclass(frozen=True)
class WindowMatchesRecent:
    pass


@dataclass(frozen=True)
class WindowDiffers:
    value: ChartCost


type WindowValue = WindowMatchesRecent | WindowDiffers


@dataclass(frozen=True)
class TopCost:
    machine: str
    cause: costs.Cause
    recent: ChartCost
    window: WindowValue
    # One value for each whole day of the recent window, oldest first.
    days: tuple[ChartCost, ...]
    share: cost_actions.LargestShare | cost_actions.NoShare
    action: cost_actions.CostAction


@dataclass(frozen=True)
class TopCosts:
    start: datetime
    end: datetime
    recent_days: int
    rows: tuple[TopCost, ...] | NoDataYet


@dataclass(frozen=True)
class PageData:
    top: TopCosts


@dataclass(frozen=True)
class IndexUnavailable:
    reason: str


def with_first_sources(reading: costs.CostReading) -> costs.CostReading:
    """Include the first observation when the source history has no earlier value."""
    first_sources = dict(reading.first_sources)
    for observation in reading.observations:
        first_sources[observation.machine] = min(first_sources.get(observation.machine, observation.at), observation.at)
    return replace(reading, first_sources=first_sources)


def build_bucket(reading: costs.CostReading, start: datetime, end: datetime) -> DailyBucket:
    """Use the same coverage and cost rules for each window."""
    low, high = start.timestamp(), end.timestamp()
    observed = frozenset(item.machine for item in reading.observations if low <= item.at < high)
    covered = frozenset(item.machine for item in reading.coverage if item.start <= low and item.end >= high)
    return DailyBucket(start, end, costs.aggregate_costs(reading, start, end), observed | covered, reading.first_sources)


def read_snapshot(connection: sqlite3.Connection, start: datetime, end: datetime) -> costs.CostReading:
    """Read costs in one transaction before any chart calculation."""
    _ = connection.execute("BEGIN")
    try:
        return costs.read_costs(connection, start, end, boundary_context=True)
    finally:
        _ = connection.execute("ROLLBACK")


def read_index[T](read: Callable[[sqlite3.Connection], T]) -> T | IndexUnavailable:
    """Close the index after the read and preserve an index error as data."""
    try:
        with closing(index.read_only()) as connection:
            return read(connection)
    except sqlite3.Error as error:
        return IndexUnavailable(str(error))


def cost_value(bucket: DailyBucket, machine: str, cause: costs.Cause) -> ChartCost:
    idle = bucket.costs.mac_idle
    idle_wait = cause == "Mac idle while natedev waits" and machine == "mac" and idle.candidate_wait_seconds > 0
    if bucket.first_sources.get(machine, bucket.end.timestamp()) >= bucket.end.timestamp() and not idle_wait:
        return NoDataYet.VALUE
    if cause == "Mac idle while natedev waits" and machine == "mac":
        if idle_wait:
            return MacIdleUpperBound(idle.upper_bound_seconds-idle.unknown_seconds, idle.unknown_seconds)
        if "natedev" not in bucket.observed_machines:
            return CostUnavailable("no recorded candidate coverage")
    elif machine not in bucket.observed_machines:
        return CostUnavailable("no recorded source coverage")
    missing = sum(row.missing_waits for row in bucket.costs.ranked if row.machine == machine and row.cause == cause)
    if missing:
        return CostUnavailable(f"{missing} applicable waits missing")
    return MeasuredCost(sum(row.seconds for row in bucket.costs.ranked if row.machine == machine and row.cause == cause))


def known_seconds(value: ChartCost) -> float | None:
    """Return the measured part of a cost when that part is available."""
    if isinstance(value, MeasuredCost):
        return value.seconds
    if isinstance(value, MacIdleUpperBound):
        return value.known_seconds
    return None


@dataclass(frozen=True)
class DailyRange:
    """The lowest and highest day of a cost, in hours."""
    low_hours: float
    high_hours: float


class RangeUnavailable(StrEnum):
    """A day is not fully measured, so a range would hide the unknown part."""
    VALUE = "range unavailable"


def daily_range(days: tuple[ChartCost, ...]) -> DailyRange | RangeUnavailable:
    """The range of the days, only when every day is measured."""
    measured = [day.seconds for day in days if isinstance(day, MeasuredCost)]
    if not measured or len(measured) != len(days):
        return RangeUnavailable.VALUE
    return DailyRange(min(measured) / 3600, max(measured) / 3600)


def window_value(recent: ChartCost, window: ChartCost, recent_days: int, days: int) -> WindowValue:
    """Compare daily costs and preserve unavailable comparison values."""
    recent_seconds = known_seconds(recent)
    if recent_seconds is None:
        return WindowMatchesRecent()
    window_seconds = known_seconds(window)
    if window_seconds is None:
        return WindowDiffers(window)
    recent_seconds /= recent_days
    window_seconds /= days
    if abs(window_seconds - recent_seconds) <= 0.5 * recent_seconds:
        return WindowMatchesRecent()
    value = (MacIdleUpperBound(window_seconds, window.unknown_seconds / days)
             if isinstance(window, MacIdleUpperBound) else MeasuredCost(window_seconds))
    return WindowDiffers(value)


def page_data(days: int, n: int = 3) -> PageData | IndexUnavailable:
    """Read the recent costs from one index snapshot."""

    settings = conf.read("buildlog")
    recent_days = settings.whole("TOP_COST_RECENT_DAYS")
    if recent_days > days:
        raise conf.Unusable(f"TOP_COST_RECENT_DAYS={recent_days} exceeds the page days ({days})")
    zone = user_zone()
    end = datetime.now(zone)
    start = end - timedelta(days=days)
    recent_start = end - timedelta(days=recent_days)

    def collect(connection: sqlite3.Connection) -> PageData:
        reading = with_first_sources(read_snapshot(connection, start, end))
        recent = build_bucket(reading, recent_start, end)
        window = build_bucket(reading, start, end)
        slices = [build_bucket(reading, recent_start + timedelta(days=day), recent_start + timedelta(days=day + 1))
                  for day in range(recent_days)]
        rows: list[TopCost] = []
        for row in recent.costs.ranked[:max(0, n)]:
            value = cost_value(recent, row.machine, row.cause)
            comparison = window_value(value, cost_value(window, row.machine, row.cause), recent_days, days)
            rows.append(TopCost(row.machine, row.cause, value, comparison,
                                tuple(cost_value(day, row.machine, row.cause) for day in slices),
                                cost_actions.largest_share(reading, row.machine, row.cause, recent_start, end),
                                cost_actions.cost_action(connection, reading, row.machine, row.cause, recent_start, end)))
        top = TopCosts(recent_start, end, recent_days, tuple(rows) if rows else NoDataYet.VALUE)
        return PageData(top)

    return read_index(collect)


def cost_json(value: ChartCost, divisor: float, *, machine: str = "") -> dict[str, object]:
    if isinstance(value, NoDataYet):
        return {"state": value.value}
    if isinstance(value, CostUnavailable):
        reason = value.reason
        if machine and reason == "no recorded source coverage":
            reason = f"no complete indexed {machine} monthly log covers this day"
        elif reason == "no recorded candidate coverage":
            reason = "no complete indexed natedev monthly log covers candidate waits"
        return {"state": "unavailable", "reason": reason}
    if isinstance(value, MacIdleUpperBound):
        return {"state": "upper bound", "value": value.known_seconds/divisor,
                "unknown": value.unknown_seconds/divisor, "upper": (value.known_seconds+value.unknown_seconds)/divisor}
    return {"state": "measured", "value": value.seconds/divisor}
