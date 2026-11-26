"""Read module times and append gate runs to the build log."""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
import hashlib
import io
import json
import math
import os
import sqlite3
import stat
import statistics
from pathlib import Path
import sys
import time
from typing import Final, Literal, NotRequired, TypedDict, cast, get_args

from scripts.lib import conf
from scripts.lib.boot_identity import boot_identity
from scripts.lib.process_start import Started, process_start


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


# The (suite, module) of the gate job that only checks types.
TYPE_CHECK_JOB: Final = ("types", "basedpyright")


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


GateEnd = Literal["normal", "red", "sigterm"]


class GateFolderOwner(TypedDict):
    pid: int
    process_start: NotRequired[str]
    boot_id: NotRequired[str]


class GateFolderStart(GateFolderOwner):
    folder: str
    sha: str
    branch: str
    step: str


class GateFolderEnd(GateFolderOwner):
    suite_folders: list[str]
    end: GateEnd


class GateRun(TypedDict):
    vcpus: NotRequired[int]
    pid: NotRequired[int]
    process_start: NotRequired[str]
    boot_id: NotRequired[str]
    suite_folders: NotRequired[list[str]]
    end: NotRequired[GateEnd]
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


def config_read_record(value: object) -> RecordedConfigReads | NoConfigReadRecord:
    if value is None:
        return NoConfigReadRecord()
    if not isinstance(value, list) or not all(isinstance(stem, str) for stem in cast(list[object], value)):
        raise ValueError("expected a list of config stems")
    return RecordedConfigReads(frozenset(cast(list[str], value)))


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
        if not isinstance(folders, list) or not all(isinstance(path, str) for path in cast(list[object], folders)):
            raise ValueError("expected a list of scanned folders")
        decoded: set[ListedDirectory | RecursiveDirectory | WholeRepository] = set()
        for path in cast(list[str], folders):
            decoded.add(WholeRepository() if path == "/" else RecursiveDirectory(path[:-1])
                        if path.endswith("/") else ListedDirectory(path))
        folder_record = RecordedFolderScans(frozenset(decoded))
    content_record: RecordedContentReads | NoContentReadRecord = NoContentReadRecord()
    if contents is not None:
        if not isinstance(contents, list) or not all(isinstance(path, str) for path in cast(list[object], contents)):
            raise ValueError("expected a list of repository file reads")
        content_record = RecordedContentReads(frozenset(cast(list[str], contents)))
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
    return conf.read("test_times").number("slow_test_s")


def module_runs(executions: Sequence[Execution], slow_test_s: float) -> list[ModuleRun]:
    """Keep each attempt and select its test times with one threshold."""
    from scripts.buildlog import store

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


def _report_record_error(message: str, error: BaseException) -> None:
    detail = " ".join(str(error).splitlines())
    with suppress(OSError, ValueError):
        print(f"{message}: {detail}", file=sys.stderr, flush=True)


def report_not_written(error: BaseException, *, record_name: str = "gate run") -> None:
    """Report a failed record on one line."""
    _report_record_error(f"{record_name} not written", error)


def keep_failure_logs(record_id: str, host: str, epoch: float, modules: Sequence[ModuleRun],
                      executions: Sequence[Execution]) -> None:
    """Keep each failed attempt under the host that recorded the gate."""
    from scripts.buildlog import record

    for module in modules:
        module["log"] = ""
    failures = [(module, job) for module, job in zip(modules, executions, strict=True)
                if job.status != 0 and not job.restart]
    if failures:
        limit = conf.read("test_times").whole("gate_failure_log_kib") * 1024
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
    row_written = False
    try:
        # This import keeps gate_record free of an import cycle with index.py.
        from scripts.buildlog import record, store

        epoch = time.time()
        host = store.host_name()
        entry: GateRunRecord = {"kind": "gate_run", "v": record.RECORD_VERSION,
                                "id": record.new_id(host, epoch), "host": host, **run,
                                "tests_run": sum(module["tests_run"] for module in run["modules"])}
        keep_failure_logs(entry["id"], host, epoch, run["modules"], executions)
        with store.locked_append(store.host_file(host, epoch)) as handle:
            store.append_locked(handle, entry)
            row_written = True
    except Exception as error:
        if row_written:
            _report_record_error("gate run sync failed", error)
            return
        report_not_written(error)
        if "pid" in run and "suite_folders" in run and run["suite_folders"] and "end" in run:
            end: GateFolderEnd = {"pid": run["pid"], "suite_folders": run["suite_folders"], "end": run["end"]}
            if "process_start" in run:
                end["process_start"] = run["process_start"]
            if "boot_id" in run:
                end["boot_id"] = run["boot_id"]
            write_gate_folder_event(end, report_failure=False)


