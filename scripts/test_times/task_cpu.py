"""Measure task CPU at kernel exit events and group it by the last command."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import ExitStack, suppress
import ctypes
from dataclasses import dataclass
import math
import os
from pathlib import Path
import resource
import select
import signal
import threading
import time
from typing import Never, cast

from scripts.lib import conf


CLEANUP_LIMIT_S = 2.0
PTRACE_SEIZE = 0x4206
WAIT_OWN_CHILDREN = 0x20000000  # __WNOTHREAD
WAIT_TRACED = 0x40000000 | WAIT_OWN_CHILDREN  # __WALL


class MeasurementTimeout(TimeoutError):
    """Name the processes still present when a measurement reaches its limit."""

    def __init__(self, timeout_s: float, pending: Sequence[str]) -> None:
        names = ", ".join(pending) or "none visible"
        super().__init__(f"timed out after {timeout_s:g} s. Pending processes: {names}")


class LeftoverProcesses(RuntimeError):
    """Name the processes still running after the test ended."""

    def __init__(self, pending: Sequence[str]) -> None:
        names = ", ".join(pending) or "none visible"
        super().__init__(f"left processes after the test ended: {names}")


class TraceStartFailure(RuntimeError):
    """Report a failed trace attachment."""


def measurement_timeout_s() -> float:
    """Read the time limit for one measurement stage."""
    return conf.read("test_times").number("MODULE_COST_STAGE_TIMEOUT_S")


def command_arguments(pid: int) -> list[str]:
    """Read the arguments of a live process or return an empty list."""
    with suppress(OSError):
        data = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0")
        if data:
            return [os.fsdecode(argument) for argument in data.split(b"\0")]
    return []


@dataclass(frozen=True)
class ProcessLocation:
    parent: int
    start: str
    state: str


def process_location(pid: int) -> ProcessLocation:
    """Read process ownership and identity from procfs."""
    folder = Path(f"/proc/{pid}")
    fields = (folder / "stat").read_text(errors="replace").rsplit(")", 1)[1].split()
    return ProcessLocation(int(fields[1]), fields[19], fields[0])


def pending_processes(root_pid: int) -> dict[int, ProcessLocation]:
    """Find descendants for cleanup, including groups with exited leaders."""
    locations: dict[int, ProcessLocation] = {}
    children: dict[int, list[int]] = {}
    for folder in Path("/proc").iterdir():
        if folder.name.isdecimal():
            pid = int(folder.name)
            with suppress(OSError, IndexError):
                location = process_location(pid)
                locations[pid] = location
                children.setdefault(location.parent, []).append(pid)
    pending: dict[int, ProcessLocation] = {}
    parents = [root_pid]
    while parents:
        parent = parents.pop()
        for pid in children.pop(parent, []):
            parents.append(pid)
            pending[pid] = locations[pid]
    return pending


def reap_exited() -> bool:
    """Reap this thread's exited children. Return true if children remain."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG | WAIT_OWN_CHILDREN)
        except ChildProcessError:
            return False
        if not pid:
            return True


def stop_pending() -> tuple[str, ...]:
    """Name and stop live processes, including children that start during cleanup."""
    root_pid = os.getpid()
    deadline = time.monotonic() + CLEANUP_LIMIT_S
    names: dict[tuple[int, str], str] = {}
    zombies: set[tuple[int, str]] = set()
    while time.monotonic() < deadline:
        children_remain = reap_exited()
        pending = pending_processes(root_pid)
        if not pending and not children_remain:
            break
        with ExitStack() as cleanup:
            ended = select.poll()
            exited_during_scan = False
            live = False
            targets: list[int] = []
            for pid, snapshot in sorted(pending.items()):
                key = (pid, snapshot.start)
                if key in zombies:
                    continue
                try:
                    descriptor = os.pidfd_open(pid)
                except ProcessLookupError:
                    exited_during_scan = True
                    continue
                _ = cleanup.callback(os.close, descriptor)
                try:
                    location = process_location(pid)
                except (FileNotFoundError, ProcessLookupError):
                    exited_during_scan = True
                    continue
                if location.start != snapshot.start:
                    exited_during_scan = True
                    continue
                if location.state == "Z":
                    group_exit = select.poll()
                    group_exit.register(descriptor, select.POLLIN)
                    # A zombie leader can have live threads. The pidfd marks the group exit.
                    if group_exit.poll(0):
                        zombies.add(key)
                        exited_during_scan = True
                        continue
                live = True
                ended.register(descriptor, select.POLLIN)
                if key not in names:
                    command = " ".join(" ".join(command_arguments(pid)).split()) or "[no command line]"
                    names[key] = f"{pid} ({command})"
                    targets.append(descriptor)
            for descriptor in targets:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            # A missing or replaced PID already confirms an exit.
            if not exited_during_scan:
                remaining_ms = math.ceil(max(0, deadline - time.monotonic()) * 1000)
                if not live and not children_remain:
                    break
                # Rescan after a short wait if a child changes parents during the scan.
                _ = ended.poll(remaining_ms if live else min(remaining_ms, 10))
    _ = reap_exited()
    return tuple(names.values())


