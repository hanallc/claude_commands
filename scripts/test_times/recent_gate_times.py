"""Store the recent passing wall times used for gate deadlines."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NamedTuple, TypedDict, cast

from scripts.buildlog import store
from scripts.lib import conf, file_lock
from scripts.lib.atomic_file import write_text_atomic


# The type-check job runs no test modules.
TYPE_CHECK_JOB: Final = ("types", "basedpyright")


@dataclass(frozen=True)
class GateTimeLimit:
    """The history supports a deadline for this run."""

    seconds: float


@dataclass(frozen=True)
class NoGateTimeLimit:
    """No passing history supports a deadline for this run."""

    reason: str


class WallSample(NamedTuple):
    """Order a wall time by the start, run ID, and attempt."""

    started_at: str
    run_id: str
    attempt: int
    wall_s: float


class RecentGateTimes(TypedDict):
    """Keep samples under vCPU/job and vCPU/suite/module keys."""

    full: dict[str, list[WallSample]]
    modules: dict[str, list[WallSample]]


def _path() -> Path:
    return store.root() / "recent-gate-times.json"


def _groups(value: object) -> dict[str, list[WallSample]]:
    if not isinstance(value, dict):
        raise ValueError("expected a map of timing keys")
    groups: dict[str, list[WallSample]] = {}
    for key, rows in cast(dict[str, object], value).items():
        if not isinstance(rows, list):
            raise ValueError("expected a list of wall samples")
        samples: list[WallSample] = []
        for row in cast(list[object], rows):
            if not isinstance(row, list):
                raise ValueError("expected four sample fields")
            fields = cast(list[object], row)
            if len(fields) != 4:
                raise ValueError("expected four sample fields")
            started_at, run_id, attempt, wall_s = fields
            if (not isinstance(started_at, str) or not isinstance(run_id, str) or type(attempt) is not int
                    or not isinstance(wall_s, (int, float)) or isinstance(wall_s, bool)
                    or not math.isfinite(wall_s) or wall_s <= 0):
                raise ValueError("invalid wall sample")
            samples.append(WallSample(started_at, run_id, attempt, wall_s))
        groups[key] = samples
    return groups


def _read(path: Path) -> RecentGateTimes:
    try:
        data = cast(object, json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(data, dict):
            raise ValueError("expected a timing object")
        fields = cast(dict[str, object], data)
        return {"full": _groups(fields["full"]), "modules": _groups(fields["modules"])}
    except (OSError, ValueError, KeyError, OverflowError, RecursionError):
        return {"full": {}, "modules": {}}


def _sample_order(sample: WallSample) -> tuple[str, str, int]:
    return sample.started_at, sample.run_id, sample.attempt


def _merge(samples: RecentGateTimes, count: int) -> int:
    """Merge under the file lock so writers and the seed retain each other's samples."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(path.with_suffix(".lock")):
        current = _read(path)
        for group in ("full", "modules"):
            for key in current[group].keys() | samples[group].keys():
                by_attempt = {_sample_order(row): row
                              for row in (*samples[group].get(key, ()), *current[group].get(key, ()))}
                current[group][key] = sorted(by_attempt.values(), key=_sample_order, reverse=True)[:count]
        write_text_atomic(path, json.dumps(current, separators=(",", ":"), allow_nan=False) + "\n")
    return len(current["full"]) + len(current["modules"])


def _record_samples(record: Mapping[str, object]) -> RecentGateTimes:
    samples: RecentGateTimes = {"full": {}, "modules": {}}
    vcpus, started_at, run_id = record.get("vcpus"), record.get("started_at"), record.get("id")
    selection = record.get("selection")
    if (record.get("status") != 0 or record.get("aborted") != 0 or not isinstance(selection, str)
            or selection == "reused" or type(vcpus) is not int or vcpus <= 0
            or not isinstance(started_at, str) or not isinstance(run_id, str)):
        return samples

    raw_modules = record.get("modules")
    if not isinstance(raw_modules, list):
        return samples
    has_test_module = False
    for raw_module in cast(list[object], raw_modules):
        if not isinstance(raw_module, dict):
            continue
        row = cast(dict[str, object], raw_module)
        suite, module = row.get("suite"), row.get("module")
        if not isinstance(suite, str) or not isinstance(module, str):
            continue
        has_test_module |= (suite, module) != TYPE_CHECK_JOB
        attempt, wall_s = row.get("attempt"), row.get("wall_s")
        if (row.get("status") == 0 and row.get("restart") == "" and type(attempt) is int
                and isinstance(wall_s, (int, float)) and not isinstance(wall_s, bool) and 0 < wall_s < math.inf):
            samples["modules"].setdefault(f"{vcpus}/{suite}/{module}", []).append(
                WallSample(started_at, run_id, attempt, wall_s))
    jobs, wall_s = record.get("max_jobs"), record.get("wall_s")
    if (has_test_module and selection == "full" and record.get("step") == "gate" and type(jobs) is int
            and isinstance(wall_s, (int, float)) and not isinstance(wall_s, bool) and 0 < wall_s < math.inf):
        samples["full"][f"{vcpus}/{jobs}"] = [WallSample(started_at, run_id, 0, wall_s)]
    return samples