def write_gate_folder_event(event: GateFolderStart | GateFolderEnd, *, report_failure: bool = True) -> None:
    """Append a folder start or an end without timing history."""
    kind = "gate_folder_start" if "folder" in event else "gate_folder_end"
    try:
        from scripts.buildlog import record, store

        epoch = time.time()
        host = store.host_name()
        store.append_line(store.host_file(host, epoch), {
            "kind": kind, "v": record.RECORD_VERSION, "id": record.new_id(host, epoch),
            "host": host, "at": store.utc_iso(epoch), **event,
        })
    except Exception as error:
        if report_failure:
            report_not_written(error, record_name=kind)


@dataclass(frozen=True)
class _GateLeaseOwner:
    """Identify a process on one boot."""

    pid: int
    process_start: str
    boot_id: str


def _lease_owner(fields: Mapping[str, object]) -> _GateLeaseOwner | None:
    pid, start, boot = fields.get("pid"), fields.get("process_start"), fields.get("boot_id")
    if type(pid) is int and pid > 0 and isinstance(start, str) and start and isinstance(boot, str) and boot:
        return _GateLeaseOwner(pid, start, boot)
    return None


GateFolderOutcome = GateEnd | Literal["running", "killed", "no record"]


class GateFolderStatus(TypedDict):
    folder: str
    pid: int | None
    sha: NotRequired[str]
    step: NotRequired[str]
    end: GateFolderOutcome


class _FolderLogChanged(OSError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"log changed during the read ({str(path)!r})")


def _folder_log_rows(path: Path) -> Iterator[dict[str, object]]:
    """Read the complete prefix of a log without holding a lock."""
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return
    with handle:
        descriptor = handle.fileno()
        before = os.fstat(descriptor)
        remaining = before.st_size
        # Log writers can cut the unfinished tail. Each observed newline protects its prefix.
        while remaining:
            offset = max(0, remaining - io.DEFAULT_BUFFER_SIZE)
            block = os.pread(descriptor, remaining - offset, offset)
            if len(block) != remaining - offset:
                raise _FolderLogChanged(path)
            if (newline := block.rfind(b"\n")) >= 0:
                remaining = offset + newline + 1
                break
            remaining = offset
        had_unfinished_tail = remaining < before.st_size
        while remaining:
            line = handle.readline(remaining)
            if not line.endswith(b"\n"):
                raise _FolderLogChanged(path)
            remaining -= len(line)
            try:
                row = cast(object, json.loads(line.decode("utf-8")))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(row, dict):
                yield cast(dict[str, object], row)
        try:
            named = path.stat()
        except FileNotFoundError as error:
            raise _FolderLogChanged(path) from error
        after = os.fstat(descriptor)
        if ((named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)
                or min(named.st_size, after.st_size) < before.st_size
                or (had_unfinished_tail and after.st_mtime_ns != before.st_mtime_ns)):
            raise _FolderLogChanged(path)


def _matches_leased_folder(folder: Path, fields: Mapping[str, object]) -> bool:
    device, inode = fields.get("device"), fields.get("inode")
    if type(device) is not int or type(inode) is not int:
        return False
    try:
        info = folder.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and (device, inode) == (info.st_dev, info.st_ino)


