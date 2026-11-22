"""Read module times and append gate runs to the build log."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from typing import Final, Literal, NotRequired, TypedDict, cast, get_args

from scripts.buildlog import store
from scripts.lib import conf, file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.test_times.recent_gate_times import (
    GateTimeLimit as GateTimeLimit, NoGateTimeLimit as NoGateTimeLimit, TYPE_CHECK_JOB as TYPE_CHECK_JOB,
    update_recent_times as update_recent_times,
)


TestStatus = Literal["pass", "fail", "error", "skip", "expected_failure", "unexpected_success"]
_TEST_STATUSES = frozenset(cast(tuple[str, ...], get_args(TestStatus)))


class TestTime(TypedDict):
    test: str
    s: float
    status: TestStatus


class ModuleTimes(TypedDict):
    config_reads: NotRequired[list[str]]
    folder_scans: NotRequired[list[str]]
    content_reads: NotRequired[list[str]]
    wall_s: float
    cpu_s: float
    tests_s: float
    tests_run: int
    failures: int
    errors: int
    skipped: int
    tests: list[TestTime]


class ModuleRun(TypedDict):
    # Older records can omit the module times.
    started_at: NotRequired[str]
    ended_at: NotRequired[str]
    config_reads: NotRequired[list[str]]
    folder_scans: NotRequired[list[str]]
    content_reads: NotRequired[list[str]]
    suite: str
    module: str
    attempt: int
    wall_s: float
    cpu_s: float
    peak_mib: float
    status: int
    tests_run: int
    failures: int
    errors: int
    skipped: int
    tests_s: float
    tests: list[TestTime]
    restart: str
    times_missing: str
    log: str


class SuiteRun(TypedDict):
    suite: str
    wall_s: float
    status: int


class GateRun(TypedDict):
    vcpus: NotRequired[int]
    step: str
    dirty_paths: list[str]
    dirty_path_count: int
    started_at: str
    ended_at: str
    wall_s: float
    hold_wait_s: float
    selection: str
    reason: str
    status: int
    max_jobs: int
    peak_mib: float
    cap_mib: float
    checkout: str
    branch: str
    sha: str
    tree: str
    source: str
    aborted: bool
    dirty: bool
    suites: list[SuiteRun]
    modules: list[ModuleRun]
    slow_test_s: float


class GateRunRecord(GateRun):
    kind: str
    v: int
    id: str
    host: str
    tests_run: int


@dataclass(frozen=True)
class NoModuleTimes:
    reason: str


@dataclass(frozen=True)
class WallClockSpan:
    """The wall-clock epoch seconds for one module attempt."""
    started_at: float
    ended_at: float


@dataclass(frozen=True)
class WallClockNotRecorded:
    """The attempt has no recorded wall-clock span."""
    reason: str


@dataclass(frozen=True)
class RecordedConfigReads:
    """The module recorded its reads, including a known empty set."""

    stems: frozenset[str]


@dataclass(frozen=True)
class NoConfigReadRecord:
    """The module has no usable record of its config reads."""


def _read_names(value: object, description: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(name, str) for name in cast(list[object], value)):
        raise ValueError(f"expected a list of {description}")
    return cast(list[str], value)


def config_read_record(value: object) -> RecordedConfigReads | NoConfigReadRecord:
    if value is None:
        return NoConfigReadRecord()
    return RecordedConfigReads(frozenset(_read_names(value, "config stems")))


@dataclass(frozen=True)
class ListedDirectory:
    """The module listed the direct entries of one repository folder."""

    path: str


@dataclass(frozen=True)
class RecursiveDirectory:
    """The module walked every path under one repository folder."""

    prefix: str


@dataclass(frozen=True)
class WholeRepository:
    """Every repository path is within this scan."""


@dataclass(frozen=True)
class RecordedFolderScans:
    """The module recorded its repository listings, including an empty set."""

    folders: frozenset[ListedDirectory | RecursiveDirectory | WholeRepository]


@dataclass(frozen=True)
class NoFolderScanRecord:
    """The module has no usable record of its repository listings."""


@dataclass(frozen=True)
class RecordedContentReads:
    """The module recorded repository file reads, including an empty set."""

    paths: frozenset[str]


@dataclass(frozen=True)
class NoContentReadRecord:
    """The module has no usable record of repository file reads."""


@dataclass(frozen=True)
class ModuleReadRecord:
    config_reads: RecordedConfigReads | NoConfigReadRecord
    folder_scans: RecordedFolderScans | NoFolderScanRecord
    content_reads: RecordedContentReads | NoContentReadRecord


def module_read_record(configs: object, folders: object, contents: object) -> ModuleReadRecord:
    """Keep absent fields separate from recorded empty fields."""
    config_record = config_read_record(configs)
    folder_record: RecordedFolderScans | NoFolderScanRecord = NoFolderScanRecord()
    if folders is not None:
        decoded: set[ListedDirectory | RecursiveDirectory | WholeRepository] = set()
        for path in _read_names(folders, "scanned folders"):
            decoded.add(WholeRepository() if path == "/" else RecursiveDirectory(path[:-1])
                        if path.endswith("/") else ListedDirectory(path))
        folder_record = RecordedFolderScans(frozenset(decoded))
    content_record: RecordedContentReads | NoContentReadRecord = NoContentReadRecord()
    if contents is not None:
        content_record = RecordedContentReads(frozenset(_read_names(contents, "repository file reads")))
    return ModuleReadRecord(config_record, folder_record, content_record)


def folder_scan_strings(scans: RecordedFolderScans) -> list[str]:
    """Encode the named folder states in the existing JSON format."""
    return sorted(folder.path if isinstance(folder, ListedDirectory)
                  else folder.prefix + "/" if isinstance(folder, RecursiveDirectory) else "/"
                  for folder in scans.folders)


@dataclass(frozen=True)
class TimingNotApplicable:
    """The type-check job runs no tests and writes no timing file."""


@dataclass(frozen=True)
class Execution:
    """One finished gate job, including each hold restart and retry."""
    suite: str
    module: str
    wall_s: float
    cpu_s: float
    peak_mib: float
    status: int
    restart: Literal["", "hold"]
    times: ModuleTimes | NoModuleTimes | TimingNotApplicable
    output: str
    wall_clock: WallClockSpan | WallClockNotRecorded = field(default=WallClockNotRecorded("not recorded"), kw_only=True)


def _fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return cast(dict[str, object], value)


ReadField = Literal["config_reads", "folder_scans", "content_reads"]
READ_FIELDS: Final[tuple[ReadField, ...]] = ("config_reads", "folder_scans", "content_reads")
LATEST_MODULE_READS_NAME: Final = "latest-module-reads.json"


class GateReadSnapshot(TypedDict):
    """Store one gate's module reads for one field.

    A null module value means the gate did not record that field for the module.
    """

    started_at: str
    run_id: str
    modules: dict[str, list[str] | None]


def _read_snapshots(path: Path) -> dict[ReadField, GateReadSnapshot]:
    document = _fields(cast(object, json.loads(path.read_text(encoding="utf-8"))))
    snapshots: dict[ReadField, GateReadSnapshot] = {}
    if set(document) - set(READ_FIELDS):
        raise ValueError("unknown module read field")
    for field in READ_FIELDS:
        if field not in document:
            continue
        snapshot = _fields(document[field])
        started_at, run_id = snapshot.get("started_at"), snapshot.get("run_id")
        if not isinstance(started_at, str) or not isinstance(run_id, str):
            raise ValueError("invalid module read gate identity")
        modules = {name: None if reads is None else _read_names(reads, field)
                   for name, reads in _fields(snapshot.get("modules")).items()}
        if not any(reads is not None for reads in modules.values()):
            raise ValueError("expected a recorded module read")
        snapshots[field] = {"started_at": started_at, "run_id": run_id, "modules": modules}
    return snapshots


def read_latest_module_reads() -> dict[str, ModuleReadRecord]:
    """Read the latest gate recorded for each module read field."""
    try:
        snapshots = _read_snapshots(store.root() / LATEST_MODULE_READS_NAME)
    except (FileNotFoundError, ValueError, RecursionError):
        return {}
    configs = snapshots["config_reads"]["modules"] if "config_reads" in snapshots else {}
    folders = snapshots["folder_scans"]["modules"] if "folder_scans" in snapshots else {}
    contents = snapshots["content_reads"]["modules"] if "content_reads" in snapshots else {}
    return {name: module_read_record(configs.get(name), folders.get(name), contents.get(name))
            for name in configs.keys() | folders.keys() | contents.keys()}


def without_module_reads(modules: Sequence[ModuleRun]) -> list[ModuleRun]:
    """Keep only the module data that belongs in the gate run record."""
    stripped: list[ModuleRun] = []
    for module in modules:
        row = module.copy()
        _ = row.pop("config_reads", None)
        _ = row.pop("folder_scans", None)
        _ = row.pop("content_reads", None)
        stripped.append(row)
    return stripped


def update_latest_module_reads(record: Mapping[str, object], modules: Sequence[object]) -> None:
    """Replace each field only when a later full gate recorded it."""
    started_at, run_id = record.get("started_at"), record.get("id")
    if (record.get("selection") != "full" or record.get("aborted") != 0
            or not isinstance(started_at, str) or not isinstance(run_id, str)):
        return
    try:
        module_paths: set[str] = set()
        reads_by_field: dict[ReadField, dict[str, set[str]]] = {field: {} for field in READ_FIELDS}
        for module in modules:
            row = _fields(module)
            suite, name = row.get("suite"), row.get("module")
            if not isinstance(suite, str) or not isinstance(name, str):
                continue
            path = f"scripts/{suite}/{name}"
            module_paths.add(path)
            for field in READ_FIELDS:
                value = row.get(field)
                if value is not None:
                    reads_by_field[field].setdefault(path, set()).update(_read_names(value, field))
        if not any(reads_by_field.values()):
            return
        paths = sorted(module_paths)
        cache = store.root() / LATEST_MODULE_READS_NAME
        cache.parent.mkdir(parents=True, exist_ok=True)
        with file_lock.exclusive(cache.with_suffix(".lock")):
            try:
                snapshots = _read_snapshots(cache)
            except (FileNotFoundError, ValueError, RecursionError):
                snapshots = {}
            changed = False
            for field, reads in reads_by_field.items():
                if not reads:
                    continue
                if field in snapshots:
                    previous = snapshots[field]
                    if (started_at, run_id) <= (previous["started_at"], previous["run_id"]):
                        continue
                snapshots[field] = {
                    "started_at": started_at, "run_id": run_id,
                    "modules": {path: sorted(reads[path]) if path in reads else None for path in paths},
                }
                changed = True
            if changed:
                write_text_atomic(cache, json.dumps(snapshots, separators=(",", ":")) + "\n")
    except (OSError, ValueError, RecursionError) as error:
        detail = " ".join(str(error).splitlines())
        print(f"latest module reads not written: {detail}", file=sys.stderr)


def update_gate_history(record: Mapping[str, object], modules: Sequence[object]) -> None:
    """Update the files that the next gate reads."""
    update_recent_times(record)
    update_latest_module_reads(record, modules)


def _seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected seconds as a number")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("expected finite, nonnegative seconds")
    return seconds


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected a nonnegative integer")
    return value


def _test_time(value: object) -> TestTime:
    fields = _fields(value)
    test = fields["test"]
    status = fields["status"]
    if not isinstance(test, str):
        raise ValueError("expected a test name")
    if not isinstance(status, str) or status not in _TEST_STATUSES:
        raise ValueError("invalid test status")
    return {"test": test, "s": _seconds(fields["s"]), "status": cast(TestStatus, status)}


def read_module_times(path: Path) -> ModuleTimes | NoModuleTimes:
    """Validate the complete file before returning module times."""
    try:
        decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
        fields = _fields(decoded)
        tests = fields["tests"]
        if not isinstance(tests, list):
            raise ValueError("expected a list of test times")
        times: ModuleTimes = {
            "wall_s": _seconds(fields["wall_s"]),
            "cpu_s": _seconds(fields["cpu_s"]),
            "tests_s": _seconds(fields["tests_s"]),
            "tests_run": _count(fields["tests_run"]),
            "failures": _count(fields["failures"]),
            "errors": _count(fields["errors"]),
            "skipped": _count(fields["skipped"]),
            "tests": [_test_time(test) for test in cast(list[object], tests)],
        }
        reads = module_read_record(fields.get("config_reads"), fields.get("folder_scans"), fields.get("content_reads"))
        if isinstance(reads.config_reads, RecordedConfigReads):
            times["config_reads"] = sorted(reads.config_reads.stems)
        if isinstance(reads.folder_scans, RecordedFolderScans):
            times["folder_scans"] = folder_scan_strings(reads.folder_scans)
        if isinstance(reads.content_reads, RecordedContentReads):
            times["content_reads"] = sorted(reads.content_reads.paths)
        return times
    except (OSError, UnicodeError, ValueError, KeyError, OverflowError, RecursionError) as error:
        return NoModuleTimes(f"{path}: {str(error).replace(chr(10), ' ')}")


def kept_tests(times: ModuleTimes, slow_test_s: float) -> list[TestTime]:
    """Keep failures and tests that meet the time threshold."""
    return sorted(
        (test for test in times["tests"]
         if test["status"] in {"fail", "error", "unexpected_success"} or test["s"] >= slow_test_s),
        key=lambda test: test["s"], reverse=True,
    )


def slow_test_s() -> float:
    return conf.read("test_times").number("SLOW_TEST_S")


def module_runs(executions: Sequence[Execution], slow_test_s: float) -> list[ModuleRun]:
    """Keep each attempt and select its test times with one threshold."""
    attempts: dict[tuple[str, str], int] = {}
    modules: list[ModuleRun] = []
    for job in executions:
        key = (job.suite, job.module)
        attempts[key] = attempts.get(key, 0) + 1
        module: ModuleRun = {
            "suite": job.suite, "module": job.module, "attempt": attempts[key],
            "wall_s": job.wall_s, "cpu_s": job.cpu_s, "peak_mib": job.peak_mib,
            "status": job.status, "tests_run": 0, "failures": 0, "errors": 0,
            "skipped": 0, "tests_s": 0.0, "tests": [], "restart": job.restart,
            "times_missing": job.times.reason if isinstance(job.times, NoModuleTimes) else "",
            "log": "",
        }
        if isinstance(job.wall_clock, WallClockSpan):
            module["started_at"] = store.utc_iso(job.wall_clock.started_at)
            module["ended_at"] = store.utc_iso(job.wall_clock.ended_at)
        if not isinstance(job.times, (NoModuleTimes, TimingNotApplicable)):
            times = job.times
            reads = module_read_record(times.get("config_reads"), times.get("folder_scans"), times.get("content_reads"))
            if isinstance(reads.config_reads, RecordedConfigReads):
                module["config_reads"] = sorted(reads.config_reads.stems)
            if isinstance(reads.folder_scans, RecordedFolderScans):
                module["folder_scans"] = folder_scan_strings(reads.folder_scans)
            if isinstance(reads.content_reads, RecordedContentReads):
                module["content_reads"] = sorted(reads.content_reads.paths)
            module.update(tests_run=times["tests_run"], failures=times["failures"],
                          errors=times["errors"], skipped=times["skipped"], tests_s=times["tests_s"],
                          tests=kept_tests(times, slow_test_s))
        modules.append(module)
    return modules


def report_not_written(error: BaseException) -> None:
    """Report a failed record on one line."""
    detail = " ".join(str(error).splitlines())
    print(f"gate run not written: {detail}", file=sys.stderr)


def keep_failure_logs(record_id: str, host: str, epoch: float, modules: Sequence[ModuleRun],
                      executions: Sequence[Execution]) -> None:
    """Keep each failed attempt under the host that recorded the gate."""
    from scripts.buildlog import record

    for module in modules:
        module["log"] = ""
    failures = [(module, job) for module, job in zip(modules, executions, strict=True)
                if job.status != 0 and not job.restart]
    if failures:
        limit = conf.read("test_times").whole("GATE_FAILURE_LOG_KIB") * 1024
        for module, job in failures:
            data = job.output.encode("utf-8")
            cut = max(0, len(data) - limit)
            kept = f"Cut {cut} bytes from the start of this output.\n".encode("utf-8") + data[-limit:]
            key = json.dumps((module["suite"], module["module"], module["attempt"])).encode("utf-8")
            name = f"{record_id}-{hashlib.sha256(key).hexdigest()}"
            try:
                module["log"] = record.keep_log(kept, host, epoch, name)
            except Exception as error:
                report_not_written(error)


def write_gate_run(run: GateRun, executions: Sequence[Execution]) -> None:
    """Append one gate record without failing the caller."""
    try:
        # This import keeps gate_record free of an import cycle with index.py.
        from scripts.buildlog import record

        epoch = time.time()
        host = store.host_name()
        entry: GateRunRecord = {"kind": "gate_run", "v": record.RECORD_VERSION,
                                "id": record.new_id(host, epoch), "host": host, **run,
                                "tests_run": sum(module["tests_run"] for module in run["modules"])}
        keep_failure_logs(entry["id"], host, epoch, run["modules"], executions)
        store.append_line(store.host_file(host, epoch), {**entry, "modules": without_module_reads(run["modules"])})
        update_gate_history(entry, run["modules"])
    except Exception as error:
        report_not_written(error)


@dataclass(frozen=True)
class ReadGateTimeLimit:
    """Read the recent gate times before the run starts."""


READ_GATE_TIME_LIMIT: Final = ReadGateTimeLimit()
