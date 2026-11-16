"""Name what cuts a top build cost, only from the rows behind that cost."""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast

from scripts.buildlog import costs, index, moments
from scripts.buildlog.costs import StepKind

GIB = 1 << 30


@dataclass(frozen=True)
class LargestShare:
    """The step kind that paid the largest part of one cost."""
    kind: StepKind
    share: float


@dataclass(frozen=True)
class LowerMemoryPeak:
    """The step kind that held the most build memory over time on the machine."""
    kind: StepKind
    gib_hours_per_day: float
    peak_p90_bytes: int


@dataclass(frozen=True)
class FixTest:
    """The test that failed most often on the machine."""
    repo: str
    test: str
    failures: int


@dataclass(frozen=True)
class FixStep:
    """The step kind that failed most often, when no failed test was recorded."""
    kind: StepKind
    failures: int


class NoShare(StrEnum):
    """No build step paid a known part of the cost."""
    VALUE = "no share"


class NoActionKnown(StrEnum):
    VALUE = "no action known"


type CostAction = LowerMemoryPeak | FixTest | FixStep | NoActionKnown


def largest_share(reading: costs.CostReading, machine: str, cause: costs.Cause,
                  start: datetime, end: datetime) -> LargestShare | NoShare:
    """The step kind with the most seconds of one cost."""
    lower, upper = start.timestamp(), end.timestamp()
    seconds: defaultdict[StepKind, float] = defaultdict(float)
    for row in reading.contributions:
        if (row.machine == machine and row.cause == cause and isinstance(row.kind, StepKind)
                and lower <= row.at < upper and not isinstance(row.seconds, costs.CostUnavailable)):
            seconds[row.kind] += row.seconds
    total = sum(seconds.values())
    if total <= 0:
        return NoShare.VALUE
    kind = min(seconds, key=lambda key: (-seconds[key], key.name))
    return LargestShare(kind, seconds[kind] / total)


def memory_holder(connection: sqlite3.Connection, machine: str, start: datetime,
                  end: datetime) -> LowerMemoryPeak | NoActionKnown:
    """The step kind with the largest memory peak times run time, from steps with a recorded peak."""
    rows = cast(list[tuple[str | None, str | None, str | None, str | None, str, int, float]], connection.execute(
        "SELECT target, host, repo, repo_identity, step, peak_mem_bytes, duration_s FROM steps WHERE "
        + moments.window_bounds("started_at") + " AND peak_mem_bytes > 0 AND duration_s > 0",
        moments.window_parameters(start, end)).fetchall())
    legacy_hosts: dict[str, str] = {}
    held: defaultdict[StepKind, float] = defaultdict(float)
    peaks: defaultdict[StepKind, list[int]] = defaultdict(list)
    for target, host, repo, identity, step, peak, duration in rows:
        if costs.machine_name(target, host, legacy_hosts) != machine:
            continue
        kind = StepKind(costs.repository_name(repo, identity), step)
        held[kind] += peak / GIB * duration / 3600
        peaks[kind].append(peak)
    if not held:
        return NoActionKnown.VALUE
    kind = min(held, key=lambda key: (-held[key], key.name))
    days = (end - start).total_seconds() / 86400
    return LowerMemoryPeak(kind, held[kind] / days, int(moments.nearest_rank([float(peak) for peak in peaks[kind]], 90)))


def failing_test(connection: sqlite3.Connection, machine: str, start: datetime, end: datetime) -> FixTest | NoActionKnown:
    """The test with the most failed or timed out runs on the machine, from counted failed steps only."""
    rows = cast(list[tuple[str | None, str | None, str | None, str | None, str]], connection.execute(
        "SELECT s.target, s.host, s.repo, s.repo_identity, t.test FROM tests t JOIN steps s ON s.id = t.step_id"
        + " WHERE t.status IN ('failed', 'timed_out') AND s.id IN (SELECT id FROM steps WHERE "
        + moments.window_bounds("started_at") + " AND " + index.COUNTED_FAILURE + ")",
        moments.window_parameters(start, end)).fetchall())
    legacy_hosts: dict[str, str] = {}
    failures = Counter((costs.repository_name(repo, identity), test) for target, host, repo, identity, test in rows
                       if costs.machine_name(target, host, legacy_hosts) == machine)
    if not failures:
        return NoActionKnown.VALUE
    (repo, test), count = min(failures.items(), key=lambda item: (-item[1], item[0]))
    return FixTest(repo, test, count)


def failing_step(reading: costs.CostReading, machine: str, start: datetime, end: datetime) -> FixStep | NoActionKnown:
    """The step kind with the most counted failures on the machine."""
    lower, upper = start.timestamp(), end.timestamp()
    failures = Counter(row.kind for row in reading.contributions
                       if row.machine == machine and row.cause == "Failed runs" and isinstance(row.kind, StepKind)
                       and lower <= row.at < upper)
    if not failures:
        return NoActionKnown.VALUE
    kind, count = min(failures.items(), key=lambda item: (-item[1], item[0].name))
    return FixStep(kind, count)


def cost_action(connection: sqlite3.Connection, reading: costs.CostReading, machine: str, cause: costs.Cause,
                start: datetime, end: datetime) -> CostAction:
    """Name an action only where the rows name one: memory holders and failed tests or steps."""
    if cause == "Memory wait":
        return memory_holder(connection, machine, start, end)
    if cause == "Failed runs":
        test = failing_test(connection, machine, start, end)
        return failing_step(reading, machine, start, end) if isinstance(test, NoActionKnown) else test
    return NoActionKnown.VALUE


def action_text(action: CostAction) -> str:
    """One plain sentence for the page."""
    match action:
        case LowerMemoryPeak(kind, gib_hours, peak):
            return (f"Action: lower the memory peak of {kind.name}. It holds the most build memory:"
                    f" {gib_hours:.0f} GiB·h/day, peak p90 {peak / GIB:.1f} GiB.")
        case FixTest(repo, test, failures):
            where = f" in {repo}" if repo else ""
            return f"Action: fix the test {test}{where}. It failed {failures} {'time' if failures == 1 else 'times'}."
        case FixStep(kind, failures):
            return f"Action: fix the failing {kind.name} runs. {failures} {'run' if failures == 1 else 'runs'} failed."
        case NoActionKnown.VALUE:
            return "No action known."


def share_text(share: LargestShare | NoShare) -> str:
    match share:
        case LargestShare(kind, part):
            return f"{kind.name} paid {part:.0%} of it."
        case NoShare.VALUE:
            return ""
