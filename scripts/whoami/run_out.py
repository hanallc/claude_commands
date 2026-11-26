"""Weekly usage rates and credit use from saved account readings."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import NotRequired, TypedDict, cast


from scripts.lib import conf
from scripts.lib.conf import UseConfiguredValue
from scripts.whoami.note_file import canonical_label

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
    """The readings do not show a rise from the start of the window."""


def hourly_usage_rate(readings: list[Reading], now: datetime, *,
                      account_config: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT
                      ) -> UsageRate | NoUsageRate:
    """Measure the observed rise from the cutoff to the last recent reading."""
    if isinstance(account_config, UseConfiguredValue):
        account_config = conf.read("accounts")
    lookback = timedelta(minutes=account_config.whole("rate.lookback_minutes"))
    cutoff = now - lookback
    earliest = cutoff - MAX_GAP
    by_time = {reading.at: reading for reading in readings if earliest <= reading.at <= now}
    recent = sorted(by_time.values())
    if not recent or recent[0].at > cutoff or now - recent[-1].at > MAX_GAP:
        return NoUsageRate()

    points = 0.0
    for before, after in zip(recent, recent[1:]):
        if after.at <= cutoff:
            continue
        span = after.at - before.at
        if span > MAX_GAP or before.used_percent >= 100 or after.used_percent < before.used_percent:
            return NoUsageRate()
        observed = after.at - max(before.at, cutoff)
        points += (after.used_percent - before.used_percent) * (observed / span)
    if not math.isfinite(points) or points <= 0:
        return NoUsageRate()
    return UsageRate(points * 3600 / (recent[-1].at - cutoff).total_seconds())
