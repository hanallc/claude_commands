#!/usr/bin/env python3
"""Choose admission from command arguments and manage the program scope."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, redirect_stdout, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum, auto
import errno
import os
from pathlib import Path
import re
import signal
import shlex
import subprocess
import sys
import threading
import time
from typing import BinaryIO, TypedDict, Unpack

from scripts.lib import build_folders, atomic_file, conf
from scripts.lib.build_folders import program_starts as program_starts
from scripts.build_queue import admission_records, snapshot_codec
from scripts.build_queue import program_start as startup
from scripts.build_queue import step_estimator
from scripts.lint import memory_admit
from scripts.build_queue import admission_client, admission_protocol as wire
import uuid


DETACHED_SLICE_ENV = "DETACHED_RUN_SLICE"


@dataclass(frozen=True)
class ConfiguredFirstPeak:
    pass


CONFIGURED_FIRST_PEAK = ConfiguredFirstPeak()


class ProgramPurpose(Enum):
    BATCH = auto()
    PROBE = auto()
    EDITOR_SERVER = auto()
    RUNNER = auto()


def program_purpose(argv: Sequence[str], *, batch: bool = False) -> ProgramPurpose:
    """Classify the complete command before admission or scope startup."""
    if not argv:
        raise QueuePlaceRefused("the program command is empty")
    if Path(argv[0]).name != "rust-analyzer":
        return ProgramPurpose.BATCH
    probes = conf.read("build_admission").words("program_probe_arguments.rust-analyzer")
    if len(argv) == 2 and argv[1] in probes:
        return ProgramPurpose.PROBE
    if not batch and len(argv) == 1:
        return ProgramPurpose.EDITOR_SERVER
    return ProgramPurpose.BATCH


@dataclass(frozen=True)
class UnreservedProgram:
    pass


@dataclass(frozen=True)
class UnmeasuredProbe:
    pass


class QueuePlaceRefused(Exception):
    """The program cannot acquire or keep its queue place."""


class CleanupUnresolved(QueuePlaceRefused):
    """Recovery still owns the program or reservation."""

    def __init__(self, reason: str):
        super().__init__(f"UNRESOLVED: {reason}")


def no_hold_wait() -> None:
    pass


def in_builds_slice() -> bool:
    if sys.platform != "linux":
        return False
    try:
        return "/builds.slice/" in Path("/proc/self/cgroup").read_text()
    except OSError:
        return False


def suite_scope_prefix() -> list[str]:
    if os.environ.get("BUILDLOG_SCOPE") == "0":
        print("Module scope skipped: BUILDLOG_SCOPE=0", flush=True)
        return []
    if not os.environ.get(DETACHED_SLICE_ENV) and in_builds_slice():
        print("Module scope skipped: gate already runs in builds.slice", flush=True)
        return []
    return probe_scope_prefix()


def probe_scope_prefix() -> list[str]:
    """Check whether the user manager can start a separate program scope."""
    if sys.platform != "linux":
        return []
    slice_name = os.environ.get(DETACHED_SLICE_ENV) or "builds.slice"
    prefix = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
              "--expand-environment=no", f"--slice={slice_name}", "--"]
    try:
        probe = subprocess.run([*prefix, "true"], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"Module scope skipped: probe failed ({error})", flush=True)
        return []
    if probe.returncode:
        print(f"Module scope skipped: probe failed (exit {probe.returncode})", flush=True)
        return []
    print(f"Module scope: {slice_name}", flush=True)
    return prefix


def wait_reasons(result: admission_records.AdmissionWaitSnapshot) -> tuple[str, ...]:
    return admission_client.wait_reasons(result)


class PopenArguments(TypedDict, total=False):
    cwd: str | Path
    env: Mapping[str, str]
    stdin: int | BinaryIO
    stdout: int | BinaryIO
    stderr: int | BinaryIO
    bufsize: int
    close_fds: bool
    pass_fds: Sequence[int]
    start_new_session: bool
    preexec_fn: Callable[[], None]


@dataclass(frozen=True)
class ProgramNotStarted:
    pass


@dataclass
class NoDescendantAdoption:
    pass


@dataclass
class ProgramTree:
    group: int
    adopter: wire.Owner | NoDescendantAdoption = field(default_factory=NoDescendantAdoption)
    observed: dict[int, str] = field(default_factory=dict[int, str])
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def live_members(self, scope_members: set[int], *, deadline: float | None = None) -> dict[int, str]:
        """Retain observed descendants after a session change or reparenting."""
        wait = -1 if deadline is None else max(0, deadline - time.monotonic())
        if not self.lock.acquire(timeout=wait):
            raise CleanupUnresolved("the program sampler did not release its process list")
        try:
            processes = memory_admit.process_table(Path("/proc"))
            roots = dict(self.observed)
            if isinstance(self.adopter, wire.Owner):
                roots[self.adopter.pid] = self.adopter.start_time
            members = memory_admit.process_tree(roots, processes)
            members.update(scope_members)
            for pid in processes:
                try:
                    if os.getpgid(pid) == self.group:
                        members.add(pid)
                except ProcessLookupError:
                    continue
            if isinstance(self.adopter, wire.Owner):
                members.discard(self.adopter.pid)
            live = {pid: processes[pid].start for pid in members if pid in processes}
            self.observed = live
            return live
        finally:
            self.lock.release()


@dataclass
class ScopedProgram:
    """The published scope contains every program descendant."""


@dataclass
class StartedProgram:
    process: subprocess.Popen[bytes]
    stopped: threading.Event
    sampler: threading.Thread | UnmeasuredProbe
    tracking: ScopedProgram | ProgramTree
    record: Path
    finished: bool = False


def read_program_start(record: Path) -> startup.ProgramStart | None:
    try:
        return wire.parse_record(build_folders.read_program_record(record), startup.ProgramStart)
    except FileNotFoundError:
        return None
    except snapshot_codec.SnapshotParseError:
        raise
    except (OSError, ValueError, RecursionError) as error:
        raise snapshot_codec.SnapshotParseError(str(error)) from error


def _recorded_sidecar(path: Path) -> str:
    """Read a complete sidecar field from a damaged record for retention."""
    with suppress(OSError, ValueError):
        text = build_folders.read_program_record(path)
        if match := re.search(r'(?:^|[,{])\s*"sidecar"\s*:\s*("(?:[^"\\]|\\.)*")', text):
            return wire.parse_record(match[1], str)
    return ""


def _mark_program_recovery(path: Path, sidecar: Path) -> None:
    """Link the folder to its record if the folder has no recovery marker."""
    marker = sidecar.with_name(build_folders.RECOVERY_MARKER)
    with suppress(FileExistsError, FileNotFoundError):
        marker.symlink_to(path.absolute())


@contextmanager
def recoverable_tool_folder(tool: str, parent: Path | None = None, *,
                            keep: Callable[[], bool] | None = None) -> Generator[Path]:
    """Keep the folder and its record when cleanup cannot finish."""
    with ExitStack() as lease:
        path = build_folders._claim_tool_folder(tool, parent,  # pyright: ignore[reportPrivateUsage]
            lambda spec: lease.enter_context(build_folders.leased_build_folder(spec)))
        unresolved = False
        try:
            yield path
        except CleanupUnresolved:
            unresolved = True
            raise
        finally:
            lease.close()
            if not unresolved and (keep is None or not keep()):
                remove_program_folder(path)


@contextmanager
def queue_place_folder(*, keep: Callable[[], bool] | None = None) -> Generator[Path]:
    """Retain the queue place folder when cleanup is unresolved."""
    with recoverable_tool_folder("queue-place", keep=keep) as path:
        yield path


def remove_program_folder(path: Path) -> None:
    if not build_folders.remove_build_folder(path, lambda: build_folders.remove_folder(path), wait=False):
        raise CleanupUnresolved(f"could not remove {path}")


def cleanup_queue_place_folders(kept_sidecars: set[Path]) -> None:
    """Remove this account's temporary folders after their exact holders end."""
    prefix = build_folders.FOLDER_PREFIXES["queue-place"]
    for path in build_folders.inherited_temporary_parent().glob(prefix + "*"):
        fields = path.name[len(prefix):].split("-", 2)
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        if admission_records.process_start_time(int(fields[0])) == fields[1]:
            continue
        if path / build_folders.QUEUE_PROGRAM_SIDECAR in kept_sidecars:
            continue
        try:
            if not path.is_symlink() and path.is_dir() and path.stat().st_uid == os.getuid():
                _ = build_folders.reclaim_build_folder(path, wait=False)
        except FileNotFoundError:
            pass


