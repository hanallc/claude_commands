"""Weekly usage rates and credit use from saved account readings."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import NotRequired, TypeGuard, TypedDict, cast


from scripts.lib import conf
from scripts.lib.conf import UseConfiguredValue
from scripts.whoami.note_file import NoResetKnown, NumberedAccountLabel, canonical_label

STATE = Path.home() / ".local/state/agent-notes"
READINGS_LOG = STATE / "readings.jsonl"
MAX_GAP = timedelta(minutes=15)


@dataclass(frozen=True, order=True)
class Reading:
    at: datetime
    used_percent: float


class StoredReading(TypedDict):
    account: str
    at: str
    remaining: NotRequired[float]
    credits: NotRequired[float]


@dataclass(frozen=True, order=True)
class CreditReading:
    at: datetime
    credits: float


@dataclass(frozen=True)
class CreditsUsed:
    used: float
    observed_from: datetime
    observed_to: datetime


@dataclass(frozen=True)
class NoCreditReading:
    """No numeric credit observation exists for this account by the cutoff."""


@dataclass(frozen=True)
class UsageUnknown:
    reason: str


def _reading_records(path: Path) -> list[StoredReading]:
    """Parse the shared observations once for either allowance or credits."""
    records: list[StoredReading] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return records
    for line in lines:
        try:
            item = cast(object, json.loads(line))
            if not isinstance(item, dict):
                continue
            value = cast(dict[str, object], item)
            if not isinstance(value.get("account"), str) or not isinstance(value.get("at"), str):
                continue
            at = datetime.fromisoformat(cast(str, value["at"]))
            if at.tzinfo is not None:
                record = StoredReading(account=canonical_label(cast(str, value["account"])), at=cast(str, value["at"]))
                if _numeric(value.get("remaining")):
                    record["remaining"] = cast(float, value["remaining"])
                if _numeric(value.get("credits")):
                    record["credits"] = cast(float, value["credits"])
                records.append(record)
        except (ValueError, TypeError):
            continue
    return records


def _numeric(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def read_readings(path: Path) -> dict[str, list[Reading]]:
    """Each account's readings in time order; a line that does not parse is skipped."""
    readings: dict[str, list[Reading]] = {}
    for record in _reading_records(path):
        remaining = record.get("remaining")
        if _numeric(remaining):
            readings.setdefault(record["account"], []).append(
                Reading(datetime.fromisoformat(record["at"]), 100 - cast(float, remaining)))
    return {account: sorted(found) for account, found in readings.items()}


def read_credit_readings(path: Path) -> dict[str, list[CreditReading]]:
    readings: dict[str, list[CreditReading]] = {}
    for record in _reading_records(path):
        credits = record.get("credits")
        if _numeric(credits) and cast(float, credits) >= 0:
            readings.setdefault(record["account"], []).append(
                CreditReading(datetime.fromisoformat(record["at"]), cast(float, credits)))
    return {account: sorted(found) for account, found in readings.items()}


def credits_used(readings: dict[str, list[CreditReading]], label: str, *,
                 since: datetime, until: datetime) -> CreditsUsed | NoCreditReading | UsageUnknown:
    available = sorted(item for item in readings.get(canonical_label(label), []) if item.at <= until)
    if not available:
        return NoCreditReading()
    before = [item for item in available if item.at <= since]
    start = before[-1] if before else available[0]
    observed = [item for item in available if item.at >= start.at]
    newest = observed[-1]
    if newest.at < until - timedelta(minutes=10):
        return UsageUnknown("the newest credit reading is older than 10 minutes")
    if newest.at - max(start.at, since) < timedelta(minutes=30):
        return UsageUnknown("credit observations cover less than 30 minutes of the hour")
    used = sum(max(0, old.credits - new.credits) for old, new in zip(observed, observed[1:]))
    return CreditsUsed(used, start.at, newest.at)


@dataclass(frozen=True)
class UsageRate:
    points_per_hour: float


@dataclass(frozen=True)
class NoUsageRate:
    """The readings do not support a positive usage rate."""


@dataclass(frozen=True)
class ObservedUsage:
    """The usage rise and duration of the spans that count."""

    points: float
    duration: timedelta
    first_reading_at: datetime
    last_reading_at: datetime

    @property
    def points_per_hour(self) -> float:
        return self.points * 3600 / self.duration.total_seconds()


