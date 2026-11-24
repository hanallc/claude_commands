#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Measure module CPU, process starts, successful execs and wall time."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import codecs
import json
from collections.abc import Generator
from contextlib import contextmanager, redirect_stderr, redirect_stdout, suppress
from dataclasses import dataclass
import io
import math
import os
from pathlib import Path
import select
import shutil
import signal
import stat
from struct import Struct
import sys
from tempfile import TemporaryFile
import threading
import time
import traceback
from typing import Literal, TextIO, TypedDict, cast

from scripts.lib import build_folders, conf
from scripts.lib.descendants import adopt_descendants
from scripts.lib.terminal_cells import print_table, seconds
from scripts.production.gate import test_module_environment
from scripts.test_times import task_cpu


STAGE_START = Struct("d")


class MeasuredTest(TypedDict):
    test: str
    kind: Literal["test", "fixture"]
    cpu_s: float
    s: float


class TimeReport(TypedDict):
    tests: list[MeasuredTest]
    fixtures: list[MeasuredTest]


@dataclass(frozen=True)
class ModuleCost:
    module: str
    cpu_s: float
    wall_s: float
    test_status: int
    trace: task_cpu.TaskCPU
    tests: list[MeasuredTest]

    @property
    def passed(self) -> bool:
        return self.test_status == self.trace.status == 0

    def row(self) -> list[str]:
        status = "PASS" if self.passed else f"RED (test={self.test_status}, trace={self.trace.status})"
        return [self.module, seconds(self.cpu_s), str(self.trace.processes), str(self.trace.execs),
                seconds(self.wall_s), status]


def runner_command(tree: Path, module: str) -> list[str]:
    folder, filename = module.rsplit("/", 1)
    return [sys.executable, str(tree / "scripts/test_times/module_run.py"), f"scripts/{folder}", filename]


def wait_for_stage(pid: int, deadline: float, timeout_s: float) -> tuple[int, float]:
    """Return the stage status and CPU after a bounded wait."""
    descriptor = os.pidfd_open(pid)
    try:
        ended = select.poll()
        ended.register(descriptor, select.POLLIN)
        if ended.poll(math.ceil(max(0, deadline - time.monotonic()) * 1000)):
            reaped, status, usage = os.wait4(pid, os.WNOHANG)
            if reaped and time.monotonic() < deadline:
                _ = task_cpu.reap_exited()
                return os.waitstatus_to_exitcode(status), usage.ru_utime + usage.ru_stime
    finally:
        os.close(descriptor)
    raise task_cpu.MeasurementTimeout(timeout_s, task_cpu.stop_pending())


def measure(tree: Path, module: str, timeout_s: float, stage_updates: int, *, command_rows: bool) -> ModuleCost:
    command = runner_command(tree, module)
    with build_folders.tool_folder("module-cost") as temporary:
        root = Path(temporary)
        environment = test_module_environment(root, dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        report = root / "times.json"
        environment["TEST_TIMES_OUT"] = str(report)
        lock_fd = build_folders.build_folder_lock_fd(root)
        with (root / "time.log").open("w+") as output:
            started = time.monotonic()
            _ = os.write(stage_updates, STAGE_START.pack(started))
            print(f"{module}: starting time run", file=sys.stderr, flush=True)
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(stage_updates)
                    task_cpu.exec_stage(command, tree, environment, (lock_fd,), output.fileno())
                except BaseException:
                    traceback.print_exc(file=output)
                    output.flush()
                finally:
                    os._exit(127)
            test_status, cpu_s = wait_for_stage(pid, started + timeout_s, timeout_s)
            wall_s = time.monotonic() - started
            if test_status:
                _ = output.seek(0)
                _ = shutil.copyfileobj(output, sys.stderr)
        tests: list[MeasuredTest] = []
        if report.is_file():
            data = cast(TimeReport, json.loads(report.read_text()))
            tests = [row for row in data["tests"] if row["kind"] == "test"] + data["fixtures"]
        _ = environment.pop("TEST_TIMES_OUT")
        with (root / "trace.log").open("w+") as output:
            _ = os.write(stage_updates, STAGE_START.pack(time.monotonic()))
            print(f"{module}: starting trace run", file=sys.stderr, flush=True)
            try:
                trace = task_cpu.measure(command, cwd=tree, environment=environment, timeout_s=timeout_s,
                                         pass_fds=(lock_fd,), command_rows=command_rows, output_fd=output.fileno())
            except task_cpu.TraceStartFailure as error:
                print(f"{module}: {error}", file=sys.stderr, flush=True)
                trace = task_cpu.TaskCPU([], 0.0, 1, 0, 0)
            else:
                if trace.status:
                    _ = output.seek(0)
                    _ = shutil.copyfileobj(output, sys.stderr)
    return ModuleCost(module, cpu_s, wall_s, test_status, trace, tests)


@dataclass(init=False)
class CostArguments(argparse.Namespace):
    tree: Path
    modules: list[str]
    tests: int
    commands: int


def stop_worker(descriptor: int) -> None:
    """End the worker so its descendants can reparent to the supervisor."""
    with suppress(ProcessLookupError):
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)