def update_recent_times(record: Mapping[str, object]) -> None:
    """Add eligible walls from a completed record without changing its verdict."""
    try:
        samples = _record_samples(record)
        if samples["full"] or samples["modules"]:
            _ = _merge(samples, conf.read("test_times").whole("TOP_RUNS"))
    except Exception as error:
        detail = " ".join(str(error).splitlines())
        print(f"recent gate times not written: {detail}", file=sys.stderr)


def read_time_limit(vcpus: int, modules: Sequence[str], jobs: int,
                    targeted: bool) -> GateTimeLimit | NoGateTimeLimit:
    """Use the newest passing walls for the requested hardware and work."""
    factor = conf.read("gate").number("GATE_TIME_LIMIT_FACTOR")
    count = conf.read("test_times").whole("TOP_RUNS")
    samples = _read(_path())
    if not targeted:
        walls = [row.wall_s for row in samples["full"].get(f"{vcpus}/{jobs}", ())[:count]]
        if walls:
            return GateTimeLimit(factor * statistics.median(walls))
        return NoGateTimeLimit(f"no passing full gate history for {vcpus} vCPUs at {jobs} jobs")
    medians: list[float] = []
    for path in modules:
        name = path.removeprefix("scripts/")
        walls = [row.wall_s for row in samples["modules"].get(f"{vcpus}/{name}", ())[:count]]
        if not walls:
            return NoGateTimeLimit(f"no passing module history for {name} on {vcpus} vCPUs")
        medians.append(statistics.median(walls))
    if medians:
        return GateTimeLimit(factor * max(max(medians), sum(medians) / jobs))
    return NoGateTimeLimit(f"no passing module history for {vcpus} vCPUs")


def _indexed_groups(connection: sqlite3.Connection, sql: str, count: int) -> dict[str, list[WallSample]]:
    samples: dict[str, list[WallSample]] = {}
    with closing(connection.execute(sql, (count,))) as cursor:
        rows = cast(Iterator[tuple[str, str, str, int, float]], iter(cursor))
        for key, started_at, run_id, attempt, wall_s in rows:
            if math.isfinite(wall_s):
                samples.setdefault(key, []).append(WallSample(started_at, run_id, attempt, wall_s))
    return samples


def seed(connection: sqlite3.Connection) -> int:
    """Read a committed index snapshot and merge its latest passing walls."""
    count = conf.read("test_times").whole("TOP_RUNS")
    full = _indexed_groups(connection, """
        SELECT key, started_at, id, 0, wall_s FROM (
            SELECT vcpus || '/' || max_jobs AS key, started_at, id, wall_s,
                   row_number() OVER (PARTITION BY vcpus, max_jobs ORDER BY started_at DESC, id DESC) AS rank
            FROM gate_runs WHERE status = 0 AND aborted = 0 AND selection = 'full' AND step = 'gate'
              AND wall_s > 0 AND modules_run > 0 AND vcpus > 0 AND max_jobs IS NOT NULL
        ) WHERE rank <= ?
    """, count)
    modules = _indexed_groups(connection, """
        SELECT key, started_at, id, attempt, wall_s FROM (
            SELECT r.vcpus || '/' || m.suite || '/' || m.module AS key, r.started_at, r.id, m.attempt, m.wall_s,
                   row_number() OVER (PARTITION BY r.vcpus, m.suite, m.module
                                      ORDER BY r.started_at DESC, r.id DESC, m.attempt DESC) AS rank
            FROM gate_modules m JOIN gate_runs r ON r.id = m.run_id
            WHERE r.status = 0 AND r.aborted = 0 AND r.selection != 'reused' AND r.vcpus > 0
              AND m.status = 0 AND m.restart = '' AND m.wall_s > 0
              AND m.suite IS NOT NULL AND m.module IS NOT NULL AND m.attempt IS NOT NULL
        ) WHERE rank <= ?
    """, count)
    return _merge({"full": full, "modules": modules}, count)