def cleanup_program_starts(configuration: admission_records.MemoryConfig | wire.RunnerExecutionTiming) -> set[Path]:
    """Stop recorded programs whose exact holders have ended."""
    directory = program_starts()
    kept: set[Path] = set()
    if not directory.exists():
        return kept
    for path in directory.iterdir():
        target = atomic_file.temporary_target(path.name)
        if target is not None:
            atomic_file.remove_dead_temporary_files(directory / target)
    for path in directory.glob("*.json"):
        record: startup.ProgramStart | None = None
        try:
            record = read_program_start(path)
            if record is None or admission_records.process_start_time(record.holder.pid) == record.holder.start_time:
                continue
            deadline = time.monotonic() + configuration.program_stop_seconds
            if record.sidecar:
                # Records from older launches may have no folder marker.
                _mark_program_recovery(path, Path(record.sidecar))
            if record.scope:
                stop_scope(Path(record.scope), deadline, configuration.program_sample_seconds)
            else:
                start = admission_records.process_start_time(record.program.pid)
                if start is None or start == record.program.start_time:
                    # RUNNER programs (CI Cargo) start without a scope.
                    # As on main, recovery relies on the process tree and group.
                    # A reparented descendant that calls setsid can leave both trackers.
                    tree = ProgramTree(record.program.pid)
                    tree.observed[record.program.pid] = record.program.start_time
                    stop_members(lambda: tree.live_members(set()), deadline, configuration.program_sample_seconds)
                published = read_program_start(path)
                if published is not None and published.scope:
                    stop_scope(Path(published.scope), deadline, configuration.program_sample_seconds)
            path.unlink(missing_ok=True)
        except (CleanupUnresolved, OSError, snapshot_codec.SnapshotParseError) as error:
            name = record.sidecar if record is not None else _recorded_sidecar(path)
            if name:
                sidecar = Path(name)
                kept.add(sidecar)
                if record is None:
                    with suppress(OSError, ValueError):
                        if sidecar.is_file():
                            _mark_program_recovery(path, sidecar)
            print(f"UNRESOLVED: recovery kept {path}: {error}", file=sys.stderr)
    return kept