def _observed_usage(readings: list[Reading], cutoff: datetime) -> ObservedUsage | NoUsageRate:
    points = 0.0
    duration = timedelta()
    first_reading_at = last_reading_at = cutoff
    for before, after in zip(readings, readings[1:]):
        span = after.at - before.at
        if (span > MAX_GAP or before.used_percent >= 100
                or after.used_percent < before.used_percent or before.used_percent == after.used_percent == 0):
            continue
        observed = after.at - max(before.at, cutoff)
        points += (after.used_percent - before.used_percent) * (observed / span)
        if not duration:
            first_reading_at = before.at
        duration += observed
        last_reading_at = after.at
    if not duration or not math.isfinite(points):
        return NoUsageRate()
    return ObservedUsage(points, duration, first_reading_at, last_reading_at)


def _whole_window_usage(readings: list[Reading], now: datetime, lookback: timedelta) -> ObservedUsage | NoUsageRate:
    cutoff = now - lookback
    start_reading: Reading | None = None
    by_time: dict[datetime, Reading] = {}
    for reading in readings:
        if reading.at <= cutoff:
            if start_reading is None or reading.at >= start_reading.at:
                start_reading = reading
        elif reading.at <= now:
            by_time[reading.at] = reading
    if start_reading is None:
        return NoUsageRate()

    recent = [start_reading, *sorted(by_time.values())]
    if now - recent[-1].at > MAX_GAP:
        return NoUsageRate()
    usage = _observed_usage(recent, cutoff)
    return usage if isinstance(usage, ObservedUsage) and usage.points > 0 else NoUsageRate()


def hourly_usage_rate(readings: list[Reading], now: datetime, *,
                      account_config: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT
                      ) -> UsageRate | NoUsageRate:
    """Divide the usage rise by the observed hours in the lookback window."""
    if isinstance(account_config, UseConfiguredValue):
        account_config = conf.read("accounts")
    lookback = timedelta(minutes=account_config.whole("rate.lookback_minutes"))
    usage = _whole_window_usage(readings, now, lookback)
    return UsageRate(usage.points_per_hour) if isinstance(usage, ObservedUsage) else usage


@dataclass(frozen=True)
class UnknownMoveSource:
    """The records do not identify the account that the role left."""


@dataclass(frozen=True)
class RoleArrival:
    role: str
    source: NumberedAccountLabel | UnknownMoveSource
    destination: NumberedAccountLabel
    at: datetime


@dataclass(frozen=True)
class LastKnownRoleRate:
    role: str
    account: NumberedAccountLabel
    points_per_hour: float
    until: datetime


@dataclass(frozen=True)
class WholeWindowRate:
    usage: ObservedUsage

    @property
    def points_per_hour(self) -> float:
        return self.usage.points_per_hour


@dataclass(frozen=True)
class LastKnownRate:
    sources: tuple[LastKnownRoleRate, ...]

    @property
    def points_per_hour(self) -> float:
        return sum(source.points_per_hour for source in self.sources)


@dataclass(frozen=True)
class PartialWindowRate:
    since: datetime
    usage: ObservedUsage
    lookback: timedelta
    prior: LastKnownRate | NoUsageRate

    @property
    def points_per_hour(self) -> float:
        measured = self.usage.points_per_hour
        if isinstance(self.prior, NoUsageRate):
            return measured
        covered = self.usage.duration / self.lookback
        return covered * measured + (1 - covered) * self.prior.points_per_hour


PageUsageRate = WholeWindowRate | PartialWindowRate | LastKnownRate | NoUsageRate
WEEK = timedelta(weeks=1)


@dataclass(frozen=True)
class WeeklyReadingUnavailable:
    """The account status has no weekly balance with a time."""


def is_current_weekly_reading(current: Reading | WeeklyReadingUnavailable, now: datetime,
                              reset: datetime | NoResetKnown) -> TypeGuard[Reading]:
    return (isinstance(current, Reading) and now - MAX_GAP <= current.at <= now
            and 0 <= current.used_percent <= 100
            and (isinstance(reset, NoResetKnown) or reset - WEEK <= current.at and now < reset))


def _weekly_window_start(at: datetime, reset: datetime) -> datetime:
    return at - (at - reset) % WEEK