def wait_for_measurement(pid: int, descriptor: int, stage_updates: int, deadline: float, stage_limit: float) -> int:
    """Watch the worker and give each stage time to complete its cleanup."""
    ready = select.poll()
    ready.register(descriptor, select.POLLIN)
    ready.register(stage_updates, select.POLLIN)
    while events := ready.poll(math.ceil(max(0, deadline - time.monotonic()) * 1000)):
        for fd, _ in events:
            if fd == descriptor:
                reaped, status = os.waitpid(pid, os.WNOHANG)
                if reaped:
                    return os.waitstatus_to_exitcode(status)
            else:
                update = os.read(stage_updates, STAGE_START.size)
                if update:
                    stage_started, = cast(tuple[float], STAGE_START.unpack(update))
                    deadline = stage_started + stage_limit
                else:
                    ready.unregister(stage_updates)
    raise TimeoutError


def supervise(request: CostArguments, timeout_s: float, stage_limit: float, stdout: TextIO, stderr: TextIO) -> int:
    """Keep ownership of the worker tree until all descendants exit."""
    _ = adopt_descendants()
    reader, writer = os.pipe()
    try:
        started = time.monotonic()
        pid = os.fork()
        if pid == 0:
            os.close(reader)
            status = 1
            try:
                status = run_measurements(request, timeout_s, writer)
            except BaseException:
                traceback.print_exc()
            finally:
                stdout.flush()
                stderr.flush()
                os._exit(status)
    finally:
        os.close(writer)
    timed_out = False
    try:
        descriptor = os.pidfd_open(pid)
        try:
            status = wait_for_measurement(pid, descriptor, reader, started + stage_limit, stage_limit)
        except TimeoutError:
            timed_out = True
            status = 1
            stop_worker(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(reader)
        pending = dict.fromkeys(task_cpu.stop_pending())
        while task_cpu.reap_exited():
            pending.update(dict.fromkeys(task_cpu.stop_pending()))
    if timed_out:
        error = task_cpu.MeasurementTimeout(timeout_s, (f"{pid} (module_cost measurement)", *pending))
        print(f"module_cost: {error}", file=sys.stderr, flush=True)
    elif pending:
        error = task_cpu.LeftoverProcesses(tuple(pending))
        print(f"module_cost: {error}", file=sys.stderr, flush=True)
        status = 1
    return status if status >= 0 else 128 - status


def reap_supervisor(descriptor: int) -> None:
    """Reap the supervisor when cleanup takes longer than the caller can wait."""
    try:
        with suppress(ChildProcessError):
            _ = os.waitid(os.P_PIDFD, descriptor, os.WEXITED)
    finally:
        os.close(descriptor)


def supervisor_status(pid: int, descriptor: int, deadline: float, timeout_s: float) -> int:
    """Read the supervisor's status within the caller's deadline."""
    ended = select.poll()
    ended.register(descriptor, select.POLLIN)
    if ended.poll(math.ceil(max(0, deadline - time.monotonic()) * 1000)):
        try:
            result = os.waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG)
        except ChildProcessError:
            print("module_cost: another reaper collected the supervisor", file=sys.stderr, flush=True)
            return 1
        if result is not None:
            return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status
    raise task_cpu.MeasurementTimeout(timeout_s, (f"{pid} (module_cost supervisor)",))


def wait_for_supervisor(pid: int, descriptor: int, release: int, deadline: float, timeout_s: float) -> int:
    """Give the pidfd to the deferred reaper if the caller's wait ends early."""
    try:
        _ = os.write(release, b"1")
        status = supervisor_status(pid, descriptor, deadline, timeout_s)
    except BaseException:
        threading.Thread(target=reap_supervisor, args=(descriptor,), daemon=True).start()
        raise
    os.close(descriptor)
    return status


def start_supervisor(request: CostArguments, timeout_s: float, stdout: TextIO, stderr: TextIO) -> int:
    """Start the supervisor with an exclusive pidfd acquisition window."""
    stage_limit = timeout_s + 2 * task_cpu.CLEANUP_LIMIT_S
    deadline = time.monotonic() + (1 + 2 * len(request.modules)) * stage_limit + task_cpu.CLEANUP_LIMIT_S
    reader, writer = os.pipe()
    try:
        try:
            pid = os.fork()
            if pid == 0:
                os.close(writer)
                status = 1
                try:
                    released = os.read(reader, 1)
                    os.close(reader)
                    if released == b"1":
                        isolate_child_output(stdout, stderr)
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            try:
                                status = supervise(request, timeout_s, stage_limit, stdout, stderr)
                            except BaseException:
                                traceback.print_exc()
                            finally:
                                stdout.flush()
                                stderr.flush()
                finally:
                    os._exit(status)
        finally:
            os.close(reader)
        # The child waits for release. The caller must let it live until pidfd_open returns.
        descriptor = os.pidfd_open(pid)
        return wait_for_supervisor(pid, descriptor, writer, deadline, timeout_s)
    finally:
        os.close(writer)