def _scope_is_empty(scope: Path) -> bool:
    try:
        return not memory_admit.scope_populated(scope, require_events=True)
    except ValueError as error:
        raise CleanupUnresolved(str(error)) from error
    except OSError as error:
        if error.errno not in (errno.ENOENT, errno.ENODEV):
            raise
        try:
            _ = scope.lstat()
        except FileNotFoundError:
            return True
        raise CleanupUnresolved(f"scope {scope} has no readable cgroup.events") from error


def stop_scope(scope: Path, deadline: float, sample_seconds: float) -> None:
    """Stop scope members until the scope is empty."""
    kill_at = (time.monotonic() + deadline) / 2
    while True:
        try:
            members = memory_admit.scope_members(scope)
        except ValueError as error:
            raise CleanupUnresolved(f"could not read the members of {scope}: {error}") from error
        if not members and _scope_is_empty(scope):
            return
        now = time.monotonic()
        if now >= deadline:
            raise CleanupUnresolved(f"scope {scope} still has live processes")
        number = signal.SIGKILL if now >= kill_at else signal.SIGTERM
        killed_scope = False
        if members and number == signal.SIGKILL:
            try:
                _ = (scope / "cgroup.kill").write_text("1\n")
                killed_scope = True
            except FileNotFoundError:
                pass
        if not killed_scope:
            for pid in members:
                try:
                    os.kill(pid, number)
                except ProcessLookupError:
                    pass
        time.sleep(max(0, min(sample_seconds, (kill_at if now < kill_at else deadline) - time.monotonic())))


def stop_members(read_members: Callable[[], dict[int, str]], deadline: float, sample_seconds: float) -> None:
    """Stop the processes that still match their recorded identities."""
    kill_at = (time.monotonic() + deadline) / 2
    while members := read_members():
        now = time.monotonic()
        if now >= deadline:
            raise CleanupUnresolved(f"processes still live: {', '.join(map(str, members))}")
        number = signal.SIGKILL if now >= kill_at else signal.SIGTERM
        for pid, identity in members.items():
            try:
                if admission_records.process_start_time(pid) == identity:
                    os.kill(pid, number)
            except ProcessLookupError:
                pass
        time.sleep(max(0, min(sample_seconds, (kill_at if now < kill_at else deadline) - time.monotonic())))


def program_members(tree: ProgramTree, sidecar: Path, *, deadline: float | None = None) -> dict[int, str]:
    """Find observed tree members and scope members, including nested scopes."""
    members: set[int] = set()
    try:
        scope = sidecar.read_text().strip()
        if scope:
            members = memory_admit.scope_members(Path(scope))
    except FileNotFoundError:
        pass
    return tree.live_members(members, deadline=deadline)


