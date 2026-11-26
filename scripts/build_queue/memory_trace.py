"""Store bounded memory traces and project the Hana nextest chart data."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from enum import StrEnum
import errno
from functools import cache
import hashlib
import json
from math import floor
import os
from pathlib import Path
import re
import sys
from threading import Condition, Thread
from time import monotonic
from typing import Protocol, cast, final
from uuid import uuid4

from scripts.build_queue import admission_records as records, progress_profile as progress
from scripts.build_queue.admission_records import RequestedTestWidth, UnspecifiedTestWidth
from scripts.build_queue.admission_wire import CLEANUP_STATUS_MAX as _MAX_INTEGER, CLEANUP_STATUS_MIN
from scripts.build_queue.progress_profile import CompilingCrateUnavailable as CompilingCrateUnavailable
from scripts.buildlog import record as buildlog_record
from scripts.lib import conf, test_phase
from scripts.lib.atomic_file import sync_folder, write_bytes_atomic
from scripts.lint import memory_admit as gate


_POLICY_SETTINGS = {
    "sample_seconds": ("sample.seconds", 1, 60),
    "max_samples_per_run": ("sample.max_per_run", 1, 65536),
    "max_bytes_per_run": ("sample.max_bytes_per_run", 1024, 4194304),
    "past_run_max_age_days": ("feed.past_run_max_age_days", 1, 90),
    "points_per_phase": ("feed.points_per_phase", 3, 4096),
    "feed_bytes_per_refresh": ("feed.bytes_per_refresh", 1024, 1048576),
    "final_replay_bytes": ("seal.replay_bytes", 1, 1048576),
    "scope_loss_grace_seconds": ("seal.scope_loss_grace_seconds", 1, 60),
    "seal_retry_count": ("seal.retry_count", 0, 10),
    "max_pending_commands": ("worker.max_pending_commands", 2, 4096),
    "shutdown_timeout_seconds": ("worker.shutdown_timeout_seconds", 1, 60),
}

# Each general trace can use two slots. Allow general traces to use at most half of all slots.
_GENERAL_TRACE_LIMIT_DIVISOR = 4


@dataclass(frozen=True)
class MemoryTracePolicy:
    sample_seconds: int
    max_samples_per_run: int
    max_bytes_per_run: int
    past_run_max_age_days: int
    points_per_phase: int
    feed_bytes_per_refresh: int
    final_replay_bytes: int
    max_pending_commands: int
    scope_loss_grace_seconds: int = _POLICY_SETTINGS["scope_loss_grace_seconds"][1]
    seal_retry_count: int = _POLICY_SETTINGS["seal_retry_count"][1]
    shutdown_timeout_seconds: int = _POLICY_SETTINGS["shutdown_timeout_seconds"][1]

    def __post_init__(self) -> None:
        for setting in fields(self):
            key, policy_low, high = _POLICY_SETTINGS[setting.name]
            # Direct callers can use small byte budgets to test the limits.
            low = 1 if setting.name in ("max_bytes_per_run", "feed_bytes_per_refresh") else policy_low
            value = cast(object, getattr(self, setting.name))
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise ValueError(f"{key} must be in {low}..{high}")

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
    def whole(key: str, low: int, high: int) -> int:
        reading = configuration.origin(key, int)
        if not isinstance(reading, conf.UsableLiveValue):
            raise conf.Unusable(reading.reason)
        if not low <= reading.value <= high:
            raise ValueError(f"{key} must be in {low}..{high}")
        return reading.value

    try:
        configuration = conf.read("memory_trace") if isinstance(settings, conf.UseConfiguredValue) else settings
        return MemoryTracePolicy(**{field: whole(*setting) for field, setting in _POLICY_SETTINGS.items()})
    except (OSError, ValueError, ArithmeticError) as error:
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


@dataclass(frozen=True)
class GeneralTraceShape:
    host: str
    repository_identity: str
    step: str
    argv: tuple[str, ...]


# Keep the argument order and all selection and execution flags in the key.
_VALUE_FLAGS = frozenset({
    "--bin", "--cargo-profile", "--example", "--features", "--filterset", "--package",
    "--profile", "--target", "--test", "--test-threads",
})


def shape_key(shape: HanaTraceShape | GeneralTraceShape) -> str:
    """Hash the command and normalize the spelling of known value flags."""
    if isinstance(shape, GeneralTraceShape):
        return hashlib.sha256(_json_line(("general", shape.host, shape.repository_identity,
                                         shape.step, shape.argv))).hexdigest()
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
_RESERVATION_NAME = re.compile(r"[A-Za-z0-9_-]+")
_SHAPE_NAME = re.compile(r"[0-9a-f]{64}")
_SUFFIX = ".jsonl"
_MS_PER_SECOND = 1000


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
    general: GeneralTraceShape | None = None

    def __post_init__(self) -> None:
        _ = _name(self.reservation_id, _RESERVATION_NAME)
        _ = _name(self.shape_key, _SHAPE_NAME)
        _ = _integer(self.scope_started_at_ms)
        _ = _integer(self.sample_interval_ms, minimum=1)
        _ = _integer(self.compile_reserve_bytes, minimum=1 if self.general is None else 0)
        if self.general is not None and self.shape_key != shape_key(self.general):
            raise ValueError("trace shape key differs from the general shape")

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
    """The recorder has samples for the full run."""


class TraceLimit(StrEnum):
    SAMPLE_COUNT = "sample_limit"
    FILE_SIZE = "byte_limit"


class IncompleteReason(StrEnum):
    NO_NEXTTEST_COMPLETION = "no_nextest_completion"
    SCOPE_LOST = "scope_lost"
    RESTART = "restart"
    MISSING_COMPILE = "missing_compile"
    MISSING_TEST = "missing_test"
    MISSING_SAMPLES = "missing_samples"
    SAMPLE_GAP = "sample_gap"
    NEXTTEST_STATUS = "nextest_status"
    RECORDING_ERROR = "recording_error"


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
            _ = _integer(self.exit_status, minimum=CLEANUP_STATUS_MIN)


type TraceObservation = CompileSample | TestStarted | TestReserveObserved | TestSample
type TraceRecord = TraceHeader | TraceObservation | TraceLimit | TraceEnd


def _json_line(values: Sequence[object]) -> bytes:
    return (json.dumps(values, separators=(",", ":")) + "\n").encode("ascii")


class _TraceCheckUnavailable(ValueError):
    """The reader cannot check the trace with its current limits or format."""


def encode_record(record: TraceRecord) -> bytes:
    """Encode one complete record as a compact JSONL row."""
    match record:
        case TraceHeader(reservation_id, key, started, interval, reserve, GeneralTraceShape(host, repo, step, argv)):
            return _json_line(("h", _VERSION, reservation_id, key, started, interval, reserve, [host, repo, step, argv]))
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
    try:
        value = cast(object, json.loads(line))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise
    except ValueError as error:
        raise _TraceCheckUnavailable(str(error)) from error
    if not isinstance(value, list):
        raise ValueError("expected a trace record")
    match cast(list[object], value):
        case ["h", version, *_] if _integer(version) != _VERSION:
            raise _TraceCheckUnavailable("unsupported trace version")
        case ["h", _, str(reservation_id), str(key), int(started), int(interval), int(reserve)]:
            return TraceHeader(reservation_id, key, started, interval, reserve)
        case ["h", _, str(reservation_id), str(key), int(started), int(interval), int(reserve), general]:
            if not isinstance(general, list):
                raise ValueError("invalid general trace shape")
            match cast(list[object], general):
                case [str(host), str(repo), str(step), argv] if isinstance(argv, list):
                    arguments = tuple(cast(list[object], argv))
                    if all(isinstance(arg, str) for arg in arguments):
                        return TraceHeader(reservation_id, key, started, interval, reserve,
                                           GeneralTraceShape(host, repo, step, cast(tuple[str, ...], arguments)))
                case _:
                    pass
            raise ValueError("invalid general trace shape")
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
    _MAX_INTEGER, _MAX_INTEGER, CLEANUP_STATUS_MIN, IncompleteRun(reason)
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

    @property
    def last_observation_ms(self) -> int:
        start = self.test_start.elapsed_ms if isinstance(self.test_start, TestStarted) else -1
        return max(self.last_sample_ms, start)

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
        if end.elapsed_ms < self.last_observation_ms:
            raise ValueError("trace end precedes an observation")
        if isinstance(state, TraceLimit):
            return replace(end, coverage=IncompleteRun(state))
        if isinstance(end.coverage, IncompleteRun):
            return end
        if header.general is not None:
            if not self.compile_count:
                reason = IncompleteReason.MISSING_SAMPLES
            elif self.has_gap or end.elapsed_ms - self.last_sample_ms > header.max_gap_ms:
                reason = IncompleteReason.SAMPLE_GAP
            else:
                return end
        elif end.exit_status not in (0, 100):
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

    last_complete_state: TraceRecording | TraceLimit | TraceEnd


@dataclass(frozen=True)
class TraceUnavailable:
    reason: str


@dataclass(frozen=True)
class MemoryTrace:
    header: TraceHeader
    compile_samples: tuple[CompileSample, ...]
    test_samples: tuple[TestSample, ...]
    state: TraceRecording | TraceLimit | TraceTailIncomplete | TraceEnd
    _order: _RecordingOrder

    @property
    def test_start(self) -> TestStarted | TestNotStarted:
        return self._order.test_start

    def read_body(self, lines: Iterable[bytes], policy: MemoryTracePolicy) -> MemoryTrace:
        """Read bounded lines after the last complete record."""
        order = self._order
        compile_samples: list[CompileSample] = []
        test_samples: list[TestSample] = []
        state = self.state
        if isinstance(state, TraceTailIncomplete):
            state = state.last_complete_state
        for line in lines:
            if not line.endswith(b"\n"):
                state = TraceTailIncomplete(state)
                break
            record = decode_record(line)
            if isinstance(state, TraceEnd) or isinstance(record, TraceHeader):
                raise ValueError("record outside the trace body")
            if isinstance(record, TraceEnd):
                if record != order.finish(record, self.header, state):
                    raise ValueError("trace coverage disagrees with its records")
                state = record
            elif isinstance(state, TraceLimit):
                raise ValueError("observation follows a trace limit")
            elif isinstance(record, TraceLimit):
                state = record
            else:
                order = order.observe(record, self.header.max_gap_ms)
                if order.sample_count > policy.max_samples_per_run:
                    raise _TraceCheckUnavailable("trace exceeds its sample limit")
                if isinstance(record, CompileSample):
                    compile_samples.append(record)
                elif isinstance(record, TestSample):
                    test_samples.append(record)
        return replace(self, compile_samples=self.compile_samples + tuple(compile_samples),
                       test_samples=self.test_samples + tuple(test_samples), state=state, _order=order)


@dataclass(frozen=True)
class NoMatchingWholeRun:
    pass


def read_trace(path: Path, policy: MemoryTracePolicy, *, raise_unchecked: bool = False
               ) -> MemoryTrace | TraceUnavailable:
    """Read bounded complete lines and validate phase and end order."""
    try:
        with path.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size > policy.max_bytes_per_run:
                raise _TraceCheckUnavailable("trace exceeds its byte limit")
            line = stream.readline(policy.max_bytes_per_run + 1)
            if len(line) > policy.max_bytes_per_run:
                raise _TraceCheckUnavailable("trace exceeds its byte limit")
            if not line.endswith(b"\n"):
                raise ValueError("missing complete trace header")
            header = decode_record(line)
            if not isinstance(header, TraceHeader):
                raise ValueError("trace must start with its header")

            def bounded_body(remaining: int) -> Iterator[bytes]:
                while record_line := stream.readline(remaining + 1):
                    remaining -= len(record_line)
                    if remaining < 0:
                        raise _TraceCheckUnavailable("trace exceeds its byte limit")
                    yield record_line

            trace = MemoryTrace(header, (), (), TraceRecording(), _RecordingOrder())
            return trace.read_body(bounded_body(policy.max_bytes_per_run - len(line)), policy)
    except (OSError, RecursionError, _TraceCheckUnavailable) as error:
        if raise_unchecked:
            raise
        return TraceUnavailable(str(error))
    except ValueError as error:
        return TraceUnavailable(str(error))


@final
class TraceWriter:
    """Write observations for one reservation from the trace thread."""

    def __init__(self, path: Path, finished_folder: Path, header: TraceHeader, policy: MemoryTracePolicy) -> None:
        line = encode_record(header)
        if len(line) + _LIMIT_ROW_BYTES + _END_ROW_BYTES > policy.max_bytes_per_run:
            raise ValueError("trace header and final records exceed the byte limit")
        self.path = path
        self.header = header
        self._finished_folder = finished_folder
        self._policy = policy
        self._order = _RecordingOrder()
        self._state: TraceRecording | TraceLimit | TraceEnd = TraceRecording()
        self._bytes_written = len(line)
        self._saved = False
        self._published = False
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stream = path.open("xb")
        try:
            with stream:
                if stream.write(line) != len(line):
                    raise OSError(errno.EIO, "incomplete trace header")
        except OSError:
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                _report(error, action=f"remove partial header {path}")
            raise

    def _append(self, line: bytes) -> None:
        with self.path.open("r+b", buffering=0) as stream:
            size = stream.seek(0, os.SEEK_END)
            if size < self._bytes_written:
                raise ValueError("trace lost verified bytes")
            if size > self._bytes_written:
                _ = stream.truncate(self._bytes_written)
                _ = stream.seek(self._bytes_written)
            if stream.write(line) != len(line):
                raise OSError(errno.EIO, "incomplete trace write")
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
        if isinstance(self._state, TraceEnd):
            end = self._state
        else:
            end = self._order.finish(end, self.header, self._state)
            self._append(encode_record(end))
            self._state = end
        if not self._saved:
            self._finished_folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination = _sealed_path(self._finished_folder, self.header, end)
            with self.path.open("rb") as stream:
                os.fsync(stream.fileno())
            _ = self.path.replace(destination)
            self._saved = True
        if not self._published:
            sync_folder(self._finished_folder)
            sync_folder(self.path.parent)
            self._published = True
        return end

    def seal_prefix(self, end: TraceEnd) -> None:
        """Publish the verified prefix and keep the live file."""
        if self._saved:
            return
        state = self._state
        suffix = b""
        if isinstance(state, TraceEnd):
            end = state
        else:
            reason = state if isinstance(state, TraceLimit) else IncompleteReason.RECORDING_ERROR
            end = replace(end, elapsed_ms=max(end.elapsed_ms, self._order.last_observation_ms), coverage=IncompleteRun(reason))
            suffix = encode_record(end)
        with self.path.open("rb") as stream:
            prefix = stream.read(self._bytes_written)
        if len(prefix) != self._bytes_written:
            raise OSError(errno.EIO, "trace lost verified bytes")
        destination = _sealed_path(self._finished_folder, self.header, end)
        write_bytes_atomic(destination, prefix + suffix, mode=0o600)
        self._state = end
        self._saved = True
        self._published = True


def _sealed_path(folder: Path, header: TraceHeader, end: TraceEnd) -> Path:
    return folder / f"{end.ended_at_ms}-{header.reservation_id}{_SUFFIX}"


@dataclass(frozen=True, order=True)
class _SealedTraceFile:
    ended_at_ms: int
    reservation_id: str
    path: Path


def _directory_entries(folder: Path, *, raise_io_errors: bool = False) -> Iterator[Path]:
    try:
        yield from folder.iterdir()
    except FileNotFoundError:
        pass
    except OSError as error:
        if raise_io_errors:
            raise
        _report(error, action=f"scan {folder}")


def _trace_paths(entries: Iterable[Path]) -> Iterator[Path]:
    return (entry for entry in entries if entry.suffix == _SUFFIX)


def _finished_files(entries: Iterable[Path]) -> Iterator[_SealedTraceFile]:
    for path in _trace_paths(entries):
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

    def _finished_folder(self, header: TraceHeader) -> Path:
        return self.root / "finished" / header.shape_key

    def _finished_for(self, header: TraceHeader, *, raise_io_errors: bool = False) -> Iterator[_SealedTraceFile]:
        entries = _directory_entries(self._finished_folder(header), raise_io_errors=raise_io_errors)
        return (file for file in _finished_files(entries) if file.reservation_id == header.reservation_id)

    def _read_finished(self, candidate: _SealedTraceFile, shape_key: str, *, raise_unchecked: bool = False
                       ) -> MemoryTrace | None:
        trace = read_trace(candidate.path, self.policy, raise_unchecked=raise_unchecked)
        if isinstance(trace, TraceUnavailable):
            _report(trace.reason, action=f"read {candidate.path}")
            return None
        if (isinstance(trace.state, TraceEnd) and trace.state.ended_at_ms == candidate.ended_at_ms
                and trace.header.reservation_id == candidate.reservation_id and trace.header.shape_key == shape_key):
            return trace
        return None

    def start(self, header: TraceHeader) -> TraceWriter:
        """Start a fresh trace. A prior trace for this reservation blocks a restart."""
        if next(self._finished_for(header), None) is not None:
            raise FileExistsError(header.reservation_id)
        return TraceWriter(self._live_path(header.reservation_id), self._finished_folder(header), header, self.policy)

    def read_live(self, reservation_id: str) -> MemoryTrace | TraceUnavailable:
        trace = read_trace(self._live_path(reservation_id), self.policy)
        if isinstance(trace, MemoryTrace) and trace.header.reservation_id != reservation_id:
            return TraceUnavailable("trace belongs to another reservation")
        return trace

    def peaks_for(self, identifiers: set[str]) -> dict[str, int]:
        """Read sample peaks for the requested reservations."""
        peaks: dict[str, int] = {}
        remaining = {identifier for identifier in identifiers if _RESERVATION_NAME.fullmatch(identifier)}

        def record(trace: MemoryTrace) -> bool:
            peak = max((sample.program_bytes for sample in (*trace.compile_samples, *trace.test_samples)), default=None)
            if peak is None:
                return False
            peaks[trace.header.reservation_id] = peak
            return True

        if not remaining:
            return peaks
        for folder in _directory_entries(self.root / "finished"):
            if not _SHAPE_NAME.fullmatch(folder.name):
                continue
            for candidate in _finished_files(_directory_entries(folder)):
                if candidate.reservation_id not in remaining:
                    continue
                trace = self._read_finished(candidate, folder.name)
                if trace is not None and record(trace):
                    remaining.remove(candidate.reservation_id)
                    if not remaining:
                        return peaks
        for identifier in remaining:
            trace = self.read_live(identifier)
            if isinstance(trace, MemoryTrace):
                _ = record(trace)
        return peaks

    def recover_abandoned(self) -> None:
        """Save an incomplete copy of each readable trace from a previous worker."""
        for path in _trace_paths(_directory_entries(self.root / "live")):
            try:
                self._recover_file(path)
            except Exception as error:
                _report(error, action=f"recover {path}")

    def _recover_file(self, path: Path) -> None:
        trace = read_trace(path, self.policy)
        if isinstance(trace, TraceUnavailable):
            _report(trace.reason, action=f"recover {path}")
            return
        if path.stem != trace.header.reservation_id:
            _report("reservation ID differs from the file name", action=f"recover {path}")
            return
        invalid_paths: set[Path] = set()
        for file in self._finished_for(trace.header, raise_io_errors=True):
            if self._read_finished(file, trace.header.shape_key, raise_unchecked=True) is not None:
                return
            invalid_paths.add(file.path)
        observations: list[TraceRecord] = [trace.header, *trace.compile_samples]
        elapsed = trace.compile_samples[-1].elapsed_ms if trace.compile_samples else 0
        if isinstance(trace.test_start, TestStarted):
            observations.extend((trace.test_start, *trace.test_samples))
            elapsed = trace.test_samples[-1].elapsed_ms if trace.test_samples else trace.test_start.elapsed_ms
        ended = trace.header.scope_started_at_ms + elapsed
        state = trace.state.last_complete_state if isinstance(trace.state, TraceTailIncomplete) else trace.state
        if isinstance(state, TraceEnd):
            elapsed, ended = state.elapsed_ms, state.ended_at_ms
        end = TraceEnd(ended, elapsed, ExitStatusUnavailable(), IncompleteRun(IncompleteReason.RESTART))
        content = b"".join(encode_record(record) for record in (*observations, end))
        if len(content) > self.policy.max_bytes_per_run:
            _report("verified prefix exceeds the byte limit", action=f"recover {path}")
            return
        destination = _sealed_path(self._finished_folder(trace.header), trace.header, end)
        if destination in invalid_paths:
            _ = destination.rename(destination.with_suffix(f".{uuid4().hex}.invalid"))
        else:
            try:
                _ = destination.lstat()
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(f"recovery destination was not checked: {destination}")
        write_bytes_atomic(destination, content, mode=0o600)

    def newest_whole_run(self, key: str, *, exclude_reservations: Collection[str] = ()
                         ) -> MemoryTrace | NoMatchingWholeRun:
        """Read the newest eligible trace, using the reservation ID to break ties."""
        folder = self.root / "finished" / _name(key, _SHAPE_NAME)
        for candidate in sorted(_finished_files(_directory_entries(folder)), reverse=True):
            if candidate.reservation_id in exclude_reservations:
                continue
            trace = self._read_finished(candidate, key)
            if trace is not None and isinstance(trace.state, TraceEnd) and isinstance(trace.state.coverage, WholeRun):
                return trace
        return NoMatchingWholeRun()


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
class TraceGrant:
    reservation_id: str
    shape: HanaTraceShape | GeneralTraceShape
    row: records.Reservation
    wall: float
    monotonic: float
    process_tree: bool = False


@dataclass(frozen=True)
class TraceRelease:
    reservation_id: str
    wall: float
    monotonic: float
    status: int | ExitStatusUnavailable
    test_reserve: int | TestReserveUnavailable = TestReserveUnavailable()


type TraceCommand = TraceGrant | TraceRelease | Mapping[str, int]


class TraceClock(Protocol):
    def now(self) -> float: ...
    def monotonic(self) -> float: ...


@dataclass(frozen=True)
class _ScopeGeneration:
    """Keep the directory open so a new scope cannot reuse its identity."""
    directory: Path
    descriptor: int
    identity: os.stat_result

    @property
    def open_directory(self) -> Path:
        return Path("/proc/self/fd") / str(self.descriptor)

    def matches(self, sidecar: Path | None) -> bool:
        try:
            return ((sidecar is None or sidecar.read_text().strip() == str(self.directory))
                    and os.path.samestat(self.identity, self.directory.stat()))
        except (FileNotFoundError, UnicodeError):
            return False
        except OSError as error:
            if error.errno == errno.ENODEV:
                return False
            raise


@dataclass
class _BoundTrace:
    scope: _ScopeGeneration | None
    started_monotonic: float
    started_wall: float
    writer: TraceWriter
    log: progress.CaptureReader | None
    log_path: Path
    sampling: bool = True
    saw_completion: bool = False
    failed: bool = False

    def elapsed_ms(self, monotonic: float) -> int:
        return max(0, round((monotonic - self.started_monotonic) * _MS_PER_SECOND))

    def close(self) -> None:
        try:
            if self.log is not None:
                self.log.close_trace_log()
        finally:
            if self.scope is not None:
                os.close(self.scope.descriptor)


@dataclass
class _TraceReservation:
    grant: TraceGrant
    due: float
    capture: _BoundTrace | None = None
    test_reserve: int | TestReserveUnavailable = TestReserveUnavailable()
    scope_loss: TraceRelease | None = None

    @property
    def sidecar(self) -> Path:
        return Path(self.grant.row["sidecar"])


@dataclass(frozen=True)
class _PendingSeal:
    writer: TraceWriter
    end: TraceEnd
    test_start: TestStarted | None
    due: float = 0
    retries: int = 0


@final
class TraceCapture:
    """Own the traces, sampling deadlines, and files on the trace thread."""

    def __init__(self, store: TraceStore, capture_root: Path, boot_id: str, clock: TraceClock, *,
                 process_start: Callable[[int], str | None] = records.process_start_time, proc: Path = gate.PROC) -> None:
        self.store = store
        self.capture_root = capture_root
        self.boot_id = boot_id
        self.clock = clock
        self.process_start = process_start
        self.proc = proc
        self._reservations: dict[str, _TraceReservation] = {}
        self._pending_seals: dict[str, _PendingSeal] = {}

    @property
    def deadline(self) -> float:
        return min(min((reservation.due for reservation in self._reservations.values()), default=float("inf")),
                   min((seal.due for seal in self._pending_seals.values()), default=float("inf")))

    def recover(self) -> None:
        self.store.recover_abandoned()

    def process(self, command: TraceCommand) -> None:
        if isinstance(command, TraceGrant):
            if command.reservation_id not in self._pending_seals:
                _ = self._reservations.setdefault(command.reservation_id, _TraceReservation(command, command.monotonic))
        elif isinstance(command, TraceRelease):
            self._release(command)
        else:
            self._record_reserves(command)

    def _release(self, release: TraceRelease) -> None:
        reservation = self._reservations.get(release.reservation_id)
        if reservation is None:
            return
        try:
            if isinstance(release.test_reserve, int):
                self._record_reserves({release.reservation_id: release.test_reserve})
            loss = reservation.scope_loss
            if loss is not None and release.monotonic >= reservation.due:
                release = loss
            self._finish(reservation, release)
        except Exception as error:
            _report(error)
        finally:
            self._forget(release.reservation_id)

    def _record_reserves(self, reserves: Mapping[str, int]) -> None:
        for identifier, reserve in reserves.items():
            reservation = self._reservations.get(identifier)
            if reservation is None or isinstance(reservation.test_reserve, int):
                continue
            reservation.test_reserve = reserve
            capture = reservation.capture
            if capture is not None and not capture.failed:
                writer = capture.writer
                start = writer.test_start
                if isinstance(start, TestStarted) and not isinstance(start.reserve_bytes, int):
                    try:
                        _ = writer.append(TestReserveObserved(reserve))
                    except Exception as error:
                        self._recording_failed(capture, error)

    def _bind(self, reservation: _TraceReservation) -> _BoundTrace | None:
        try:
            with reservation.sidecar.open() as sidecar:
                published = os.fstat(sidecar.fileno()).st_mtime
                location = sidecar.read().strip()
            if published < reservation.grant.wall or not location:
                return None
            directory = Path(location)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except (FileNotFoundError, UnicodeError):
            return None
        try:
            scope = _ScopeGeneration(directory, descriptor, os.fstat(descriptor))
            if not scope.matches(None) or not self._scope_populated(scope):
                os.close(descriptor)
                return None
            wall, monotonic = self.clock.now(), self.clock.monotonic()
            started = min(published, wall)
            started_monotonic = max(reservation.grant.monotonic, monotonic - (wall - started))
            return self._begin(reservation, scope, started, started_monotonic)
        except BaseException:
            os.close(descriptor)
            raise

    def _begin(self, reservation: _TraceReservation, scope: _ScopeGeneration | None,
               started: float, started_monotonic: float) -> _BoundTrace:
        grant = reservation.grant
        general = grant.shape if isinstance(grant.shape, GeneralTraceShape) else None
        header = TraceHeader(grant.reservation_id, shape_key(grant.shape), round(started * _MS_PER_SECOND),
                             self.store.policy.sample_interval_ms, grant.row["need"], general)
        log = (progress.CaptureReader(self.capture_root, track_trace=True, scope_started_at=started)
               if general is None else None)
        log_path = progress.progress_path(reservation.sidecar, "log")
        writer = self.store.start(header)
        if log is not None:
            log.bind_trace_log(log_path)
        return _BoundTrace(scope, started_monotonic, started, writer, log, log_path)

    def sample_due(self, release_time: Callable[[str], float],
                   claim_expiry: Callable[[TraceRelease], TraceRelease | None]) -> None:
        @cache
        def processes() -> dict[int, gate.ProcessIdentity]:
            return gate.process_table(self.proc)

        now = self.clock.monotonic()
        for identifier, seal in tuple(self._pending_seals.items()):
            if seal.due <= now:
                self._seal(identifier, seal)
        for identifier, reservation in tuple(self._reservations.items()):
            if reservation.due > now:
                continue
            if reservation.scope_loss is not None:
                if (release := claim_expiry(reservation.scope_loss)) is not None:
                    self._release(release)
                continue
            try:
                if reservation.grant.process_tree:
                    self._sample_tree(reservation, processes(), release_time)
                else:
                    self._sample_scope(reservation, release_time)
            except Exception as error:
                if reservation.capture is None:
                    _report(error, action=f"bind {identifier}")
                else:
                    self._recording_failed(reservation.capture, error)
            finally:
                if reservation.scope_loss is None:
                    reservation.due = self.clock.monotonic() + self.store.policy.sample_seconds

    def _sample_tree(self, reservation: _TraceReservation, processes: dict[int, gate.ProcessIdentity],
                     release_time: Callable[[str], float]) -> None:
        grant = reservation.grant
        row = grant.row
        memory = gate.tree_memory(row["pid"], row["start_time"], processes, self.proc)
        if isinstance(memory, records.GoneMemory):
            self._scope_lost(reservation)
            return
        sampled_at = self.clock.monotonic()
        capture = reservation.capture
        if capture is None:
            capture = reservation.capture = self._begin(reservation, None, grant.wall, grant.monotonic)
        if capture.sampling:
            self._append_memory(capture, memory, sampled_at, release_time(grant.reservation_id), progress.NoTraceCapture())

    def _sample_scope(self, reservation: _TraceReservation, release_time: Callable[[str], float]) -> None:
        if reservation.capture is None:
            reservation.capture = self._bind(reservation)
        else:
            scope = reservation.capture.scope
            assert scope is not None
            if not scope.matches(reservation.sidecar):
                self._scope_lost(reservation)
                return
        capture = reservation.capture
        if capture is None or not capture.sampling:
            return
        writer = capture.writer
        log = self._read_log(capture) if capture.log is not None else progress.NoTraceCapture()
        capture.saw_completion |= isinstance(log, progress.TraceCaptureReading) and log.frames.test_completed

        def cutoff() -> float:
            return release_time(reservation.grant.reservation_id)

        if capture.log is not None and (start := self._test_started(reservation, capture, log, cutoff)) is not None:
            if isinstance(writer.append(start), TraceLimit):
                capture.sampling = False
                return
        capture.sampling = not capture.saw_completion
        if (not capture.sampling or self.clock.monotonic() > cutoff()
                or isinstance(log, progress.TraceCaptureReading) and not log.caught_up):
            return
        reading = self._read_scope(reservation, capture)
        if reading is None:
            return
        memory, sampled_at = reading
        self._append_memory(capture, memory, sampled_at, cutoff(), log)

    def _append_memory(self, capture: _BoundTrace, memory: records.MeasuredMemory, sampled_at: float, cutoff: float,
                       log: progress.TraceCaptureReading | progress.NoTraceCapture) -> None:
        writer = capture.writer
        elapsed_ms = capture.elapsed_ms(sampled_at)
        if (not isinstance(memory.anonymous, records.AnonymousRunUse)
                or elapsed_ms <= writer.last_sample_ms or sampled_at > cutoff):
            return
        sample = (TestSample(elapsed_ms, memory.anonymous.bytes) if isinstance(writer.test_start, TestStarted)
                  else self._compile_sample(elapsed_ms, memory.anonymous.bytes, log))
        capture.sampling = isinstance(writer.append(sample), TraceRecording)

    def _read_scope(self, reservation: _TraceReservation, capture: _BoundTrace
                    ) -> tuple[records.MeasuredMemory, float] | None:
        scope = capture.scope
        assert scope is not None
        reading = None
        try:
            if self._scope_populated(scope):
                memory = gate.slice_working_set(scope.open_directory)
                sampled_at = self.clock.monotonic()
                if self._scope_populated(scope):
                    reading = memory, sampled_at
        except (OSError, ValueError) as error:
            _report(error, action=f"read {scope.directory}")
        if scope.matches(None):
            return reading
        self._scope_lost(reservation)
        return None

    @staticmethod
    def _scope_populated(scope: _ScopeGeneration) -> bool:
        members = scope.open_directory / "cgroup.procs"
        gate.check_memory_reading(members)
        return bool(members.read_text().strip())

    def _read_log(self, capture: _BoundTrace) -> progress.TraceCaptureReading | progress.NoTraceCapture:
        assert capture.log is not None and capture.scope is not None
        reading = capture.log.trace_log(capture.log_path)
        if isinstance(reading, progress.TraceCaptureReading) or not self.boot_id:
            return reading
        try:
            members = gate.scope_members(capture.scope.open_directory)
        except (OSError, ValueError) as error:
            _report(error, action=f"read members {capture.scope.directory}")
            return progress.NoTraceCapture()
        identities: dict[int, str] = {}
        for pid in members:
            try:
                if (start := self.process_start(pid)) is not None:
                    identities[pid] = start
            except (OSError, ValueError) as error:
                _report(error, action=f"read process {pid}")
        return capture.log.trace_terminal(identities, self.boot_id)

    def _test_started(self, reservation: _TraceReservation, capture: _BoundTrace,
                      log: progress.TraceCaptureReading | progress.NoTraceCapture, cutoff: Callable[[], float]
                      ) -> TestStarted | None:
        writer = capture.writer
        if isinstance(writer.test_start, TestStarted):
            return None
        phase = self._test_phase(reservation, capture.started_wall)
        observed_at = self.clock.monotonic()
        latest = cutoff()
        elapsed_ms = capture.elapsed_ms(observed_at)
        phase_ms = -1
        if isinstance(phase, test_phase.ObservedTestPhase):
            # The sampler records phase start in whole seconds.
            scope_start = floor(capture.started_wall) if phase.started_at.is_integer() else capture.started_wall
            observation_wall = capture.started_wall + (min(observed_at, latest) - capture.started_monotonic)
            if scope_start <= phase.started_at <= observation_wall:
                phase_ms = max(0, round(phase.started_at * _MS_PER_SECOND) - writer.header.scope_started_at_ms)
        if not 0 <= phase_ms <= elapsed_ms:
            if not isinstance(log, progress.TraceCaptureReading) or not log.frames.test_started:
                return None
            phase_ms = elapsed_ms
        started_ms = max(writer.last_sample_ms, phase_ms)
        if observed_at > latest and started_ms > capture.elapsed_ms(latest):
            return None
        return TestStarted(started_ms, reservation.test_reserve)

    @staticmethod
    def _test_phase(reservation: _TraceReservation, scope_start: float
                    ) -> test_phase.ObservedTestPhase | test_phase.UnobservedTestPhase:
        path = gate.test_phase_path(reservation.grant.row)
        if not isinstance(path, Path):
            return test_phase.UnobservedTestPhase()
        try:
            with path.open("rb") as source:
                descriptor = source.fileno()
                if not progress.written_since(os.fstat(descriptor), scope_start):
                    return test_phase.UnobservedTestPhase()
                phase = buildlog_record.read_test_phase(Path("/proc/self/fd") / str(descriptor))
        except FileNotFoundError:
            pass
        except OSError as error:
            _report(error, action=f"read phase {path}")
        else:
            if (isinstance(phase, buildlog_record.ReservedTestPhaseRecord)
                    and phase.reservation_id == reservation.grant.reservation_id):
                return phase.window
            if isinstance(phase, test_phase.UnreadableTestPhaseRecord):
                _report(phase.reason, action=f"read phase {path}")
        return test_phase.UnobservedTestPhase()

    @staticmethod
    def _compile_sample(elapsed_ms: int, used: int, log: progress.TraceCaptureReading | progress.NoTraceCapture
                        ) -> CompileSample:
        if not isinstance(log, progress.TraceCaptureReading):
            return CompileSample(elapsed_ms, used, CargoProgressUnavailable(), CompilingCrateUnavailable())
        counter = log.counter
        cargo = (CargoProgress(counter.done, counter.total) if isinstance(counter, progress.ProgressCounter)
                 and counter.phase.startswith("build:") else CargoProgressUnavailable())
        return CompileSample(elapsed_ms, used, cargo, log.frames.last_compiling)

    def _finish(self, reservation: _TraceReservation, release: TraceRelease) -> None:
        capture = reservation.capture
        if capture is None:
            return
        test_start = None
        if not capture.failed and capture.log is not None:
            try:
                budget = self.store.policy.final_replay_bytes
                log = capture.log.trace_tail(final=True, replay_bytes=budget)
                if isinstance(log, progress.NoTraceCapture):
                    log = capture.log.trace_log(capture.log_path, final=True, replay_bytes=budget)
                capture.saw_completion |= isinstance(log, progress.TraceCaptureReading) and log.frames.test_completed
                test_start = self._test_started(reservation, capture, log, lambda: release.monotonic)
            except Exception as error:
                self._recording_failed(capture, error)
        elapsed_ms = capture.elapsed_ms(release.monotonic)
        try:
            ended_ms = _integer(round(release.wall * _MS_PER_SECOND))
        except (ValueError, OverflowError) as error:
            self._recording_failed(capture, error)
            test_start = None
            ended_ms = min(_MAX_INTEGER, capture.writer.header.scope_started_at_ms + elapsed_ms)
        reason = None
        if capture.failed:
            reason = IncompleteReason.RECORDING_ERROR
        elif isinstance(release.status, ExitStatusUnavailable):
            reason = IncompleteReason.SCOPE_LOST
        elif capture.log is not None and not capture.saw_completion:
            reason = IncompleteReason.NO_NEXTTEST_COMPLETION
        coverage = WholeRun() if reason is None else IncompleteRun(reason)
        end = TraceEnd(ended_ms, elapsed_ms, release.status, coverage)
        self._seal(release.reservation_id, _PendingSeal(capture.writer, end, test_start))

    def _seal(self, identifier: str, seal: _PendingSeal) -> None:
        _ = self._pending_seals.pop(identifier, None)
        try:
            if seal.test_start is not None:
                _ = seal.writer.append(seal.test_start)
                seal = replace(seal, test_start=None)
            _ = seal.writer.seal(seal.end)
        except Exception as error:
            _report(error)
            if isinstance(error, OSError) and seal.retries < self.store.policy.seal_retry_count:
                due = self.clock.monotonic() + self.store.policy.sample_seconds
                self._pending_seals[identifier] = replace(seal, due=due, retries=seal.retries + 1)
                return
            try:
                seal.writer.seal_prefix(seal.end)
            except Exception as fallback_error:
                _report(fallback_error, action=f"seal prefix {identifier}")
                return

    @staticmethod
    def _recording_failed(capture: _BoundTrace, error: Exception) -> None:
        _report(error, action=f"record {capture.writer.path}")
        capture.sampling = False
        capture.failed = True

    def _scope_lost(self, reservation: _TraceReservation) -> None:
        now = self.clock.monotonic()
        reservation.scope_loss = TraceRelease(reservation.grant.reservation_id, self.clock.now(), now,
                                              ExitStatusUnavailable())
        reservation.due = now + self.store.policy.scope_loss_grace_seconds

    def _forget(self, identifier: str) -> None:
        reservation = self._reservations.pop(identifier, None)
        if reservation is not None and reservation.capture is not None:
            try:
                reservation.capture.close()
            except Exception as error:
                _report(error)

    def close(self, *, seal_until: float = 0) -> None:
        """Retry pending seals and close retained descriptors."""
        while self._pending_seals and monotonic() < seal_until:
            for identifier, seal in tuple(self._pending_seals.items()):
                if monotonic() >= seal_until:
                    break
                self._seal(identifier, seal)
        for identifier in tuple(self._reservations):
            self._forget(identifier)
        self._pending_seals.clear()


@final
class TraceWorker:
    """Run trace work on a separate thread."""

    def __init__(self, capture: TraceCapture) -> None:
        self.capture = capture
        self._ready = Condition()
        self._commands: deque[TraceGrant | TraceRelease] = deque()
        self._shutdown_at: float | None = None
        self._queue_full = False
        self._tracked: dict[str, int | TestReserveUnavailable] = {}
        self._general: set[str] = set()
        self._reserves_ready = False
        self.thread = Thread(target=self._run, name="admission-memory-traces", daemon=True)
        try:
            self.thread.start()
        except RuntimeError as error:
            self._shutdown_at = 0
            _report(error)

    def submit(self, command: TraceGrant | Mapping[str, int]) -> None:
        with self._ready:
            if self._shutdown_at is not None:
                return
            if not isinstance(command, TraceGrant):
                for identifier, reserve in command.items():
                    if isinstance(self._tracked.get(identifier), TestReserveUnavailable):
                        self._tracked[identifier] = reserve
                        self._reserves_ready = True
                if self._reserves_ready:
                    self._ready.notify()
                return
            identifier = command.reservation_id
            if identifier in self._tracked or identifier in self._general:
                return
            general = isinstance(command.shape, GeneralTraceShape)
            limit = self.capture.store.policy.max_pending_commands
            if general and len(self._general) >= limit // _GENERAL_TRACE_LIMIT_DIVISOR:
                return
            # Each grant needs a command slot and a slot for its release.
            if len(self._commands) + len(self._tracked) + 2 > limit:
                self._queue_full = True
            else:
                self._tracked[identifier] = TestReserveUnavailable()
                if general:
                    self._general.add(identifier)
                self._commands.append(command)
            self._ready.notify()

    def release(self, identifier: str, wall: float, status: int | ExitStatusUnavailable) -> None:
        with self._ready:
            if self._shutdown_at is None and identifier in self._tracked:
                now = self.capture.clock.monotonic()
                self._commands.append(TraceRelease(identifier, wall, now, status, self._tracked.pop(identifier)))
                self._ready.notify()

    def _release_time(self, identifier: str) -> float:
        with self._ready:
            return next((command.monotonic for command in self._commands
                         if isinstance(command, TraceRelease) and command.reservation_id == identifier), float("inf"))

    def _claim_expiry(self, release: TraceRelease) -> TraceRelease | None:
        with self._ready:
            identifier = release.reservation_id
            if identifier in self._tracked:
                self._general.discard(identifier)
                return replace(release, test_reserve=self._tracked.pop(identifier))
        return None

    def _step(self) -> bool:
        deadline = self.capture.deadline
        with self._ready:
            while not (self._commands or self._queue_full or self._reserves_ready or self._shutdown_at is not None):
                delay = deadline - self.capture.clock.monotonic()
                if delay <= 0:
                    break
                _ = self._ready.wait(None if delay == float("inf") else delay)
            shutdown_at = self._shutdown_at
            if shutdown_at is not None and monotonic() >= shutdown_at:
                return False
            command: TraceCommand | None = None
            if self._commands:
                command = self._commands.popleft()
                if isinstance(command, TraceRelease):
                    self._general.discard(command.reservation_id)
            elif self._reserves_ready:
                command = {identifier: reserve for identifier, reserve in self._tracked.items() if isinstance(reserve, int)}
                self._reserves_ready = False
            full, self._queue_full = self._queue_full, False
        if full:
            _report("command queue is full")
        if command is not None:
            self.capture.process(command)
        elif shutdown_at is None:
            self.capture.sample_due(self._release_time, self._claim_expiry)
        else:
            return False
        return True

    def _run(self) -> None:
        try:
            try:
                self.capture.recover()
            except Exception as error:
                _report(error, action="recover")
            while self._step():
                pass
        except Exception as error:
            _report(error)
        finally:
            with self._ready:
                shutdown_at, self._shutdown_at = self._shutdown_at, 0
                self._commands.clear()
                self._tracked.clear()
                self._general.clear()
            self.capture.close(seal_until=shutdown_at or 0)

    def close(self) -> None:
        timeout = self.capture.store.policy.shutdown_timeout_seconds
        with self._ready:
            if self._shutdown_at is None:
                self._shutdown_at = monotonic() + timeout
            self._ready.notify()
        if self.thread.ident is not None:
            self.thread.join(timeout=timeout)


def _report(error: Exception | str, *, action: str = "") -> None:
    context = f" {action}" if action else ""
    print(f"memory trace{context}: {error}", file=sys.stderr)
