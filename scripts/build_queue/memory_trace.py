"""Store bounded Hana nextest memory traces and project the chart data."""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
import errno
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import re
import sys
from typing import cast, final

from scripts.build_queue import admission_records as records, progress_profile as progress
from scripts.build_queue.admission_records import RequestedTestWidth, UnspecifiedTestWidth
from scripts.build_queue.progress_profile import CompilingCrateUnavailable as CompilingCrateUnavailable
from scripts.lib import conf, test_phase
from scripts.lib.atomic_file import sync_folder, write_bytes_atomic
from scripts.lint import memory_admit as gate


@dataclass(frozen=True)
class MemoryTracePolicy:
    sample_seconds: int
    max_samples_per_run: int
    max_bytes_per_run: int
    retention_days: int
    max_finished_runs: int
    points_per_phase: int
    feed_bytes_per_refresh: int

    def __post_init__(self) -> None:
        if min(self.sample_seconds, self.max_samples_per_run, self.max_bytes_per_run, self.retention_days,
               self.max_finished_runs, self.points_per_phase, self.feed_bytes_per_refresh) <= 0:
            raise ValueError("trace policy values must be positive")
        if self.points_per_phase < 3:
            raise ValueError("points_per_phase must allow two endpoints and a peak")

    @property
    def sample_interval_ms(self) -> int:
        return self.sample_seconds * _MS_PER_SECOND


@dataclass(frozen=True)
class TracesOff:
    reason: str


TRACES_DISABLED = TracesOff("memory trace policy was not supplied")


def read_policy(settings: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT
                ) -> MemoryTracePolicy | TracesOff:
    """Disable traces when a setting has no usable live value."""
    configuration = conf.read("memory_trace") if isinstance(settings, conf.UseConfiguredValue) else settings

    def whole(key: str) -> int:
        reading = configuration.origin(key, int)
        if not isinstance(reading, conf.UsableLiveValue):
            raise conf.Unusable(reading.reason)
        return reading.value

    try:
        return MemoryTracePolicy(
            sample_seconds=whole("sample_seconds"), max_samples_per_run=whole("max_samples_per_run"),
            max_bytes_per_run=whole("max_bytes_per_run"), retention_days=whole("retention_days"),
            max_finished_runs=whole("max_finished_runs"), points_per_phase=whole("points_per_phase"),
            feed_bytes_per_refresh=whole("feed_bytes_per_refresh"),
        )
    except ValueError as error:
        return TracesOff(str(error))


@dataclass(frozen=True)
class HanaTraceShape:
    host: str
    repository_identity: str
    nextest_argv: tuple[str, ...]
    requested_test_width: RequestedTestWidth | UnspecifiedTestWidth

    def __post_init__(self) -> None:
        if not self.host or not self.repository_identity or not self.nextest_argv:
            raise ValueError("trace shape needs a host, repository, and nextest arguments")


# Keep the argument order and all selection and execution flags in the key.
_VALUE_FLAGS = frozenset({
    "--bin", "--cargo-profile", "--example", "--features", "--filterset", "--package",
    "--profile", "--target", "--test", "--test-threads",
})


def shape_key(shape: HanaTraceShape) -> str:
    """Hash the command and normalize the spelling of known value flags."""
    arguments = shape.nextest_argv
    normalized: list[str] = []
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word == "--":
            normalized.extend(arguments[index:])
            break
        if word in _VALUE_FLAGS and index + 1 < len(arguments) and not arguments[index + 1].startswith("-"):
            index += 1
            word += "=" + arguments[index]
        normalized.append(word)
        index += 1
    width = shape.requested_test_width
    encoded = _json_line((shape.host, shape.repository_identity, normalized,
                          width.threads if isinstance(width, RequestedTestWidth) else None))
    return hashlib.sha256(encoded).hexdigest()


_VERSION = 1
_MAX_INTEGER = (1 << 63) - 1
_RESERVATION_NAME = re.compile(r"[A-Za-z0-9_-]+")
_SHAPE_NAME = re.compile(r"[0-9a-f]{64}")
_SUFFIX = ".jsonl"
_MS_PER_SECOND = 1000
_MS_PER_DAY = 86400 * _MS_PER_SECOND


