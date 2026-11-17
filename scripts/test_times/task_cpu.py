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
from typing import cast

from scripts.lib import conf


CLEANUP_LIMIT_S = 2.0
WAIT_TRACED = 0x40000000 | 0x20000000  # __WALL | __WNOTHREAD


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


def pending_processes() -> dict[int, ProcessLocation]:
    """Find the live descendants of this process."""
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
    parents = [os.getpid()]
    while parents:
        parent = parents.pop()
        for pid in children.pop(parent, []):
            parents.append(pid)
            if locations[pid].state != "Z":
                pending[pid] = locations[pid]
    return pending


def stop_pending() -> tuple[str, ...]:
    """Name and stop live processes, including children that start during cleanup."""
    deadline = time.monotonic() + CLEANUP_LIMIT_S
    names: dict[tuple[int, str], str] = {}
    while time.monotonic() < deadline:
        pending = pending_processes()
        if not pending:
            break
        with ExitStack() as cleanup:
            ended = select.poll()
            exited_during_scan = False
            targets: list[int] = []
            for pid, snapshot in sorted(pending.items()):
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
                ended.register(descriptor, select.POLLIN)
                key = (pid, location.start)
                if location.state != "Z" and key not in names:
                    command = " ".join(" ".join(command_arguments(pid)).split()) or "[no command line]"
                    names[key] = f"{pid} ({command})"
                    targets.append(descriptor)
            for descriptor in targets:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            # A missing or replaced PID already confirms an exit.
            if not exited_during_scan and not ended.poll(math.ceil(max(0, deadline - time.monotonic()) * 1000)):
                break
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


@dataclass(frozen=True)
class ExitedTask:
    tid: int
    tgid: int
    cpu_s: float


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


def measure(command: Sequence[str], *, cwd: Path, environment: dict[str, str],
            pass_fds: Sequence[int] = (), timeout_s: float | None = None) -> TaskCPU:
    """Trace process events in a process that adopts its orphaned descendants."""
    limit = measurement_timeout_s() if timeout_s is None else timeout_s
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
    deadline = time.monotonic() + limit
    reader, writer = os.pipe()
    root = os.fork()
    if root == 0:
        try:
            _ = os.setsid()
            os.chdir(cwd)
            for descriptor in (1, 2):
                with open(os.devnull, "wb") as output:
                    _ = os.dup2(output.fileno(), descriptor)
            os.close(writer)
            released = os.read(reader, 1)
            os.close(reader)
            if released != b"1":
                os._exit(127)
            for descriptor in pass_fds:
                os.set_inheritable(descriptor, True)
            os.execvpe(command[0], list(command), environment)
        except BaseException:
            os._exit(127)
    commands = {root: command_name(command)}
    exited: list[ExitedTask] = []
    root_status = 0
    root_or_trace_ended = threading.Event()
    failures: list[BaseException] = []
    # Trace creation, exec and exit events. End tracees if the tracer ends.
    options = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 6) | (1 << 20)
    os.close(reader)

    def collect() -> None:
        nonlocal root_status
        try:
            try:
                trace(0x4206, root, ctypes.c_void_p(options))
                _ = os.write(writer, b"1")
            finally:
                os.close(writer)
            while True:
                try:
                    pid, status = os.waitpid(-1, WAIT_TRACED)
                except ChildProcessError:
                    break
                if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    if pid == root:
                        root_status = os.waitstatus_to_exitcode(status)
                        root_or_trace_ended.set()
                    continue
                if not os.WIFSTOPPED(status):
                    continue
                event = status >> 16
                if pid not in commands:
                    tgid, parent = task_ids(pid)
                    inherited = commands.get(tgid if tgid != pid else parent)
                    # A child can outlive its parent before the first stop is read.
                    arguments = command_arguments(pid) if inherited is None else []
                    commands[pid] = command_name(arguments) if arguments else inherited or "[no command line]"
                if event == 4:
                    arguments = command_arguments(pid)
                    if arguments:
                        commands[pid] = command_name(arguments)
                elif event == 6:
                    cpu_s = int(Path(f"/proc/{pid}/schedstat").read_text().split()[0]) / 1e9
                    exited.append(ExitedTask(pid, task_ids(pid)[0], cpu_s))
                stopped = os.WSTOPSIG(status)
                if event == 128 and stopped in (signal.SIGSTOP, signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU):
                    resume(0x4208, pid)
                else:
                    resume(7, pid, 0 if event else stopped)
        except BaseException as error:
            failures.append(error)
        finally:
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
        return pending

    try:
        ended = root_or_trace_ended.wait(max(0, deadline - time.monotonic()))
        if ended:
            tracer.join(leftover_limit)
    except BaseException:
        _ = stop_trace()
        raise
    if not ended:
        raise MeasurementTimeout(limit, stop_trace())
    if tracer.is_alive():
        raise LeftoverProcesses(stop_trace())
    if failures:
        raise failures[0]
    totals: dict[str, float] = {}
    starts: dict[str, int] = {}
    for task in exited:
        name = commands[task.tid]
        totals[name] = totals.get(name, 0.0) + task.cpu_s
        if task.tid == task.tgid:
            starts[name] = starts.get(name, 0) + 1
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    rows = [CommandCPU(name, starts.get(name, 0), cpu) for name, cpu in totals.items()]
    rows.sort(key=lambda row: (-row.cpu_s, row.command))
    cpu_s = after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
    return TaskCPU(rows, cpu_s, root_status)