def _rate_before_move(arrival: RoleArrival, histories: Mapping[str, list[Reading]],
                      resets: Mapping[str, datetime], lookback: timedelta) -> LastKnownRoleRate | NoUsageRate:
    source = arrival.source
    if not isinstance(source, NumberedAccountLabel) or source.tool != arrival.destination.tool:
        return NoUsageRate()
    readings = histories.get(source.label, [])
    if reset := resets.get(source.label):
        start = _weekly_window_start(arrival.at, reset)
        readings = [reading for reading in readings if start <= reading.at < start + WEEK]
    usage = _whole_window_usage(readings, arrival.at, lookback)
    if isinstance(usage, NoUsageRate):
        return usage
    return LastKnownRoleRate(arrival.role, source, usage.points_per_hour, arrival.at)


def page_usage_rate(label: str, current: Reading | WeeklyReadingUnavailable, now: datetime, *, lookback: timedelta,
                    histories: Mapping[str, list[Reading]], current_reset: datetime | NoResetKnown,
                    source_resets: Mapping[str, datetime],
                    arrivals: Sequence[RoleArrival]) -> PageUsageRate:
    """Estimate the page rate from this account and the roles that moved onto it."""
    cutoff = now - lookback
    recent = [arrival for arrival in arrivals if cutoff < arrival.at <= now]
    sources = tuple(rate for arrival in recent
                    if isinstance(rate := _rate_before_move(arrival, histories, source_resets, lookback), LastKnownRoleRate))
    prior = LastKnownRate(sources) if sources else NoUsageRate()
    reset = current_reset if isinstance(current_reset, datetime) else source_resets.get(label, NoResetKnown())
    stored = histories.get(label, [])
    since = cutoff
    if isinstance(reset, datetime):
        since = reset - WEEK if reset > now else _weekly_window_start(now, reset)
        stored = [reading for reading in stored if reading.at >= since]
    since = max(since, max((arrival.at for arrival in recent), default=since))
    if since <= cutoff:
        if not is_current_weekly_reading(current, now, reset):
            return prior
        if not any(now - MAX_GAP <= reading.at <= current.at for reading in stored):
            return NoUsageRate()
        readings = [reading for reading in stored if reading.at < current.at] + [current]
        usage = _whole_window_usage(readings, now, lookback)
        return WholeWindowRate(usage) if isinstance(usage, ObservedUsage) else usage

    partial_readings = {reading.at: reading for reading in stored
                        if since <= reading.at <= now and 0 <= reading.used_percent <= 100}
    if isinstance(current, Reading) and since <= current.at <= now and 0 <= current.used_percent <= 100:
        partial_readings[current.at] = current
    partial = _observed_usage(sorted(partial_readings.values()), since)
    if isinstance(partial, ObservedUsage) and (partial.points > 0 or isinstance(prior, LastKnownRate)):
        return PartialWindowRate(since, partial, lookback, prior)
    return prior


@dataclass(frozen=True)
class WeeklyRunOutAt:
    at: datetime


class WeeklyRunOutWithoutTime(Enum):
    NO_USAGE_RATE = auto()
    NO_CURRENT_BALANCE = auto()
    ALREADY_EMPTY = auto()
    NO_RESET_TIME = auto()
    LASTS_UNTIL_RESET = auto()


WeeklyRunOut = WeeklyRunOutAt | WeeklyRunOutWithoutTime


def weekly_run_out(rate: PageUsageRate, reset: datetime | NoResetKnown,
                   current: Reading | WeeklyReadingUnavailable, now: datetime) -> WeeklyRunOut:
    """Estimate when the weekly balance runs out at the displayed rate."""
    if not is_current_weekly_reading(current, now, reset):
        if isinstance(rate, NoUsageRate):
            return WeeklyRunOutWithoutTime.NO_USAGE_RATE
        return WeeklyRunOutWithoutTime.NO_CURRENT_BALANCE
    if current.used_percent == 100:
        return WeeklyRunOutWithoutTime.ALREADY_EMPTY
    if isinstance(rate, NoUsageRate):
        return WeeklyRunOutWithoutTime.NO_USAGE_RATE
    if isinstance(reset, NoResetKnown):
        return WeeklyRunOutWithoutTime.NO_RESET_TIME
    points = rate.points_per_hour
    hours = (100 - current.used_percent) / points if points > 0 else float("inf")
    if hours >= (reset - now).total_seconds() / 3600:
        return WeeklyRunOutWithoutTime.LASTS_UNTIL_RESET
    return WeeklyRunOutAt(now + timedelta(hours=hours))
