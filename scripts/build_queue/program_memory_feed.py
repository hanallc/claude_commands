"""Read and display program memory traces for the Builds feed."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Literal, TypedDict

from scripts.buildlog.store import root as buildlog_root
from scripts.build_queue import admission_records, memory_trace


type ReservationsByOwner = dict[str, list[tuple[admission_records.Reservation, admission_records.ExplainedRunningStep]]]


type ProgramMemoryPoint = tuple[int, int]
type ProgramMemoryPhaseState = Literal["sampling", "complete", "not_started", "no_samples", "stopped"]


class ProgramMemoryPhase(TypedDict):
    state: ProgramMemoryPhaseState
    duration_ms: int
    segments: tuple[tuple[ProgramMemoryPoint, ...], ...]
    peak: ProgramMemoryPoint | None
    reserve_bytes: int | None
    above_half_reserve_ms: int | None


class ProgramMemoryRun(TypedDict):
    compile: ProgramMemoryPhase
    test: ProgramMemoryPhase
    hana_crate_bands: tuple[tuple[int, int], ...]
    hana_compile_ms: int


class ProgramMemoryReserve(TypedDict):
    bytes: int
    source: Literal["live", "past"]


class ProgramMemoryView(TypedDict):
    live: ProgramMemoryRun
    past: ProgramMemoryRun | None
    compile_reserve: ProgramMemoryReserve
    test_reserve: ProgramMemoryReserve | None


@dataclass(frozen=True)
class _TraceFileStamp:
    identity: tuple[int, int]
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> _TraceFileStamp:
        return cls((info.st_dev, info.st_ino), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _trace_file_stamp(path: Path) -> _TraceFileStamp | None:
    try:
        return _TraceFileStamp.from_stat(path.stat())
    except OSError:
        return None


@dataclass(frozen=True)
class _LiveTraceRead:
    stamp: _TraceFileStamp
    offset: int
    trace: memory_trace.MemoryTrace


@dataclass(frozen=True)
class _PastTraceRead:
    folder: _TraceFileStamp
    file: tuple[Path, _TraceFileStamp] | None
    trace: memory_trace.MemoryTrace | memory_trace.NoMatchingWholeRun


def _program_memory_phase(chart: memory_trace.PhaseChart | memory_trace.NoPhaseSamples, state: ProgramMemoryPhaseState,
                          reserve: int | None, duration_ms: int = 0) -> ProgramMemoryPhase:
    if isinstance(chart, memory_trace.NoPhaseSamples):
        return {"state": state if state in ("not_started", "stopped") else "no_samples", "duration_ms": duration_ms,
                "segments": (), "peak": None, "reserve_bytes": reserve, "above_half_reserve_ms": None}
    return {"state": state, "duration_ms": chart.duration_ms,
            "segments": tuple(tuple((point.elapsed_ms, point.program_bytes) for point in segment)
                              for segment in chart.segments),
            "peak": (chart.peak.elapsed_ms, chart.peak.program_bytes), "reserve_bytes": reserve,
            "above_half_reserve_ms": (chart.above_half_reserve_ms if isinstance(chart.above_half_reserve_ms, int) else None)}


def _program_memory_run(trace: memory_trace.MemoryTrace, policy: memory_trace.MemoryTracePolicy,
                        now_ms: int, *, past: bool = False) -> ProgramMemoryRun:
    charts = memory_trace.project_trace(trace, policy)
    test_start = trace.test_start
    started = isinstance(test_start, memory_trace.TestStarted)
    reserve = test_start.reserve_bytes if isinstance(test_start, memory_trace.TestStarted) else None
    test_ms = test_start.elapsed_ms if isinstance(test_start, memory_trace.TestStarted) else 0
    samples = trace.test_samples or trace.compile_samples
    last_ms = max(test_ms, samples[-1].elapsed_ms if samples else 0)
    state: ProgramMemoryPhaseState = "sampling"
    if isinstance(trace.state, memory_trace.TraceEnd):
        state = "complete" if isinstance(trace.state.coverage, memory_trace.WholeRun) else "stopped"
    elif (isinstance(trace.state, memory_trace.TraceLimit)
          or now_ms > trace.header.scope_started_at_ms + last_ms + trace.header.max_gap_ms):
        state = "stopped"
    return {"compile": _program_memory_phase(charts.compile, "complete" if started else state,
                                             trace.header.compile_reserve_bytes, test_ms),
            "test": _program_memory_phase(charts.test, state if started else "not_started",
                                          reserve if isinstance(reserve, int) else None),
            "hana_crate_bands": tuple((band.start_ms, band.end_ms) for band in charts.hana_crate_bands) if past else (),
            "hana_compile_ms": charts.hana_compile_ms if past else 0}


class ProgramMemoryFeed:
    """Keep trace reads for this feed process."""

    def __init__(self, store: memory_trace.TraceStore) -> None:
        self.store: memory_trace.TraceStore = store
        self.live: dict[str, _LiveTraceRead] = {}
        self.past: dict[str, _PastTraceRead] = {}

    def _read_live(self, reservation_id: str) -> memory_trace.MemoryTrace | None:
        if re.fullmatch(r"[A-Za-z0-9_-]+", reservation_id) is None:
            return None
        path = self.store.root / "live" / f"{reservation_id}.jsonl"
        previous = self.live.pop(reservation_id, None)
        try:
            with path.open("rb") as stream:
                stamp = _TraceFileStamp.from_stat(os.fstat(stream.fileno()))
                if stamp.size > self.store.policy.max_bytes_per_run:
                    return None
                if previous is not None and previous.stamp.identity == stamp.identity:
                    if previous.stamp == stamp:
                        self.live[reservation_id] = previous
                        return previous.trace
                    if (stamp.size > previous.stamp.size
                            and isinstance(previous.trace.state, (memory_trace.TraceRecording, memory_trace.TraceTailIncomplete))):
                        _ = stream.seek(previous.offset)
                        tail = stream.read(stamp.size - previous.offset)
                        complete = tail.rfind(b"\n") + 1
                        trace = previous.trace.read_body(BytesIO(tail), self.store.policy)
                        if _trace_file_stamp(path) == stamp:
                            self.live[reservation_id] = _LiveTraceRead(stamp, previous.offset + complete, trace)
                            return trace
                trace = self.store.read_live(reservation_id)
                if isinstance(trace, memory_trace.TraceUnavailable):
                    return None
                offset = stamp.size
                if isinstance(trace.state, memory_trace.TraceTailIncomplete):
                    _ = stream.seek(0)
                    offset = stream.read(stamp.size).rfind(b"\n") + 1
                if _trace_file_stamp(path) == stamp:
                    self.live[reservation_id] = _LiveTraceRead(stamp, offset, trace)
                return trace
        except (OSError, ValueError, RecursionError):
            return None

    def _read_past(self, live: memory_trace.TraceHeader, now_ms: int) -> memory_trace.MemoryTrace | None:
        key = live.shape_key
        folder = self.store.root / "finished" / key
        stamp = _trace_file_stamp(folder)
        previous = self.past.get(key)
        if (previous is not None and previous.folder == stamp
                and (previous.file is None or _trace_file_stamp(previous.file[0]) == previous.file[1])):
            trace = previous.trace
        else:
            _ = self.past.pop(key, None)
            trace = self.store.newest_whole_run(key)
            file: tuple[Path, _TraceFileStamp] | None = None
            if isinstance(trace, memory_trace.MemoryTrace) and isinstance(trace.state, memory_trace.TraceEnd):
                path = folder / f"{trace.state.ended_at_ms}-{trace.header.reservation_id}.jsonl"
                file_stamp = _trace_file_stamp(path)
                if file_stamp is not None:
                    file = (path, file_stamp)
            if stamp is not None and stamp == _trace_file_stamp(folder) and (
                    file is not None or isinstance(trace, memory_trace.NoMatchingWholeRun)):
                self.past[key] = _PastTraceRead(stamp, file, trace)
        if isinstance(trace, memory_trace.MemoryTrace) and trace.header.reservation_id == live.reservation_id:
            trace = self.store.newest_whole_run(key, exclude_reservations=(live.reservation_id,))
        cutoff = now_ms - self.store.policy.retention_days * 86_400_000
        if (isinstance(trace, memory_trace.MemoryTrace) and isinstance(trace.state, memory_trace.TraceEnd)
                and trace.state.ended_at_ms >= cutoff):
            return trace
        return None

    def views(self, rows: Iterable[tuple[str, float]], reservations: ReservationsByOwner,
              now: float, local_host: str) -> list[tuple[int, ProgramMemoryView]]:
        """Return charts with their row indices until the refresh reaches its byte limit."""
        trace_rows: list[tuple[int, float, admission_records.Reservation, float]] = []
        for index, (owner, started_at) in enumerate(rows):
            entries = reservations.get(owner, ())
            if len(entries) != 1:
                continue
            reservation, entry = entries[0]
            if (reservation["repo"] == "hana" and reservation["step"] == "nextest"
                    and reservation.get("scoped", False) and reservation.get("reservation_id")
                    and admission_records.execution_host(entry.step, local_host) == local_host
                    and not isinstance(entry.admitted_at, admission_records.AdmissionTimeNotRecorded)):
                trace_rows.append((index, started_at, reservation, entry.admitted_at))
        now_ms = int(now * 1000)
        active = {reservation.get("reservation_id", "") for _, _, reservation, _ in trace_rows}
        self.live = {key: value for key, value in self.live.items() if key in active}
        shapes: set[str] = set()
        remaining = self.store.policy.feed_bytes_per_refresh
        views: list[tuple[int, ProgramMemoryView]] = []
        for index, _, reservation, admitted in sorted(trace_rows, key=lambda entry: entry[1]):
            live = self._read_live(reservation.get("reservation_id", ""))
            if (live is None or live.header.compile_reserve_bytes != reservation["need"]
                    or not int(admitted * 1000) <= live.header.scope_started_at_ms <= now_ms):
                continue
            shapes.add(live.header.shape_key)
            past = self._read_past(live.header, now_ms)
            live_run = _program_memory_run(live, self.store.policy, now_ms)
            past_run = _program_memory_run(past, self.store.policy, now_ms, past=True) if past is not None else None
            test_reserve: ProgramMemoryReserve | None = None
            known = live_run["test"]["reserve_bytes"]
            if known is not None:
                test_reserve = {"bytes": known, "source": "live"}
            elif isinstance(live.test_start, memory_trace.TestNotStarted) and past_run is not None:
                known = past_run["test"]["reserve_bytes"]
                if known is not None:
                    test_reserve = {"bytes": known, "source": "past"}
            view: ProgramMemoryView = {"live": live_run, "past": past_run,
                "compile_reserve": {"bytes": reservation["need"], "source": "live"}, "test_reserve": test_reserve}
            remaining -= len(json.dumps({"program_memory": view}).encode("utf-8"))
            if remaining < 0:
                break
            views.append((index, view))
        self.past = {key: value for key, value in self.past.items() if key in shapes}
        return views


def configured_feed() -> ProgramMemoryFeed | None:
    policy = memory_trace.read_policy()
    if isinstance(policy, memory_trace.MemoryTracePolicy):
        return ProgramMemoryFeed(memory_trace.TraceStore(buildlog_root() / "admission" / "memory_traces", policy))
    return None