@dataclass
class QueuePlace:
    path: Path
    sidecar: Path
    peak_bytes: int = 0
    wave_at: float = 0.0
    status: int = 1
    program: ProgramNotStarted | StartedProgram = field(default_factory=ProgramNotStarted, init=False)
    on_started: Callable[[], None] = no_hold_wait
    configuration: admission_records.MemoryConfig | wire.RunnerExecutionTiming = field(kw_only=True, repr=False)
    purpose: ProgramPurpose = field(default=ProgramPurpose.BATCH, kw_only=True)
    arguments: tuple[str, ...] = field(default=(), kw_only=True)
    reservation: HeldReservation | UnreservedProgram = field(default_factory=UnreservedProgram, kw_only=True)
    _samples: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    keep_reservation: bool = field(default=False, init=False)

    def publish_sample(self, suffix: str, value: str) -> None:
        path = self.path.with_suffix(suffix)
        atomic_file.atomic_sample(path, value)
        if self.purpose == ProgramPurpose.RUNNER:
            path.chmod(0o644)

    def observe(self, peak_bytes: int, current_bytes: int) -> None:
        """Publish the existing peak and near-peak evidence in bytes."""
        with self._samples:
            now = datetime.now(UTC).timestamp()
            if peak_bytes > self.peak_bytes:
                self.publish_sample(".max", f"{peak_bytes}\n")
                self.peak_bytes = peak_bytes
                self.publish_sample(".wave", f"{peak_bytes} {int(now)}\n")
                self.wave_at = now
            elif self.peak_bytes and current_bytes >= self.peak_bytes / 2 and now - self.wave_at >= self.configuration.peak_wave_seconds:
                self.publish_sample(".wave", f"{self.peak_bytes} {int(now)}\n")
                self.wave_at = now

    def child_environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        """Mark children that run under this reservation."""
        child = dict(environment)
        if isinstance(self.reservation, HeldReservation):
            grant = self.reservation.grant
            directory = Path(grant.max_path).parent.resolve()
            socket_path = wire.admission_socket_path().resolve()
            cpu_pool = admission_client.cpu_pool_path().resolve()
            child[admission_client.RESERVED_STEP_ENV] = (
                f"{grant.reservation}\t{os.getpid()}\t{directory}\t{socket_path}\t{cpu_pool}")
        return child

    def popen(self, argv: Sequence[str], **popen_arguments: Unpack[PopenArguments]) -> subprocess.Popen[bytes]:
        if sys.platform != "linux":
            raise QueuePlaceRefused("a queued program requires Linux")
        if isinstance(self.program, StartedProgram):
            raise QueuePlaceRefused("a queue place can start only one program")
        if self.arguments and tuple(argv) != self.arguments:
            raise QueuePlaceRefused("the program arguments changed after the purpose decision")
        if not popen_arguments.get("start_new_session", True):
            raise QueuePlaceRefused("a queued program must start in its own process group")
        popen_arguments["start_new_session"] = True
        if isinstance(self.reservation, HeldReservation):
            environment = popen_arguments.get("env")
            popen_arguments["env"] = self.child_environment(os.environ if environment is None else environment)
        prefix: list[str] = []
        if self.purpose not in (ProgramPurpose.PROBE, ProgramPurpose.RUNNER) and os.environ.get("BUILDLOG_SCOPE") != "0":
            with redirect_stdout(sys.stderr):
                prefix = probe_scope_prefix()
            if not prefix:
                raise QueuePlaceRefused(
                    "the queued program requires a memory scope; set BUILDLOG_SCOPE=0 only for tests")
        holder_pid = os.getpid()
        holder_start = admission_records.process_start_time(holder_pid)
        if holder_start is None:
            raise QueuePlaceRefused("the queue place holder has no live process identity")
        _ = cleanup_program_starts(self.configuration)
        directory = program_starts()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        record = directory / (uuid.uuid4().hex + ".json")
        self.sidecar.with_name(build_folders.RECOVERY_MARKER).symlink_to(record.absolute())
        atomic_file.sync_folder(self.sidecar.parent)
        wrapper = [sys.executable, str(Path(startup.__file__).resolve())]
        holder = wire.Owner(holder_pid, holder_start)
        reservation = (startup.ScopeReservation.HELD if isinstance(self.reservation, HeldReservation)
                       else startup.ScopeReservation.UNRESERVED)
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as reader, os.fdopen(write_fd, "wb", buffering=0) as permission:
            action = "--scope-start" if prefix else "--program-start"
            scope_words = reservation.arguments() if prefix else []
            command = [*prefix, *wrapper, action, *scope_words, "--authorized", str(self.sidecar),
                       str(holder_pid), holder_start,
                       str(record), str(read_fd), str(self.configuration.program_stop_seconds), *argv]
            popen_arguments["pass_fds"] = (*popen_arguments.get("pass_fds", ()), reader.fileno())
            process = subprocess.Popen(command, **popen_arguments)
            stopped = threading.Event()
            tracking = (ScopedProgram() if prefix else ProgramTree(process.pid))
            sampler = (UnmeasuredProbe() if self.purpose == ProgramPurpose.PROBE else
                       threading.Thread(target=self._sample, args=(tracking, stopped), daemon=True))
            self.program = StartedProgram(process, stopped, sampler, tracking, record)
            start = admission_records.process_start_time(process.pid)
            if start is None:
                raise QueuePlaceRefused("the program has no live process identity")
            if isinstance(tracking, ProgramTree):
                if self.purpose == ProgramPurpose.RUNNER:
                    tracking.adopter = holder
                tracking.observed[process.pid] = start
            atomic_file.write_text_atomic(record, wire.encode_record(
                startup.ProgramStart(holder, wire.Owner(process.pid, start), "", ready=False, sidecar=str(self.sidecar))))
            if isinstance(sampler, threading.Thread):
                sampler.start()
            _ = permission.write(b"1")
        deadline = time.monotonic() + self.configuration.program_stop_seconds
        while (published := read_program_start(record)) is None or not published.ready:
            if process.poll() is not None:
                published = read_program_start(record)
                if published is not None and published.ready:
                    break
                raise QueuePlaceRefused("the program exited before it published its ownership")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CleanupUnresolved("the program did not publish its ownership")
            time.sleep(min(self.configuration.program_sample_seconds, remaining))
        self.on_started()
        return process

    def _sample(self, tracking: ScopedProgram | ProgramTree, stopped: threading.Event) -> None:
        while True:
            try:
                scope = self.sidecar.read_text().strip()
                if scope:
                    reading = memory_admit.slice_working_set(Path(scope))
                    if isinstance(reading.anonymous, admission_records.AnonymousRunUse):
                        self.observe(reading.anonymous.bytes, reading.anonymous.bytes)
                elif isinstance(tracking, ProgramTree):
                    members = program_members(tracking, self.sidecar)
                    proc = Path(os.environ.get("BUILDLOG_PROC", "/proc"))
                    matching = set(members)
                    if proc != Path("/proc"):
                        processes = memory_admit.process_table(proc)
                        matching = {pid for pid, start in members.items()
                                    if pid in processes and processes[pid].start == start}
                    reading = memory_admit.process_memory(matching, proc)
                    if self.purpose == ProgramPurpose.RUNNER:
                        if isinstance(reading.anonymous, admission_records.AnonymousRunUse):
                            self.observe(reading.anonymous.bytes, reading.anonymous.bytes)
                    else:
                        self.observe(reading.bytes, reading.bytes)
            except (OSError, ValueError, StopIteration):
                pass  # A process or scope can disappear between sampling reads.
            if stopped.wait(self.configuration.program_sample_seconds):
                return

    def finish(self, *, failed: bool = False) -> None:
        if not isinstance(self.program, StartedProgram):
            return
        program = self.program
        if program.finished:
            program.record.unlink(missing_ok=True)
            return
        process = program.process
        config = self.configuration
        deadline = time.monotonic() + config.program_stop_seconds
        scope = ""
        program_stopped = False
        try:
            completed = process.poll()
            if isinstance(program.tracking, ScopedProgram):
                published = read_program_start(program.record)
                scope = published.scope if published is not None else ""
                if scope:
                    stop_scope(Path(scope), deadline, config.program_sample_seconds)
                elif completed is None:
                    process.kill()
            else:
                def live_members() -> dict[int, str]:
                    _ = process.poll()
                    assert isinstance(program.tracking, ProgramTree)
                    return program_members(program.tracking, self.sidecar, deadline=deadline)

                stop_members(live_members, deadline, config.program_sample_seconds)
            try:
                _ = process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise CleanupUnresolved(f"program {process.pid} did not exit") from error
            if isinstance(program.tracking, ScopedProgram):
                published = read_program_start(program.record)
                if published is not None and published.scope and published.scope != scope:
                    stop_scope(Path(published.scope), deadline, config.program_sample_seconds)
            if completed is not None and not failed:
                self.status = completed
            program_stopped = True
        finally:
            program.stopped.set()
            if isinstance(program.sampler, threading.Thread) and program.sampler.ident is not None:
                program.sampler.join(timeout=max(0, deadline - time.monotonic()))
                if program_stopped and program.sampler.is_alive():
                    raise CleanupUnresolved(f"the sampler for program {process.pid} did not stop")
        program.finished = True
        program.record.unlink(missing_ok=True)