@dataclass(frozen=True)
class CommandCPU:
    command: str
    starts: int
    cpu_s: float


@dataclass(frozen=True)
class TaskCPU:
    commands: list[CommandCPU]
    cpu_s: float
    status: int
    processes: int
    execs: int


def command_name(arguments: Sequence[str]) -> str:
    """Keep the program and the script when the program is an interpreter."""
    program = Path(arguments[0]).name
    python = program.startswith(("python", "pypy"))
    if not python and program not in {"bash", "sh", "zsh"}:
        return program
    options = iter(arguments[1:])
    for argument in options:
        if argument == "--":
            script = next(options, "")
            return f"{program} {script}" if script else program
        if not argument.startswith("-") or argument == "-":
            return f"{program} {argument}"
        if argument.startswith("--"):
            continue
        if argument.endswith("c"):
            return f"{program} -c"
        if python and argument.endswith("m"):
            return f"{program} -m {next(options, '')}".rstrip()
        if argument.endswith(("W", "X") if python else ("o", "O")):
            _ = next(options, "")
    return program


def task_ids(pid: int) -> tuple[int, int]:
    """Return a task's thread group id and its parent process id."""
    lines = Path(f"/proc/{pid}/status").read_bytes().splitlines()
    tgid = int(next(line.split()[1] for line in lines if line.startswith(b"Tgid:")))
    parent = int(next(line.split()[1] for line in lines if line.startswith(b"PPid:")))
    return tgid, parent


def exec_stage(command: Sequence[str], cwd: Path, environment: dict[str, str],
               pass_fds: Sequence[int], output_fd: int) -> Never:
    """Set up and replace the stage child."""
    _ = os.setsid()
    os.chdir(cwd)
    for descriptor in (1, 2):
        _ = os.dup2(output_fd, descriptor)
    for descriptor in pass_fds:
        os.set_inheritable(descriptor, True)
    os.execvpe(command[0], list(command), environment)