def _integer(value: object, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= _MAX_INTEGER:
        raise ValueError("invalid trace integer")
    return value


def _name(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("invalid trace name")
    return value


@dataclass(frozen=True)
class TraceHeader:
    reservation_id: str
    shape_key: str
    scope_started_at_ms: int
    sample_interval_ms: int
    compile_reserve_bytes: int

    def __post_init__(self) -> None:
        _ = _name(self.reservation_id, _RESERVATION_NAME)
        _ = _name(self.shape_key, _SHAPE_NAME)
        _ = _integer(self.scope_started_at_ms)
        _ = _integer(self.sample_interval_ms, minimum=1)
        _ = _integer(self.compile_reserve_bytes, minimum=1)

    @property
    def max_gap_ms(self) -> int:
        return 2 * self.sample_interval_ms


@dataclass(frozen=True)
class CargoProgress:
    build_done: int
    build_total: int

    def __post_init__(self) -> None:
        _ = _integer(self.build_done)
        _ = _integer(self.build_total, minimum=1)
        if self.build_done > self.build_total:
            raise ValueError("invalid Cargo progress")


@dataclass(frozen=True)
class CargoProgressUnavailable:
    pass


@dataclass(frozen=True)
class CompileSample:
    elapsed_ms: int
    program_bytes: int
    progress: CargoProgress | CargoProgressUnavailable
    last_compiling: str | CompilingCrateUnavailable

    def __post_init__(self) -> None:
        _ = _integer(self.elapsed_ms)
        _ = _integer(self.program_bytes)
        if isinstance(self.last_compiling, str) and not self.last_compiling:
            raise ValueError("empty crate name")


@dataclass(frozen=True)
class TestSample:
    elapsed_ms: int
    program_bytes: int

    def __post_init__(self) -> None:
        _ = _integer(self.elapsed_ms)
        _ = _integer(self.program_bytes)


@dataclass(frozen=True)
class TestReserveUnavailable:
    pass


@dataclass(frozen=True)
class TestStarted:
    elapsed_ms: int
    reserve_bytes: int | TestReserveUnavailable

    def __post_init__(self) -> None:
        _ = _integer(self.elapsed_ms)
        if isinstance(self.reserve_bytes, int):
            _ = _integer(self.reserve_bytes, minimum=1)


@dataclass(frozen=True)
class TestReserveObserved:
    """Admission supplies the first known test reserve after the start."""
    reserve_bytes: int

    def __post_init__(self) -> None:
        _ = _integer(self.reserve_bytes, minimum=1)


@dataclass(frozen=True)
class TestNotStarted:
    pass


@dataclass(frozen=True)
class ExitStatusUnavailable:
    pass


@dataclass(frozen=True)
class WholeRun:
    """The recorder observed a nextest completion and both phases have coverage."""


class TraceLimit(StrEnum):
    SAMPLE_COUNT = "sample_limit"
    FILE_SIZE = "byte_limit"


class IncompleteReason(StrEnum):
    NO_NEXTTEST_COMPLETION = "no_nextest_completion"
    SCOPE_LOST = "scope_lost"
    RESTART = "restart"
    MISSING_COMPILE = "missing_compile"
    MISSING_TEST = "missing_test"
    SAMPLE_GAP = "sample_gap"
    NEXTTEST_STATUS = "nextest_status"


@dataclass(frozen=True)
class IncompleteRun:
    reason: IncompleteReason | TraceLimit


@dataclass(frozen=True)
class TraceEnd:
    ended_at_ms: int
    elapsed_ms: int
    exit_status: int | ExitStatusUnavailable
    coverage: WholeRun | IncompleteRun

    def __post_init__(self) -> None:
        _ = _integer(self.ended_at_ms)
        _ = _integer(self.elapsed_ms)
        if isinstance(self.exit_status, int):
            _ = _integer(self.exit_status, minimum=-_MAX_INTEGER)


type TraceObservation = CompileSample | TestStarted | TestReserveObserved | TestSample
type TraceRecord = TraceHeader | TraceObservation | TraceLimit | TraceEnd


def _json_line(values: Sequence[object]) -> bytes:
    return (json.dumps(values, separators=(",", ":")) + "\n").encode("ascii")


def encode_record(record: TraceRecord) -> bytes:
    """Encode one complete record as a compact JSONL row."""
    match record:
        case TraceHeader(reservation_id, key, started, interval, reserve):
            return _json_line(("h", _VERSION, reservation_id, key, started, interval, reserve))
        case CompileSample(elapsed, used, progress, crate):
            done, total = (progress.build_done, progress.build_total) if isinstance(progress, CargoProgress) else (None, None)
            return _json_line(("c", elapsed, used, done, total, crate if isinstance(crate, str) else None))
        case TestStarted(elapsed, reserve):
            return _json_line(("s", elapsed, reserve if isinstance(reserve, int) else None))
        case TestReserveObserved(reserve):
            return _json_line(("r", reserve))
        case TestSample(elapsed, used):
            return _json_line(("t", elapsed, used))
        case TraceLimit():
            return _json_line(("l", record.value))
        case TraceEnd(ended, elapsed, status, coverage):
            return _json_line(("e", ended, elapsed, status if isinstance(status, int) else None,
                               coverage.reason.value if isinstance(coverage, IncompleteRun) else None))


def decode_record(line: bytes) -> TraceRecord:
    """Validate a record from the file boundary."""
    value = cast(object, json.loads(line))
    if not isinstance(value, list):
        raise ValueError("expected a trace record")
    match cast(list[object], value):
        case ["h", version, str(reservation_id), str(key), int(started), int(interval), int(reserve)] if (
            _integer(version) == _VERSION
        ):
            return TraceHeader(reservation_id, key, started, interval, reserve)
        case ["c", int(elapsed), int(used), done, total, crate] if crate is None or isinstance(crate, str):
            if done is None and total is None:
                progress = CargoProgressUnavailable()
            elif isinstance(done, int) and isinstance(total, int):
                progress = CargoProgress(done, total)
            else:
                raise ValueError("invalid Cargo progress")
            return CompileSample(elapsed, used, progress,
                                 CompilingCrateUnavailable() if crate is None else crate)
        case ["s", int(elapsed), reserve] if reserve is None or isinstance(reserve, int):
            return TestStarted(elapsed, TestReserveUnavailable() if reserve is None else reserve)
        case ["r", int(reserve)]:
            return TestReserveObserved(reserve)
        case ["t", int(elapsed), int(used)]:
            return TestSample(elapsed, used)
        case ["l", str(reason)]:
            return TraceLimit(reason)
        case ["e", int(ended), int(elapsed), status, reason] if (
            (reason is None or isinstance(reason, str)) and (status is None or isinstance(status, int))
        ):
            if reason is None:
                coverage: WholeRun | IncompleteRun = WholeRun()
            else:
                limit = next((limit for limit in TraceLimit if limit.value == reason), None)
                coverage = IncompleteRun(limit if limit is not None else IncompleteReason(reason))
            return TraceEnd(ended, elapsed, ExitStatusUnavailable() if status is None else status, coverage)
        case _:
            raise ValueError("unknown trace record")


# Leave space for the limit and final records before accepting each observation.
_LIMIT_ROW_BYTES = max(len(encode_record(limit)) for limit in TraceLimit)
_END_ROW_BYTES = max(len(encode_record(TraceEnd(
    _MAX_INTEGER, _MAX_INTEGER, -_MAX_INTEGER, IncompleteRun(reason)
))) for reason in (*IncompleteReason, *TraceLimit))


@dataclass(frozen=True)
class _RecordingOrder:
    compile_count: int = 0
    test_count: int = 0
    last_sample_ms: int = -1
    test_start: TestStarted | TestNotStarted = TestNotStarted()
    has_gap: bool = False

    @property
    def sample_count(self) -> int:
        return self.compile_count + self.test_count

    def observe(self, record: TraceObservation, max_gap_ms: int) -> _RecordingOrder:
        if isinstance(record, TestStarted):
            if isinstance(self.test_start, TestStarted) or record.elapsed_ms < self.last_sample_ms:
                raise ValueError("test start is out of order")
            return replace(self, test_start=record)
        if isinstance(record, TestReserveObserved):
            if isinstance(self.test_start, TestNotStarted) or isinstance(self.test_start.reserve_bytes, int):
                raise ValueError("test reserve is out of order")
            return replace(self, test_start=replace(self.test_start, reserve_bytes=record.reserve_bytes))
        if record.elapsed_ms <= self.last_sample_ms:
            raise ValueError("sample time is out of order")
        has_gap = self.has_gap or record.elapsed_ms - max(self.last_sample_ms, 0) > max_gap_ms
        if isinstance(record, CompileSample):
            if isinstance(self.test_start, TestStarted):
                raise ValueError("compile sample follows test start")
            return replace(self, compile_count=self.compile_count + 1, last_sample_ms=record.elapsed_ms,
                           has_gap=has_gap)
        if isinstance(self.test_start, TestNotStarted) or record.elapsed_ms < self.test_start.elapsed_ms:
            raise ValueError("test sample precedes test start")
        return replace(self, test_count=self.test_count + 1, last_sample_ms=record.elapsed_ms, has_gap=has_gap)

    def finish(self, end: TraceEnd, header: TraceHeader, state: TraceRecording | TraceLimit) -> TraceEnd:
        if end.elapsed_ms < self.last_sample_ms or (
            isinstance(self.test_start, TestStarted) and end.elapsed_ms < self.test_start.elapsed_ms
        ):
            raise ValueError("trace end precedes an observation")
        if isinstance(state, TraceLimit):
            return replace(end, coverage=IncompleteRun(state))
        if isinstance(end.coverage, IncompleteRun):
            return end
        if end.exit_status not in (0, 100):
            reason = IncompleteReason.NEXTTEST_STATUS
        elif not self.compile_count:
            reason = IncompleteReason.MISSING_COMPILE
        elif not self.test_count:
            reason = IncompleteReason.MISSING_TEST
        elif self.has_gap or end.elapsed_ms - self.last_sample_ms > header.max_gap_ms:
            reason = IncompleteReason.SAMPLE_GAP
        else:
            return end
        return replace(end, coverage=IncompleteRun(reason))


@dataclass(frozen=True)
class TraceRecording:
    pass


@dataclass(frozen=True)
class TraceTailIncomplete:
    """The last line has no newline. Earlier observations remain readable."""


@dataclass(frozen=True)
class TraceUnavailable:
    reason: str


@dataclass(frozen=True)
class MemoryTrace:
    header: TraceHeader
    compile_samples: tuple[CompileSample, ...]
    test_start: TestStarted | TestNotStarted
    test_samples: tuple[TestSample, ...]
    state: TraceRecording | TraceLimit | TraceTailIncomplete | TraceEnd


@dataclass(frozen=True)
class NoMatchingWholeRun:
    pass


def read_trace(path: Path, policy: MemoryTracePolicy) -> MemoryTrace | TraceUnavailable:
    """Read bounded complete lines and validate phase and end order."""
    try:
        with path.open("rb") as stream:
            line = stream.readline(policy.max_bytes_per_run + 1)
            if not line.endswith(b"\n") or len(line) > policy.max_bytes_per_run:
                raise ValueError("missing complete trace header")
            header = decode_record(line)
            if not isinstance(header, TraceHeader):
                raise ValueError("trace must start with its header")
            remaining = policy.max_bytes_per_run - len(line)
            order = _RecordingOrder()
            compile_samples: list[CompileSample] = []
            test_samples: list[TestSample] = []
            state: TraceRecording | TraceLimit | TraceTailIncomplete | TraceEnd = TraceRecording()
            while line := stream.readline(remaining + 1):
                remaining -= len(line)
                if remaining < 0:
                    raise ValueError("trace exceeds its byte limit")
                if not line.endswith(b"\n"):
                    state = TraceTailIncomplete()
                    break
                record = decode_record(line)
                if isinstance(state, TraceEnd) or isinstance(record, TraceHeader):
                    raise ValueError("record outside the trace body")
                if isinstance(record, TraceEnd):
                    if record != order.finish(record, header, state):
                        raise ValueError("trace coverage disagrees with its records")
                    state = record
                elif isinstance(state, TraceLimit):
                    raise ValueError("observation follows a trace limit")
                elif isinstance(record, TraceLimit):
                    state = record
                else:
                    order = order.observe(record, header.max_gap_ms)
                    if order.sample_count > policy.max_samples_per_run:
                        raise ValueError("trace exceeds its sample limit")
                    if isinstance(record, CompileSample):
                        compile_samples.append(record)
                    elif isinstance(record, TestSample):
                        test_samples.append(record)
    except (OSError, ValueError, RecursionError) as error:
        return TraceUnavailable(str(error))
    return MemoryTrace(header, tuple(compile_samples), order.test_start, tuple(test_samples), state)


@dataclass(frozen=True)
class _TraceWriteInterrupted:
    pass


@final
class TraceWriter:
    """Write observations for one reservation from a single reader thread."""

    def __init__(self, path: Path, finished_folder: Path, header: TraceHeader, policy: MemoryTracePolicy) -> None:
        line = encode_record(header)
        if len(line) + _LIMIT_ROW_BYTES + _END_ROW_BYTES > policy.max_bytes_per_run:
            raise ValueError("trace header and final records exceed the byte limit")
        self.path = path
        self.header = header
        self._finished_folder = finished_folder
        self._policy = policy
        self._order = _RecordingOrder()
        self._state: TraceRecording | TraceLimit | TraceEnd | _TraceWriteInterrupted = TraceRecording()
        self._bytes_written = len(line)
        self._published = False
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as stream:
            _ = stream.write(line)

    def _append(self, line: bytes) -> None:
        try:
            with self.path.open("r+b") as stream:
                _ = stream.seek(0, os.SEEK_END)
                _ = stream.write(line)
        except OSError:
            self._state = _TraceWriteInterrupted()
            raise
        self._bytes_written += len(line)

    @property
    def test_start(self) -> TestStarted | TestNotStarted:
        return self._order.test_start

    @property
    def last_sample_ms(self) -> int:
        return self._order.last_sample_ms

    def append(self, record: TraceObservation) -> TraceRecording | TraceLimit:
        """Append one observation or mark the trace when it reaches a limit."""
        if isinstance(self._state, TraceLimit):
            return self._state
        if not isinstance(self._state, TraceRecording):
            raise ValueError("trace cannot accept more observations")
        order = self._order.observe(record, self.header.max_gap_ms)
        line = encode_record(record)
        if order.sample_count > self._policy.max_samples_per_run:
            limit = TraceLimit.SAMPLE_COUNT
        elif self._bytes_written + len(line) + _LIMIT_ROW_BYTES + _END_ROW_BYTES > self._policy.max_bytes_per_run:
            limit = TraceLimit.FILE_SIZE
        else:
            self._append(line)
            self._order = order
            return self._state
        self._append(encode_record(limit))
        self._state = limit
        return limit

    def seal(self, end: TraceEnd) -> TraceEnd:
        """Record the end and atomically move the trace into its shape folder."""
        if isinstance(self._state, _TraceWriteInterrupted):
            raise ValueError("trace write was interrupted")
        if isinstance(self._state, TraceEnd):
            end = self._state
        else:
            end = self._order.finish(end, self.header, self._state)
            self._append(encode_record(end))
            self._state = end
        if not self._published:
            self._finished_folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination = _sealed_path(self._finished_folder, self.header, end)
            with self.path.open("rb") as stream:
                os.fsync(stream.fileno())
            _ = self.path.replace(destination)
            self._published = True
            sync_folder(self._finished_folder)
            sync_folder(self.path.parent)
        return end


def _sealed_path(folder: Path, header: TraceHeader, end: TraceEnd) -> Path:
    return folder / f"{end.ended_at_ms}-{header.reservation_id}{_SUFFIX}"


@dataclass(frozen=True, order=True)
class _SealedTraceFile:
    ended_at_ms: int
    reservation_id: str
    path: Path


def _finished_files(folder: Path) -> Iterator[_SealedTraceFile]:
    for path in folder.glob(f"*{_SUFFIX}"):
        ended, _, reservation_id = path.stem.partition("-")
        if ended.isascii() and ended.isdecimal() and _RESERVATION_NAME.fullmatch(reservation_id):
            if len(ended) <= len(str(_MAX_INTEGER)) and int(ended) <= _MAX_INTEGER:
                yield _SealedTraceFile(int(ended), reservation_id, path)


@dataclass(frozen=True)
class TraceStore:
    """Own one directory of live traces and shape folders of finished traces."""

    root: Path
    policy: MemoryTracePolicy

    def _live_path(self, reservation_id: str) -> Path:
        return self.root / "live" / (_name(reservation_id, _RESERVATION_NAME) + _SUFFIX)

    def start(self, header: TraceHeader) -> TraceWriter:
        """Start a fresh trace. A prior trace for this reservation blocks a restart."""
        finished = self.root / "finished" / header.shape_key
        if any(file.reservation_id == header.reservation_id for file in _finished_files(finished)):
            raise FileExistsError(header.reservation_id)
        return TraceWriter(self._live_path(header.reservation_id), finished, header, self.policy)

    def read_live(self, reservation_id: str) -> MemoryTrace | TraceUnavailable:
        trace = read_trace(self._live_path(reservation_id), self.policy)
        if isinstance(trace, MemoryTrace) and trace.header.reservation_id != reservation_id:
            return TraceUnavailable("trace belongs to another reservation")
        return trace

    def recover_abandoned(self) -> None:
        """Seal the readable prefix from each previous reader as incomplete."""
        for path in (self.root / "live").glob(f"*{_SUFFIX}"):
            trace = read_trace(path, self.policy)
            if not isinstance(trace, MemoryTrace) or path.stem != trace.header.reservation_id:
                continue
            observations: list[TraceRecord] = [trace.header, *trace.compile_samples]
            elapsed = trace.compile_samples[-1].elapsed_ms if trace.compile_samples else 0
            if isinstance(trace.test_start, TestStarted):
                observations.append(trace.test_start)
                observations.extend(trace.test_samples)
                elapsed = trace.test_samples[-1].elapsed_ms if trace.test_samples else trace.test_start.elapsed_ms
            ended = trace.header.scope_started_at_ms + elapsed
            if isinstance(trace.state, TraceEnd):
                elapsed, ended = trace.state.elapsed_ms, trace.state.ended_at_ms
            end = TraceEnd(ended, elapsed, ExitStatusUnavailable(), IncompleteRun(IncompleteReason.RESTART))
            content = b"".join(encode_record(record) for record in (*observations, end))
            if len(content) <= self.policy.max_bytes_per_run:
                destination = _sealed_path(self.root / "finished" / trace.header.shape_key, trace.header, end)
                if not destination.exists():
                    write_bytes_atomic(destination, content, mode=0o600)
                path.unlink(missing_ok=True)

    def newest_whole_run(self, key: str, *, exclude_reservations: Collection[str] = ()
                         ) -> MemoryTrace | NoMatchingWholeRun:
        """Read the newest eligible trace, using the reservation ID to break ties."""
        folder = self.root / "finished" / _name(key, _SHAPE_NAME)
        for candidate in sorted(_finished_files(folder), reverse=True):
            if candidate.reservation_id in exclude_reservations:
                continue
            trace = read_trace(candidate.path, self.policy)
            if (isinstance(trace, MemoryTrace) and isinstance(trace.state, TraceEnd)
                    and isinstance(trace.state.coverage, WholeRun) and trace.state.ended_at_ms == candidate.ended_at_ms
                    and trace.header.reservation_id == candidate.reservation_id and trace.header.shape_key == key):
                return trace
        return NoMatchingWholeRun()

    def prune(self, now_ms: int, active_reservations: Collection[str]) -> None:
        """Remove aged files and keep the newest finished files within the count limit."""
        cutoff_ms = now_ms - self.policy.retention_days * _MS_PER_DAY
        folders = {folder for folder in (self.root / "finished").glob("*")
                   if _SHAPE_NAME.fullmatch(folder.name) and folder.is_dir()}
        files = (file for folder in folders for file in _finished_files(folder))
        for index, file in enumerate(sorted(files, reverse=True)):
            if file.ended_at_ms < cutoff_ms or index >= self.policy.max_finished_runs:
                file.path.unlink(missing_ok=True)
            else:
                folders.discard(file.path.parent)
        for folder in folders:
            try:
                folder.rmdir()
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                    raise
        for path in (self.root / "live").glob(f"*{_SUFFIX}"):
            if path.stem in active_reservations or not _RESERVATION_NAME.fullmatch(path.stem):
                continue
            try:
                modified_ms = path.stat().st_mtime_ns // 1_000_000
            except FileNotFoundError:
                continue
            if modified_ms < cutoff_ms:
                path.unlink(missing_ok=True)


@dataclass(frozen=True)
class ChartPoint:
    elapsed_ms: int
    program_bytes: int


@dataclass(frozen=True)
class PhaseChart:
    duration_ms: int
    segments: tuple[tuple[ChartPoint, ...], ...]
    peak: ChartPoint
    reserve_bytes: int | TestReserveUnavailable
    above_half_reserve_ms: int | TestReserveUnavailable


@dataclass(frozen=True)
class NoPhaseSamples:
    pass


@dataclass(frozen=True)
class HanaCrateBand:
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class ProgramMemoryCharts:
    compile: PhaseChart | NoPhaseSamples
    test: PhaseChart | NoPhaseSamples
    hana_crate_bands: tuple[HanaCrateBand, ...]

    @property
    def hana_compile_ms(self) -> int:
        return sum(band.end_ms - band.start_ms for band in self.hana_crate_bands)


def _project_phase(samples: Sequence[CompileSample | TestSample], start_ms: int, end_ms: int,
                   reserve: int | TestReserveUnavailable, max_gap_ms: int,
                   points_per_phase: int) -> PhaseChart | NoPhaseSamples:
    if not samples:
        return NoPhaseSamples()
    peak_index = max(range(len(samples)), key=lambda index: samples[index].program_bytes)
    keep = {0, peak_index, len(samples) - 1}
    candidates = [index for index in range(len(samples)) if index not in keep]
    slots = min(points_per_phase - len(keep), len(candidates))
    keep.update(candidates[(2 * index + 1) * len(candidates) // (2 * slots)] for index in range(slots))
    segments: list[list[ChartPoint]] = []
    segment: list[ChartPoint] = []
    above_ms = 0
    previous = samples[0]
    for index, sample in enumerate(samples):
        interval = sample.elapsed_ms - previous.elapsed_ms
        if interval > max_gap_ms:
            if segment:
                segments.append(segment)
                segment = []
        elif isinstance(reserve, int) and 2 * previous.program_bytes > reserve:
            above_ms += interval
        if index in keep:
            segment.append(ChartPoint(sample.elapsed_ms - start_ms, sample.program_bytes))
        previous = sample
    if segment:
        segments.append(segment)
    peak = samples[peak_index]
    return PhaseChart(end_ms - start_ms, tuple(tuple(points) for points in segments),
                      ChartPoint(peak.elapsed_ms - start_ms, peak.program_bytes), reserve,
                      above_ms if isinstance(reserve, int) else reserve)


def _hana_bands(samples: Sequence[CompileSample], max_gap_ms: int) -> Iterator[HanaCrateBand]:
    band: HanaCrateBand | None = None
    for sample in samples:
        last_unit = (sample.last_compiling == "hana" and isinstance(sample.progress, CargoProgress)
                     and sample.progress.build_done == sample.progress.build_total - 1)
        if band is not None and (not last_unit or sample.elapsed_ms - band.end_ms > max_gap_ms):
            yield band
            band = None
        if last_unit:
            band = HanaCrateBand(sample.elapsed_ms if band is None else band.start_ms, sample.elapsed_ms)
    if band is not None:
        yield band


def project_trace(trace: MemoryTrace, policy: MemoryTracePolicy) -> ProgramMemoryCharts:
    """Keep chart endpoints and peaks. Use all readings to calculate phase values."""
    gap = trace.header.max_gap_ms
    if isinstance(trace.state, TraceEnd):
        ended = trace.state.elapsed_ms
    elif trace.test_samples:
        ended = trace.test_samples[-1].elapsed_ms
    elif isinstance(trace.test_start, TestStarted):
        ended = trace.test_start.elapsed_ms
    else:
        ended = trace.compile_samples[-1].elapsed_ms if trace.compile_samples else 0
    compile_end = trace.test_start.elapsed_ms if isinstance(trace.test_start, TestStarted) else ended
    compile_chart = _project_phase(trace.compile_samples, 0, compile_end, trace.header.compile_reserve_bytes,
                                  gap, policy.points_per_phase)
    test_chart = (_project_phase(trace.test_samples, trace.test_start.elapsed_ms, ended,
                                trace.test_start.reserve_bytes, gap, policy.points_per_phase)
                  if isinstance(trace.test_start, TestStarted) else NoPhaseSamples())
    return ProgramMemoryCharts(compile_chart, test_chart, tuple(_hana_bands(trace.compile_samples, gap)))


@dataclass(frozen=True)
class TraceTestAdmission:
    """The frozen test need and any charge that admission already recorded."""
    need: records.TestPhaseNeed | gate.NoTestPhaseNeed
    charged_bytes: int | TestReserveUnavailable

    def reserve(self, phase: test_phase.ObservedTestPhase | test_phase.UnobservedTestPhase
                ) -> int | TestReserveUnavailable:
        if isinstance(self.charged_bytes, int):
            return self.charged_bytes
        if isinstance(phase, test_phase.ObservedTestPhase) and isinstance(self.need, records.TestPhaseNeed):
            return gate.test_phase_charge(records.TestPhaseStarted(phase.started_at, phase.peak_bytes, self.need))
        return TestReserveUnavailable()


@dataclass(frozen=True)
class TraceGrant:
    reservation_id: str
    shape: HanaTraceShape
    row: records.Reservation
    test_admission: TraceTestAdmission
    wall: float
    monotonic: float


@dataclass(frozen=True)
class TraceRelease:
    reservation_id: str
    wall: float
    monotonic: float
    status: int | ExitStatusUnavailable
    test_admission: TraceTestAdmission


@dataclass(frozen=True)
class TraceScopeReading:
    memory: records.StepMemory
    phase: test_phase.ObservedTestPhase | test_phase.UnobservedTestPhase


@dataclass(frozen=True)
class TraceTick:
    wall: float
    monotonic: float
    reservations: tuple[str, ...]


@dataclass(frozen=True)
class _ScopePending:
    """The grant has no verified scope yet."""


@dataclass(frozen=True)
class _ScopeGeneration:
    """Keep the directory open so a new scope cannot reuse its identity."""
    directory: Path
    descriptor: int
    identity: os.stat_result

    def matches(self, sidecar: Path) -> bool:
        try:
            return (sidecar.read_text().strip() == str(self.directory)
                    and os.path.samestat(self.identity, self.directory.stat()))
        except (FileNotFoundError, UnicodeError):
            return False


@dataclass
class _BoundTrace:
    scope: _ScopeGeneration
    started_monotonic: float
    writer: TraceWriter
    log: progress.CaptureReader
    log_path: Path
    saw_completion: bool = False

    def elapsed_ms(self, monotonic: float) -> int:
        return max(0, round((monotonic - self.started_monotonic) * _MS_PER_SECOND))

    def close(self) -> None:
        try:
            self.log.close_trace_log()
        finally:
            os.close(self.scope.descriptor)


@dataclass
class _TraceReservation:
    grant: TraceGrant
    due: float
    capture: _ScopePending | _BoundTrace = _ScopePending()

    @property
    def sidecar(self) -> Path:
        return Path(self.grant.row["sidecar"])


@final
class TraceCapture:
    """Record scope traces on the operating system reader thread."""

    def __init__(self, store: TraceStore, capture_root: Path, boot_id: str, *,
                 process_start: Callable[[int], str | None] = records.process_start_time) -> None:
        self.store = store
        self.capture_root = capture_root
        self.boot_id = boot_id
        self.process_start = process_start
        self.commands: SimpleQueue[TraceGrant | TraceRelease] = SimpleQueue()
        self._reservations: dict[str, _TraceReservation] = {}
        self._next_prune = float("-inf")
        self._needs_prune = False

    def submit(self, command: TraceGrant | TraceRelease) -> None:
        """Send one fact from admission without reading files there."""
        self.commands.put(command)

    @property
    def deadline(self) -> float:
        return min((reservation.due for reservation in self._reservations.values()), default=float("inf"))

    def recover(self) -> None:
        try:
            self.store.recover_abandoned()
        except (OSError, ValueError) as error:
            self._report(error)

    def maintain(self, wall: float) -> None:
        """Prune after the reader publishes any full reading from this tick."""
        if not self._needs_prune and wall < self._next_prune:
            return
        try:
            self.store.prune(round(wall * _MS_PER_SECOND), self._reservations.keys())
        except (OSError, ValueError) as error:
            self._report(error)
        self._needs_prune = False
        self._next_prune = wall + _MS_PER_DAY / _MS_PER_SECOND

    def prepare(self, wall: float, monotonic: float) -> TraceTick:
        """Apply releases and verify due scopes before any memory read."""
        self._drain()
        due: list[str] = []
        for identifier, reservation in tuple(self._reservations.items()):
            if identifier not in self._reservations or reservation.due > monotonic:
                continue
            reservation.due = monotonic + self.store.policy.sample_seconds
            try:
                if isinstance(reservation.capture, _ScopePending):
                    reservation.capture = self._bind(reservation, wall, monotonic)
                if isinstance(reservation.capture, _BoundTrace):
                    if reservation.capture.scope.matches(reservation.sidecar):
                        due.append(identifier)
                    else:
                        self._scope_lost(reservation, wall, monotonic)
            except (OSError, ValueError) as error:
                self._report(error)
                self._forget(identifier)
        return TraceTick(wall, monotonic, tuple(due))

    def _drain(self) -> None:
        while True:
            try:
                command = self.commands.get_nowait()
            except Empty:
                return
            if isinstance(command, TraceGrant):
                if command.reservation_id not in self._reservations:
                    self._reservations[command.reservation_id] = _TraceReservation(command, command.monotonic)
                continue
            reservation = self._reservations.get(command.reservation_id)
            if reservation is None:
                continue
            try:
                self._finish(reservation, command)
            except (OSError, ValueError) as error:
                self._report(error)
            finally:
                self._forget(command.reservation_id)

    def _bind(self, reservation: _TraceReservation, wall: float, monotonic: float
              ) -> _ScopePending | _BoundTrace:
        try:
            published = reservation.sidecar.stat().st_mtime
            if published < reservation.grant.wall:
                return _ScopePending()
            lifetime = gate.reservation_lifetime(reservation.grant.row, True)
            if not isinstance(lifetime, gate.PopulatedStepScope):
                return _ScopePending()
            descriptor = os.open(lifetime.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except FileNotFoundError:
            return _ScopePending()
        try:
            scope = _ScopeGeneration(lifetime.directory, descriptor, os.fstat(descriptor))
            if not scope.matches(reservation.sidecar):
                os.close(descriptor)
                return _ScopePending()
            grant = reservation.grant
            started = min(published, wall)
            started_monotonic = max(grant.monotonic, monotonic - (wall - started))
            header = TraceHeader(grant.reservation_id, shape_key(grant.shape), round(started * _MS_PER_SECOND),
                                 self.store.policy.sample_interval_ms, grant.row["need"])
            writer = self.store.start(header)
            return _BoundTrace(scope, started_monotonic, writer,
                               progress.CaptureReader(self.capture_root, track_trace=True),
                               progress.progress_path(reservation.sidecar, "log"))
        except BaseException:
            os.close(descriptor)
            raise

    def collect(self, tick: TraceTick, full: Mapping[str, TraceScopeReading],
                test_admission: Mapping[str, TraceTestAdmission]) -> None:
        """Write only due scope samples. Use matching raw evidence from a full read."""
        self._drain()
        for identifier in tick.reservations:
            reservation = self._reservations.get(identifier)
            if reservation is None or isinstance(reservation.capture, _ScopePending):
                continue
            capture = reservation.capture
            try:
                evidence = full.get(identifier)
                if evidence is None:
                    evidence = self._read_scope(reservation, capture.scope)
                elif (not isinstance(evidence.memory, records.MeasuredMemory)
                        or not isinstance(evidence.memory.anonymous_source, records.ScopeSource)
                        or evidence.memory.anonymous_source.directory != capture.scope.directory):
                    evidence = replace(evidence, memory=records.GoneMemory())
                if not capture.scope.matches(reservation.sidecar):
                    self._scope_lost(reservation, tick.wall, tick.monotonic)
                    continue
                capture_log = self._read_log(capture, evidence.memory)
                admission = test_admission.get(identifier, reservation.grant.test_admission)
                elapsed_ms = capture.elapsed_ms(tick.monotonic)
                if self._observe_phase(capture, evidence.phase, elapsed_ms, admission, capture_log):
                    reservation.due = float("inf")
                    continue
                memory = evidence.memory
                if not isinstance(memory, records.MeasuredMemory) or not isinstance(memory.anonymous, records.AnonymousRunUse):
                    continue
                if isinstance(capture_log, progress.TraceCaptureReading) and not capture_log.caught_up:
                    continue
                if isinstance(capture.writer.test_start, TestStarted):
                    sample = TestSample(elapsed_ms, memory.anonymous.bytes)
                else:
                    sample = self._compile_sample(elapsed_ms, memory.anonymous.bytes, capture_log)
                if isinstance(capture.writer.append(sample), TraceLimit):
                    reservation.due = float("inf")
            except (OSError, ValueError) as error:
                self._report(error)
                self._forget(identifier)

    @staticmethod
    def _read_scope(reservation: _TraceReservation, scope: _ScopeGeneration) -> TraceScopeReading:
        phase = gate.observed_test_phase(reservation.grant.row)
        lifetime = gate.reservation_lifetime(reservation.grant.row, True)
        memory: records.StepMemory = records.GoneMemory()
        if isinstance(lifetime, gate.PopulatedStepScope) and lifetime.directory == scope.directory:
            try:
                memory = gate.slice_working_set(scope.directory)
            except (OSError, ValueError):
                pass
        return TraceScopeReading(memory, phase)

    def _read_log(self, capture: _BoundTrace, memory: records.StepMemory
                  ) -> progress.TraceCaptureReading | progress.NoTraceCapture:
        reading = capture.log.trace_log(capture.log_path)
        if isinstance(reading, progress.TraceCaptureReading) or not self.boot_id:
            return reading
        if isinstance(memory, records.MeasuredMemory) and isinstance(memory.anonymous_source, records.ScopeSource):
            members = memory.anonymous_source.members
        else:
            members = gate.scope_members(capture.scope.directory)
        identities = {pid: start for pid in members if (start := self.process_start(pid)) is not None}
        return capture.log.trace_terminal(identities, self.boot_id)

    @staticmethod
    def _observe_phase(capture: _BoundTrace, phase: test_phase.ObservedTestPhase | test_phase.UnobservedTestPhase,
                       elapsed_ms: int, admission: TraceTestAdmission,
                       log: progress.TraceCaptureReading | progress.NoTraceCapture) -> bool:
        writer = capture.writer
        start = writer.test_start
        log_started = isinstance(log, progress.TraceCaptureReading) and log.frames.test_started
        if isinstance(start, TestNotStarted) and (log_started or isinstance(phase, test_phase.ObservedTestPhase)):
            observed_ms = elapsed_ms
            if isinstance(phase, test_phase.ObservedTestPhase):
                observed_ms = round(phase.started_at * _MS_PER_SECOND) - writer.header.scope_started_at_ms
            observed_ms = min(elapsed_ms, max(0, writer.last_sample_ms, observed_ms))
            if isinstance(writer.append(TestStarted(observed_ms, admission.reserve(phase))), TraceLimit):
                return True
        elif (isinstance(start, TestStarted) and not isinstance(start.reserve_bytes, int)
              and isinstance(reserve := admission.reserve(phase), int)):
            if isinstance(writer.append(TestReserveObserved(reserve)), TraceLimit):
                return True
        if isinstance(log, progress.TraceCaptureReading) and log.frames.test_completed:
            capture.saw_completion = True
        return capture.saw_completion

    @staticmethod
    def _compile_sample(elapsed_ms: int, used: int, log: progress.TraceCaptureReading | progress.NoTraceCapture
                        ) -> CompileSample:
        if isinstance(log, progress.TraceCaptureReading):
            counter = log.counter
            if isinstance(counter, progress.ProgressCounter) and counter.phase.startswith("build:"):
                return CompileSample(elapsed_ms, used, CargoProgress(counter.done, counter.total),
                                     log.frames.last_compiling)
            return CompileSample(elapsed_ms, used, CargoProgressUnavailable(), log.frames.last_compiling)
        return CompileSample(elapsed_ms, used, CargoProgressUnavailable(), CompilingCrateUnavailable())

    def _finish(self, reservation: _TraceReservation, release: TraceRelease) -> None:
        capture = reservation.capture
        if isinstance(capture, _ScopePending):
            return
        elapsed_ms = capture.elapsed_ms(release.monotonic)
        phase = gate.observed_test_phase(reservation.grant.row)
        log = capture.log.trace_tail(final=True)
        if isinstance(log, progress.NoTraceCapture):
            log = capture.log.trace_log(capture.log_path, final=True)
        _ = self._observe_phase(capture, phase, elapsed_ms, release.test_admission, log)
        coverage: WholeRun | IncompleteRun
        if isinstance(release.status, ExitStatusUnavailable):
            coverage = IncompleteRun(IncompleteReason.SCOPE_LOST)
        elif not capture.saw_completion:
            coverage = IncompleteRun(IncompleteReason.NO_NEXTTEST_COMPLETION)
        else:
            coverage = WholeRun()
        _ = capture.writer.seal(TraceEnd(round(release.wall * _MS_PER_SECOND), elapsed_ms, release.status, coverage))
        self._needs_prune = True

    def _scope_lost(self, reservation: _TraceReservation, wall: float, monotonic: float) -> None:
        self._drain()
        if reservation.grant.reservation_id not in self._reservations:
            return
        try:
            self._finish(reservation, TraceRelease(reservation.grant.reservation_id, wall, monotonic,
                         ExitStatusUnavailable(), reservation.grant.test_admission))
        finally:
            self._forget(reservation.grant.reservation_id)

    def _forget(self, identifier: str) -> None:
        reservation = self._reservations.pop(identifier, None)
        if reservation is not None and isinstance(reservation.capture, _BoundTrace):
            reservation.capture.close()

    def close(self) -> None:
        """Finish queued releases and leave other traces for restart recovery."""
        self._drain()
        for identifier in tuple(self._reservations):
            self._forget(identifier)

    @staticmethod
    def _report(error: OSError | ValueError) -> None:
        print(f"memory trace: {error}", file=sys.stderr)