def gate_folder_status(root: Path) -> list[GateFolderStatus]:
    """Join direct suite folders to the local host log without changing either."""
    from scripts.buildlog import store
    from scripts.lib.build_folders import gate_folder_lease_fields

    root = root.expanduser().resolve()
    statuses: list[GateFolderStatus] = []
    candidates: dict[tuple[str, _GateLeaseOwner], GateFolderStatus] = {}
    for folder in sorted(root.iterdir()):
        if not folder.name.startswith(("g-", "t-")) or folder.is_symlink() or not folder.is_dir():
            continue
        try:
            fields = gate_folder_lease_fields(folder, root)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            print(f"cannot read the lease for {folder}: {error}", file=sys.stderr)
            fields = {}
        pid = fields.get("pid")
        status: GateFolderStatus = {"folder": str(folder), "pid": pid if type(pid) is int else None, "end": "no record"}
        statuses.append(status)
        owner = _lease_owner(fields)
        if owner is not None and _matches_leased_folder(folder, fields):
            candidates[str(folder), owner] = status
    if not candidates:
        return statuses

    current_boot = ""
    with suppress(OSError, ValueError):
        current_boot = boot_identity()
    live: set[_GateLeaseOwner] = set()
    for owner in {owner for _, owner in candidates}:
        if owner.boot_id == current_boot:
            start = process_start(owner.pid)
            if isinstance(start, Started) and start.start == owner.process_start:
                live.add(owner)

    ends: dict[tuple[str, _GateLeaseOwner], GateEnd] = {}
    host_logs = store.root() / store.host_name()
    for path in sorted(host_logs.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9].jsonl")):
        for row in _folder_log_rows(path):
            kind = row.get("kind")
            if kind not in ("gate_folder_start", "gate_run", "gate_folder_end"):
                continue
            owner = _lease_owner(row)
            if owner is None:
                continue
            if kind == "gate_folder_start":
                folder, sha, step = row.get("folder"), row.get("sha"), row.get("step")
                if isinstance(folder, str) and isinstance(sha, str) and isinstance(step, str):
                    if (candidate := candidates.get((folder, owner))) is not None:
                        candidate.update(sha=sha, step=step)
            else:
                folders, end = row.get("suite_folders"), row.get("end")
                if isinstance(folders, list) and end in ("normal", "red", "sigterm"):
                    for folder in cast(list[object], folders):
                        if isinstance(folder, str) and (folder, owner) in candidates:
                            ends[folder, owner] = end
    for key, candidate in candidates.items():
        if "sha" not in candidate:
            continue
        if (end := ends.get(key)) is not None:
            candidate["end"] = end
            continue
        candidate["end"] = "running" if key[1] in live else "killed"
    return statuses


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from scripts.lib.build_folders import configured_gate_root

    parser = argparse.ArgumentParser(description="Read suite folders and their gate outcomes.")
    _ = parser.add_argument("root", nargs="?", type=Path, help="suite parent (default: the configured gate root)")
    args = parser.parse_args(argv)
    try:
        root = cast(Path | None, args.root)
        for status in gate_folder_status(configured_gate_root() if root is None else root):
            print(json.dumps(status))
    except (OSError, ValueError) as error:
        print(f"gate folder status: {error}", file=sys.stderr)
        return 1
    return 0


@dataclass(frozen=True)
class GateTimeLimit:
    """The history supports a deadline for this run."""

    seconds: float


@dataclass(frozen=True)
class NoGateTimeLimit:
    """No passing history supports a deadline for this run."""

    reason: str


@dataclass(frozen=True)
class ReadGateTimeLimit:
    """Read the local gate index before the run starts."""


READ_GATE_TIME_LIMIT: Final = ReadGateTimeLimit()


def history_time_limit(connection: sqlite3.Connection, vcpus: int, modules: Sequence[str],
                       jobs: int, targeted: bool) -> GateTimeLimit | NoGateTimeLimit:
    """Use passing records from the same vCPU count and the configured history size.

    A full run also matches the job count: a cloud host runs one job per vCPU, and a
    shared natedev run with the same vCPU count runs fewer jobs and takes longer.
    A full run counts only when it ran test modules: a run of the type check alone is shorter.
    """
    factor = conf.read("gate").number("gate_time_limit_factor")
    count = conf.read("test_times").whole("top_runs")
    if not targeted:
        walls = [float(row[0]) for row in cast(list[tuple[float]], connection.execute(
            """SELECT wall_s FROM gate_runs
               WHERE vcpus = ? AND max_jobs = ? AND status = 0 AND aborted = 0
                 AND selection = 'full' AND step = 'gate' AND wall_s > 0 AND modules_run > 0
               ORDER BY started_at DESC, id DESC LIMIT ?""", (vcpus, jobs, count)).fetchall())]
        if walls:
            return GateTimeLimit(factor * statistics.median(walls))
        return NoGateTimeLimit(f"no passing full gate history for {vcpus} vCPUs at {jobs} jobs")
    medians: list[float] = []
    for path in modules:
        name = path.removeprefix("scripts/")
        suite, module = name.split("/", 1)
        walls = [float(row[0]) for row in cast(list[tuple[float]], connection.execute(
            """SELECT m.wall_s FROM gate_modules m JOIN gate_runs r ON r.id = m.run_id
               WHERE r.vcpus = ? AND r.status = 0 AND r.aborted = 0
                 AND r.selection != 'reused' AND m.status = 0 AND m.restart = ''
                 AND m.suite = ? AND m.module = ? AND m.wall_s > 0
               ORDER BY r.started_at DESC, r.id DESC, m.attempt DESC LIMIT ?""",
            (vcpus, suite, module, count)).fetchall())]
        if not walls:
            return NoGateTimeLimit(f"no passing module history for {name} on {vcpus} vCPUs")
        medians.append(statistics.median(walls))
    if medians:
        return GateTimeLimit(factor * max(max(medians), sum(medians) / jobs))
    return NoGateTimeLimit(f"no passing module history for {vcpus} vCPUs")


if __name__ == "__main__":
    raise SystemExit(main())