@dataclass(frozen=True)
class RefreshedRequest:
    """A step or program request that the client rebuilds while it waits."""
    refresh: Callable[[], wire.AdmitRequest]


@dataclass(frozen=True)
class RefreshedLaunch:
    """A launch request: the service admits the start and keeps no reservation."""
    refresh: Callable[[], wire.LaunchRequest]


QueueRequest = RefreshedRequest | wire.ServiceEstimateRequest
QueueReply = wire.Admitted | wire.LaunchAdmitted | wire.Refused | admission_client.LeftQueue


def request_place(request: QueueRequest | RefreshedLaunch, output: admission_client.WaitOutput, *,
                  before_check: Callable[[], None] = no_hold_wait) -> QueueReply:
    """Send a queue request and wait for its answer.

    This is the one place that requests a build queue place.
    """
    if isinstance(request, RefreshedRequest):
        return admission_client.wait_for_admission(request.refresh, output, attempt=before_check)
    if isinstance(request, RefreshedLaunch):
        return admission_client.wait_for_launch(request.refresh, output)
    return admission_client.service_admit(request, output)


def local_context(local: wire.AdmitRequest) -> wire.LocalAdmissionContext:
    """The local part of a placement request: what a local grant admits."""
    return wire.LocalAdmissionContext(local.argv, local.cwd, local.sidecar, local.call, local.test_width,
                                      local.fresh_test, local.token_back_at)


PlacementEnd = wire.LocalPlacementGranted | wire.Assignment | wire.DispatchStatus | admission_client.LeftQueue


def refreshed_placement(request: wire.PlacementRequest, local: wire.LocalAdmissionContext) -> wire.PlacementRequest:
    """The request with a new estimate for its local part. A program profile stays; a step profile comes from argv."""
    profile = (request.profile if isinstance(request.profile, admission_records.ProgramProfile)
               else admission_records.INFER_FROM_ARGV)
    refreshed = admission_client.step_request(list(local.argv), local.cwd, wire.Owner(request.owner.pid, request.owner.start_time),
                                              request.since, local.sidecar, request.episode, profile, run=request.run)
    return replace(request, profile=refreshed.profile, local=local_context(refreshed))


