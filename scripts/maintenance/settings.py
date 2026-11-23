"""Runtime schedules and deadlines for the independent agent-notes parts."""
from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import cast


class Part(str, Enum):
    NOTES = "notes"
    QUOTA = "quota"
    PACER = "pacer"
    REFUSALS = "refusals"
    WATCHDOG = "watchdog"
    LOGINS = "logins"
    PRUNE = "prune"
    FOLDER_SIZE = "folder_size"
    VAULT = "vault"
    VAULT_SAVE = "vault_save"


@dataclass(frozen=True)
class Schedule:
    interval_seconds: float
    timeout_seconds: float


@dataclass(frozen=True)
class InvalidPartSchedule:
    reason: str


@dataclass(frozen=True)
class UsageIntervals:
    claude_seconds: float
    codex_seconds: float


@dataclass(frozen=True)
class Settings:
    job: Schedule
    parts: dict[Part, Schedule | InvalidPartSchedule]
    vault_git_seconds: float
    vault_named_paths: int
    dispatch_seconds: float
    recovery_seconds: float
    recovery_dispatch_seconds: float
    recovery_result_seconds: float


def table(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("agent-notes config: expected a table")
    return cast(dict[str, object], value)


def positive(values: dict[str, object], name: str) -> float:
    value = values.get(name)
    if isinstance(value, bool) or not isinstance(value, (float, int)) or value <= 0 or not math.isfinite(value):
        raise ValueError(f"agent-notes config: {name} must be positive")
    return float(value)


def schedule(value: object) -> Schedule:
    values = table(value)
    return Schedule(positive(values, "interval_seconds"), positive(values, "timeout_seconds"))


def part_schedule(part: Part, value: object) -> Schedule | InvalidPartSchedule:
    try:
        cadence = schedule(value)
        if part is Part.VAULT_SAVE and not 60 <= cadence.interval_seconds <= 3600:
            raise ValueError("parts.vault_save.interval_seconds must be from 60 through 3600")
        return cadence
    except (ValueError, OverflowError) as error:
        return InvalidPartSchedule(f"Invalid [parts.{part.value}] schedule. {error}")


def configuration_path() -> Path:
    return Path(os.environ.get("AGENT_NOTES_JOB_CONFIG", str(Path(__file__).resolve().parents[2] / "config/agent_notes.toml")))


def configuration() -> dict[str, object]:
    return table(tomllib.loads(configuration_path().read_text(encoding="utf-8")))


def part_cadence(part: Part) -> Schedule | InvalidPartSchedule:
    """Read one part's schedule and do not check the other settings."""
    return part_schedule(part, table(configuration().get("parts", {})).get(part.value))


def usage_only_intervals() -> UsageIntervals:
    """Read both Mac usage intervals from one configuration."""
    key = "interval_seconds"
    try:
        values = table(configuration().get("mac_usage"))
        claude_seconds = positive(values, key)
        key = "codex_interval_seconds"
        return UsageIntervals(claude_seconds, positive(values, key))
    except (OSError, ValueError, OverflowError) as error:
        raise ValueError(f"{configuration_path()}: mac_usage.{key} must be a finite positive number") from error


def settings() -> Settings:
    values = configuration()
    parts = table(values.get("parts", {}))
    vault = table(values.get("vault"))
    named = vault.get("named_paths")
    if isinstance(named, bool) or not isinstance(named, int) or named <= 0:
        raise ValueError("agent-notes config: named_paths must be a positive integer")
    return Settings(schedule(values.get("job")), {part: part_schedule(part, parts.get(part.value)) for part in Part},
                    positive(vault, "git_seconds"), named, positive(table(values.get("watchdog")), "dispatch_seconds"),
                    positive(table(values.get("watchdog")), "recovery_seconds"),
                    positive(table(values.get("watchdog")), "pass_dispatch_seconds"),
                    positive(table(values.get("watchdog")), "result_seconds"))