def isolate_child_output(stdout: TextIO, stderr: TextIO) -> None:
    """Replace both standard descriptors and close inherited output pipe aliases."""
    streams = (sys.stdout, sys.stderr)
    descriptors = {1, 2}
    for stream in streams:
        with suppress(io.UnsupportedOperation):
            descriptors.add(stream.fileno())
    pipes: set[tuple[int, int]] = set()
    for descriptor in descriptors:
        with suppress(OSError):
            info = os.fstat(descriptor)
            if stat.S_ISFIFO(info.st_mode) or stat.S_ISSOCK(info.st_mode):
                pipes.add((info.st_dev, info.st_ino))
    _ = os.dup2(stdout.fileno(), 1)
    _ = os.dup2(stderr.fileno(), 2)
    if pipes:
        for entry in os.listdir("/proc/self/fd"):
            descriptor = int(entry)
            with suppress(OSError):
                info = os.fstat(descriptor)
                if (info.st_dev, info.st_ino) in pipes:
                    os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    """Run main in its own CLI process.

    A caller that stops the child, reaps its children, or stops reading its output is out of contract.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--tree", type=Path, required=True)
    _ = parser.add_argument("--tests", type=int, default=0, metavar="N")
    _ = parser.add_argument("--commands", type=int, default=0, metavar="N")
    _ = parser.add_argument("modules", nargs="+", metavar="folder/file")
    request = parser.parse_args(argv, namespace=CostArguments())
    try:
        timeout_s = task_cpu.measurement_timeout_s()
        replay_limit = conf.read("test_times").whole("module_cost.output_replay_limit_bytes")
    except conf.Unusable as error:
        print(f"module_cost: {error}", file=sys.stderr, flush=True)
        return 1
    timeout_message = ""
    with child_output(sys.stdout, replay_limit) as stdout, child_output(sys.stderr, replay_limit) as stderr:
        try:
            status = start_supervisor(request, timeout_s, stdout, stderr)
        except task_cpu.MeasurementTimeout as error:
            timeout_message = str(error)
            status = 1
    if timeout_message:
        print(f"module_cost: {timeout_message}", file=sys.stderr, flush=True)
    return status if status >= 0 else 128 - status


@contextmanager
def child_output(stream: TextIO, replay_limit: int) -> Generator[TextIO]:
    """Copy a limited snapshot and preserve the child's write offset."""
    stream.flush()
    with TemporaryFile(mode="w+", encoding="utf-8", errors="backslashreplace") as output:
        yield output
        descriptor = output.fileno()
        size = os.fstat(descriptor).st_size
        limit = min(size, replay_limit)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        offset = 0
        ends_with_newline = True
        while offset < limit:
            data = os.pread(descriptor, min(limit - offset, 65536), offset)
            if not data:
                break
            offset += len(data)
            ends_with_newline = data.endswith(b"\n")
            _ = stream.write(decoder.decode(data))
        _ = stream.write(decoder.decode(b"", final=True))
        if size > limit:
            start = "" if ends_with_newline else "\n"
            print(f"{start}module_cost: output truncated after {limit} bytes", file=stream)
        stream.flush()


def run_measurements(request: CostArguments, timeout_s: float, stage_updates: int) -> int:
    """Reap stage orphans for CPU accounting while the worker is alive."""
    _ = adopt_descendants()
    try:
        return report_costs(request, timeout_s, stage_updates)
    except (conf.Unusable, task_cpu.MeasurementTimeout, task_cpu.LeftoverProcesses) as error:
        print(f"module_cost: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        if task_cpu.reap_exited():
            _ = task_cpu.stop_pending()


def report_costs(request: CostArguments, timeout_s: float, stage_updates: int) -> int:
    tree = request.tree.resolve()
    costs = [measure(tree, module, timeout_s, stage_updates, command_rows=bool(request.commands))
             for module in request.modules]
    passed = all(cost.passed for cost in costs)
    total = ["total", seconds(sum(cost.cpu_s for cost in costs)),
             str(sum(cost.trace.processes for cost in costs)), str(sum(cost.trace.execs for cost in costs)),
             seconds(sum(cost.wall_s for cost in costs)), "PASS" if passed else "RED"]
    print_table(["module", "CPU s", "processes", "execs", "wall s", "test status"],
                [*[cost.row() for cost in costs], total])
    print("Counts include the module_run supervisor: one extra process and no extra exec.")
    if request.tests:
        rows = [[cost.module, row["test"], row["kind"], seconds(row["cpu_s"]), seconds(row["s"])]
                for cost in costs
                for row in sorted(cost.tests, key=lambda row: (-row["cpu_s"], row["test"]))[:request.tests]]
        print_table(["module", "test or fixture", "kind", "CPU s", "wall s"], rows)
    if request.commands:
        for cost in costs:
            tasks = cost.trace
            print_table([cost.module + " command", "starts", "CPU s"],
                        [[row.command, str(row.starts), seconds(row.cpu_s)]
                         for row in tasks.commands[:request.commands]])
            print(f"unassigned {tasks.cpu_s - sum(row.cpu_s for row in tasks.commands):+.6f} CPU s")
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(main())