def wait_for_placement(request: wire.PlacementRequest, cancel_reason: str) -> PlacementEnd:
    """Send the placement request until the queue gives it a machine or ends its episode.

    An expired estimate rebuilds the local part of the request with refreshed_placement. An answer that
    gives no machine, and an error, cancel the episode with cancel_reason.
    """
    output = admission_client.WaitOutput()
    try:
        with admission_client.placement_acquisition():
            while True:
                answer = admission_client.acquire_placement(request)
                if isinstance(answer, admission_client.LeftQueue):
                    break
                output.placement_notices(answer, placement_events=False)
                if isinstance(answer, (wire.Assignment, wire.LocalPlacementGranted)):
                    output.finished(left=isinstance(answer, wire.Assignment))
                    return answer
                if isinstance(answer, wire.DispatchStatus):
                    break
                if isinstance(answer.wait, wire.Wait):
                    output(answer.wait)
                    cause = answer.wait.reason
                    if isinstance(cause, admission_records.HeadAdmissionStatus):
                        cause = cause.cause
                    if isinstance(request.local, wire.LocalAdmissionContext) and isinstance(cause, admission_records.EstimateExpired):
                        request = refreshed_placement(request, request.local)
                elif isinstance(answer.wait, wire.PlacementResourceWait):
                    output.resource_wait(answer.wait)
                time.sleep(admission_records.read_memory_config().waiting_pass_seconds)
    except admission_client.RunReplaced:
        # The newer run holds this place now, so this run has no placement left to cancel.
        raise
    except BaseException:
        _ = admission_client.cancel_placement(wire.CancelPlacement(uuid.uuid4().hex, request.episode, request.owner,
                                                                   cancel_reason))
        raise
    _ = admission_client.cancel_placement(wire.CancelPlacement(uuid.uuid4().hex, request.episode, request.owner,
                                                               cancel_reason))
    return answer


def release_place(reservation: str, status: int, timing: wire.CleanupTiming,
                  ending: wire.ReleaseEnding = "result", *, stop_seconds: float | None = None) -> wire.Ok | wire.Refused:
    """End a reservation with its holder's status."""
    release = admission_client.release
    if stop_seconds is None:
        return release(reservation, status, timing, ending)
    deadline = admission_client.CLOCK.monotonic() + stop_seconds
    try:
        if ending == "result":
            return release(reservation, status, timing, deadline=deadline)
        return release(reservation, status, timing, ending, deadline=deadline)
    except admission_client.ReleaseDeadlineExceeded as error:
        raise CleanupUnresolved(str(error)) from error


def shell_stop_seconds() -> float:
    try:
        return conf.read("build_admission").number("program_stop_seconds")
    except conf.Unusable:
        return wire.PROTOCOL_CLEANUP_TIMING.silence_seconds


@dataclass
class HeldReservation:
    grant: wire.Admitted
    status: int = 1
    keep: bool = False
    released: bool = field(default=False, init=False)


@contextmanager
def held_reservation(request: QueueRequest, *,
                     before_check: Callable[[], None] = no_hold_wait,
                     stop_seconds: float | None = None) -> Generator[HeldReservation]:
    """Wait for admission, then release the grant with the holder's status."""
    # service_admit sets the service estimate timing on the output.
    output = admission_client.WaitOutput()
    reply = request_place(request, output, before_check=before_check)
    if isinstance(reply, wire.Refused):
        raise QueuePlaceRefused(reply.reason)
    if not isinstance(reply, wire.Admitted):
        raise QueuePlaceRefused("left the admission queue")
    held = HeldReservation(reply)
    if stop_seconds is None and isinstance(reply, wire.RunnerAdmitted):
        stop_seconds = reply.execution.program_stop_seconds
    try:
        output.finished()
        yield held
    finally:
        if not held.keep:
            answer = release_place(reply.reservation, held.status, reply.cleanup_timing, stop_seconds=stop_seconds)
            if isinstance(answer, wire.Refused):
                raise CleanupUnresolved(f"the service refused release of reservation {reply.reservation}: {answer.reason}")
            held.released = True


@dataclass
class PlaceSetup:
    """A queue place before its grant: its folder, its sidecar and the request for its reservation.

    request is None for a program that takes no reservation.
    """
    folder: Path
    sidecar: Path
    configuration: admission_records.MemoryConfig
    purpose: ProgramPurpose
    arguments: tuple[str, ...]
    request: Callable[[], wire.AdmitRequest] | None
    keep_folder: bool = field(default=False, init=False)

    @contextmanager
    def place(self, grant: HeldReservation | UnreservedProgram) -> Generator[QueuePlace]:
        """Start the program in the place of the grant, and stop what is left of it at the end."""
        sample_path = (Path(grant.grant.max_path).with_suffix("") if isinstance(grant, HeldReservation)
                       else self.folder / "program")
        place = QueuePlace(sample_path, self.sidecar, configuration=self.configuration, purpose=self.purpose,
                           arguments=self.arguments, reservation=grant)
        self.keep_folder = True
        if isinstance(grant, HeldReservation):
            grant.keep = True
        failed = False
        try:
            yield place
        except BaseException as error:
            failed = True
            place.status = (128 + signal.SIGINT if isinstance(error, KeyboardInterrupt) else
                            error.code if isinstance(error, SystemExit) and isinstance(error.code, int) else 1)
            raise
        finally:
            place.finish(failed=failed)
            self.keep_folder = place.keep_reservation
            if isinstance(grant, HeldReservation):
                grant.status = place.status
                grant.keep = place.keep_reservation