def measure(command: Sequence[str], *, cwd: Path, environment: dict[str, str],
            pass_fds: Sequence[int] = (), timeout_s: float | None = None,
            command_rows: bool = True, output_fd: int | None = None) -> TaskCPU:
    """Trace process events in a process that adopts its orphaned descendants."""
    limit = measurement_timeout_s() if timeout_s is None else timeout_s
    deadline = time.monotonic() + limit
    leftover_limit = conf.read("test_times").number("MODULE_COST_LEFTOVER_S")
    libc = ctypes.CDLL(None, use_errno=True)
    ptrace = libc.ptrace
    ptrace.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    ptrace.restype = ctypes.c_long

    def trace(request: int, pid: int, data: ctypes.c_void_p) -> None:
        result = cast(int, ptrace(request, pid, None, data))
        if result == -1:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))

    def resume(request: int, pid: int, delivered: int = 0) -> None:
        with suppress(ProcessLookupError):
            trace(request, pid, ctypes.c_void_p(delivered))

    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    reader, writer = os.pipe()
    root = os.fork()
    if root == 0:
        try:
            os.close(writer)
            released = os.read(reader, 1)
            os.close(reader)
            if released != b"1":
                os._exit(127)
            if output_fd is None:
                with open(os.devnull, "wb") as output:
                    exec_stage(command, cwd, environment, pass_fds, output.fileno())
            exec_stage(command, cwd, environment, pass_fds, output_fd)
        except BaseException:
            os._exit(127)
    commands = {root: command_name(command) if command_rows else ""}
    leaders = {root}
    # The initial tracee has no traced creation event.
    processes = 0
    execs = 0
    totals: dict[str, float] = {}
    starts: dict[str, int] = {}
    root_status = 0
    root_or_trace_ended = threading.Event()
    trace_ended_at = math.inf
    failures: list[BaseException] = []
    # Trace creation, exec and exit events. End tracees if the tracer ends.
    options = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 20)
    if command_rows:
        options |= 1 << 6
    os.close(reader)

    def collect() -> None:
        nonlocal root_status, processes, execs, trace_ended_at
        try:
            try:
                try:
                    trace(PTRACE_SEIZE, root, ctypes.c_void_p(options))
                except OSError as error:
                    raise TraceStartFailure(f"cannot attach tracer: {error}") from error
                _ = os.write(writer, b"1")
            finally:
                os.close(writer)
            while True:
                try:
                    pid, status = os.waitpid(-1, WAIT_TRACED)
                except ChildProcessError:
                    break
                if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    name = commands.pop(pid, "[no command line]")
                    if pid in leaders:
                        leaders.remove(pid)
                        if command_rows:
                            starts[name] = starts.get(name, 0) + 1
                    if pid == root:
                        root_status = os.waitstatus_to_exitcode(status)
                        root_or_trace_ended.set()
                    continue
                if not os.WIFSTOPPED(status):
                    continue
                event = status >> 16
                if pid not in commands:
                    tgid, parent = task_ids(pid)
                    if tgid == pid:
                        processes += 1
                        leaders.add(pid)
                    commands[pid] = ""
                    if command_rows:
                        inherited = commands.get(tgid if tgid != pid else parent)
                        # A child can outlive its parent before the first stop.
                        arguments = command_arguments(pid) if inherited is None else []
                        commands[pid] = command_name(arguments) if arguments else inherited or "[no command line]"
                if event == 4:
                    execs += 1
                    if command_rows:
                        arguments = command_arguments(pid)
                        if arguments:
                            commands[pid] = command_name(arguments)
                elif event == 6:
                    name = commands[pid]
                    cpu_s = int(Path(f"/proc/{pid}/schedstat").read_text().split()[0]) / 1e9
                    totals[name] = totals.get(name, 0.0) + cpu_s
                stopped = os.WSTOPSIG(status)
                if event == 128 and stopped in (signal.SIGSTOP, signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU):
                    resume(0x4208, pid)
                else:
                    resume(7, pid, 0 if event else stopped)
        except BaseException as error:
            failures.append(error)
        finally:
            trace_ended_at = time.monotonic()
            root_or_trace_ended.set()

    tracer = threading.Thread(target=collect, daemon=True)
    try:
        tracer.start()
    except BaseException:
        os.close(writer)
        raise

    def stop_trace() -> tuple[str, ...]:
        pending = stop_pending()
        tracer.join(CLEANUP_LIMIT_S)
        _ = reap_exited()
        return pending

    try:
        ended = root_or_trace_ended.wait(max(0, deadline - time.monotonic()))
        leftover_deadline = time.monotonic() + leftover_limit
        if ended:
            tracer.join(max(0, min(deadline, leftover_deadline) - time.monotonic()))
    except BaseException:
        _ = stop_trace()
        raise
    if not ended or (deadline <= leftover_deadline and trace_ended_at > deadline):
        raise MeasurementTimeout(limit, stop_trace())
    if trace_ended_at > leftover_deadline:
        raise LeftoverProcesses(stop_trace())
    if failures:
        failure = failures[0]
        if isinstance(failure, TraceStartFailure):
            pending = stop_trace()
            if tracer.is_alive() or reap_exited():
                raise LeftoverProcesses(pending or (f"{root} (trace child)",)) from failure
        raise failure
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    rows = [CommandCPU(name, starts.get(name, 0), cpu) for name, cpu in totals.items()]
    rows.sort(key=lambda row: (-row.cpu_s, row.command))
    cpu_s = after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
    return TaskCPU(rows, cpu_s, root_status, processes, execs)