@contextmanager
def place_setup(program: str | Sequence[str], repo: Path, *, batch: bool = False,
                sizing: admission_records.HistoryPeakSizing | ConfiguredFirstPeak = CONFIGURED_FIRST_PEAK
                ) -> Generator[PlaceSetup]:
    """Use full commands for purpose selection; retain named batch reservations.

    A complete Cargo command sizes from its step history, as each Cargo step does.
    """
    # A name requests a batch place. A complete command permits purpose selection.
    try:
        if isinstance(program, str):
            arguments: tuple[str, ...] = ()
            name, purpose = program, ProgramPurpose.BATCH
        else:
            arguments = tuple(program)
            purpose = program_purpose(arguments, batch=batch)
            name = Path(program[0]).name
        config = step_estimator.memory_config()
    except conf.Unusable as error:
        raise QueuePlaceRefused(str(error)) from error
    estimate: admission_records.HistoryPeakSizing | ConfiguredFirstPeak | admission_records.InferFromArgv = sizing
    if purpose == ProgramPurpose.BATCH and arguments and name == "cargo" and isinstance(sizing, ConfiguredFirstPeak):
        estimate = admission_records.INFER_FROM_ARGV
    if purpose == ProgramPurpose.BATCH and isinstance(estimate, ConfiguredFirstPeak):
        first = config.program_first_peaks.get(name)
        if first is None:
            raise QueuePlaceRefused(f"no first peak for {name} in build_admission.conf [memory]; {conf.path('build_admission')}: program_first_peak_gib.{name} is missing or invalid")
        estimate = admission_records.HistoryPeakSizing(first, admission_records.FirstProgramPeak(first), 0.0)
    cleanup_queue_place_folders(cleanup_program_starts(config))
    pid = os.getpid()
    if admission_records.process_start_time(pid) is None:
        raise QueuePlaceRefused("owner no longer exists")
    setup: PlaceSetup | None = None
    with queue_place_folder(keep=lambda: setup is not None and setup.keep_folder) as temporary:
        sidecar = temporary / build_folders.QUEUE_PROGRAM_SIDECAR
        atomic_file.atomic_sample(sidecar, "")
        request: Callable[[], wire.AdmitRequest] | None = None
        if purpose == ProgramPurpose.BATCH:
            assert not isinstance(estimate, ConfiguredFirstPeak)
            profile = (estimate if isinstance(estimate, admission_records.InferFromArgv) else
                       admission_records.ProgramProfile(name, str(Path.home() / ".claude"), str(repo.resolve()), estimate))
            start = admission_records.process_start_time(pid)
            if start is None:
                raise QueuePlaceRefused("owner no longer exists")
            owner, episode, since = wire.Owner(pid, start), uuid.uuid4().hex, time.time()

            def refresh() -> wire.AdmitRequest:
                return admission_client.step_request(list(arguments) or [name], str(repo), owner, since, str(sidecar), episode, profile)
            request = refresh
        setup = PlaceSetup(temporary, sidecar, config, purpose, arguments, request)
        yield setup


@contextmanager
def queue_place(program: str | Sequence[str], repo: Path, *, batch: bool = False,
                sizing: admission_records.HistoryPeakSizing | ConfiguredFirstPeak = CONFIGURED_FIRST_PEAK,
                before_check: Callable[[], None] = no_hold_wait) -> Generator[QueuePlace]:
    """Keep the reservation and sidecar through cleanup."""
    with place_setup(program, repo, batch=batch, sizing=sizing) as setup:
        grant: HeldReservation | UnreservedProgram = UnreservedProgram()
        try:
            with ExitStack() as reservation:
                if setup.request is not None:
                    grant = reservation.enter_context(held_reservation(
                        RefreshedRequest(setup.request), before_check=before_check,
                        stop_seconds=setup.configuration.program_stop_seconds))
                with setup.place(grant) as active:
                    yield active
        finally:
            if isinstance(grant, HeldReservation) and not grant.released:
                setup.keep_folder = True


class ShellArguments(argparse.Namespace):
    command: str = ""
    pid: int = 0
    since: float = 0.0
    sidecar: str = ""
    shell_state: Path | None = None
    wait_events: bool = False
    argv: list[str] = []
    reservation: str = ""
    status: int = 0


SHELL_ACTIONS = ("admit", "launch", "release")


def shell_arguments(argv: list[str]) -> ShellArguments:
    parser = argparse.ArgumentParser(prog="queue_place.py",
                                     description="Wait for a shell step's queue place, or release it.")
    actions = parser.add_subparsers(dest="command", required=True)
    for name in ("admit", "launch"):
        wait = actions.add_parser(name)
        _ = wait.add_argument("--pid", type=int, default=os.getppid())
        _ = wait.add_argument("--since", type=float, default=0.0)
        _ = wait.add_argument("--sidecar", default="")
        _ = wait.add_argument("--shell-state", type=Path)
        _ = wait.add_argument("--wait-events", action="store_true")
        _ = wait.add_argument("argv", nargs=argparse.REMAINDER)
    release = actions.add_parser("release")
    _ = release.add_argument("reservation")
    _ = release.add_argument("status", type=int)
    return parser.parse_args(argv, namespace=ShellArguments())


def shell_wait(args: ShellArguments) -> int:
    """Wait for a shell step's place and write the grant as shell assignments.

    The step's shell releases the reservation later with the release action.
    """
    output = admission_client.WaitOutput(on_event=(lambda event: print(event, flush=True))
                                         if args.wait_events else lambda _event: None)
    launching = args.command == "launch"
    outcome = "Refused"
    replaced = ""
    try:
        owner_start = admission_records.process_start_time(args.pid)
        if owner_start is None:
            print("memory admission check failed (owner no longer exists); step refused", file=sys.stderr)
            return 2
        owner, episode = wire.Owner(args.pid, owner_start), uuid.uuid4().hex
        since = args.since or admission_client.CLOCK.now()
        run = admission_client.parse_run_identity(os.environ.get("BUILDLOG_RUN", ""))
        step_argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        unit = ""

        def refresh() -> wire.AdmitRequest:
            nonlocal unit
            request = admission_client.step_request(step_argv, os.getcwd(), owner, since, args.sidecar, episode, run=run)
            unit = request.identity.unit
            return request

        def refresh_launch() -> wire.LaunchRequest:
            request = refresh()
            return wire.LaunchRequest(episode, owner, since, request.identity, request.profile)

        grant = request_place(RefreshedLaunch(refresh_launch) if launching else RefreshedRequest(refresh), output)
        if isinstance(grant, admission_client.LeftQueue):
            outcome = "LeftQueue"
            output.finished(left=True)
            if args.shell_state is not None:
                _ = args.shell_state.write_text("BUILDLOG_MEM_OUTCOME=LeftQueue\nBUILDLOG_MEM_WAIT_S="
                    + str(output.elapsed_seconds()) + "\n")
            return 75
        if isinstance(grant, wire.Refused):
            output.deadline_reached = launching and grant.reason == admission_client.LAUNCH_WAIT_LIMIT_REASON
            if (line := admission_client.replaced_line(grant.reason, unit)) is not None:
                replaced = line
                print(line, file=sys.stderr)
            return 2
        if args.shell_state is not None:
            try:
                admission_client.write_shell_grant(args.shell_state, grant, output.elapsed_seconds())
            except BaseException:
                if isinstance(grant, wire.Admitted):
                    _ = release_place(grant.reservation, 1, grant.cleanup_timing, stop_seconds=shell_stop_seconds())
                raise
        outcome = "Granted"
        output.finished()
        if not args.wait_events:
            print(wire.encode_reply(grant), end="")
        return 0
    except wire.AdmissionUnavailable as error:
        print(f"admission service unavailable: {error.reason}", file=sys.stderr)
        return 2
    except (ValueError, OSError) as error:
        print(f"memory admission check failed ({str(error) or 'checker returned an error'}); step refused", file=sys.stderr)
        return 2
    finally:
        if output.held:
            output.on_event("done")
        if args.shell_state is not None:
            try:
                with args.shell_state.open("a") as state:
                    _ = state.write(f"BUILDLOG_MEM_WAIT_S={output.elapsed_seconds()}\n"
                                    + f"BUILDLOG_MEM_OUTCOME={outcome}\n"
                                    + f"BUILDLOG_MEM_REPLACED={shlex.quote(replaced)}\n")
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    """Run a program in its queue place, or serve a shell step's admit, launch or release."""
    argv = sys.argv[1:] if argv is None else argv
    # A run that loaded queue_place.py before this change still calls these actions.
    if argv[:1] in (["--program-start"], ["--scope-start"]):
        return startup.main(argv)
    if argv[:1] and argv[0] in SHELL_ACTIONS:
        args = shell_arguments(argv)
        if args.command == "release":
            try:
                reply = release_place(args.reservation, args.status, admission_client.saved_cleanup_timing(),
                                      stop_seconds=shell_stop_seconds())
            except CleanupUnresolved as error:
                print(error, file=sys.stderr)
                return 2
            if isinstance(reply, wire.Refused):
                return 2
            print(wire.encode_record(reply))
            return 0
        return shell_wait(args)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    try:
        descriptor = os.environ.pop("CARGO_OUTPUT_LOCK_FD", "")
        pass_fds = (int(descriptor),) if descriptor else ()
        for fd in pass_fds:
            os.set_inheritable(fd, False)
        with queue_place(argv, Path.cwd()) as place:
            return place.popen(argv, pass_fds=pass_fds).wait()
    except (QueuePlaceRefused, OSError, ValueError) as error:
        print(f"queue place: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
